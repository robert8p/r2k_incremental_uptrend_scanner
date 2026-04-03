from __future__ import annotations

import os
import tempfile

from app.config import Settings
from app.runtime import AppRuntime


class DummyAlpaca:
    def __init__(self, settings: Settings):
        self.settings = settings


class DummyDB:
    def __init__(self, path: str):
        self.path = path



def test_runtime_initializes_settings_db_and_alpaca():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, 'a.db')
        runtime = AppRuntime(
            settings_loader=lambda: Settings(database_path=db_path),
            db_factory=DummyDB,
            alpaca_factory=DummyAlpaca,
        )
        assert runtime.settings.database_path == db_path
        assert runtime.db.path == db_path
        assert runtime.alpaca.settings.database_path == db_path



def test_runtime_refresh_reloads_settings_and_alpaca():
    with tempfile.TemporaryDirectory() as tmpdir:
        paths = [os.path.join(tmpdir, 'a.db'), os.path.join(tmpdir, 'b.db')]
        calls = {'n': 0}

        def loader() -> Settings:
            idx = min(calls['n'], 1)
            calls['n'] += 1
            return Settings(database_path=paths[idx], trading_mode='paper' if idx == 0 else 'scan_only')

        runtime = AppRuntime(settings_loader=loader, db_factory=DummyDB, alpaca_factory=DummyAlpaca)
        original_db = runtime.db
        original_alpaca = runtime.alpaca
        runtime.refresh()

        assert runtime.settings.database_path == paths[1]
        assert runtime.settings.trading_mode == 'scan_only'
        assert runtime.db.path == paths[1]
        assert runtime.db is not original_db
        assert runtime.alpaca is not original_alpaca
