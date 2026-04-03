from __future__ import annotations

from app.config import Settings
from app.services.validation_engine import (
    ValidationRunRequest,
    _baseline_precision_at_10,
    execute_validation_and_calibration,
    execute_validation_run,
)


class DummyDB:
    path = 'dummy.db'


class DummyAlpaca:
    def has_credentials(self) -> bool:
        return True


def test_baseline_precision_helper_handles_missing_values():
    assert _baseline_precision_at_10({}) == 0.0
    assert _baseline_precision_at_10({'baseline_comparison': {'mover_rank_only': {'precision_at_10': '0.125'}}}) == 0.125


def test_execute_validation_run_normalizes_summary(monkeypatch):
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

    payload = execute_validation_run(
        Settings(),
        DummyDB(),
        DummyAlpaca(),
        ValidationRunRequest(start_date='2026-01-01', end_date='2026-01-31', scan_offset_minutes=120),
    )

    assert payload['id'] == 7
    assert payload['summary']['days'] == 5
    assert payload['summary']['precision_at_10'] == 0.4
    assert 'validation_verdict' in payload['summary']


def test_execute_validation_and_calibration_attaches_calibration(monkeypatch):
    def fake_run_validation(settings, db, alpaca, start_date, end_date, scan_offset_minutes, **kwargs):
        return {
            'id': 11,
            'created_at': '2026-03-26T00:00:00+00:00',
            'start_date': start_date,
            'end_date': end_date,
            'scan_offset_minutes': scan_offset_minutes,
            'status': 'ok',
            'summary': {
                'days': 10,
                'precision_at_10': 0.55,
                'baseline_comparison': {'mover_rank_only': {'precision_at_10': 0.12}},
            },
            'rows': [{'symbol': 'ABC', 'advanced_to_stage2': True, 'hit_target': True}],
        }

    captured = {}

    def fake_calibrate_rows(rows, current_weights, min_improvement, *, mover_rank_baseline_precision_at_10=0.0):
        captured['rows'] = rows
        captured['baseline'] = mover_rank_baseline_precision_at_10
        return {'eligible': True, 'should_apply': False, 'recommended': {'weights': current_weights}}

    monkeypatch.setattr('app.services.validation_engine._run_validation_impl', fake_run_validation)
    monkeypatch.setattr('app.services.calibration.calibrate_rows', fake_calibrate_rows)

    result = execute_validation_and_calibration(
        Settings(),
        DummyDB(),
        DummyAlpaca(),
        ValidationRunRequest(start_date='2026-01-01', end_date='2026-01-31', scan_offset_minutes=120),
    )

    assert result['validation']['id'] == 11
    assert result['calibration']['eligible'] is True
    assert result['validation']['summary']['calibration']['eligible'] is True
    assert captured['baseline'] == 0.12
