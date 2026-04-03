from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from typing import Any

from app.config import Settings
from app.db import Database
from app.repositories import RepositoryBundle
from app.services.evidence_pack import pack_to_zip_bytes
from app.services.overstrictness_shadow_pack import DEFAULT_SHADOW_PROFILES
from app.version import VERSION

UTC = timezone.utc
DEFAULT_REPLAY_LOOKBACK_DAYS = 90
DEFAULT_REPLAY_OFFSETS = [120, 150]
_CACHE_DIR_NAME = 'historical_replay_shadow'
_CACHE_ZIP_NAME = 'historical_replay_shadow_latest.zip'
_CACHE_SUMMARY_NAME = 'historical_replay_shadow_latest.json'

PREDICATE_THRESHOLD_MAP: dict[str, tuple[str, str]] = {
    'stable_range_width_retention': ('width_retention_ratio', '>='),
    'stable_range_cycle_persistence': ('cycle_persistence_ratio', '>='),
    'stable_range_bounce_quality': ('bounce_quality_score', '>='),
    'stable_range_cycle_durability': ('cycle_durability_score', '>='),
}


def _cache_dir(settings: Settings) -> Path:
    path = Path(settings.data_dir) / _CACHE_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_zip_path(settings: Settings) -> Path:
    return _cache_dir(settings) / _CACHE_ZIP_NAME


def _cache_summary_path(settings: Settings) -> Path:
    return _cache_dir(settings) / _CACHE_SUMMARY_NAME


def read_cached_historical_replay_summary(settings: Settings) -> dict[str, Any] | None:
    path = _cache_summary_path(settings)
    if not path.exists():
        return None
    try:
        return dict(json.loads(path.read_text(encoding='utf-8')))
    except Exception:
        return None


def read_cached_historical_replay_zip(settings: Settings) -> bytes | None:
    path = _cache_zip_path(settings)
    if not path.exists():
        return None
    try:
        return path.read_bytes()
    except Exception:
        return None


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


def _csv_bytes(rows: list[dict[str, Any]]) -> bytes:
    if not rows:
        return b''
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode('utf-8')


def _metric_value(row: dict[str, Any], field: str) -> float | None:
    metrics = dict(row.get('metrics') or {})
    return _to_float(metrics.get(field))


def _row_passes_profile(row: dict[str, Any], profile_name: str) -> tuple[bool, list[str]]:
    overrides = dict(DEFAULT_SHADOW_PROFILES.get(profile_name) or {})
    failed: list[str] = []
    for predicate_name, threshold in overrides.items():
        metric_name, comparator = PREDICATE_THRESHOLD_MAP[predicate_name]
        value = _metric_value(row, metric_name)
        if value is None:
            failed.append(predicate_name)
            continue
        passed = value >= float(threshold) if comparator == '>=' else value <= float(threshold)
        if not passed:
            failed.append(predicate_name)
    return not failed, failed


def _tradeable_from_row(row: dict[str, Any]) -> bool:
    return bool(row.get('entry_touched')) and bool(row.get('hit_target'))


def _select_replay_window(settings: Settings, lookback_days: int) -> tuple[str, str, list[str], int]:
    from app.services.market_time import latest_or_previous_trading_day, list_trading_days

    requested = max(int(lookback_days or DEFAULT_REPLAY_LOOKBACK_DAYS), 1)
    effective_limit = min(requested, max(int(settings.max_validation_days), 1))
    end_date = latest_or_previous_trading_day()
    end_dt = datetime.fromisoformat(end_date)
    calendar_start = (end_dt - timedelta(days=max(effective_limit * 3, effective_limit + 20))).date().isoformat()
    trading_days = list_trading_days(calendar_start, end_date)
    selected_days = trading_days[-effective_limit:] if len(trading_days) > effective_limit else trading_days
    if not selected_days:
        raise ValueError('No trading days available for historical replay shadow window.')
    return selected_days[0], selected_days[-1], selected_days, effective_limit


