from __future__ import annotations

import csv
import json
import zipfile
from datetime import datetime, timezone
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any

from app.config import Settings
from app.db import Database
from app.repositories import RepositoryBundle, ensure_repository_bundle
from app.services.checkpoint_decision_surface import build_checkpoint_decision_pack, build_checkpoint_decision_surface
from app.services.evidence_pack import pack_to_zip_bytes
from app.services.goal_alignment import build_goal_alignment_summary, build_goal_alignment_text
from app.services.replay_bottleneck_pack import build_replay_bottleneck_pack
from app.services.historical_replay_shadow_pack import (
    read_cached_historical_replay_summary,
    read_cached_historical_replay_zip,
)
from app.services.live_trust import build_live_trust_snapshot
from app.services.shadow_promotion_pack import build_shadow_promotion_pack
from app.version import VERSION

UTC = timezone.utc
DEFAULT_DECISION_BUNDLE_DAYS = 60
DEFAULT_DECISION_BUNDLE_OFFSETS = [120, 150]
DEFAULT_REPLAY_LOOKBACK_DAYS = 90
_CACHE_DIR_NAME = 'decision_bundle'
_CACHE_ZIP_NAME = 'decision_bundle_latest.zip'
_CACHE_SUMMARY_NAME = 'decision_state_latest.json'


def _read_json_bytes(raw: bytes) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        return dict(json.loads(raw.decode('utf-8')))
    except Exception:
        return {}



def _read_csv_bytes(raw: bytes) -> list[dict[str, Any]]:
    if not raw:
        return []
    try:
        reader = csv.DictReader(StringIO(raw.decode('utf-8')))
        return [dict(row) for row in reader]
    except Exception:
        return []



def _to_int(value: Any) -> int:
    try:
        if value in (None, '', 'None'):
            return 0
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _to_float(value: Any) -> float | None:
    try:
        if value in (None, '', 'None'):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None



def _cache_dir(settings: Settings) -> Path:
    path = Path(settings.data_dir) / _CACHE_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path



def _cache_zip_path(settings: Settings) -> Path:
    return _cache_dir(settings) / _CACHE_ZIP_NAME



def _cache_summary_path(settings: Settings) -> Path:
    return _cache_dir(settings) / _CACHE_SUMMARY_NAME



def _backfill_summary_from_shadow_pack(shadow_pack: dict[str, bytes], *, days: int, offsets: list[int]) -> dict[str, Any]:
    shadow_summary = _read_json_bytes(shadow_pack.get('shadow_promotion_summary.json', b''))
    readiness_rows = _read_csv_bytes(shadow_pack.get('shadow_promotion_readiness_rows.csv', b''))
    daily_rollup = _read_csv_bytes(shadow_pack.get('overstrictness_shadow_daily_rollup.csv', b''))
    profile_rollup = _read_csv_bytes(shadow_pack.get('shadow_threshold_profile_rollup.csv', b''))
    clean_days = sorted({str(row.get('trading_day') or '') for row in daily_rollup if str(row.get('trading_day') or '')})
    recommended_profile = dict(shadow_summary.get('recommended_profile') or {})
    return {
        'bundle_type': 'historical_shadow_backfill',
        'days_requested': int(days),
        'offsets_requested': list(offsets),
        'clean_day_count': _to_int(shadow_summary.get('source_clean_day_count')),
        'clean_trading_days': clean_days,
        'overall_promotion_readiness': shadow_summary.get('overall_promotion_readiness'),
        'overall_reason': shadow_summary.get('overall_reason'),
        'best_shadow_profile': recommended_profile.get('profile_name'),
        'best_shadow_profile_verdict': recommended_profile.get('promotion_readiness_verdict'),
        'best_shadow_profile_flagged_possible_overstrict': _to_int(recommended_profile.get('flagged_possible_classifier_overstrict')),
        'best_shadow_profile_flagged_correct_reject': _to_int(recommended_profile.get('flagged_classifier_correct_reject')),
        'best_shadow_profile_precision_like_overstrict_share': recommended_profile.get('precision_like_overstrict_share'),
        'source_verdict_counts': dict(shadow_summary.get('source_verdict_counts') or {}),
        'profile_readiness_rows': readiness_rows,
        'profile_rollup_rows': profile_rollup,
        'daily_rollup_rows': daily_rollup,
    }



