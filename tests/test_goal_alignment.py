from __future__ import annotations

import json
import os
import tempfile

from fastapi.testclient import TestClient

from app.config import Settings
from app.factory import create_app
from app.page_contexts import build_index_page_context
from app.runtime import AppRuntime
from app.services.goal_alignment import build_goal_alignment_summary, build_goal_alignment_text
from app.services.decision_bundle import build_decision_bundle_pack
from app.db import Database


class DummyAlpaca:
    def __init__(self, settings: Settings):
        self.settings = settings

    def has_credentials(self) -> bool:
        return False

    def ping_data_api(self):
        return {'message': 'ok'}


class DummyDB:
    def __init__(self, path: str):
        self.path = path


def _runtime() -> AppRuntime:
    tmpdir = tempfile.mkdtemp()
    settings = Settings(
        database_path=os.path.join(tmpdir, 'goal-align.db'),
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


def test_goal_alignment_summary_highlights_current_phase():
    settings = Settings(
        app_env='production',
        trading_mode='scan_only',
        enable_live_trading=False,
        alpaca_data_feed='sip',
        scheduled_scan_offsets='120,150',
    )
    summary = build_goal_alignment_summary(
        settings,
        universe_status={'tradable_count': 1921},
        decision_state={
            'clean_day_count': 1,
            'best_shadow_profile': 'soft_bounce_quality',
            'overall_promotion_readiness': 'shadow_profile_promising_but_early',
            'decision_recommendation_code': 'hold_live_behavior_and_keep_accumulating',
            'currently_valid_now_count': 0,
            'regressed_after_earlier_validity_count': 2,
            'latest_selected_day': '2026-04-02',
            'historical_replay_shadow': {'available': True, 'overall_verdict': 'historical_replay_supports_candidate_profile', 'recommended_profile': {'profile_name': 'soft_bounce_quality'}, 'trading_day_count': 60},
        },
    )
    assert summary['best_shadow_profile'] == 'soft_bounce_quality'
    assert 'structural classifier' in summary['likely_future_pressure_point']
    assert any('Evidence density is still thin' in item for item in summary['what_matters_now'])
    assert any('primary evidence engine' in item for item in summary['what_matters_now'])
    text = build_goal_alignment_text(summary)
    assert 'Goal alignment readout' in text
    assert 'What would justify change' in text


def test_diagnostics_page_renders_goal_alignment_block_and_download(monkeypatch):
    runtime = _runtime()
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
            'goal_alignment': {
                'operating_posture': 'Production, scan_only, live trading disabled.',
                'overall_assessment': 'Coherent for current phase.',
                'likely_future_pressure_point': 'Structural classifier strictness.',
                'what_matters_now': ['Accumulate clean sessions.'],
                'what_is_frozen': ['Live threshold changes.'],
                'what_would_justify_change': ['Promotion gate opens.'],
            },
        },
    )
    monkeypatch.setattr('app.services.diagnostics.read_recent_logs', lambda settings: ['log line'])

    client = TestClient(app)
    response = client.get('/diagnostics')
    assert response.status_code == 200
    body = response.text
    assert 'Goal alignment readout' in body
    assert 'Download goal alignment .txt' in body
    assert 'Accumulate clean sessions.' in body


def test_goal_alignment_text_download_returns_plain_text(monkeypatch):
    runtime = _runtime()
    app = create_app(runtime)

    monkeypatch.setattr(
        'app.services.diagnostics.build_diagnostics_snapshot',
        lambda *args, **kwargs: {
            'goal_alignment': {
                'generated_at_utc': '2026-04-03T20:24:02+00:00',
                'objective_summary': 'Objective text',
                'operating_posture': 'Production posture',
                'overall_assessment': 'Assessment',
                'likely_future_pressure_point': 'Pressure point',
                'what_matters_now': ['One'],
                'what_is_frozen': ['Two'],
                'what_would_justify_change': ['Three'],
                'latest_selected_day': '2026-04-02',
                'clean_day_count': 1,
                'best_shadow_profile': 'soft_bounce_quality',
                'overall_promotion_readiness': 'shadow_profile_promising_but_early',
                'decision_recommendation_code': 'hold_live_behavior_and_keep_accumulating',
                'tradable_universe_count': 1921,
                'historical_replay_available': True,
                'historical_replay_best_profile': 'soft_bounce_quality',
                'historical_replay_overall_verdict': 'historical_replay_supports_candidate_profile',
                'historical_replay_trading_day_count': 60,
            }
        },
    )

    client = TestClient(app)
    response = client.get('/diagnostics/goal-alignment.txt')
    assert response.status_code == 200
    assert response.headers['content-disposition'] == 'attachment; filename=goal_alignment.txt'
    assert 'Goal alignment readout' in response.text
    assert 'What matters now' in response.text


