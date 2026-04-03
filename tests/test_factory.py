from __future__ import annotations

import inspect
import os
import tempfile

from app.config import Settings
from app.factory import create_app
from app.runtime import AppRuntime


class DummyAlpaca:
    def __init__(self, settings: Settings):
        self.settings = settings

    def has_credentials(self) -> bool:
        return False


class DummyDB:
    def __init__(self, path: str):
        self.path = path


def _build_runtime() -> AppRuntime:
    tmpdir = tempfile.mkdtemp()
    settings = Settings(
        database_path=os.path.join(tmpdir, 'factory.db'),
        enable_scheduler=False,
        auth_token='',
    )
    return AppRuntime(
        initial_settings=settings,
        settings_loader=lambda: settings,
        db_factory=DummyDB,
        alpaca_factory=DummyAlpaca,
    )


def test_create_app_registers_runtime_and_core_routes():
    runtime = _build_runtime()
    app = create_app(runtime)

    assert app.state.runtime is runtime

    paths = {route.path for route in app.router.routes}
    expected = {
        '/',
        '/scan/recent/export.zip',
        '/scan/{scan_id}',
        '/scan/{scan_id}/candidate/{symbol}',
        '/validation',
        '/validation/{validation_id}',
        '/validation/research/{run_id}',
        '/validation/research/run-goal-seek',
        '/settings',
        '/diagnostics/decision-bundle.zip',
        '/diagnostics/config-snapshot.txt',
        '/diagnostics/universe-snapshot.txt',
        '/healthz',
        '/status',
        '/api/latest-scan',
        '/api/research/{run_id}',
    }
    assert expected.issubset(paths)


def test_api_routes_close_over_runtime_not_stale_db():
    runtime = _build_runtime()
    app = create_app(runtime)

    target_paths = {'/api/latest-scan', '/api/research/{run_id}', '/status'}
    for route in app.router.routes:
        if getattr(route, 'path', None) not in target_paths:
            continue
        closure = inspect.getclosurevars(route.endpoint).nonlocals
        assert 'runtime' in closure
        assert 'db' not in closure