def _historical_replay_summary_from_cache(summary: dict[str, Any] | None, *, lookback_days: int, offsets: list[int]) -> dict[str, Any]:
    payload = dict(summary or {})
    if not payload:
        return {
            'available': False,
            'bundle_type': 'historical_replay_shadow_pack',
            'lookback_days_requested': int(lookback_days),
            'offsets_requested': list(offsets),
            'overall_verdict': 'historical_replay_not_built',
            'overall_reason': 'Historical replay shadow pack has not been generated yet.',
            'recommended_profile': None,
            'trading_day_count': 0,
        }
    payload['available'] = True
    payload.setdefault('bundle_type', 'historical_replay_shadow_pack')
    payload.setdefault('lookback_days_requested', int(lookback_days))
    payload.setdefault('offsets_requested', list(offsets))
    return payload



def _decision_recommendation(
    *,
    replay_summary: dict[str, Any],
    replay_bottleneck_summary: dict[str, Any] | None,
    promotion_readiness: str | None,
    clean_day_count: int,
    current_valid_now_count: int,
    regressed_count: int,
) -> tuple[str, str]:
    replay_verdict = str(replay_summary.get('overall_verdict') or '')
    replay_profile = ((replay_summary.get('recommended_profile') or {}) or {}).get('profile_name')
    bottleneck = dict(replay_bottleneck_summary or {})
    best_offset = dict(bottleneck.get('best_offset_by_tradeable_share') or {})
    best_offset_minutes = _to_int(best_offset.get('scan_offset_minutes'))
    best_offset_share = best_offset.get('tradeable_share')
    worst_offset = dict(bottleneck.get('worst_offset_by_tradeable_share') or {})
    worst_offset_minutes = _to_int(worst_offset.get('scan_offset_minutes'))
    worst_offset_share = worst_offset.get('tradeable_share')
    support_threshold = bottleneck.get('tradeable_share_support_threshold')
    readiness = str(promotion_readiness or 'insufficient_evidence')
    if readiness == 'eligible_for_narrow_live_trial':
        return (
            'eligible_for_narrow_live_trial',
            'A shadow profile has satisfied the automated promotion gate. Keep live behavior unchanged until a controlled trial is explicitly approved.',
        )
    if replay_verdict == 'historical_replay_supports_candidate_profile' and replay_profile:
        return (
            'historical_replay_supports_candidate_profile_hold_live_gate',
            f'Historical replay under the current clean logic supports {replay_profile} as the leading candidate profile, but live behavior remains frozen until the live release gate opens.',
        )
    if replay_profile and best_offset_minutes > 0 and _to_float(best_offset_share) is not None and (_to_float(best_offset_share) or 0.0) >= (_to_float(support_threshold) or 0.5):
        best_share_text = f"{(_to_float(best_offset_share) or 0.0):.4f}"
        if worst_offset_minutes > 0 and _to_float(worst_offset_share) is not None:
            worst_share_text = f"{(_to_float(worst_offset_share) or 0.0):.4f}"
            return (
                'historical_replay_supports_checkpoint_specific_candidate_hold_live_gate',
                f'Historical replay does not yet support {replay_profile} across all checkpoints, but the {best_offset_minutes}-minute checkpoint clears the replay support bar ({best_share_text}) while the {worst_offset_minutes}-minute checkpoint remains weaker ({worst_share_text}). Keep live behavior frozen and use the next tranche to isolate checkpoint-specific decay before any live change.',
            )
        return (
            'historical_replay_supports_checkpoint_specific_candidate_hold_live_gate',
            f'Historical replay does not yet support {replay_profile} across all checkpoints, but the {best_offset_minutes}-minute checkpoint clears the replay support bar ({best_share_text}). Keep live behavior frozen and isolate checkpoint-specific decay before any live change.',
        )
    if readiness == 'shadow_profile_promising_but_early':
        return (
            'hold_live_behavior_and_keep_accumulating',
            'The current best shadow profile is promising, but the clean-day evidence is still too thin for a live change.',
        )
    if clean_day_count <= 0:
        return (
            'await_first_clean_day',
            'No clean post-fix shadow backfill days were available yet. Keep the current live behavior and let the automated tracker accumulate evidence.',
        )
    if current_valid_now_count > 0:
        return (
            'use_current_valid_names_only',
            'The latest checkpoint still has currently valid names. Use only the currently valid set and keep live thresholds unchanged.',
        )
    if regressed_count > 0:
        return (
            'checkpoint_regression_observed_hold_live_behavior',
            'Earlier-valid names regressed by the latest checkpoint. Hold live behavior unchanged and keep accumulating checkpoint-aware evidence.',
        )
    return (
        'insufficient_shadow_evidence',
        'The automated shadow evidence does not yet support a live calibration change.',
    )



