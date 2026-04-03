from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Tuple

from app.config import Settings
from app.db import Database
from app.repositories import RepositoryBundle, ensure_repository_bundle
from app.services.diagnostics import build_contract_health
from app.services.evidence_pack import _json_bytes, _rows_to_csv
from app.version import VERSION
from app.view_models import build_candidate_list, build_scan_view


LATEST_DEFAULT_OFFSETS = [120, 150]
ACTIONABLE_TIERS = {'headline_shortlist', 'ready_now', 'near_ready'}


def _select_recent_scans(
    repos: RepositoryBundle,
    *,
    days: int,
    offsets: Iterable[int],
) -> tuple[list[str], dict[tuple[str, int], dict[str, Any]]]:
    requested_offsets = sorted({int(value) for value in offsets if int(value) > 0}) or list(LATEST_DEFAULT_OFFSETS)
    scans = repos.scan.list_recent(limit=500)
    selected_days: list[str] = []
    for scan in scans:
        day = str(scan.get('trading_day') or '')
        if day and day not in selected_days:
            selected_days.append(day)
        if len(selected_days) >= int(days):
            break
    chosen_scans: dict[tuple[str, int], dict[str, Any]] = {}
    for day in selected_days:
        for offset in requested_offsets:
            match = next(
                (
                    scan
                    for scan in scans
                    if str(scan.get('trading_day') or '') == day and int(scan.get('scan_offset_minutes') or 0) == int(offset)
                ),
                None,
            )
            if match:
                chosen_scans[(day, int(offset))] = match
    return selected_days, chosen_scans


def _candidate_maps(repos: RepositoryBundle, scan_id: int) -> dict[str, dict[str, Any]]:
    return {str(candidate.get('symbol') or ''): candidate for candidate in build_candidate_list(repos.scan.get_candidates(scan_id))}


