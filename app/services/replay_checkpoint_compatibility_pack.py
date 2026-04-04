from __future__ import annotations

import csv
import io
import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from app.config import Settings
from app.db import Database
from app.repositories import RepositoryBundle
from app.services.historical_replay_shadow_pack import (
    DEFAULT_REPLAY_LOOKBACK_DAYS,
    DEFAULT_REPLAY_OFFSETS,
    get_or_build_historical_replay_shadow_zip,
)
from app.services.replay_bottleneck_pack import (
    TRADEABLE_SHARE_SUPPORT_THRESHOLD,
    _csv_bytes,
    _failure_path,
    _read_json_bytes,
    _read_replay_pack,
    _to_float,
    _to_int,
    build_replay_bottleneck_pack,
)
from app.services.replay_checkpoint_decay_pack import build_replay_checkpoint_decay_pack
from app.version import VERSION

UTC = timezone.utc
MIN_GATE_CANDIDATE_SIZE = 30
SCAN_TIME_METRIC_SPECS: list[tuple[str, str]] = [
    ('mover_rank', 'low'),
    ('distance_to_entry_pct', 'high'),
    ('width_retention_ratio', 'high'),
    ('cycle_persistence_ratio', 'high'),
    ('bounce_quality_score', 'high'),
    ('cycle_durability_score', 'high'),
    ('intraday_pct_gain', 'high'),
]


def _quantile_thresholds(values: list[float]) -> list[float]:
    if not values:
        return []
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered
    output: list[float] = []
    for pct in [0.10, 0.20, 0.25, 0.30, 0.40, 0.50, 0.60, 0.70, 0.75, 0.80, 0.90]:
        idx = int(round((len(ordered) - 1) * pct))
        candidate = round(float(ordered[idx]), 6)
        if candidate not in output:
            output.append(candidate)
    return output



def _enrich_focus_rows(replay_rows: list[dict[str, Any]], profile_name: str) -> list[dict[str, Any]]:
    focus_rows: list[dict[str, Any]] = []
    for row in replay_rows:
        if str(row.get('profile_name') or '') != profile_name:
            continue
        if str(row.get('would_pass_shadow_profile') or '').lower() != 'true':
            continue
        enriched = dict(row)
        enriched['failure_path'] = _failure_path(row)
        focus_rows.append(enriched)
    return focus_rows



def _partition_rows(
    rows: list[dict[str, Any]],
    supported_offset: int,
    weaker_offset: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], dict[int, dict[str, Any]]] = defaultdict(dict)
    supported_all: list[dict[str, Any]] = []
    weaker_all: list[dict[str, Any]] = []
    for row in rows:
        offset = _to_int(row.get('scan_offset_minutes'))
        if offset == supported_offset:
            supported_all.append(row)
        elif offset == weaker_offset:
            weaker_all.append(row)
        grouped[(str(row.get('trading_day') or ''), str(row.get('symbol') or ''))][offset] = row

    supported_only: list[dict[str, Any]] = []
    weaker_only: list[dict[str, Any]] = []
    paired: list[dict[str, Any]] = []
    for key in sorted(grouped):
        by_offset = grouped[key]
        supported = by_offset.get(supported_offset)
        weaker = by_offset.get(weaker_offset)
        if supported and weaker:
            paired.append({'trading_day': key[0], 'symbol': key[1], 'supported': supported, 'weaker': weaker})
        elif supported:
            supported_only.append(supported)
        elif weaker:
            weaker_only.append(weaker)
    return supported_all, weaker_all, supported_only, weaker_only



