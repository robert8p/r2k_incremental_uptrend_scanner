from __future__ import annotations

import os
import sqlite3
import tempfile

from app.config import Settings
from app.db import Database
from app.runtime import AppRuntime
from app.services.diagnostics import build_contract_health


class DummyAlpaca:
    def __init__(self, settings: Settings):
        self.settings = settings


class DummyDB:
    def __init__(self, path: str):
        self.path = path


def test_contract_health_reports_malformed_validation_json_without_raising(tmp_path):
    db = Database(str(tmp_path / 'broken_validation.db'))
    conn = sqlite3.connect(db.path)
    conn.execute(
        """
        INSERT INTO validation_runs (created_at, start_date, end_date, scan_offset_minutes, status, summary_json, rows_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ('2026-01-01T00:00:00+00:00', '2026-01-01', '2026-01-31', 120, 'ok', '{bad json', '[]'),
    )
    conn.commit()
    conn.close()

    report = build_contract_health(db)

    assert report['ok'] is False
    assert report['latest_validation_ok'] is False
    assert any('latest_validation_load' in err for err in report['errors'])


def test_contract_health_reports_malformed_research_json_without_raising(tmp_path):
    db = Database(str(tmp_path / 'broken_research.db'))
    conn = sqlite3.connect(db.path)
    conn.execute(
        """
        INSERT INTO research_runs (created_at, started_at, finished_at, status, progress, message, params_json, result_json)
        VALUES (?, NULL, NULL, ?, ?, ?, ?, ?)
        """,
        ('2026-01-01T00:00:00+00:00', 'completed', 1.0, 'done', '{}', '{bad json'),
    )
    conn.commit()
    conn.close()

    report = build_contract_health(db)

    assert report['ok'] is False
    assert report['latest_research_ok'] is False
    assert any('latest_research_load' in err for err in report['errors'])


def test_recover_stale_research_runs_keeps_live_lock_and_status(tmp_path):
    from app.services.research import recover_stale_research_runs
    import app.services.research as rm

    lock_dir = tmp_path / 'locks'
    staging_dir = tmp_path / 'staging'
    lock_dir.mkdir()
    staging_dir.mkdir()
    rm.LOCK_DIR = str(lock_dir)
    rm.SETTINGS_STAGING_DIR = str(staging_dir)

    db = Database(str(tmp_path / 'test.db'))
    run_id = db.insert_research_run({'created_at': '2026-01-01T00:00:00', 'mode': 'test'}, status='running', message='test')
    (lock_dir / f'research_{run_id}.lock').write_text(str(os.getpid()))

    recovered = recover_stale_research_runs(db)
    run = db.get_research_run(run_id)

    assert recovered == 0
    assert run['status'] == 'running'


def test_recover_stale_research_runs_cleans_orphaned_settings_file(tmp_path):
    from app.services.research import recover_stale_research_runs
    import app.services.research as rm

    lock_dir = tmp_path / 'locks'
    staging_dir = tmp_path / 'staging'
    lock_dir.mkdir()
    staging_dir.mkdir()
    rm.LOCK_DIR = str(lock_dir)
    rm.SETTINGS_STAGING_DIR = str(staging_dir)

    db = Database(str(tmp_path / 'test.db'))
    orphan = staging_dir / 'research_settings_42.json'
    orphan.write_text('{}')

    recover_stale_research_runs(db)

    assert not orphan.exists()


def test_runtime_refresh_same_db_path_reuses_db_handle():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, 'same.db')
        settings = Settings(database_path=db_path)
        runtime = AppRuntime(
            settings_loader=lambda: settings,
            db_factory=DummyDB,
            alpaca_factory=DummyAlpaca,
            initial_settings=settings,
        )
        original_db = runtime.db

        runtime.refresh()

        assert runtime.db is original_db
