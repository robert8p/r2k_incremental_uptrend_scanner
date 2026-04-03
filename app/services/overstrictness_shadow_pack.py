from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from app.config import Settings
from app.db import Database
from app.repositories import RepositoryBundle, ensure_repository_bundle
from app.services.classifier_audit_pack import (
    DEFAULT_REJECT_SAMPLE_PER_OFFSET,
    _intraday_bar_rows,
    _iso_z,
    _normalise_timestamp_column,
    _sample_rejected_classification_c,
    _session_bounds_for_regular_day,
)
from app.services.diagnostics import build_contract_health
from app.services.evidence_pack import _json_bytes, _rows_to_csv
from app.services.outcome_adjudication_pack import (
    _candidate_checkpoint_row,
    _progression_rows,
    _rollup_rows,
)
from app.services.stage2_regression_pack import (
    LATEST_DEFAULT_OFFSETS,
    _candidate_maps,
    _gate_predicate_rows,
    _select_recent_scans,
)
from app.version import VERSION

UTC = timezone.utc

DEFAULT_SHADOW_PROFILES: dict[str, dict[str, float]] = {
    'baseline_excluding_classifier_veto': {},
    'soft_width_retention': {'stable_range_width_retention': 0.50},
    'soft_cycle_persistence': {'stable_range_cycle_persistence': 0.30},
    'soft_bounce_quality': {'stable_range_bounce_quality': 30.0},
    'soft_cycle_durability': {'stable_range_cycle_durability': 25.0},
    'combined_soft_structure': {
        'stable_range_width_retention': 0.50,
        'stable_range_cycle_persistence': 0.30,
        'stable_range_bounce_quality': 30.0,
        'stable_range_cycle_durability': 25.0,
    },
}


def _is_clean_alignment_scan(scan: dict[str, Any]) -> bool:
    summary = dict(scan.get('summary') or {})
    shortlist_alignment = dict(summary.get('shortlist_alignment') or {})
    return bool(shortlist_alignment.get('enabled')) and shortlist_alignment.get('alignment_prefilter_kept_count') is not None


def _single_candidate_gate_rows(settings: Settings, trading_day: str, offset: int, candidate: dict[str, Any]) -> list[dict[str, Any]]:
    symbol = str(candidate.get('symbol') or '')
    rows = _gate_predicate_rows(settings, trading_day, symbol, offset, candidate, offset, candidate)
    output: list[dict[str, Any]] = []
    for row in rows:
        output.append(
            {
                'trading_day': trading_day,
                'scan_offset_minutes': int(offset),
                'symbol': symbol,
                'predicate_name': row['predicate_name'],
                'comparator': row['comparator'],
                'threshold': row['threshold'],
                'value': row['value_at_early_offset'],
                'passed': row['passed_at_early_offset'],
                'note': row['note'],
            }
        )
    return output


def _evaluate_predicate(*, comparator: str, value: Any, threshold: Any) -> bool:
    if comparator == '==':
        return value == threshold
    if comparator == '!=':
        return value != threshold
    try:
        value_num = float(value)
        threshold_num = float(threshold)
    except (TypeError, ValueError):
        return False
    if comparator == '>':
        return value_num > threshold_num
    if comparator == '>=':
        return value_num >= threshold_num
    if comparator == '<':
        return value_num < threshold_num
    if comparator == '<=':
        return value_num <= threshold_num
    return False