def test_index_page_context_includes_goal_alignment(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        settings = Settings(database_path=os.path.join(tmpdir, 'test.db'), data_dir=tmpdir)
        db = Database(os.path.join(tmpdir, 'test.db'))
        monkeypatch.setattr('app.page_contexts.read_cached_decision_state', lambda settings: {'overall_promotion_readiness': 'shadow_profile_promising_but_early'})
        monkeypatch.setattr('app.services.universe.load_universe', lambda *args, **kwargs: {'status': {'tradable_count': 1921}})
        context = build_index_page_context(settings, db)
        assert 'goal_alignment' in context
        assert 'what_matters_now' in context['goal_alignment']


def test_decision_bundle_pack_includes_goal_alignment_files(monkeypatch, tmp_path):
    settings = Settings(data_dir=str(tmp_path), database_path=str(tmp_path / 'bundle.db'))
    shadow_summary = {
        'source_clean_day_count': 3,
        'overall_promotion_readiness': 'shadow_profile_promising_but_early',
        'overall_reason': 'Profile looks promising but still early.',
        'recommended_profile': {
            'profile_name': 'soft_bounce_quality',
            'promotion_readiness_verdict': 'shadow_profile_promising_but_early',
            'flagged_possible_classifier_overstrict': 4,
            'flagged_classifier_correct_reject': 0,
            'precision_like_overstrict_share': 1.0,
        },
        'source_verdict_counts': {'possible_classifier_overstrict': 4, 'classifier_correct_reject': 2},
    }
    shadow_pack = {
        'shadow_promotion_summary.json': json.dumps(shadow_summary).encode('utf-8'),
        'shadow_promotion_readiness_rows.csv': b'profile_name,promotion_readiness_verdict\nsoft_bounce_quality,shadow_profile_promising_but_early\n',
        'overstrictness_shadow_daily_rollup.csv': b'trading_day,scan_offset_minutes,verdict_bucket,count\n2026-04-02,120,possible_classifier_overstrict,2\n',
        'shadow_threshold_profile_rollup.csv': b'profile_name,flagged_total,flagged_possible_classifier_overstrict,flagged_classifier_correct_reject\nsoft_bounce_quality,4,4,0\n',
    }
    checkpoint_pack = {
        'checkpoint_decision_scan_rows.csv': b'scan_id,scan_offset_minutes,stage2_count\n42,120,2\n'
    }
    checkpoint_surface = {
        'summary': {
            'selected_day': '2026-04-02',
            'currently_valid_now_count': 0,
            'regressed_after_earlier_validity_count': 2,
            'best_checkpoint_offset_minutes': 120,
            'max_stage2_count_any_checkpoint': 2,
            'surface_message': 'Earlier-valid names still matter.',
        }
    }

    monkeypatch.setattr('app.services.decision_bundle.build_shadow_promotion_pack', lambda *args, **kwargs: shadow_pack)
    monkeypatch.setattr('app.services.decision_bundle.build_checkpoint_decision_pack', lambda *args, **kwargs: checkpoint_pack)
    monkeypatch.setattr('app.services.decision_bundle.build_checkpoint_decision_surface', lambda *args, **kwargs: checkpoint_surface)
    monkeypatch.setattr('app.services.decision_bundle.build_live_trust_snapshot', lambda *args, **kwargs: {'latest_research_run_id': 42})
    monkeypatch.setattr('app.services.decision_bundle.read_cached_historical_replay_summary', lambda settings: {'overall_verdict': 'historical_replay_supports_candidate_profile', 'recommended_profile': {'profile_name': 'soft_bounce_quality'}, 'trading_day_count': 60})
    monkeypatch.setattr('app.services.decision_bundle.read_cached_historical_replay_zip', lambda settings: None)
    monkeypatch.setattr('app.services.universe.load_universe', lambda *args, **kwargs: {'status': {'tradable_count': 1921}})

    pack = build_decision_bundle_pack(settings, object(), DummyAlpaca(settings), days=60, offsets=[120, 150])
    assert 'goal_alignment_summary.json' in pack
    assert 'goal_alignment.txt' in pack
    assert b'Goal alignment readout' in pack['goal_alignment.txt']