def _metric_population_comparison(
    supported_only: list[dict[str, Any]],
    weaker_only: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    metrics = [
        'mover_rank',
        'distance_to_entry_pct',
        'width_retention_ratio',
        'cycle_persistence_ratio',
        'bounce_quality_score',
        'cycle_durability_score',
        'intraday_pct_gain',
    ]
    output: list[dict[str, Any]] = []
    for metric in metrics:
        supported_values = sorted(v for v in (_to_float(row.get(metric)) for row in supported_only) if v is not None)
        weaker_values = sorted(v for v in (_to_float(row.get(metric)) for row in weaker_only) if v is not None)

        def _summary(values: list[float]) -> tuple[int, float | None, float | None]:
            if not values:
                return 0, None, None
            return len(values), round(sum(values) / len(values), 4), round(values[len(values) // 2], 4)

        supported_count, supported_mean, supported_p50 = _summary(supported_values)
        weaker_count, weaker_mean, weaker_p50 = _summary(weaker_values)
        output.append(
            {
                'metric_name': metric,
                'supported_only_count': supported_count,
                'supported_only_mean': supported_mean,
                'supported_only_p50': supported_p50,
                'weaker_only_count': weaker_count,
                'weaker_only_mean': weaker_mean,
                'weaker_only_p50': weaker_p50,
                'mean_delta_weaker_minus_supported': round((weaker_mean or 0.0) - (supported_mean or 0.0), 4) if supported_mean is not None and weaker_mean is not None else None,
                'p50_delta_weaker_minus_supported': round((weaker_p50 or 0.0) - (supported_p50 or 0.0), 4) if supported_p50 is not None and weaker_p50 is not None else None,
            }
        )
    return output



def _categorical_population_comparison(
    supported_only: list[dict[str, Any]],
    weaker_only: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for field in ['range_classification_code', 'within_range_target_possible']:
        values = sorted({str(row.get(field) or '') for row in supported_only + weaker_only})
        supported_total = len(supported_only)
        weaker_total = len(weaker_only)
        for value in values:
            supported_rows = [row for row in supported_only if str(row.get(field) or '') == value]
            weaker_rows = [row for row in weaker_only if str(row.get(field) or '') == value]
            supported_tradeable = sum(1 for row in supported_rows if str(row.get('failure_path') or '') == 'tradeable')
            weaker_tradeable = sum(1 for row in weaker_rows if str(row.get('failure_path') or '') == 'tradeable')
            output.append(
                {
                    'field_name': field,
                    'field_value': value,
                    'supported_only_count': len(supported_rows),
                    'supported_only_share_of_population': round(len(supported_rows) / supported_total, 4) if supported_total else None,
                    'supported_only_tradeable_share': round(supported_tradeable / len(supported_rows), 4) if supported_rows else None,
                    'weaker_only_count': len(weaker_rows),
                    'weaker_only_share_of_population': round(len(weaker_rows) / weaker_total, 4) if weaker_total else None,
                    'weaker_only_tradeable_share': round(weaker_tradeable / len(weaker_rows), 4) if weaker_rows else None,
                }
            )
    return output



def _metric_gate_candidates(rows: list[dict[str, Any]], scope_name: str) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for metric_name, direction in SCAN_TIME_METRIC_SPECS:
        values = [v for v in (_to_float(row.get(metric_name)) for row in rows) if v is not None]
        thresholds = _quantile_thresholds(values)
        for threshold in thresholds:
            if direction == 'high':
                subset = [row for row in rows if (_to_float(row.get(metric_name)) is not None and _to_float(row.get(metric_name)) >= threshold)]
                comparator = '>='
            else:
                subset = [row for row in rows if (_to_float(row.get(metric_name)) is not None and _to_float(row.get(metric_name)) <= threshold)]
                comparator = '<='
            flagged_total = len(subset)
            if flagged_total < MIN_GATE_CANDIDATE_SIZE:
                continue
            tradeable_count = sum(1 for row in subset if str(row.get('failure_path') or '') == 'tradeable')
            tradeable_share = round(tradeable_count / flagged_total, 4) if flagged_total else None
            output.append(
                {
                    'scope_name': scope_name,
                    'gate_type': 'numeric_threshold',
                    'metric_name': metric_name,
                    'comparator': comparator,
                    'threshold_value': round(float(threshold), 6),
                    'flagged_total': flagged_total,
                    'tradeable_count': tradeable_count,
                    'tradeable_share': tradeable_share,
                    'tradeable_share_gap_to_support': round((tradeable_share or 0.0) - TRADEABLE_SHARE_SUPPORT_THRESHOLD, 4) if tradeable_share is not None else None,
                    'reaches_support_threshold': bool(tradeable_share is not None and tradeable_share >= TRADEABLE_SHARE_SUPPORT_THRESHOLD),
                }
            )

    for categorical in ['within_range_target_possible', 'range_classification_code']:
        values = sorted({str(row.get(categorical) or '') for row in rows})
        for value in values:
            subset = [row for row in rows if str(row.get(categorical) or '') == value]
            flagged_total = len(subset)
            if flagged_total < MIN_GATE_CANDIDATE_SIZE:
                continue
            tradeable_count = sum(1 for row in subset if str(row.get('failure_path') or '') == 'tradeable')
            tradeable_share = round(tradeable_count / flagged_total, 4) if flagged_total else None
            output.append(
                {
                    'scope_name': scope_name,
                    'gate_type': 'categorical_match',
                    'metric_name': categorical,
                    'comparator': '==',
                    'threshold_value': value,
                    'flagged_total': flagged_total,
                    'tradeable_count': tradeable_count,
                    'tradeable_share': tradeable_share,
                    'tradeable_share_gap_to_support': round((tradeable_share or 0.0) - TRADEABLE_SHARE_SUPPORT_THRESHOLD, 4) if tradeable_share is not None else None,
                    'reaches_support_threshold': bool(tradeable_share is not None and tradeable_share >= TRADEABLE_SHARE_SUPPORT_THRESHOLD),
                }
            )

    output.sort(key=lambda row: (0 if row['reaches_support_threshold'] else 1, -(row['flagged_total'] or 0), -(row['tradeable_share'] or 0.0), str(row['metric_name'] or '')))
    return output



def _best_gate(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not candidates:
        return None
    return dict(candidates[0])



def build_replay_checkpoint_compatibility_pack(
    settings: Settings,
    db: Database | RepositoryBundle,
    alpaca,
    *,
    lookback_days: int = DEFAULT_REPLAY_LOOKBACK_DAYS,
    offsets: list[int] | None = None,
) -> dict[str, bytes]:
    requested_offsets = list(offsets or DEFAULT_REPLAY_OFFSETS)
    raw_replay_zip = get_or_build_historical_replay_shadow_zip(
        settings,
        db,
        alpaca,
        lookback_days=lookback_days,
        offsets=requested_offsets,
    )
    replay_summary, replay_rows = _read_replay_pack(raw_replay_zip)
    bottleneck_pack = build_replay_bottleneck_pack(settings, db, alpaca, lookback_days=lookback_days, offsets=requested_offsets)
    bottleneck_summary = _read_json_bytes(bottleneck_pack.get('replay_bottleneck_summary.json', b''))
    decay_pack = build_replay_checkpoint_decay_pack(settings, db, alpaca, lookback_days=lookback_days, offsets=requested_offsets)
    decay_summary = _read_json_bytes(decay_pack.get('replay_checkpoint_decay_summary.json', b''))

    focus_profile_name = str(
        bottleneck_summary.get('focus_profile_name')
        or (replay_summary.get('recommended_profile') or {}).get('profile_name')
        or ''
    )
    supported_offset = _to_int(decay_summary.get('supported_offset_minutes') or (bottleneck_summary.get('best_offset_by_tradeable_share') or {}).get('scan_offset_minutes'))
    weaker_offset = _to_int(decay_summary.get('weaker_offset_minutes') or (bottleneck_summary.get('worst_offset_by_tradeable_share') or {}).get('scan_offset_minutes'))

    focus_rows = _enrich_focus_rows(replay_rows, focus_profile_name)
    supported_all, weaker_all, supported_only, weaker_only = _partition_rows(focus_rows, supported_offset, weaker_offset)

    population_metric_rows = _metric_population_comparison(supported_only, weaker_only)
    population_categorical_rows = _categorical_population_comparison(supported_only, weaker_only)
    weaker_total_candidates = _metric_gate_candidates(weaker_all, 'weaker_offset_total')
    weaker_only_candidates = _metric_gate_candidates(weaker_only, 'weaker_offset_only')

    best_total_gate = _best_gate(weaker_total_candidates)
    best_weaker_only_gate = _best_gate(weaker_only_candidates)

    summary = {
        'generated_at_utc': datetime.now(UTC).isoformat(),
        'app_version': VERSION,
        'bundle_type': 'replay_checkpoint_compatibility_pack',
        'source_replay_generated_at_utc': replay_summary.get('generated_at_utc'),
        'source_replay_app_version': replay_summary.get('app_version'),
        'source_decay_generated_at_utc': decay_summary.get('generated_at_utc'),
        'source_decay_app_version': decay_summary.get('app_version'),
        'source_bottleneck_generated_at_utc': bottleneck_summary.get('generated_at_utc'),
        'source_bottleneck_app_version': bottleneck_summary.get('app_version'),
        'lookback_days_requested': lookback_days,
        'lookback_days_effective': replay_summary.get('lookback_days_effective'),
        'focus_profile_name': focus_profile_name,
        'supported_offset_minutes': supported_offset,
        'supported_offset_tradeable_share': decay_summary.get('supported_offset_tradeable_share'),
        'weaker_offset_minutes': weaker_offset,
        'weaker_offset_tradeable_share': decay_summary.get('weaker_offset_tradeable_share'),
        'paired_symbol_day_count': decay_summary.get('paired_symbol_day_count'),
        'supported_only_symbol_day_count': len(supported_only),
        'weaker_only_symbol_day_count': len(weaker_only),
        'composition_shift_larger_than_shared_decay': decay_summary.get('composition_shift_larger_than_shared_decay'),
        'scan_time_metrics_evaluated': [metric for metric, _ in SCAN_TIME_METRIC_SPECS] + ['within_range_target_possible', 'range_classification_code'],
        'minimum_candidate_subset_size': MIN_GATE_CANDIDATE_SIZE,
        'tradeable_share_support_threshold': TRADEABLE_SHARE_SUPPORT_THRESHOLD,
        'best_gate_weaker_offset_total': best_total_gate,
        'best_gate_weaker_offset_only': best_weaker_only_gate,
        'weaker_offset_total_candidate_count': len(weaker_total_candidates),
        'weaker_offset_only_candidate_count': len(weaker_only_candidates),
        'weaker_offset_total_supporting_gate_count': sum(1 for row in weaker_total_candidates if row.get('reaches_support_threshold')),
        'weaker_offset_only_supporting_gate_count': sum(1 for row in weaker_only_candidates if row.get('reaches_support_threshold')),
    }

    report_lines = [
        '# Replay checkpoint compatibility pack',
        '',
        'Purpose: isolate whether the weaker checkpoint population can be narrowed using existing scan-time features before any live threshold change is considered.',
        '',
        f"Generated at: {summary['generated_at_utc']}",
        f"App version: {VERSION}",
        f"Focus profile: {focus_profile_name or 'none'}",
        f"Supported offset: {supported_offset} ({summary.get('supported_offset_tradeable_share')})",
        f"Weaker offset: {weaker_offset} ({summary.get('weaker_offset_tradeable_share')})",
        f"Composition shift larger than shared decay: {summary.get('composition_shift_larger_than_shared_decay')}",
        '',
        '## Best scan-time gate candidates',
        json.dumps(
            {
                'weaker_offset_total': best_total_gate,
                'weaker_offset_only': best_weaker_only_gate,
            },
            indent=2,
        ),
        '',
        '## Interpretation',
        '- Use this pack to decide whether the next tranche should test a narrow compatibility gate at the weaker checkpoint.',
        '- Candidate gates are scan-time only; no post-entry outcome fields are used.',
        '- Do not use this pack by itself to change live thresholds.',
    ]

    manifest = {
        'bundle_type': 'replay_checkpoint_compatibility_pack',
        'generated_at_utc': summary['generated_at_utc'],
        'app_version': VERSION,
        'files': [
            'MANIFEST.json',
            'population_metric_comparison.csv',
            'population_categorical_comparison.csv',
            'weaker_offset_total_gate_candidates.csv',
            'weaker_offset_only_gate_candidates.csv',
            'replay_checkpoint_compatibility_summary.json',
            'report.md',
        ],
    }

    return {
        'MANIFEST.json': json.dumps(manifest, indent=2).encode('utf-8'),
        'population_metric_comparison.csv': _csv_bytes(population_metric_rows),
        'population_categorical_comparison.csv': _csv_bytes(population_categorical_rows),
        'weaker_offset_total_gate_candidates.csv': _csv_bytes(weaker_total_candidates),
        'weaker_offset_only_gate_candidates.csv': _csv_bytes(weaker_only_candidates),
        'replay_checkpoint_compatibility_summary.json': json.dumps(summary, indent=2).encode('utf-8'),
        'report.md': ('\n'.join(report_lines).strip() + '\n').encode('utf-8'),
    }