def _surface_breakdown_row(scan: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    tier_counts = Counter(str(candidate.get('recommendation_tier') or 'unknown') for candidate in candidates)
    book_counts = Counter(str(candidate.get('recommendation_book') or 'unknown') for candidate in candidates)
    lane_counts = Counter(str(candidate.get('execution_lane') or 'unknown') for candidate in candidates)
    return {
        'trading_day': str(scan.get('trading_day') or ''),
        'scan_id': int(scan.get('id') or 0),
        'scan_offset_minutes': int(scan.get('scan_offset_minutes') or 0),
        'stage1_count': int(scan.get('stage1_count') or 0),
        'stage2_count': int(scan.get('stage2_count') or 0),
        'actionable_tier_count': sum(1 for candidate in candidates if str(candidate.get('recommendation_tier') or '') in ACTIONABLE_TIERS),
        'watchlist_tier_count': tier_counts.get('watchlist', 0),
        'rejected_tier_count': tier_counts.get('rejected', 0),
        'headline_shortlist_count': tier_counts.get('headline_shortlist', 0),
        'ready_now_count': tier_counts.get('ready_now', 0),
        'near_ready_count': tier_counts.get('near_ready', 0),
        'touch_soon_queue_count': book_counts.get('touch_soon_queue', 0),
        'touch_later_queue_count': book_counts.get('touch_later_queue', 0),
        'structural_watchlist_count': book_counts.get('structural_watchlist', 0),
        'rejected_book_count': book_counts.get('rejected', 0),
        'monitor_5m_lane_count': lane_counts.get('monitor_5m', 0),
        'passive_watchlist_lane_count': lane_counts.get('passive_watchlist', 0),
    }


def _candidate_comparison_row(
    trading_day: str,
    symbol: str,
    early_offset: int,
    early: dict[str, Any],
    late_offset: int,
    late: dict[str, Any],
) -> dict[str, Any]:
    early_metrics = dict(early.get('metrics') or {})
    late_metrics = dict(late.get('metrics') or {})
    return {
        'trading_day': trading_day,
        'symbol': symbol,
        'advanced_at_early_offset': bool(early.get('advanced_to_stage2')),
        'advanced_at_late_offset': bool(late.get('advanced_to_stage2')),
        'early_offset_minutes': early_offset,
        'late_offset_minutes': late_offset,
        'tier_at_early_offset': early.get('recommendation_tier'),
        'tier_at_late_offset': late.get('recommendation_tier'),
        'book_at_early_offset': early.get('recommendation_book'),
        'book_at_late_offset': late.get('recommendation_book'),
        'lane_at_early_offset': early.get('execution_lane'),
        'lane_at_late_offset': late.get('execution_lane'),
        'touch_window_at_early_offset': early.get('touch_window_band'),
        'touch_window_at_late_offset': late.get('touch_window_band'),
        'range_classification_at_early_offset': early_metrics.get('range_classification'),
        'range_classification_at_late_offset': late_metrics.get('range_classification'),
        'score_cap_reason_at_late_offset': late_metrics.get('score_cap_reason'),
        'exclusion_reason_at_late_offset': late.get('exclusion_reason'),
        'total_score_at_early_offset': early.get('total_score'),
        'total_score_at_late_offset': late.get('total_score'),
        'structural_score_at_early_offset': early_metrics.get('structural_score'),
        'structural_score_at_late_offset': late_metrics.get('structural_score'),
        'expected_actionability_score_at_early_offset': early_metrics.get('expected_actionability_score'),
        'expected_actionability_score_at_late_offset': late_metrics.get('expected_actionability_score'),
        'actionability_score_at_early_offset': early_metrics.get('actionability_score'),
        'actionability_score_at_late_offset': late_metrics.get('actionability_score'),
        'execution_readiness_score_at_early_offset': early_metrics.get('execution_readiness_score'),
        'execution_readiness_score_at_late_offset': late_metrics.get('execution_readiness_score'),
        'follow_through_confidence_score_at_early_offset': early_metrics.get('follow_through_confidence_score'),
        'follow_through_confidence_score_at_late_offset': late_metrics.get('follow_through_confidence_score'),
        'distance_to_entry_pct_at_early_offset': early_metrics.get('distance_to_entry_pct'),
        'distance_to_entry_pct_at_late_offset': late_metrics.get('distance_to_entry_pct'),
        'regressed_after_early_advance': bool(early.get('advanced_to_stage2')) and not bool(late.get('advanced_to_stage2')),
    }


def _metric_delta_rows(
    trading_day: str,
    symbol: str,
    early_offset: int,
    early: dict[str, Any],
    late_offset: int,
    late: dict[str, Any],
) -> list[dict[str, Any]]:
    early_metrics = dict(early.get('metrics') or {})
    late_metrics = dict(late.get('metrics') or {})
    metric_specs = [
        ('total_score', early.get('total_score'), late.get('total_score')),
        ('structural_score', early_metrics.get('structural_score'), late_metrics.get('structural_score')),
        ('expected_actionability_score', early_metrics.get('expected_actionability_score'), late_metrics.get('expected_actionability_score')),
        ('actionability_score', early_metrics.get('actionability_score'), late_metrics.get('actionability_score')),
        ('execution_readiness_score', early_metrics.get('execution_readiness_score'), late_metrics.get('execution_readiness_score')),
        ('follow_through_confidence_score', early_metrics.get('follow_through_confidence_score'), late_metrics.get('follow_through_confidence_score')),
        ('distance_to_entry_pct', early_metrics.get('distance_to_entry_pct'), late_metrics.get('distance_to_entry_pct')),
        ('range_current_location', early_metrics.get('range_current_location'), late_metrics.get('range_current_location')),
        ('range_containment_ratio', early_metrics.get('range_containment_ratio'), late_metrics.get('range_containment_ratio')),
        ('range_band_width_pct', early_metrics.get('range_band_width_pct'), late_metrics.get('range_band_width_pct')),
        ('completed_cycles_observed', early_metrics.get('completed_cycles_observed'), late_metrics.get('completed_cycles_observed')),
        ('recent_completed_cycles_observed', early_metrics.get('recent_completed_cycles_observed'), late_metrics.get('recent_completed_cycles_observed')),
        ('recent_lower_zone_touch_count', early_metrics.get('recent_lower_zone_touch_count'), late_metrics.get('recent_lower_zone_touch_count')),
        ('recent_upper_zone_touch_count', early_metrics.get('recent_upper_zone_touch_count'), late_metrics.get('recent_upper_zone_touch_count')),
        ('width_retention_ratio', early_metrics.get('width_retention_ratio'), late_metrics.get('width_retention_ratio')),
        ('cycle_persistence_ratio', early_metrics.get('cycle_persistence_ratio'), late_metrics.get('cycle_persistence_ratio')),
        ('cycle_durability_score', early_metrics.get('cycle_durability_score'), late_metrics.get('cycle_durability_score')),
        ('degradation_score', early_metrics.get('degradation_score'), late_metrics.get('degradation_score')),
        ('recent_breakout_close_ratio', early_metrics.get('recent_breakout_close_ratio'), late_metrics.get('recent_breakout_close_ratio')),
        ('recent_directional_efficiency', early_metrics.get('recent_directional_efficiency'), late_metrics.get('recent_directional_efficiency')),
        ('recent_wickiness_ratio', early_metrics.get('recent_wickiness_ratio'), late_metrics.get('recent_wickiness_ratio')),
        ('bounce_quality_score', early_metrics.get('bounce_quality_score'), late_metrics.get('bounce_quality_score')),
        ('recent_bounce_event_count', early_metrics.get('recent_bounce_event_count'), late_metrics.get('recent_bounce_event_count')),
        ('recent_bounce_observation_confidence', early_metrics.get('recent_bounce_observation_confidence'), late_metrics.get('recent_bounce_observation_confidence')),
        ('recent_shrunk_upper_reach_ratio', early_metrics.get('recent_shrunk_upper_reach_ratio'), late_metrics.get('recent_shrunk_upper_reach_ratio')),
        ('recent_shrunk_target_hit_ratio', early_metrics.get('recent_shrunk_target_hit_ratio'), late_metrics.get('recent_shrunk_target_hit_ratio')),
        ('effective_headroom_pct', early_metrics.get('effective_headroom_pct'), late_metrics.get('effective_headroom_pct')),
    ]
    rows: list[dict[str, Any]] = []
    for metric_name, early_value, late_value in metric_specs:
        delta_value = None
        if isinstance(early_value, (int, float)) and isinstance(late_value, (int, float)):
            delta_value = round(float(late_value) - float(early_value), 3)
        rows.append({
            'trading_day': trading_day,
            'symbol': symbol,
            'early_offset_minutes': early_offset,
            'late_offset_minutes': late_offset,
            'metric_name': metric_name,
            'value_at_early_offset': early_value,
            'value_at_late_offset': late_value,
            'delta_late_minus_early': delta_value,
        })
    return rows


PredicateSpec = tuple[str, str, Any, Callable[[dict[str, Any]], bool], str]


def _gate_predicate_rows(
    settings: Settings,
    trading_day: str,
    symbol: str,
    early_offset: int,
    early: dict[str, Any],
    late_offset: int,
    late: dict[str, Any],
) -> list[dict[str, Any]]:
    early_metrics = dict(early.get('metrics') or {})
    late_metrics = dict(late.get('metrics') or {})

    predicates: list[PredicateSpec] = [
        (
            'range_classification_not_unstable',
            '!=',
            'C',
            lambda metrics: str(metrics.get('range_classification_code') or '') != 'C',
            'Stage-2 thesis gate rejects unstable non-range classification.',
        ),
        (
            'within_range_target_possible',
            '==',
            True,
            lambda metrics: bool(metrics.get('within_range_target_possible')),
            'The current band must still allow a realistic +1% in-range target from preferred entry.',
        ),
        (
            'trade_window_minutes_remaining_positive',
            '>',
            0,
            lambda metrics: int(metrics.get('trade_window_minutes_remaining') or 0) > 0,
            'The valid window must still exist before the hard cutoff.',
        ),
        (
            'breakout_close_ratio_within_limit',
            '<=',
            float(settings.max_breakout_close_ratio),
            lambda metrics: float(metrics.get('breakout_close_ratio') or 0.0) <= float(settings.max_breakout_close_ratio),
            'Too many closes outside the band break repeatability.',
        ),
        (
            'wickiness_ratio_within_limit',
            '<=',
            float(settings.max_wickiness_ratio),
            lambda metrics: float(metrics.get('wickiness_ratio') or 0.0) <= float(settings.max_wickiness_ratio),
            'The range cannot be too wick-driven and chaotic.',
        ),
        (
            'stable_range_containment',
            '>=',
            0.58,
            lambda metrics: float(metrics.get('range_containment_ratio') or 0.0) >= 0.58,
            'Adaptive-range classifier requires enough containment to avoid one-way continuation.',
        ),
        (
            'stable_range_breakout_recent',
            '<=',
            0.18,
            lambda metrics: float(metrics.get('recent_breakout_close_ratio') or 0.0) <= 0.18,
            'Recent closes should not frequently break the band.',
        ),
        (
            'stable_range_directionality_recent',
            '<=',
            0.72,
            lambda metrics: float(metrics.get('recent_directional_efficiency') or 0.0) <= 0.72,
            'Recent oscillations should not become too one-directional.',
        ),
        (
            'stable_range_recent_lower_touch',
            '>=',
            1,
            lambda metrics: int(metrics.get('recent_lower_zone_touch_count') or 0) >= 1,
            'Recent lower-band interaction is required for a repeatable range thesis.',
        ),
        (
            'stable_range_recent_upper_touch',
            '>=',
            1,
            lambda metrics: int(metrics.get('recent_upper_zone_touch_count') or 0) >= 1,
            'Recent upper-band interaction is required for a repeatable range thesis.',
        ),
        (
            'stable_range_recent_completed_cycles',
            '>=',
            1,
            lambda metrics: int(metrics.get('recent_completed_cycles_observed') or 0) >= 1,
            'Recent completed cycles are required for a current-session range thesis.',
        ),
        (
            'stable_range_recent_bounce_events',
            '>=',
            1,
            lambda metrics: int(metrics.get('recent_bounce_event_count') or 0) >= 1,
            'The recent window needs at least one bounce observation.',
        ),
        (
            'stable_range_width_retention',
            '>=',
            0.55,
            lambda metrics: float(metrics.get('width_retention_ratio') or 0.0) >= 0.55,
            'Recent band width should not collapse too far below earlier width.',
        ),
        (
            'stable_range_cycle_persistence',
            '>=',
            0.40,
            lambda metrics: float(metrics.get('cycle_persistence_ratio') or 0.0) >= 0.40,
            'Cycles should still be persisting, not collapsing into noise.',
        ),
        (
            'stable_range_bounce_quality',
            '>=',
            40.0,
            lambda metrics: float(metrics.get('bounce_quality_score') or 0.0) >= 40.0,
            'Bounce-quality floor required by the adaptive-range classifier.',
        ),
        (
            'stable_range_cycle_durability',
            '>=',
            35.0,
            lambda metrics: float(metrics.get('cycle_durability_score') or 0.0) >= 35.0,
            'Recent range durability must remain above degradation floor.',
        ),
    ]

    value_lookup = {
        'range_classification_not_unstable': lambda metrics: metrics.get('range_classification_code'),
        'within_range_target_possible': lambda metrics: metrics.get('within_range_target_possible'),
        'trade_window_minutes_remaining_positive': lambda metrics: metrics.get('trade_window_minutes_remaining'),
        'breakout_close_ratio_within_limit': lambda metrics: metrics.get('breakout_close_ratio'),
        'wickiness_ratio_within_limit': lambda metrics: metrics.get('wickiness_ratio'),
        'stable_range_containment': lambda metrics: metrics.get('range_containment_ratio'),
        'stable_range_breakout_recent': lambda metrics: metrics.get('recent_breakout_close_ratio'),
        'stable_range_directionality_recent': lambda metrics: metrics.get('recent_directional_efficiency'),
        'stable_range_recent_lower_touch': lambda metrics: metrics.get('recent_lower_zone_touch_count'),
        'stable_range_recent_upper_touch': lambda metrics: metrics.get('recent_upper_zone_touch_count'),
        'stable_range_recent_completed_cycles': lambda metrics: metrics.get('recent_completed_cycles_observed'),
        'stable_range_recent_bounce_events': lambda metrics: metrics.get('recent_bounce_event_count'),
        'stable_range_width_retention': lambda metrics: metrics.get('width_retention_ratio'),
        'stable_range_cycle_persistence': lambda metrics: metrics.get('cycle_persistence_ratio'),
        'stable_range_bounce_quality': lambda metrics: metrics.get('bounce_quality_score'),
        'stable_range_cycle_durability': lambda metrics: metrics.get('cycle_durability_score'),
    }

    rows: list[dict[str, Any]] = []
    for name, comparator, threshold, predicate, note in predicates:
        early_pass = bool(predicate(early_metrics))
        late_pass = bool(predicate(late_metrics))
        value_getter = value_lookup[name]
        rows.append({
            'trading_day': trading_day,
            'symbol': symbol,
            'early_offset_minutes': early_offset,
            'late_offset_minutes': late_offset,
            'predicate_name': name,
            'comparator': comparator,
            'threshold': threshold,
            'value_at_early_offset': value_getter(early_metrics),
            'value_at_late_offset': value_getter(late_metrics),
            'passed_at_early_offset': early_pass,
            'passed_at_late_offset': late_pass,
            'changed_to_fail': early_pass and not late_pass,
            'note': note,
        })
    return rows


def build_stage2_regression_pack(
    settings: Settings,
    db: Database | RepositoryBundle,
    *,
    days: int = 5,
    offsets: list[int] | None = None,
) -> dict[str, bytes]:
    repos = ensure_repository_bundle(db)
    selected_days, chosen_scans = _select_recent_scans(repos, days=days, offsets=offsets or LATEST_DEFAULT_OFFSETS)
    latest_day = selected_days[0] if selected_days else ''
    latest_offsets = sorted({offset for day, offset in chosen_scans if day == latest_day})
    if len(latest_offsets) < 2:
        latest_offsets = sorted(offsets or LATEST_DEFAULT_OFFSETS)
    early_offset = latest_offsets[0] if latest_offsets else int((offsets or LATEST_DEFAULT_OFFSETS)[0])
    late_offset = latest_offsets[-1] if latest_offsets else int((offsets or LATEST_DEFAULT_OFFSETS)[-1])

    pack: dict[str, bytes] = {}
    surface_rows: list[dict[str, Any]] = []
    comparison_rows: list[dict[str, Any]] = []
    metric_delta_rows: list[dict[str, Any]] = []
    predicate_rows: list[dict[str, Any]] = []

    for (day, offset), scan in sorted(chosen_scans.items()):
        scan_view = build_scan_view(scan, alpaca_data_feed=settings.alpaca_data_feed)
        candidates = list(_candidate_maps(repos, int(scan.get('id') or 0)).values())
        surface_rows.append(_surface_breakdown_row(scan_view or scan, candidates))

    if latest_day and (latest_day, early_offset) in chosen_scans and (latest_day, late_offset) in chosen_scans:
        early_scan = chosen_scans[(latest_day, early_offset)]
        late_scan = chosen_scans[(latest_day, late_offset)]
        early_candidates = _candidate_maps(repos, int(early_scan.get('id') or 0))
        late_candidates = _candidate_maps(repos, int(late_scan.get('id') or 0))
        tracked_symbols = sorted(
            symbol
            for symbol, candidate in early_candidates.items()
            if candidate.get('advanced_to_stage2') and symbol in late_candidates
        )
        for symbol in tracked_symbols:
            early = early_candidates[symbol]
            late = late_candidates[symbol]
            comparison_rows.append(_candidate_comparison_row(latest_day, symbol, early_offset, early, late_offset, late))
            metric_delta_rows.extend(_metric_delta_rows(latest_day, symbol, early_offset, early, late_offset, late))
            predicate_rows.extend(_gate_predicate_rows(settings, latest_day, symbol, early_offset, early, late_offset, late))

    regressed_rows = [row for row in comparison_rows if row.get('regressed_after_early_advance')]
    changed_to_fail_rows = [row for row in predicate_rows if row.get('changed_to_fail')]
    changed_to_fail_by_symbol: dict[str, list[str]] = {}
    for row in changed_to_fail_rows:
        changed_to_fail_by_symbol.setdefault(str(row.get('symbol') or ''), []).append(str(row.get('predicate_name') or ''))

    summary = {
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'app_version': VERSION,
        'selected_days': selected_days,
        'latest_trading_day': latest_day,
        'early_offset_minutes': early_offset,
        'late_offset_minutes': late_offset,
        'tracked_symbol_count': len(comparison_rows),
        'regressed_symbol_count': len(regressed_rows),
        'regressed_symbols': [row.get('symbol') for row in regressed_rows],
        'changed_to_fail_predicate_counts': dict(Counter(str(row.get('predicate_name') or '') for row in changed_to_fail_rows)),
        'freeze_recommendation': [
            'Do not add new scoring features before resolving the 120→150 regression path.',
            'Do not retune stage-1 selection while clean shortlist alignment is retaining a large pool.',
            'Do not add new queue or tier logic before deciding whether the 150-minute thesis gate is too harsh or correctly protective.',
        ],
    }

    report_lines = [
        '# Stage-2 regression audit',
        '',
        f"Generated at: {summary['generated_at_utc']}",
        f'App version: {VERSION}',
        f"Latest trading day: {latest_day or 'None'}",
        f'Early/late offsets compared: {early_offset} -> {late_offset}',
        '',
        '## Summary',
        f"- Tracked symbols advanced earlier and still present later: {summary['tracked_symbol_count']}",
        f"- Symbols that regressed after early advance: {summary['regressed_symbol_count']}",
    ]
    if regressed_rows:
        report_lines.append(f"- Regressed symbols: {', '.join(str(row.get('symbol') or '') for row in regressed_rows)}")
    if changed_to_fail_rows:
        report_lines.append('- Predicates that flipped from pass to fail:')
        for symbol, predicate_names in changed_to_fail_by_symbol.items():
            report_lines.append(f"  - {symbol}: {', '.join(predicate_names)}")
    report_lines.extend(['', '## Freeze guidance'])
    report_lines.extend([f'- {line}' for line in summary['freeze_recommendation']])

    manifest = {
        'bundle_type': 'stage2_regression_pack',
        'bundle_contract_version': '1.0',
        'app_version': VERSION,
        'generated_at_utc': summary['generated_at_utc'],
        'days_requested': int(days),
        'offsets_requested': sorted({int(value) for value in (offsets or LATEST_DEFAULT_OFFSETS) if int(value) > 0}),
        'selected_days': selected_days,
        'latest_trading_day': latest_day,
        'settings_snapshot': settings.public_snapshot(),
    }

    pack['MANIFEST.json'] = _json_bytes(manifest)
    pack['settings_snapshot.json'] = _json_bytes(settings.public_snapshot())
    pack['contract_health.json'] = _json_bytes(build_contract_health(repos.db))
    pack['regression_summary.json'] = _json_bytes(summary)
    pack['latest_day_surface_breakdown.csv'] = _rows_to_csv([row for row in surface_rows if row.get('trading_day') == latest_day]).encode('utf-8')
    pack['regression_candidates.csv'] = _rows_to_csv(comparison_rows).encode('utf-8')
    pack['regression_metric_deltas.csv'] = _rows_to_csv(metric_delta_rows).encode('utf-8')
    pack['regression_gate_status.csv'] = _rows_to_csv(predicate_rows).encode('utf-8')
    pack['report.md'] = '\n'.join(report_lines).encode('utf-8')
    return pack
