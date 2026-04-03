from __future__ import annotations

import os
import tempfile

from fastapi.testclient import TestClient

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
        database_path=os.path.join(tmpdir, 'diagnostics-ui.db'),
        enable_scheduler=False,
        auth_token='',
        data_dir=tmpdir,
    )
    return AppRuntime(
        initial_settings=settings,
        settings_loader=lambda: settings,
        db_factory=DummyDB,
        alpaca_factory=DummyAlpaca,
    )



def test_diagnostics_page_renders_grouped_actions_and_text_snapshot_buttons(monkeypatch):
    runtime = _build_runtime()
    app = create_app(runtime)

    monkeypatch.setattr(
        'app.services.diagnostics.build_diagnostics_snapshot',
        lambda *args, **kwargs: {
            'app_env': 'production',
            'trading_mode': 'scan_only',
            'latest_trading_day': '2026-04-02',
            'data_api': {'message': 'ok'},
            'universe_status': {'tradable_count': 1921, 'as_of': '2026-04-02'},
            'latest_scan': {'id': 43},
            'scheduler_status': {'leader': True, 'scheduler_running': True, 'mode': 'enabled', 'pid': 123},
            'live_trust': {
                'latest_research_run_id': 42,
                'current_scheduled_offsets': [120, 150],
                'recommended_schedule': None,
                'schedule_alignment_ok': True,
                'schedule_alignment_message': 'Aligned.',
                'offset_rows': [],
            },
            'config': {'min_price': 1.75, 'scheduled_scan_offsets': [120, 150]},
            'decision_state': {
                'latest_selected_day': '2026-04-02',
                'clean_day_count': 1,
                'best_shadow_profile': 'soft_bounce_quality',
                'overall_promotion_readiness': 'shadow_profile_promising_but_early',
                'historical_replay_shadow': {'overall_verdict': 'historical_replay_supports_candidate_profile', 'recommended_profile': {'profile_name': 'soft_bounce_quality'}},
                'currently_valid_now_count': 0,
                'regressed_after_earlier_validity_count': 2,
                'decision_recommendation_message': 'Hold live behavior.',
            },
        },
    )
    monkeypatch.setattr('app.services.diagnostics.read_recent_logs', lambda settings: ['log line'])

    client = TestClient(app)
    response = client.get('/diagnostics')

    assert response.status_code == 200
    body = response.text
    assert 'Operational actions' in body
    assert 'Downloads' in body
    assert 'Historical replay shadow pack' in body
    assert 'Download config .txt' in body
    assert 'Download universe .txt' in body
    assert 'Config snapshot' in body
    assert 'Universe snapshot' in body



def test_config_and_universe_snapshot_downloads_return_plain_text(monkeypatch):
    runtime = _build_runtime()
    app = create_app(runtime)

    monkeypatch.setattr(
        'app.services.diagnostics.build_diagnostics_snapshot',
        lambda *args, **kwargs: {
            'universe_status': {'tradable_count': 1921, 'source': 'IWM holdings proxy'},
        },
    )

    client = TestClient(app)

    config_response = client.get('/diagnostics/config-snapshot.txt')
    assert config_response.status_code == 200
    assert config_response.headers['content-type'].startswith('text/plain')
    assert 'attachment; filename=config_snapshot.txt' == config_response.headers['content-disposition']
    assert 'Config snapshot' in config_response.text
    assert 'min_price' in config_response.text

    universe_response = client.get('/diagnostics/universe-snapshot.txt')
    assert universe_response.status_code == 200
    assert universe_response.headers['content-type'].startswith('text/plain')
    assert 'attachment; filename=universe_snapshot.txt' == universe_response.headers['content-disposition']
    assert 'Universe snapshot' in universe_response.text
    assert 'tradable_count' in universe_response.text
