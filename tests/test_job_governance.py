from __future__ import annotations

from datetime import datetime, timezone

from app.db import Database
from app.services.job_governance import (
    build_job_status_snapshot,
    complete_research_job,
    fail_research_job,
    interrupt_research_job,
    queue_research_job,
    update_research_progress,
)


def test_job_snapshot_reflects_latest_entities_and_counts(tmp_path):
    db = Database(str(tmp_path / 'jobs.db'))
    db.insert_validation_run(
        {
            'created_at': datetime.now(timezone.utc).isoformat(),
            'start_date': '2026-01-01',
            'end_date': '2026-01-31',
            'scan_offset_minutes': 120,
            'status': 'ok',
            'summary': {'days': 5},
        },
        [],
    )
    db.insert_scan(
        {
            'created_at': datetime.now(timezone.utc).isoformat(),
            'trading_day': '2026-01-10',
            'scan_offset_minutes': 120,
            'scan_timestamp': datetime.now(timezone.utc).isoformat(),
            'status': 'ok',
            'mode': 'scan_only',
            'universe_count': 1000,
            'stage1_count': 50,
            'stage2_count': 3,
            'summary': {'goal': 'test'},
        },
        [],
    )
    run_id = queue_research_job(db, {'created_at': datetime.now(timezone.utc).isoformat(), 'mode': 'test'})
    update_research_progress(db, run_id, progress=0.5, message='halfway')

    snapshot = build_job_status_snapshot(db)

    assert snapshot['latest_scan']['status'] == 'ok'
    assert snapshot['latest_validation']['status'] == 'ok'
    assert snapshot['latest_research']['status'] == 'running'
    assert snapshot['research_counts']['running'] == 1


def test_research_job_transition_helpers_update_status(tmp_path):
    db = Database(str(tmp_path / 'jobs.db'))
    run_id = queue_research_job(db, {'created_at': datetime.now(timezone.utc).isoformat(), 'mode': 'test'})
    update_research_progress(db, run_id, progress=0.1, message='started', started_at='2026-01-01T00:00:00+00:00')
    complete_research_job(db, run_id, result={'validation_id': 1}, message='done')

    run = db.get_research_run(run_id)
    assert run['status'] == 'completed'
    assert run['result']['validation_id'] == 1
    assert run['finished_at'] is not None


def test_research_job_failure_and_interruption_helpers_update_status(tmp_path):
    db = Database(str(tmp_path / 'jobs.db'))
    run_id = queue_research_job(db, {'created_at': datetime.now(timezone.utc).isoformat(), 'mode': 'test'})
    fail_research_job(db, run_id, message='boom', result={'error': 'boom'})
    run = db.get_research_run(run_id)
    assert run['status'] == 'failed'
    assert run['result']['error'] == 'boom'

    run_id2 = queue_research_job(db, {'created_at': datetime.now(timezone.utc).isoformat(), 'mode': 'test'})
    interrupt_research_job(db, run_id2, previous_status='running')
    run2 = db.get_research_run(run_id2)
    assert run2['status'] == 'interrupted'
    assert 'web process restarted' in run2['message']