def _build_validation_payload(settings: Settings, db: Database | RepositoryBundle, alpaca, *, start_date: str, end_date: str, scan_offset_minutes: int) -> dict[str, Any]:
    from app.repositories import ensure_repository_bundle
    from app.services.backtest import run_validation

    repos = ensure_repository_bundle(db)
    return run_validation(
        settings,
        repos.db,
        alpaca,
        start_date,
        end_date,
        int(scan_offset_minutes),
        cache_history=True,
        persist=False,
        evaluate_non_advanced_rows=True,
    )


def _profile_rollup_rows(rows: list[dict[str, Any]], trading_day_count: int) -> list[dict[str, Any]]:
    rollup: list[dict[str, Any]] = []
    candidate_rows = [
        row for row in rows
        if bool(row.get('baseline_eligible'))
        and bool(row.get('scored_for_replay'))
        and str(row.get('range_classification_code') or '') == 'C'
        and not bool(row.get('advanced_to_stage2'))
    ]
    for profile_name in DEFAULT_SHADOW_PROFILES:
        flagged_total = 0
        flagged_tradeable = 0
        flagged_entry_touched = 0
        flagged_untradeable = 0
        offsets_flagged: set[int] = set()
        days_flagged: set[str] = set()
        for row in candidate_rows:
            passed, _ = _row_passes_profile(row, profile_name)
            if not passed:
                continue
            flagged_total += 1
            days_flagged.add(str(row.get('trading_day') or ''))
            offsets_flagged.add(_to_int(row.get('scan_offset_minutes')))
            if bool(row.get('entry_touched')):
                flagged_entry_touched += 1
            if _tradeable_from_row(row):
                flagged_tradeable += 1
            else:
                flagged_untradeable += 1
        precision_like = round(flagged_tradeable / max(flagged_total, 1), 4) if flagged_total else None
        rollup.append(
            {
                'profile_name': profile_name,
                'flagged_total': flagged_total,
                'flagged_tradeable': flagged_tradeable,
                'flagged_entry_touched': flagged_entry_touched,
                'flagged_untradeable': flagged_untradeable,
                'tradeable_share': precision_like,
                'admitted_per_clean_day_avg': round(flagged_total / max(trading_day_count, 1), 4) if trading_day_count else None,
                'tradeable_per_clean_day_avg': round(flagged_tradeable / max(trading_day_count, 1), 4) if trading_day_count else None,
                'days_flagged_count': len(days_flagged),
                'offsets_flagged': ';'.join(str(x) for x in sorted(offsets_flagged)),
            }
        )
    return rollup


