"""Tests for the research runner refactor."""
from __future__ import annotations

import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


class TestLockMechanism:

    def test_lock_creates_file_with_pid(self):
        from app.services.research_worker import _acquire_lock, _release_lock
        import app.services.research_worker as rw
        with tempfile.TemporaryDirectory() as tmpdir:
            orig = rw.LOCK_DIR
            rw.LOCK_DIR = tmpdir
            try:
                lock_file = _acquire_lock(99)
                assert lock_file.exists()
                pid = int(lock_file.read_text().strip())
                assert pid == os.getpid()
                _release_lock(lock_file)
                assert not lock_file.exists()
            finally:
                rw.LOCK_DIR = orig

    def test_stale_lock_is_overwritten(self):
        from app.services.research_worker import _acquire_lock, _release_lock
        import app.services.research_worker as rw
        with tempfile.TemporaryDirectory() as tmpdir:
            orig = rw.LOCK_DIR
            rw.LOCK_DIR = tmpdir
            try:
                lock_path = os.path.join(tmpdir, 'research_99.lock')
                with open(lock_path, 'w') as f:
                    f.write('999999999')
                lock_file = _acquire_lock(99)
                pid = int(lock_file.read_text().strip())
                assert pid == os.getpid()
                _release_lock(lock_file)
            finally:
                rw.LOCK_DIR = orig

    def test_live_lock_blocks_acquisition(self):
        from app.services.research_worker import _acquire_lock
        import app.services.research_worker as rw
        with tempfile.TemporaryDirectory() as tmpdir:
            orig = rw.LOCK_DIR
            rw.LOCK_DIR = tmpdir
            try:
                lock_path = os.path.join(tmpdir, 'research_99.lock')
                with open(lock_path, 'w') as f:
                    f.write(str(os.getpid()))
                with pytest.raises(RuntimeError, match='already running'):
                    _acquire_lock(99)
                os.unlink(lock_path)
            finally:
                rw.LOCK_DIR = orig


class TestStaleRunRecovery:

    def test_recover_marks_running_as_interrupted(self):
        from app.db import Database
        from app.services.research import recover_stale_research_runs
        import app.services.research as rm
        with tempfile.TemporaryDirectory() as tmpdir:
            rm.LOCK_DIR = os.path.join(tmpdir, 'locks')
            rm.SETTINGS_STAGING_DIR = os.path.join(tmpdir, 'staging')
            db = Database(os.path.join(tmpdir, 'test.db'))
            run_id = db.insert_research_run({'created_at': '2026-01-01T00:00:00', 'mode': 'test'}, status='running', message='test')
            recovered = recover_stale_research_runs(db)
            assert recovered == 1
            run = db.get_research_run(run_id)
            assert run['status'] == 'interrupted'

    def test_recover_marks_queued_as_interrupted(self):
        from app.db import Database
        from app.services.research import recover_stale_research_runs
        import app.services.research as rm
        with tempfile.TemporaryDirectory() as tmpdir:
            rm.LOCK_DIR = os.path.join(tmpdir, 'locks')
            rm.SETTINGS_STAGING_DIR = os.path.join(tmpdir, 'staging')
            db = Database(os.path.join(tmpdir, 'test.db'))
            run_id = db.insert_research_run({'created_at': '2026-01-01T00:00:00', 'mode': 'test'}, status='queued', message='test')
            recovered = recover_stale_research_runs(db)
            assert recovered == 1

    def test_recover_leaves_completed_alone(self):
        from app.db import Database
        from app.services.research import recover_stale_research_runs
        import app.services.research as rm
        with tempfile.TemporaryDirectory() as tmpdir:
            rm.LOCK_DIR = os.path.join(tmpdir, 'locks')
            rm.SETTINGS_STAGING_DIR = os.path.join(tmpdir, 'staging')
            db = Database(os.path.join(tmpdir, 'test.db'))
            db.insert_research_run({'created_at': '2026-01-01T00:00:00', 'mode': 'test'}, status='completed', message='done')
            assert recover_stale_research_runs(db) == 0

    def test_recover_cleans_stale_lock_files(self):
        from app.db import Database
        from app.services.research import recover_stale_research_runs
        import app.services.research as rm
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_dir = os.path.join(tmpdir, 'locks')
            os.makedirs(lock_dir)
            rm.LOCK_DIR = lock_dir
            rm.SETTINGS_STAGING_DIR = os.path.join(tmpdir, 'staging')
            lock_path = os.path.join(lock_dir, 'research_42.lock')
            with open(lock_path, 'w') as f:
                f.write('999999999')
            db = Database(os.path.join(tmpdir, 'test.db'))
            recover_stale_research_runs(db)
            assert not os.path.exists(lock_path)