def _fallback_decision_state(*, settings: Settings, days: int, offsets: list[int], reason: str) -> dict[str, Any]:
    replay_summary = _historical_replay_summary_from_cache(None, lookback_days=DEFAULT_REPLAY_LOOKBACK_DAYS, offsets=offsets)
    recommendation_code, recommendation_message = _decision_recommendation(
        replay_summary=replay_summary,
        replay_bottleneck_summary=None,
        promotion_readiness='insufficient_runtime_context',
        clean_day_count=0,
        current_valid_now_count=0,
        regressed_count=0,
    )
    return {
        'generated_at_utc': datetime.now(UTC).isoformat(),
        'app_version': VERSION,
        'days_requested': int(days),
        'offsets_requested': list(offsets),
        'latest_selected_day': None,
        'clean_day_count': 0,
        'best_shadow_profile': None,
        'best_shadow_profile_verdict': None,
        'overall_promotion_readiness': 'insufficient_runtime_context',
        'overall_reason': reason,
        'currently_valid_now_count': 0,
        'regressed_after_earlier_validity_count': 0,
        'best_checkpoint_offset_minutes': None,
        'max_stage2_count_any_checkpoint': 0,
        'surface_message': 'Decision bundle has not been generated yet.',
        'decision_recommendation_code': recommendation_code,
        'decision_recommendation_message': recommendation_message,
        'evidence_engine': 'historical_replay_primary_live_gate_secondary',
        'historical_replay_shadow': replay_summary,
        'historical_replay_bottleneck': {},
        'historical_shadow_backfill': {
            'bundle_type': 'historical_shadow_backfill',
            'days_requested': int(days),
            'offsets_requested': list(offsets),
            'clean_day_count': 0,
            'clean_trading_days': [],
            'overall_promotion_readiness': 'insufficient_runtime_context',
            'overall_reason': reason,
            'best_shadow_profile': None,
            'best_shadow_profile_verdict': None,
            'best_shadow_profile_flagged_possible_overstrict': 0,
            'best_shadow_profile_flagged_correct_reject': 0,
            'best_shadow_profile_precision_like_overstrict_share': None,
            'source_verdict_counts': {},
            'profile_readiness_rows': [],
            'profile_rollup_rows': [],
            'daily_rollup_rows': [],
        },
        'checkpoint_summary': {},
        'live_trust_latest_research_run_id': None,
        'decision_bundle_available': False,
    }



def build_decision_state(
    settings: Settings,
    db: Database | RepositoryBundle,
    alpaca,
    *,
    days: int = DEFAULT_DECISION_BUNDLE_DAYS,
    offsets: list[int] | None = None,
) -> dict[str, Any]:
    requested_offsets = list(offsets or DEFAULT_DECISION_BUNDLE_OFFSETS)
    repos = ensure_repository_bundle(db)
    shadow_pack = build_shadow_promotion_pack(settings, repos, alpaca, days=days, offsets=requested_offsets)
    backfill = _backfill_summary_from_shadow_pack(shadow_pack, days=days, offsets=requested_offsets)
    checkpoint_surface = build_checkpoint_decision_surface(settings, repos, offsets=requested_offsets)
    checkpoint_summary = dict(checkpoint_surface.get('summary') or {})
    live_trust = build_live_trust_snapshot(settings, repos.db)
    replay_summary = _historical_replay_summary_from_cache(
        read_cached_historical_replay_summary(settings),
        lookback_days=DEFAULT_REPLAY_LOOKBACK_DAYS,
        offsets=requested_offsets,
    )
    replay_bottleneck_pack = build_replay_bottleneck_pack(
        settings,
        repos,
        alpaca,
        lookback_days=DEFAULT_REPLAY_LOOKBACK_DAYS,
        offsets=requested_offsets,
    ) if replay_summary.get('available') else {}
    replay_bottleneck_summary = _read_json_bytes(replay_bottleneck_pack.get('replay_bottleneck_summary.json', b''))
    recommendation_code, recommendation_message = _decision_recommendation(
        replay_summary=replay_summary,
        replay_bottleneck_summary=replay_bottleneck_summary,
        promotion_readiness=str(backfill.get('overall_promotion_readiness') or ''),
        clean_day_count=_to_int(backfill.get('clean_day_count')),
        current_valid_now_count=_to_int(checkpoint_summary.get('currently_valid_now_count')),
        regressed_count=_to_int(checkpoint_summary.get('regressed_after_earlier_validity_count')),
    )
    return {
        'generated_at_utc': datetime.now(UTC).isoformat(),
        'app_version': VERSION,
        'days_requested': int(days),
        'offsets_requested': requested_offsets,
        'latest_selected_day': checkpoint_summary.get('selected_day'),
        'clean_day_count': _to_int(backfill.get('clean_day_count')),
        'best_shadow_profile': backfill.get('best_shadow_profile'),
        'best_shadow_profile_verdict': backfill.get('best_shadow_profile_verdict'),
        'overall_promotion_readiness': backfill.get('overall_promotion_readiness'),
        'overall_reason': backfill.get('overall_reason'),
        'currently_valid_now_count': _to_int(checkpoint_summary.get('currently_valid_now_count')),
        'regressed_after_earlier_validity_count': _to_int(checkpoint_summary.get('regressed_after_earlier_validity_count')),
        'best_checkpoint_offset_minutes': checkpoint_summary.get('best_checkpoint_offset_minutes'),
        'max_stage2_count_any_checkpoint': _to_int(checkpoint_summary.get('max_stage2_count_any_checkpoint')),
        'surface_message': checkpoint_summary.get('surface_message'),
        'decision_recommendation_code': recommendation_code,
        'decision_recommendation_message': recommendation_message,
        'evidence_engine': 'historical_replay_primary_live_gate_secondary',
        'historical_replay_shadow': replay_summary,
        'historical_replay_bottleneck': replay_bottleneck_summary,
        'historical_shadow_backfill': backfill,
        'checkpoint_summary': checkpoint_summary,
        'live_trust_latest_research_run_id': live_trust.get('latest_research_run_id'),
        'decision_bundle_available': True,
    }



