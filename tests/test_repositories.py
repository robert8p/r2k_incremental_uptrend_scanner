from __future__ import annotations

from datetime import datetime, timezone

from app.db import Database
from app.repositories import RepositoryBundle
from app.runtime import AppRuntime
from app.config import Settings


class DummyAlpaca:
    def __init__(self, settings: Settings):
        self.settings = settings


class DummyDB(Database):
    pass


def test_repository_bundle_reads_scan_validation_and_research(tmp_path):
    db = Database(str(tmp_path / 'repo.db'))
    scan_id = db.insert_scan(
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
        [{'symbol': 'AAA', 'advanced_to_stage2': True, 'total_score': 70.0}],
    )
    validation_id = db.insert_validation_run(
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
    run_id = db.insert_research_run({'created_at': datetime.now(timezone.utc).isoformat(), 'mode': 'test'}, status='queued', message='queued')

    repos = RepositoryBundle(db)

    assert repos.scan.get(scan_id)['id'] == scan_id
    assert repos.scan.get_candidates(scan_id)[0]['symbol'] == 'AAA'
    assert repos.validation.get(validation_id)['id'] == validation_id
    assert repos.research.get(run_id)['id'] == run_id


def test_runtime_refresh_rebuilds_repositories_when_db_changes(tmp_path):
    paths = [str(tmp_path / 'a.db'), str(tmp_path / 'b.db')]
    calls = {'n': 0}

    def loader():
        idx = min(calls['n'], 1)
        calls['n'] += 1
        return Settings(database_path=paths[idx])

    runtime = AppRuntime(settings_loader=loader, db_factory=Database, alpaca_factory=DummyAlpaca)
    original_repo_db_path = runtime.repositories.db.path
    runtime.refresh()

    assert original_repo_db_path == paths[0]
    assert runtime.repositories.db.path == paths[1]