class TestConcurrentRunPrevention:

    def test_count_active_runs_with_no_locks(self):
        from app.services.research import _count_active_research_runs
        import app.services.research as rm
        with tempfile.TemporaryDirectory() as tmpdir:
            rm.LOCK_DIR = tmpdir
            assert _count_active_research_runs() == 0

    def test_count_active_runs_with_live_lock(self):
        from app.services.research import _count_active_research_runs
        import app.services.research as rm
        with tempfile.TemporaryDirectory() as tmpdir:
            rm.LOCK_DIR = tmpdir
            with open(os.path.join(tmpdir, 'research_1.lock'), 'w') as f:
                f.write(str(os.getpid()))
            assert _count_active_research_runs() == 1

    def test_count_ignores_stale_lock(self):
        from app.services.research import _count_active_research_runs
        import app.services.research as rm
        with tempfile.TemporaryDirectory() as tmpdir:
            rm.LOCK_DIR = tmpdir
            with open(os.path.join(tmpdir, 'research_1.lock'), 'w') as f:
                f.write('999999999')
            assert _count_active_research_runs() == 0


class TestOffsetLadderParsing:

    def test_parse_normal(self):
        from app.services.research import _parse_offset_ladder
        assert _parse_offset_ladder('90,120,150', [120]) == [90, 120, 150]

    def test_parse_deduplicates(self):
        from app.services.research import _parse_offset_ladder
        assert _parse_offset_ladder('120,120,90', [120]) == [90, 120]

    def test_parse_fallback(self):
        from app.services.research import _parse_offset_ladder
        assert _parse_offset_ladder('', [60, 120]) == [60, 120]

    def test_parse_ignores_invalid(self):
        from app.services.research import _parse_offset_ladder
        assert _parse_offset_ladder('90,abc,150', [120]) == [90, 150]

    def test_parse_ignores_zero_and_negative(self):
        from app.services.research import _parse_offset_ladder
        assert _parse_offset_ladder('0,-30,120', [120]) == [120]


class TestSettingsRoundtrip:

    def test_settings_json_roundtrip(self):
        from app.config import Settings
        s = Settings()
        payload = s.model_dump()
        json_str = json.dumps(payload, default=str)
        s2 = Settings(**json.loads(json_str))
        assert s2.target_pct == s.target_pct
        assert s2.weights == s.weights
        assert s2.default_scan_offset_minutes == s.default_scan_offset_minutes


class TestResearchWorkerSpawn:

    def test_start_goal_seek_run_redirects_worker_output_to_log_file_and_persists_scope(self, monkeypatch):
        from app.config import Settings
        from app.db import Database
        from app.services import research as rm

        captured = {}

        class DummyPopen:
            def __init__(self, *args, **kwargs):
                captured['args'] = args
                captured['kwargs'] = kwargs
                self.pid = 4321

        with tempfile.TemporaryDirectory() as tmpdir:
            original_staging_dir = rm.SETTINGS_STAGING_DIR
            monkeypatch.setattr(rm.subprocess, 'Popen', DummyPopen)
            rm.SETTINGS_STAGING_DIR = os.path.join(tmpdir, 'staging')
            settings = Settings(data_dir=tmpdir, database_path=os.path.join(tmpdir, 'test.db'))
            db = Database(settings.database_path)
            try:
                run_id = rm.start_goal_seek_run(
                    settings,
                    db,
                    start_date='2025-07-10',
                    end_date='2026-03-27',
                    train_days=60,
                    test_days=20,
                    step_days=20,
                    embargo_days=1,
                    offsets='150',
                    config_scope='focused_liquidity',
                )
                run = db.get_research_run(run_id)
                assert run['params']['config_scope'] == 'focused_liquidity'
                assert captured['kwargs']['stderr'] == rm.subprocess.STDOUT
                assert captured['kwargs']['stdout'] is not rm.subprocess.DEVNULL
                assert hasattr(captured['kwargs']['stdout'], 'name')
                assert str(captured['kwargs']['stdout'].name).endswith(f'research_worker_{run_id}.log')
                assert captured['kwargs']['env']['PYTHONUNBUFFERED'] == '1'
            finally:
                rm.SETTINGS_STAGING_DIR = original_staging_dir