def _extract_cached_replay_files(settings: Settings) -> dict[str, bytes]:
    raw_zip = read_cached_historical_replay_zip(settings)
    if not raw_zip:
        return {}
    wanted = {
        'historical_replay_shadow_summary.json',
        'historical_replay_shadow_daily_rollup.csv',
        'historical_replay_shadow_profile_rollup.csv',
    }
    try:
        with zipfile.ZipFile(BytesIO(raw_zip), 'r') as zf:
            return {name: zf.read(name) for name in wanted if name in zf.namelist()}
    except Exception:
        return {}



def build_decision_bundle_pack(
    settings: Settings,
    db: Database | RepositoryBundle,
    alpaca,
    *,
    days: int = DEFAULT_DECISION_BUNDLE_DAYS,
    offsets: list[int] | None = None,
) -> dict[str, bytes]:
    requested_offsets = list(offsets or DEFAULT_DECISION_BUNDLE_OFFSETS)
    repos = ensure_repository_bundle(db)
    shadow_pack = build_shadow_promotion_pack(settings, repos, alpaca, days=days, offsets=requested_offsets)
    checkpoint_pack = build_checkpoint_decision_pack(settings, repos, offsets=requested_offsets)
    decision_state = build_decision_state(settings, repos, alpaca, days=days, offsets=requested_offsets)
    backfill = dict(decision_state.get('historical_shadow_backfill') or {})
    replay_summary = dict(decision_state.get('historical_replay_shadow') or {})
    replay_bottleneck_summary = dict(decision_state.get('historical_replay_bottleneck') or {})
    replay_cached_files = _extract_cached_replay_files(settings)
    checkpoint_summary = dict(decision_state.get('checkpoint_summary') or {})
    try:
        from app.services.universe import load_universe
        universe_status = load_universe(settings, repos.db, force_refresh=False)['status']
    except Exception:
        universe_status = {}
    goal_alignment = build_goal_alignment_summary(settings, universe_status=universe_status, decision_state=decision_state)

    report_lines = [
        '# Decision bundle',
        '',
        f"Generated at: {decision_state['generated_at_utc']}",
        f"App version: {VERSION}",
        f"Latest selected day: {decision_state.get('latest_selected_day') or 'None'}",
        f"Clean day count: {decision_state.get('clean_day_count')}",
        f"Best shadow profile: {decision_state.get('best_shadow_profile') or 'None'}",
        f"Promotion readiness: {decision_state.get('overall_promotion_readiness')}",
        f"Decision recommendation: {decision_state.get('decision_recommendation_code')}",
        decision_state.get('decision_recommendation_message') or '',
        '',
        '## Current decision state',
        f"- Currently valid now: {decision_state.get('currently_valid_now_count')}",
        f"- Regressed after earlier validity: {decision_state.get('regressed_after_earlier_validity_count')}",
        f"- Best checkpoint: {decision_state.get('best_checkpoint_offset_minutes')}",
        '',
        '## Historical shadow backfill',
        f"- Overall readiness: {backfill.get('overall_promotion_readiness')}",
        f"- Best profile verdict: {backfill.get('best_shadow_profile_verdict')}",
        f"- Source verdict counts: {backfill.get('source_verdict_counts')}",
        '',
        '## Historical replay shadow',
        f"- Available: {replay_summary.get('available')}",
        f"- Overall verdict: {replay_summary.get('overall_verdict')}",
        f"- Best replay profile: {((replay_summary.get('recommended_profile') or {}) or {}).get('profile_name')}",
        f"- Replay checkpoint split best offset: {((replay_bottleneck_summary.get('best_offset_by_tradeable_share') or {}) or {}).get('scan_offset_minutes')}",
        f"- Replay checkpoint split best offset share: {((replay_bottleneck_summary.get('best_offset_by_tradeable_share') or {}) or {}).get('tradeable_share')}",
        f"- Replay checkpoint split worst offset: {((replay_bottleneck_summary.get('worst_offset_by_tradeable_share') or {}) or {}).get('scan_offset_minutes')}",
        f"- Replay checkpoint split worst offset share: {((replay_bottleneck_summary.get('worst_offset_by_tradeable_share') or {}) or {}).get('tradeable_share')}",
    ]

    manifest = {
        'bundle_type': 'decision_bundle',
        'bundle_contract_version': '1.2',
        'app_version': VERSION,
        'generated_at_utc': decision_state['generated_at_utc'],
        'days_requested': int(days),
        'offsets_requested': requested_offsets,
        'latest_selected_day': decision_state.get('latest_selected_day'),
        'overall_promotion_readiness': decision_state.get('overall_promotion_readiness'),
        'decision_recommendation_code': decision_state.get('decision_recommendation_code'),
        'evidence_engine': decision_state.get('evidence_engine'),
    }

    pack = {
        'MANIFEST.json': json.dumps(manifest, indent=2).encode('utf-8'),
        'decision_state_summary.json': json.dumps(decision_state, indent=2).encode('utf-8'),
        'goal_alignment_summary.json': json.dumps(goal_alignment, indent=2).encode('utf-8'),
        'goal_alignment.txt': build_goal_alignment_text(goal_alignment).encode('utf-8'),
        'historical_shadow_backfill_summary.json': json.dumps(backfill, indent=2).encode('utf-8'),
        'historical_replay_shadow_summary.json': json.dumps(replay_summary, indent=2).encode('utf-8'),
        'historical_replay_bottleneck_summary.json': json.dumps(replay_bottleneck_summary, indent=2).encode('utf-8'),
        'checkpoint_decision_summary.json': json.dumps(checkpoint_summary, indent=2).encode('utf-8'),
        'historical_shadow_daily_rollup.csv': shadow_pack.get('overstrictness_shadow_daily_rollup.csv', b''),
        'historical_shadow_profile_rollup.csv': shadow_pack.get('shadow_threshold_profile_rollup.csv', b''),
        'historical_shadow_promotion_readiness_rows.csv': shadow_pack.get('shadow_promotion_readiness_rows.csv', b''),
        'historical_replay_shadow_daily_rollup.csv': replay_cached_files.get('historical_replay_shadow_daily_rollup.csv', b''),
        'historical_replay_shadow_profile_rollup.csv': replay_cached_files.get('historical_replay_shadow_profile_rollup.csv', b''),
        'checkpoint_decision_scan_rows.csv': checkpoint_pack.get('checkpoint_decision_scan_rows.csv', b''),
        'report.md': '\n'.join(report_lines).encode('utf-8'),
    }
    return pack



