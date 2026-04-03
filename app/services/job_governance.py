from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any, Dict

from app.db import Database
from app.repositories import RepositoryBundle, ensure_repository_bundle
from app.services.telemetry import emit_event

RESEARCH_STATUS_VALUES = {'queued', 'running', 'completed', 'failed', 'interrupted'}
LOCK_DIR = os.environ.get('RESEARCH_LOCK_DIR', './data/locks')
SETTINGS_STAGING_DIR = os.environ.get('RESEARCH_SETTINGS_DIR', './data/research_staging')


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def queue_research_job(db: Database | RepositoryBundle, params: Dict[str, Any], *, message: str = 'Queued historical research run.') -> int:
    repos = ensure_repository_bundle(db)
    run_id = repos.research.insert(params, status='queued', message=message)
    emit_event('job.queued', job_type='research', run_id=int(run_id), status='queued')
    return run_id


def update_research_progress(db: Database | RepositoryBundle, run_id: int, *, progress: float, message: str, status: str = 'running', started_at: str | None = None) -> None:
    kwargs: Dict[str, Any] = {'status': status, 'progress': round(float(progress), 4), 'message': message}
    if started_at is not None:
        kwargs['started_at'] = started_at
    ensure_repository_bundle(db).research.update(run_id, **kwargs)
    emit_event('job.progress', job_type='research', run_id=int(run_id), status=status, progress=round(float(progress), 4), message=message)


def complete_research_job(db: Database | RepositoryBundle, run_id: int, *, result: Dict[str, Any], message: str) -> None:
    ensure_repository_bundle(db).research.update(run_id, status='completed', progress=1.0, message=message, finished_at=_utc_now(), result=result)
    emit_event('job.completed', job_type='research', run_id=int(run_id), status='completed', message=message)


def fail_research_job(db: Database | RepositoryBundle, run_id: int, *, message: str, result: Dict[str, Any] | None = None) -> None:
    ensure_repository_bundle(db).research.update(run_id, status='failed', progress=1.0, message=message, finished_at=_utc_now(), result=result)
    emit_event('job.failed', job_type='research', run_id=int(run_id), status='failed', message=message)


def interrupt_research_job(db: Database | RepositoryBundle, run_id: int, *, previous_status: str) -> None:
    message = f'Run was {previous_status} when the web process restarted. Re-queue if needed.'
    ensure_repository_bundle(db).research.update(run_id, status='interrupted', message=message, finished_at=_utc_now())
    emit_event('job.interrupted', job_type='research', run_id=int(run_id), previous_status=previous_status, status='interrupted')


def build_job_status_snapshot(db: Database | RepositoryBundle) -> Dict[str, Any]:
    repos = ensure_repository_bundle(db)
    research_runs = repos.research.list_recent(limit=20)
    counts = {status: 0 for status in sorted(RESEARCH_STATUS_VALUES)}
    for run in research_runs:
        status = str(run.get('status') or 'unknown')
        if status in counts:
            counts[status] += 1
    latest_scan = repos.scan.get_latest()
    latest_validations = repos.validation.list_recent(limit=1)
    latest_research = research_runs[0] if research_runs else None
    lock_dir = Path(LOCK_DIR)
    staging_dir = Path(SETTINGS_STAGING_DIR)
    return {
        'latest_scan': {
            'id': latest_scan.get('id') if latest_scan else None,
            'trading_day': latest_scan.get('trading_day') if latest_scan else None,
            'scan_offset_minutes': latest_scan.get('scan_offset_minutes') if latest_scan else None,
            'status': latest_scan.get('status') if latest_scan else None,
            'created_at': latest_scan.get('created_at') if latest_scan else None,
        },
        'latest_validation': {
            'id': latest_validations[0].get('id') if latest_validations else None,
            'status': latest_validations[0].get('status') if latest_validations else None,
            'created_at': latest_validations[0].get('created_at') if latest_validations else None,
            'scan_offset_minutes': latest_validations[0].get('scan_offset_minutes') if latest_validations else None,
        },
        'latest_research': {
            'id': latest_research.get('id') if latest_research else None,
            'status': latest_research.get('status') if latest_research else None,
            'progress': latest_research.get('progress') if latest_research else None,
            'message': latest_research.get('message') if latest_research else None,
            'created_at': latest_research.get('created_at') if latest_research else None,
            'started_at': latest_research.get('started_at') if latest_research else None,
            'finished_at': latest_research.get('finished_at') if latest_research else None,
        },
        'research_counts': counts,
        'active_lock_count': len(list(lock_dir.glob('research_*.lock'))) if lock_dir.exists() else 0,
        'staged_settings_count': len(list(staging_dir.glob('research_settings_*.json'))) if staging_dir.exists() else 0,
    }