def _profile_row_exports(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    exported: list[dict[str, Any]] = []
    candidate_rows = [
        row for row in rows
        if bool(row.get('baseline_eligible'))
        and bool(row.get('scored_for_replay'))
        and str(row.get('range_classification_code') or '') == 'C'
        and not bool(row.get('advanced_to_stage2'))
    ]
    for row in candidate_rows:
        base = {
            'trading_day': row.get('trading_day'),
            'scan_offset_minutes': row.get('scan_offset_minutes'),
            'symbol': row.get('symbol'),
            'entry_touched': bool(row.get('entry_touched')),
            'hit_target': bool(row.get('hit_target')),
            'minutes_to_entry': row.get('minutes_to_entry'),
            'minutes_to_target': row.get('minutes_to_target'),
            'range_classification_code': row.get('range_classification_code'),
            'mover_rank': row.get('mover_rank'),
            'intraday_pct_gain': row.get('intraday_pct_gain'),
            'distance_to_entry_pct': _metric_value(row, 'distance_to_entry_pct'),
            'width_retention_ratio': _metric_value(row, 'width_retention_ratio'),
            'cycle_persistence_ratio': _metric_value(row, 'cycle_persistence_ratio'),
            'bounce_quality_score': _metric_value(row, 'bounce_quality_score'),
            'cycle_durability_score': _metric_value(row, 'cycle_durability_score'),
            'within_range_target_possible': (row.get('metrics') or {}).get('within_range_target_possible'),
        }
        for profile_name in DEFAULT_SHADOW_PROFILES:
            passed, failed = _row_passes_profile(row, profile_name)
            exported.append(
                {
                    **base,
                    'profile_name': profile_name,
                    'would_pass_shadow_profile': passed,
                    'remaining_failed_predicates': '; '.join(sorted(failed)),
                    'shadow_tradeable_if_admitted': _tradeable_from_row(row),
                }
            )
    return exported


def _daily_rollup_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_day_offset: dict[tuple[str, int], dict[str, int]] = {}
    for row in rows:
        day = str(row.get('trading_day') or '')
        offset = _to_int(row.get('scan_offset_minutes'))
        key = (day, offset)
        bucket = by_day_offset.setdefault(
            key,
            {
                'trading_day': day,
                'scan_offset_minutes': offset,
                'rows_scored': 0,
                'baseline_advanced_to_stage2': 0,
                'baseline_advanced_and_tradeable': 0,
                'classification_c_rejects': 0,
            },
        )
        if bool(row.get('scored_for_replay')):
            bucket['rows_scored'] += 1
        if bool(row.get('advanced_to_stage2')):
            bucket['baseline_advanced_to_stage2'] += 1
            if _tradeable_from_row(row):
                bucket['baseline_advanced_and_tradeable'] += 1
        elif str(row.get('range_classification_code') or '') == 'C':
            bucket['classification_c_rejects'] += 1
    return [by_day_offset[key] for key in sorted(by_day_offset.keys())]


def _recommendation(rollup_rows: list[dict[str, Any]]) -> tuple[str, str, dict[str, Any] | None]:
    profile_priority = {
        'soft_bounce_quality': 6,
        'combined_soft_structure': 5,
        'soft_cycle_durability': 4,
        'soft_cycle_persistence': 3,
        'soft_width_retention': 2,
        'baseline_excluding_classifier_veto': 1,
    }
    sorted_rows = sorted(
        rollup_rows,
        key=lambda row: (
            _to_float(row.get('tradeable_share')) or -1.0,
            _to_int(row.get('flagged_tradeable')),
            -_to_int(row.get('flagged_untradeable')),
            profile_priority.get(str(row.get('profile_name') or ''), 0),
        ),
        reverse=True,
    )
    best = sorted_rows[0] if sorted_rows else None
    if not best:
        return 'historical_replay_no_clear_candidate', 'No replay-backed shadow profile rows were available.', None
    if _to_int(best.get('flagged_tradeable')) >= 4 and (_to_float(best.get('tradeable_share')) or 0.0) >= 0.5:
        return (
            'historical_replay_supports_candidate_profile',
            'Historical replay under the current clean logic supports this shadow profile as a candidate for a later live release-gate decision.',
            best,
        )
    return (
        'historical_replay_no_clear_candidate',
        'Historical replay did not yet surface a strong enough shadow profile to justify changing the current release-gate posture.',
        best,
    )


def build_historical_replay_shadow_pack(
    settings: Settings,
    db: Database | RepositoryBundle,
    alpaca,
    *,
    lookback_days: int = DEFAULT_REPLAY_LOOKBACK_DAYS,
    offsets: list[int] | None = None,
) -> dict[str, bytes]:
    if alpaca is None or not hasattr(alpaca, 'has_credentials') or not alpaca.has_credentials():
        raise RuntimeError('Alpaca credentials are required for historical replay shadow backfill.')

    requested_offsets = sorted({int(value) for value in (offsets or DEFAULT_REPLAY_OFFSETS) if int(value) > 0}) or list(DEFAULT_REPLAY_OFFSETS)
    start_date, end_date, selected_days, effective_days = _select_replay_window(settings, lookback_days)

    validation_payloads: dict[int, dict[str, Any]] = {}
    replay_rows: list[dict[str, Any]] = []
    daily_rollup_rows: list[dict[str, Any]] = []
    for offset in requested_offsets:
        payload = _build_validation_payload(settings, db, alpaca, start_date=start_date, end_date=end_date, scan_offset_minutes=offset)
        validation_payloads[offset] = payload
        rows = [dict(row) for row in payload.get('rows', [])]
        for row in rows:
            row['scan_offset_minutes'] = int(offset)
        replay_rows.extend(rows)
        daily_rollup_rows.extend(_daily_rollup_rows(rows))

    profile_rows = _profile_row_exports(replay_rows)
    profile_rollup_rows = _profile_rollup_rows(replay_rows, len(selected_days))
    overall_verdict, overall_reason, recommended_profile = _recommendation(profile_rollup_rows)

    summary = {
        'generated_at_utc': datetime.now(UTC).isoformat(),
        'app_version': VERSION,
        'bundle_type': 'historical_replay_shadow_pack',
        'lookback_days_requested': int(lookback_days),
        'lookback_days_effective': int(effective_days),
        'start_date': start_date,
        'end_date': end_date,
        'offsets_requested': requested_offsets,
        'trading_day_count': len(selected_days),
        'trading_days': selected_days,
        'overall_verdict': overall_verdict,
        'overall_reason': overall_reason,
        'recommended_profile': recommended_profile,
        'baseline_advanced_to_stage2_total': sum(_to_int(row.get('baseline_advanced_to_stage2')) for row in daily_rollup_rows),
        'baseline_advanced_and_tradeable_total': sum(_to_int(row.get('baseline_advanced_and_tradeable')) for row in daily_rollup_rows),
        'classification_c_reject_total': sum(_to_int(row.get('classification_c_rejects')) for row in daily_rollup_rows),
    }

    report_lines = [
        '# Historical replay shadow backfill',
        '',
        f"Generated at: {summary['generated_at_utc']}",
        f"App version: {VERSION}",
        f"Replay window: {start_date} -> {end_date}",
        f"Trading days: {summary['trading_day_count']}",
        f"Offsets: {', '.join(str(x) for x in requested_offsets)}",
        f"Overall verdict: {overall_verdict}",
        overall_reason,
        '',
        '## Recommended profile',
        json.dumps(recommended_profile or {}, indent=2),
    ]

    pack = {
        'MANIFEST.json': json.dumps(
            {
                'bundle_type': 'historical_replay_shadow_pack',
                'bundle_contract_version': '1.0',
                'app_version': VERSION,
                'generated_at_utc': summary['generated_at_utc'],
                'lookback_days_requested': int(lookback_days),
                'lookback_days_effective': int(effective_days),
                'offsets_requested': requested_offsets,
            },
            indent=2,
        ).encode('utf-8'),
        'historical_replay_shadow_summary.json': json.dumps(summary, indent=2).encode('utf-8'),
        'historical_replay_shadow_daily_rollup.csv': _csv_bytes(daily_rollup_rows),
        'historical_replay_shadow_profile_rollup.csv': _csv_bytes(profile_rollup_rows),
        'historical_replay_shadow_profile_rows.csv': _csv_bytes(profile_rows),
        'report.md': '\n'.join(report_lines).encode('utf-8'),
    }
    return pack


def write_historical_replay_shadow_cache(settings: Settings, pack: dict[str, bytes]) -> dict[str, Any]:
    summary = json.loads(pack['historical_replay_shadow_summary.json'].decode('utf-8'))
    _cache_zip_path(settings).write_bytes(pack_to_zip_bytes(pack))
    _cache_summary_path(settings).write_text(json.dumps(summary, indent=2), encoding='utf-8')
    return summary


def refresh_historical_replay_shadow_cache(
    settings: Settings,
    db: Database | RepositoryBundle,
    alpaca,
    *,
    lookback_days: int = DEFAULT_REPLAY_LOOKBACK_DAYS,
    offsets: list[int] | None = None,
) -> dict[str, Any]:
    pack = build_historical_replay_shadow_pack(settings, db, alpaca, lookback_days=lookback_days, offsets=offsets)
    return write_historical_replay_shadow_cache(settings, pack)


def get_or_build_historical_replay_shadow_zip(
    settings: Settings,
    db: Database | RepositoryBundle,
    alpaca,
    *,
    lookback_days: int = DEFAULT_REPLAY_LOOKBACK_DAYS,
    offsets: list[int] | None = None,
    prefer_cache: bool = True,
) -> bytes:
    cached = read_cached_historical_replay_zip(settings) if prefer_cache else None
    if cached:
        return cached
    summary = refresh_historical_replay_shadow_cache(settings, db, alpaca, lookback_days=lookback_days, offsets=offsets)
    return read_cached_historical_replay_zip(settings) or pack_to_zip_bytes({
        'historical_replay_shadow_summary.json': json.dumps(summary, indent=2).encode('utf-8')
    })