def write_decision_bundle_cache(
    settings: Settings,
    pack: dict[str, bytes],
    decision_state: dict[str, Any],
) -> dict[str, Any]:
    _cache_zip_path(settings).write_bytes(pack_to_zip_bytes(pack))
    _cache_summary_path(settings).write_text(json.dumps(decision_state, indent=2), encoding='utf-8')
    return decision_state



def read_cached_decision_state(settings: Settings) -> dict[str, Any] | None:
    path = _cache_summary_path(settings)
    if not path.exists():
        return None
    try:
        return dict(json.loads(path.read_text(encoding='utf-8')))
    except Exception:
        return None



def read_cached_decision_bundle_zip(settings: Settings) -> bytes | None:
    path = _cache_zip_path(settings)
    if not path.exists():
        return None
    try:
        return path.read_bytes()
    except Exception:
        return None



def refresh_decision_bundle_cache(
    settings: Settings,
    db: Database | RepositoryBundle,
    alpaca,
    *,
    days: int = DEFAULT_DECISION_BUNDLE_DAYS,
    offsets: list[int] | None = None,
) -> dict[str, Any]:
    pack = build_decision_bundle_pack(settings, db, alpaca, days=days, offsets=offsets)
    decision_state = _read_json_bytes(pack.get('decision_state_summary.json', b''))
    return write_decision_bundle_cache(settings, pack, decision_state)



