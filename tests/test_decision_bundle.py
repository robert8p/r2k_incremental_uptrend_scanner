from __future__ import annotations

import csv
import io
import json
import sys
from types import ModuleType
from datetime import datetime
from types import SimpleNamespace

from app.config import Settings
from app.services.decision_bundle import (
    build_decision_bundle_pack,
    get_or_build_decision_state,
    maybe_refresh_decision_bundle_after_close,
)
from app.services.diagnostics import build_diagnostics_snapshot


class DummyAlpaca:
    def __init__(self, has_creds: bool = True):
        self._has_creds = has_creds

    def has_credentials(self) -> bool:
        return self._has_creds

    def ping_data_api(self):
        return {'message': 'ok'}


def _csv_bytes(rows: list[dict[str, object]]) -> bytes:
    if not rows:
        return b''
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode('utf-8')


def test_build_decision_bundle_pack_includes_backfill_and_decision_state(monkeypatch, tmp_path):
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
        'shadow_promotion_readiness_rows.csv': _csv_bytes([
            {'profile_name': 'soft_bounce_quality', 'promotion_readiness_verdict': 'shadow_profile_promising_but_early'}
        ]),
        'overstrictness_shadow_daily_rollup.csv': _csv_bytes([
            {'trading_day': '2026-04-02', 'scan_offset_minutes': 120, 'verdict_bucket': 'possible_classifier_overstrict', 'count': 2}
        ]),
        'shadow_threshold_profile_rollup.csv': _csv_bytes([
            {'profile_name': 'soft_bounce_quality', 'flagged_total': 4, 'flagged_possible_classifier_overstrict': 4, 'flagged_classifier_correct_reject': 0}
        ]),
    }
    checkpoint_pack = {
        'checkpoint_decision_scan_rows.csv': _csv_bytes([
            {'scan_id': 42, 'scan_offset_minutes': 120, 'stage2_count': 2}
        ])
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
    monkeypatch.setattr('app.services.decision_bundle.build_replay_bottleneck_pack', lambda *args, **kwargs: {'replay_bottleneck_summary.json': json.dumps({'best_offset_by_tradeable_share': {'scan_offset_minutes': 120, 'tradeable_share': 0.55}, 'worst_offset_by_tradeable_share': {'scan_offset_minutes': 150, 'tradeable_share': 0.41}, 'tradeable_share_support_threshold': 0.5}).encode('utf-8')})
    monkeypatch.setattr('app.services.decision_bundle.read_cached_historical_replay_zip', lambda settings: None)

    pack = build_decision_bundle_pack(settings, object(), DummyAlpaca(), days=60, offsets=[120, 150])
    summary = json.loads(pack['decision_state_summary.json'])
    backfill = json.loads(pack['historical_shadow_backfill_summary.json'])

    assert 'decision_state_summary.json' in pack
    assert 'historical_shadow_daily_rollup.csv' in pack
    assert summary['best_shadow_profile'] == 'soft_bounce_quality'
    assert summary['latest_selected_day'] == '2026-04-02'
    assert summary['decision_recommendation_code'] == 'historical_replay_supports_candidate_profile_hold_live_gate'
    assert summary['historical_replay_shadow']['overall_verdict'] == 'historical_replay_supports_candidate_profile'
    assert summary['historical_replay_bottleneck']['best_offset_by_tradeable_share']['scan_offset_minutes'] == 120
    assert backfill['clean_day_count'] == 3


def test_build_decision_state_marks_checkpoint_specific_replay_candidate(monkeypatch, tmp_path):
    settings = Settings(data_dir=str(tmp_path), database_path=str(tmp_path / 'bundle.db'))

    shadow_summary = {
        'source_clean_day_count': 1,
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
        'shadow_promotion_readiness_rows.csv': _csv_bytes([
            {'profile_name': 'soft_bounce_quality', 'promotion_readiness_verdict': 'shadow_profile_promising_but_early'}
        ]),
        'overstrictness_shadow_daily_rollup.csv': _csv_bytes([
            {'trading_day': '2026-04-02', 'scan_offset_minutes': 120, 'verdict_bucket': 'possible_classifier_overstrict', 'count': 2}
        ]),
        'shadow_threshold_profile_rollup.csv': _csv_bytes([
            {'profile_name': 'soft_bounce_quality', 'flagged_total': 4, 'flagged_possible_classifier_overstrict': 4, 'flagged_classifier_correct_reject': 0}
        ]),
    }
    checkpoint_surface = {
        'summary': {
            'selected_day': '2026-04-02',
            'currently_valid_now_count': 0,
            'regressed_after_earlier_validity_count': 0,
            'best_checkpoint_offset_minutes': 120,
            'max_stage2_count_any_checkpoint': 0,
            'surface_message': 'No current names.',
        }
    }

    monkeypatch.setattr('app.services.decision_bundle.build_shadow_promotion_pack', lambda *args, **kwargs: shadow_pack)
    monkeypatch.setattr('app.services.decision_bundle.build_checkpoint_decision_surface', lambda *args, **kwargs: checkpoint_surface)
    monkeypatch.setattr('app.services.decision_bundle.build_live_trust_snapshot', lambda *args, **kwargs: {'latest_research_run_id': 42})
    monkeypatch.setattr('app.services.decision_bundle.read_cached_historical_replay_summary', lambda settings: {'available': True, 'overall_verdict': 'historical_replay_no_clear_candidate', 'recommended_profile': {'profile_name': 'soft_cycle_durability'}, 'trading_day_count': 80})
    monkeypatch.setattr('app.services.decision_bundle.build_replay_bottleneck_pack', lambda *args, **kwargs: {'replay_bottleneck_summary.json': json.dumps({'best_offset_by_tradeable_share': {'scan_offset_minutes': 120, 'tradeable_share': 0.5375}, 'worst_offset_by_tradeable_share': {'scan_offset_minutes': 150, 'tradeable_share': 0.4302}, 'tradeable_share_support_threshold': 0.5}).encode('utf-8')})

    from app.services.decision_bundle import build_decision_state
    summary = build_decision_state(settings, object(), DummyAlpaca(), days=60, offsets=[120, 150])

    assert summary['decision_recommendation_code'] == 'historical_replay_supports_checkpoint_specific_candidate_hold_live_gate'
    assert '120-minute checkpoint clears the replay support bar' in summary['decision_recommendation_message']
    assert summary['historical_replay_bottleneck']['best_offset_by_tradeable_share']['scan_offset_minutes'] == 120



def test_get_or_build_decision_state_returns_fallback_without_credentials(tmp_path):
    settings = Settings(data_dir=str(tmp_path), database_path=str(tmp_path / 'no_creds.db'))
    payload = get_or_build_decision_state(settings, object(), DummyAlpaca(False), prefer_cache=False)
    assert payload['overall_promotion_readiness'] == 'insufficient_runtime_context'
    assert payload['decision_bundle_available'] is False


def test_maybe_refresh_decision_bundle_after_close_respects_post_close_gate(monkeypatch, tmp_path):
    settings = Settings(data_dir=str(tmp_path), database_path=str(tmp_path / 'after_close.db'))
    repos = SimpleNamespace(scan=SimpleNamespace(list_recent=lambda limit=50: [{'trading_day': '2026-04-02'}]))
    session = SimpleNamespace(now_et=datetime(2026, 4, 2, 17, 0), market_close=datetime(2026, 4, 2, 16, 0))

    monkeypatch.setattr('app.services.decision_bundle.ensure_repository_bundle', lambda db: repos)
    fake_market_time = ModuleType('app.services.market_time')
    fake_market_time.latest_or_previous_trading_day = lambda: '2026-04-02'
    fake_market_time.get_session_for_day = lambda trading_day, offset: session
    monkeypatch.setitem(sys.modules, 'app.services.market_time', fake_market_time)
    monkeypatch.setattr('app.services.decision_bundle.read_cached_decision_state', lambda settings: None)
    monkeypatch.setattr('app.services.decision_bundle.refresh_decision_bundle_cache', lambda *args, **kwargs: {'latest_selected_day': '2026-04-02'})

    payload = maybe_refresh_decision_bundle_after_close(settings, repos, DummyAlpaca(), days=60, offsets=[120, 150])
    assert payload == {'latest_selected_day': '2026-04-02'}


def test_build_diagnostics_snapshot_includes_decision_state(monkeypatch, tmp_path):
    settings = Settings(data_dir=str(tmp_path), database_path=str(tmp_path / 'diag.db'))

    class DummyStatus:
        tradable_count = 10

    monkeypatch.setattr('app.services.diagnostics.load_universe', lambda *args, **kwargs: {'status': DummyStatus()})
    monkeypatch.setattr('app.services.diagnostics.build_contract_health', lambda *args, **kwargs: {'ok': True})
    monkeypatch.setattr('app.services.diagnostics.build_live_trust_snapshot', lambda *args, **kwargs: {'latest_research_run_id': 7})
    monkeypatch.setattr('app.services.decision_bundle.get_or_build_decision_state', lambda *args, **kwargs: {'overall_promotion_readiness': 'shadow_profile_promising_but_early'})
    fake_market_time = ModuleType('app.services.market_time')
    fake_market_time.latest_or_previous_trading_day = lambda: '2026-04-02'
    monkeypatch.setitem(sys.modules, 'app.services.market_time', fake_market_time)

    class DummyDB:
        def get_latest_scan(self):
            return {'id': 1}

    snapshot = build_diagnostics_snapshot(settings, DummyDB(), DummyAlpaca(), scheduler_status={'leader': True})
    assert snapshot['decision_state']['overall_promotion_readiness'] == 'shadow_profile_promising_but_early'