def _shadow_profile_rows(
    adjudicated_rows: list[dict[str, Any]],
    predicate_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    predicate_map: dict[tuple[str, int, str], list[dict[str, Any]]] = defaultdict(list)
    for row in predicate_rows:
        key = (str(row.get('trading_day') or ''), int(row.get('scan_offset_minutes') or 0), str(row.get('symbol') or ''))
        predicate_map[key].append(row)

    profile_rows: list[dict[str, Any]] = []
    rollup: list[dict[str, Any]] = []
    verdict_pool = {'possible_classifier_overstrict', 'classifier_correct_reject'}

    for adjudicated in adjudicated_rows:
        verdict = str(adjudicated.get('verdict_bucket') or '')
        classification_code = str(adjudicated.get('range_classification_code') or '')
        if classification_code != 'C' or verdict not in verdict_pool:
            continue
        key = (
            str(adjudicated.get('trading_day') or ''),
            int(adjudicated.get('scan_offset_minutes') or 0),
            str(adjudicated.get('symbol') or ''),
        )
        gate_rows = predicate_map.get(key, [])
        for profile_name, overrides in DEFAULT_SHADOW_PROFILES.items():
            failed_predicates: list[str] = []
            for gate in gate_rows:
                predicate_name = str(gate.get('predicate_name') or '')
                if predicate_name == 'range_classification_not_unstable':
                    continue
                threshold = overrides.get(predicate_name, gate.get('threshold'))
                passed = _evaluate_predicate(
                    comparator=str(gate.get('comparator') or ''),
                    value=gate.get('value'),
                    threshold=threshold,
                )
                if not passed:
                    failed_predicates.append(predicate_name)
            profile_rows.append(
                {
                    'trading_day': adjudicated.get('trading_day'),
                    'scan_offset_minutes': adjudicated.get('scan_offset_minutes'),
                    'symbol': adjudicated.get('symbol'),
                    'verdict_bucket': verdict,
                    'profile_name': profile_name,
                    'would_pass_shadow_profile_excluding_classifier_veto': not failed_predicates,
                    'remaining_failed_predicates': '; '.join(sorted(failed_predicates)),
                }
            )

    by_profile: dict[str, Counter] = defaultdict(Counter)
    possible_total = 0
    correct_total = 0
    for adjudicated in adjudicated_rows:
        verdict = str(adjudicated.get('verdict_bucket') or '')
        classification_code = str(adjudicated.get('range_classification_code') or '')
        if classification_code != 'C' or verdict not in verdict_pool:
            continue
        if verdict == 'possible_classifier_overstrict':
            possible_total += 1
        elif verdict == 'classifier_correct_reject':
            correct_total += 1

    for row in profile_rows:
        if not bool(row.get('would_pass_shadow_profile_excluding_classifier_veto')):
            continue
        profile = str(row.get('profile_name') or '')
        verdict = str(row.get('verdict_bucket') or '')
        by_profile[profile]['flagged_total'] += 1
        if verdict == 'possible_classifier_overstrict':
            by_profile[profile]['flagged_possible_classifier_overstrict'] += 1
        elif verdict == 'classifier_correct_reject':
            by_profile[profile]['flagged_classifier_correct_reject'] += 1

    for profile_name in DEFAULT_SHADOW_PROFILES:
        counter = by_profile.get(profile_name, Counter())
        flagged_total = int(counter.get('flagged_total', 0))
        flagged_overstrict = int(counter.get('flagged_possible_classifier_overstrict', 0))
        flagged_correct = int(counter.get('flagged_classifier_correct_reject', 0))
        rollup.append(
            {
                'profile_name': profile_name,
                'flagged_total': flagged_total,
                'flagged_possible_classifier_overstrict': flagged_overstrict,
                'flagged_classifier_correct_reject': flagged_correct,
                'precision_like_overstrict_share': round(flagged_overstrict / max(flagged_total, 1), 4) if flagged_total else None,
                'capture_rate_of_possible_overstrict': round(flagged_overstrict / max(possible_total, 1), 4) if possible_total else None,
                'false_positive_rate_on_correct_rejects': round(flagged_correct / max(correct_total, 1), 4) if correct_total else None,
            }
        )

    return profile_rows, rollup


def _predicate_fail_rollup(adjudicated_rows: list[dict[str, Any]], predicate_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    verdict_map: dict[tuple[str, int, str], str] = {}
    for row in adjudicated_rows:
        key = (str(row.get('trading_day') or ''), int(row.get('scan_offset_minutes') or 0), str(row.get('symbol') or ''))
        verdict_map[key] = str(row.get('verdict_bucket') or '')

    counters: dict[tuple[str, str], int] = defaultdict(int)
    for gate in predicate_rows:
        key = (str(gate.get('trading_day') or ''), int(gate.get('scan_offset_minutes') or 0), str(gate.get('symbol') or ''))
        verdict = verdict_map.get(key)
        if verdict not in {'possible_classifier_overstrict', 'classifier_correct_reject'}:
            continue
        if not bool(gate.get('passed')) and str(gate.get('predicate_name') or '') != 'range_classification_not_unstable':
            counters[(verdict, str(gate.get('predicate_name') or 'unknown'))] += 1

    rows: list[dict[str, Any]] = []
    for (verdict, predicate_name), count in sorted(counters.items()):
        rows.append(
            {
                'verdict_bucket': verdict,
                'predicate_name': predicate_name,
                'count': count,
            }
        )
    return rows


def _daily_rollup(adjudicated_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counters: dict[tuple[str, int, str], int] = defaultdict(int)
    for row in adjudicated_rows:
        key = (
            str(row.get('trading_day') or ''),
            int(row.get('scan_offset_minutes') or 0),
            str(row.get('verdict_bucket') or 'unknown'),
        )
        counters[key] += 1
    return [
        {
            'trading_day': trading_day,
            'scan_offset_minutes': offset,
            'verdict_bucket': verdict,
            'count': count,
        }
        for (trading_day, offset, verdict), count in sorted(counters.items())
    ]


def build_overstrictness_shadow_pack(
    settings: Settings,
    db: Database | RepositoryBundle,
    alpaca,
    *,
    days: int = 10,
    offsets: list[int] | None = None,
    rejected_sample_per_offset: int = DEFAULT_REJECT_SAMPLE_PER_OFFSET,
) -> dict[str, bytes]:
    repos = ensure_repository_bundle(db)
    requested_offsets = sorted({int(value) for value in (offsets or LATEST_DEFAULT_OFFSETS) if int(value) > 0}) or list(LATEST_DEFAULT_OFFSETS)
    selected_days, chosen_scans = _select_recent_scans(repos, days=days, offsets=requested_offsets)
    clean_days = [
        day for day in selected_days
        if all((day, offset) in chosen_scans and _is_clean_alignment_scan(chosen_scans[(day, offset)]) for offset in requested_offsets)
    ]

    adjudicated_rows: list[dict[str, Any]] = []
    progression_rows: list[dict[str, Any]] = []
    intraday_rows: list[dict[str, Any]] = []
    predicate_rows: list[dict[str, Any]] = []
    clean_day_metadata: list[dict[str, Any]] = []

    for trading_day in clean_days:
        offsets_for_day = sorted(offset for offset in requested_offsets if (trading_day, offset) in chosen_scans)
        if len(offsets_for_day) < 2:
            continue
        early_offset = offsets_for_day[0]
        late_offset = offsets_for_day[-1]
        early_scan = chosen_scans[(trading_day, early_offset)]
        late_scan = chosen_scans[(trading_day, late_offset)]
        early_candidates = _candidate_maps(repos, int(early_scan.get('id') or 0))
        late_candidates = _candidate_maps(repos, int(late_scan.get('id') or 0))

        selected_by_offset: dict[int, list[tuple[str, str, dict[str, Any]]]] = {early_offset: [], late_offset: []}
        audited_symbols: set[str] = set()

        advanced_symbols = sorted(
            symbol
            for symbol, candidate in early_candidates.items()
            if bool(candidate.get('advanced_to_stage2')) and symbol in late_candidates
        )
        for symbol in advanced_symbols:
            selected_by_offset[early_offset].append(('advanced_at_early_checkpoint', symbol, early_candidates[symbol]))
            selected_by_offset[late_offset].append(('advanced_then_late_snapshot', symbol, late_candidates[symbol]))
            audited_symbols.add(symbol)

        classification_c_samples_by_offset: dict[int, list[str]] = {}
        for offset, candidates in ((early_offset, early_candidates), (late_offset, late_candidates)):
            sample = _sample_rejected_classification_c(candidates, limit=rejected_sample_per_offset, exclude_symbols=audited_symbols)
            classification_c_samples_by_offset[offset] = [str(candidate.get('symbol') or '') for candidate in sample]
            for candidate in sample:
                symbol = str(candidate.get('symbol') or '')
                if not symbol:
                    continue
                selected_by_offset[offset].append((f'rejected_classification_c_sample_{offset}m', symbol, candidate))
                audited_symbols.add(symbol)

        if not audited_symbols:
            clean_day_metadata.append(
                {
                    'trading_day': trading_day,
                    'early_offset_minutes': early_offset,
                    'late_offset_minutes': late_offset,
                    'audited_symbol_count': 0,
                    'classification_c_sample_symbols_by_offset': classification_c_samples_by_offset,
                }
            )
            continue

        bars_map: dict[str, pd.DataFrame] = {}
        if getattr(alpaca, 'has_credentials', lambda: False)():
            market_open, market_close, _ = _session_bounds_for_regular_day(trading_day, late_offset)
            bars_map = {
                symbol: _normalise_timestamp_column(frame)
                for symbol, frame in (alpaca.fetch_bars(sorted(audited_symbols), '1Min', _iso_z(market_open), _iso_z(market_close)) or {}).items()
            }
            intraday_rows.extend(_intraday_bar_rows(bars_map, trading_day=trading_day, offset_minutes=[early_offset, late_offset]))

        day_rows: list[dict[str, Any]] = []
        for offset, triples in selected_by_offset.items():
            for audit_reason, symbol, candidate in triples:
                bars = bars_map.get(symbol)
                if bars is None or bars.empty:
                    row = {
                        'trading_day': trading_day,
                        'scan_offset_minutes': int(offset),
                        'audit_reason': audit_reason,
                        'symbol': symbol,
                        'company_name': candidate.get('company_name'),
                        'advanced_to_stage2': bool(candidate.get('advanced_to_stage2')),
                        'recommendation_tier': candidate.get('recommendation_tier'),
                        'recommendation_book': candidate.get('recommendation_book'),
                        'execution_lane': candidate.get('execution_lane'),
                        'touch_window_band': candidate.get('touch_window_band'),
                        'exclusion_reason': candidate.get('exclusion_reason'),
                        'range_classification': (candidate.get('metrics') or {}).get('range_classification'),
                        'range_classification_code': (candidate.get('metrics') or {}).get('range_classification_code'),
                        'total_score': candidate.get('total_score'),
                        'distance_to_entry_pct': (candidate.get('metrics') or {}).get('distance_to_entry_pct'),
                        'range_current_location': (candidate.get('metrics') or {}).get('range_current_location'),
                        'effective_headroom_pct': (candidate.get('metrics') or {}).get('effective_headroom_pct'),
                        'within_range_target_possible': (candidate.get('metrics') or {}).get('within_range_target_possible'),
                        'evaluation_status': 'error',
                        'error_message': 'Missing intraday bars for overstrictness shadow audit.',
                        'entry_touched': False,
                        'entry_timestamp': None,
                        'entry_price': None,
                        'minutes_to_entry': None,
                        'entry_fill_method': None,
                        'hit_target': False,
                        'intrabar_target_reached': False,
                        'minutes_to_target': None,
                        'target_timestamp': None,
                        'target_fill_method': None,
                        'mfe_pct': None,
                        'mae_pct': None,
                        'end_of_window_return_pct': None,
                        'net_end_of_window_return_pct': None,
                        'round_trip_cost_bps': None,
                        'end_of_window_close': None,
                        'target_pct': float(settings.target_pct),
                        'verdict_bucket': 'evaluation_error',
                        'verdict_reason': 'Could not fetch intraday bars for overstrictness shadow audit.',
                    }
                else:
                    row = _candidate_checkpoint_row(
                        settings=settings,
                        trading_day=trading_day,
                        offset_minutes=int(offset),
                        audit_reason=audit_reason,
                        candidate=candidate,
                        bars=bars,
                    )
                day_rows.append(row)
                predicate_rows.extend(_single_candidate_gate_rows(settings, trading_day, int(offset), candidate))

        adjudicated_rows.extend(day_rows)
        progression_rows.extend(_progression_rows(day_rows, early_offset=early_offset, late_offset=late_offset))
        clean_day_metadata.append(
            {
                'trading_day': trading_day,
                'early_offset_minutes': early_offset,
                'late_offset_minutes': late_offset,
                'audited_symbol_count': len(audited_symbols),
                'advanced_symbol_count': len(advanced_symbols),
                'classification_c_sample_symbols_by_offset': classification_c_samples_by_offset,
            }
        )

    verdict_rollup = _rollup_rows(adjudicated_rows)
    daily_rollup = _daily_rollup(adjudicated_rows)
    predicate_fail_rollup = _predicate_fail_rollup(adjudicated_rows, predicate_rows)
    shadow_profile_rows, shadow_profile_rollup = _shadow_profile_rows(adjudicated_rows, predicate_rows)

    summary = {
        'generated_at_utc': datetime.now(UTC).isoformat(),
        'app_version': VERSION,
        'selected_days': selected_days,
        'clean_days_audited': clean_days,
        'clean_day_count': len(clean_days),
        'requested_offsets': requested_offsets,
        'audited_row_count': len(adjudicated_rows),
        'progression_row_count': len(progression_rows),
        'verdict_counts': dict(Counter(str(row.get('verdict_bucket') or 'unknown') for row in adjudicated_rows)),
        'pair_verdict_counts': dict(Counter(str(row.get('pair_verdict_bucket') or 'unknown') for row in progression_rows)),
        'predicate_fail_counts_for_overstrict': {
            row['predicate_name']: row['count']
            for row in predicate_fail_rollup
            if row['verdict_bucket'] == 'possible_classifier_overstrict'
        },
        'shadow_profile_summary': shadow_profile_rollup,
        'decision_rule': 'Use this pack to accumulate clean-session evidence before any live threshold change. Threshold changes should stay shadow-only until overstrict verdicts recur across multiple clean days.',
        'freeze_recommendation': [
            'Do not change live thresholds yet; use shadow profiles only.',
            'Do not add new presentation work while classifier strictness is still being audited.',
            'Do not redesign stage 1 while shortlist alignment is retaining a large liquid pool.',
        ],
        'clean_day_metadata': clean_day_metadata,
    }

    report_lines = [
        '# Overstrictness tracker and shadow threshold audit',
        '',
        f"Generated at: {summary['generated_at_utc']}",
        f'App version: {VERSION}',
        f"Clean days audited: {', '.join(clean_days) if clean_days else 'None'}",
        f"Requested offsets: {', '.join(str(value) for value in requested_offsets)}",
        '',
        '## Why this pack exists',
        '- Accumulate automated adjudication evidence across future clean sessions without changing live behavior.',
        '- Test whether overstrict classification-C verdicts recur, and which predicate families appear most often on tradeable rejects.',
        '- Run shadow threshold profiles without altering production thresholds.',
        '',
        '## Verdict counts',
    ]
    for verdict, count in sorted(summary['verdict_counts'].items()):
        report_lines.append(f'- {verdict}: {count}')
    report_lines.extend(['', '## Shadow profile rollup'])
    for row in shadow_profile_rollup:
        report_lines.append(
            f"- {row['profile_name']}: flagged_total={row['flagged_total']}, "
            f"flagged_possible_classifier_overstrict={row['flagged_possible_classifier_overstrict']}, "
            f"flagged_classifier_correct_reject={row['flagged_classifier_correct_reject']}, "
            f"precision_like={row['precision_like_overstrict_share']}, capture_rate={row['capture_rate_of_possible_overstrict']}"
        )
    report_lines.extend(['', '## Freeze guidance'])
    report_lines.extend(f'- {line}' for line in summary['freeze_recommendation'])

    manifest = {
        'bundle_type': 'overstrictness_shadow_pack',
        'bundle_contract_version': '1.0',
        'app_version': VERSION,
        'generated_at_utc': summary['generated_at_utc'],
        'days_requested': int(days),
        'offsets_requested': requested_offsets,
        'clean_days_audited': clean_days,
        'settings_snapshot': settings.public_snapshot(),
    }

    return {
        'MANIFEST.json': _json_bytes(manifest),
        'settings_snapshot.json': _json_bytes(settings.public_snapshot()),
        'contract_health.json': _json_bytes(build_contract_health(repos.db)),
        'overstrictness_shadow_summary.json': _json_bytes(summary),
        'overstrictness_shadow_rows.csv': _rows_to_csv(adjudicated_rows).encode('utf-8'),
        'overstrictness_shadow_progression.csv': _rows_to_csv(progression_rows).encode('utf-8'),
        'overstrictness_shadow_verdict_rollup.csv': _rows_to_csv(verdict_rollup).encode('utf-8'),
        'overstrictness_shadow_daily_rollup.csv': _rows_to_csv(daily_rollup).encode('utf-8'),
        'overstrictness_predicate_fail_rollup.csv': _rows_to_csv(predicate_fail_rollup).encode('utf-8'),
        'shadow_threshold_profile_rows.csv': _rows_to_csv(shadow_profile_rows).encode('utf-8'),
        'shadow_threshold_profile_rollup.csv': _rows_to_csv(shadow_profile_rollup).encode('utf-8'),
        'overstrictness_intraday_bars.csv': _rows_to_csv(intraday_rows).encode('utf-8'),
        'report.md': '\n'.join(report_lines).encode('utf-8'),
    }