def get_or_build_decision_state(
    settings: Settings,
    db: Database | RepositoryBundle,
    alpaca,
    *,
    days: int = DEFAULT_DECISION_BUNDLE_DAYS,
    offsets: list[int] | None = None,
    prefer_cache: bool = True,
) -> dict[str, Any]:
    cached = read_cached_decision_state(settings) if prefer_cache else None
    if cached:
        return cached
    requested_offsets = list(offsets or DEFAULT_DECISION_BUNDLE_OFFSETS)
    if alpaca is None or not hasattr(alpaca, 'has_credentials') or not alpaca.has_credentials():
        return _fallback_decision_state(
            settings=settings,
            days=days,
            offsets=requested_offsets,
            reason='No cached decision bundle was available and market-data credentials were not present for an on-demand rebuild.',
        )
    return refresh_decision_bundle_cache(settings, db, alpaca, days=days, offsets=offsets)



def get_or_build_decision_bundle_zip(
    settings: Settings,
    db: Database | RepositoryBundle,
    alpaca,
    *,
    days: int = DEFAULT_DECISION_BUNDLE_DAYS,
    offsets: list[int] | None = None,
    prefer_cache: bool = True,
) -> bytes:
    cached = read_cached_decision_bundle_zip(settings) if prefer_cache else None
    if cached:
        return cached
    requested_offsets = list(offsets or DEFAULT_DECISION_BUNDLE_OFFSETS)
    if alpaca is None or not hasattr(alpaca, 'has_credentials') or not alpaca.has_credentials():
        fallback = _fallback_decision_state(
            settings=settings,
            days=days,
            offsets=requested_offsets,
            reason='No cached decision bundle was available and market-data credentials were not present for an on-demand rebuild.',
        )
        minimal_pack = {
            'MANIFEST.json': json.dumps({'bundle_type': 'decision_bundle', 'app_version': VERSION, 'generated_at_utc': fallback['generated_at_utc']}, indent=2).encode('utf-8'),
            'decision_state_summary.json': json.dumps(fallback, indent=2).encode('utf-8'),
            'report.md': fallback['decision_recommendation_message'].encode('utf-8'),
        }
        return pack_to_zip_bytes(minimal_pack)
    pack = build_decision_bundle_pack(settings, db, alpaca, days=days, offsets=offsets)
    decision_state = _read_json_bytes(pack.get('decision_state_summary.json', b''))
    write_decision_bundle_cache(settings, pack, decision_state)
    return read_cached_decision_bundle_zip(settings) or pack_to_zip_bytes(pack)



def maybe_refresh_decision_bundle_after_close(
    settings: Settings,
    db: Database | RepositoryBundle,
    alpaca,
    *,
    days: int = DEFAULT_DECISION_BUNDLE_DAYS,
    offsets: list[int] | None = None,
) -> dict[str, Any] | None:
    from app.services.market_time import get_session_for_day, latest_or_previous_trading_day

    requested_offsets = list(offsets or DEFAULT_DECISION_BUNDLE_OFFSETS)
    trading_day = latest_or_previous_trading_day()
    session = get_session_for_day(trading_day, max(requested_offsets))
    if session.now_et < session.market_close:
        return None
    repos = ensure_repository_bundle(db)
    scans_for_day = [scan for scan in repos.scan.list_recent(limit=50) if str(scan.get('trading_day') or '') == trading_day]
    if not scans_for_day:
        return None
    cached = read_cached_decision_state(settings)
    if cached and str(cached.get('latest_selected_day') or '') == trading_day:
        return cached
    return refresh_decision_bundle_cache(settings, repos, alpaca, days=days, offsets=requested_offsets)
