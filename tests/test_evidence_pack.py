from __future__ import annotations

import io
import zipfile
from datetime import datetime, timezone

from app.config import Settings
from app.db import Database
from app.services.evidence_pack import (
    build_research_evidence_pack,
    build_validation_evidence_pack,
    pack_to_zip_bytes,
)


def _make_db(tmp_path):
    return Database(str(tmp_path / 'evidence.db'))


def test_build_validation_evidence_pack_contains_standard_files(tmp_path):
    settings = Settings(database_path=str(tmp_path / 'evidence.db'))
    db = _make_db(tmp_path)
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

    pack = build_validation_evidence_pack(settings, db, validation_id)

    assert 'MANIFEST.json' in pack
    assert 'validation_summary.json' in pack
    assert 'validation_rows.csv' in pack
    assert 'contract_health.json' in pack


def test_build_research_evidence_pack_includes_linked_validation(tmp_path):
    settings = Settings(database_path=str(tmp_path / 'evidence.db'))
    db = _make_db(tmp_path)
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
    run_id = db.insert_research_run(
        {
            'created_at': datetime.now(timezone.utc).isoformat(),
            'mode': 'three_month_validation_and_calibration',
            'start_date': '2026-01-01',
            'end_date': '2026-01-31',
            'scan_offset_minutes': 120,
        },
        status='completed',
        message='done',
    )
    db.update_research_run(
        run_id,
        status='completed',
        result={
            'validation_id': validation_id,
            'best_validation_id': validation_id,
            'summary': {'days': 5, 'precision_at_10': 0.4},
            'calibration': {'eligible': True},
        },
    )

    pack = build_research_evidence_pack(settings, db, run_id)

    assert 'research_result.json' in pack
    assert 'linked_validation_summary.json' in pack
    assert 'linked_validation_rows.csv' in pack


def test_pack_to_zip_bytes_writes_all_files(tmp_path):
    zipped = pack_to_zip_bytes({'a.json': b'{}', 'b.csv': b'x,y\n1,2\n'})
    with zipfile.ZipFile(io.BytesIO(zipped), 'r') as zf:
        assert set(zf.namelist()) == {'a.json', 'b.csv'}
