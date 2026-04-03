from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd

from app.config import Settings
from app.db import Database
from app.services.evidence_pack import build_validation_evidence_pack
from app.services.scanner_engine import ScanRequest, execute_scan
from app.services.validation_engine import ValidationRunRequest, execute_validation_run


@dataclass
class DummyStatus:
    tradable_count: int


class DummyDB:
    def __init__(self):
        self.inserted = None

    def insert_scan(self, payload, candidates):
        self.inserted = (payload, candidates)
        return 99


class DummyAlpaca:
    def has_credentials(self):
        return True


def test_execute_scan_emits_started_and_completed_events(monkeypatch, caplog):
    settings = Settings()
    db = DummyDB()
    request = ScanRequest(trading_day='2026-01-05', offset_minutes=120)
    scan_inputs = {
        'request': request,
        'session': type('S', (), {'checkpoint': pd.Timestamp('2026-01-05T18:00:00Z'), 'minutes_until_close_checkpoint': 120})(),
        'universe': {'status': DummyStatus(tradable_count=1500)},
    }
    stage1 = pd.DataFrame([{'symbol': 'AAA', 'intraday_pct_gain': 3.0}])
    stage2 = {'advanced_count': 1, 'candidates': [{'symbol': 'AAA', 'advanced_to_stage2': True, 'total_score': 70.0}]}

    monkeypatch.setattr('app.services.scanner_engine.load_scan_inputs', lambda settings, db, alpaca, request: scan_inputs)
    monkeypatch.setattr('app.services.scanner_engine.build_stage1_snapshot', lambda settings, scan_inputs, alpaca=None: {'stage1': stage1})
    monkeypatch.setattr('app.services.scanner_engine.score_stage2_candidates', lambda settings, scan_inputs, stage1_snapshot, alpaca: stage2)

    caplog.set_level(logging.INFO, logger='app.telemetry')
    execute_scan(settings, db, DummyAlpaca(), request)

    messages = [record.message for record in caplog.records if record.name == 'app.telemetry']
    assert any('scan.started' in message for message in messages)
    assert any('scan.completed' in message for message in messages)


def test_execute_validation_run_emits_completed_event(monkeypatch, caplog):
    def fake_run_validation(settings, db, alpaca, start_date, end_date, scan_offset_minutes, **kwargs):
        return {
            'id': 7,
            'created_at': '2026-03-26T00:00:00+00:00',
            'start_date': start_date,
            'end_date': end_date,
            'scan_offset_minutes': scan_offset_minutes,
            'status': 'ok',
            'summary': {'days': 5, 'precision_at_10': 0.4},
            'rows': [{'symbol': 'ABC', 'advanced_to_stage2': True}],
        }

    monkeypatch.setattr('app.services.validation_engine._run_validation_impl', fake_run_validation)
    caplog.set_level(logging.INFO, logger='app.telemetry')
    execute_validation_run(Settings(), object(), DummyAlpaca(), ValidationRunRequest(start_date='2026-01-01', end_date='2026-01-31', scan_offset_minutes=120))

    messages = [record.message for record in caplog.records if record.name == 'app.telemetry']
    assert any('validation.started' in message for message in messages)
    assert any('validation.completed' in message for message in messages)


def test_build_validation_evidence_pack_emits_event(tmp_path, caplog):
    settings = Settings(database_path=str(tmp_path / 'evidence.db'))
    db = Database(str(tmp_path / 'evidence.db'))
    validation_id = db.insert_validation_run(
        {
            'created_at': datetime.now(timezone.utc).isoformat(),
            'start_date': '2026-01-01',
            'end_date': '2026-01-31',
            'scan_offset_minutes': 120,
            'status': 'ok',
            'summary': {'days': 5, 'precision_at_10': 0.4},
        },
        [{'trading_day': '2026-01-03', 'symbol': 'ABC', 'advanced_to_stage2': True, 'hit_target': True}],
    )

    caplog.set_level(logging.INFO, logger='app.telemetry')
    build_validation_evidence_pack(settings, db, validation_id)

    messages = [record.message for record in caplog.records if record.name == 'app.telemetry']
    assert any('evidence_pack.built' in message and 'validation' in message for message in messages)
