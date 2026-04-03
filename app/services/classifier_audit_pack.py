from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone, timedelta
from typing import Any

import pandas as pd

from app.config import Settings
from app.db import Database
from app.repositories import RepositoryBundle, ensure_repository_bundle
from app.services.diagnostics import build_contract_health
from app.services.evidence_pack import _json_bytes, _rows_to_csv
from zoneinfo import ZoneInfo
from app.services.stage2_regression_pack import (
    LATEST_DEFAULT_OFFSETS,
    _candidate_maps,
    _gate_predicate_rows,
    _metric_delta_rows,
    _select_recent_scans,
)
from app.version import VERSION
from app.view_models import build_scan_view


DEFAULT_REJECT_SAMPLE_PER_OFFSET = 5


NY_TZ = ZoneInfo('America/New_York')
UTC = timezone.utc


def _session_bounds_for_regular_day(trading_day: str, offset_minutes: int) -> tuple[datetime, datetime, datetime]:
    day = datetime.strptime(trading_day, '%Y-%m-%d').date()
    market_open = datetime(day.year, day.month, day.day, 9, 30, tzinfo=NY_TZ)
    market_close = datetime(day.year, day.month, day.day, 16, 0, tzinfo=NY_TZ)
    checkpoint = market_open + timedelta(minutes=int(offset_minutes))
    return market_open.astimezone(UTC), market_close.astimezone(UTC), checkpoint.astimezone(UTC)


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace('+00:00', 'Z')


def _surface_row(scan: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    summary = dict(scan.get('summary') or {})
    shortlist_alignment = dict(summary.get('shortlist_alignment') or {})
    classification_counts = Counter(
        str((candidate.get('metrics') or {}).get('range_classification_code') or 'unknown')
        for candidate in candidates
    )
    return {
        'trading_day': str(scan.get('trading_day') or ''),
        'scan_id': int(scan.get('id') or 0),
        'scan_offset_minutes': int(scan.get('scan_offset_minutes') or 0),
        'stage1_count': int(scan.get('stage1_count') or 0),
        'stage2_count': int(scan.get('stage2_count') or 0),
        'alignment_prefilter_kept_count': shortlist_alignment.get('alignment_prefilter_kept_count'),
        'classification_a_count': classification_counts.get('A', 0),
        'classification_b_count': classification_counts.get('B', 0),
        'classification_c_count': classification_counts.get('C', 0),
        'classification_unknown_count': classification_counts.get('unknown', 0),
    }


def _candidate_audit_row(*, trading_day: str, offset: int, reason: str, candidate: dict[str, Any]) -> dict[str, Any]:
    metrics = dict(candidate.get('metrics') or {})
    return {
        'trading_day': trading_day,
        'scan_offset_minutes': int(offset),
        'audit_reason': reason,
        'symbol': candidate.get('symbol'),
        'company_name': candidate.get('company_name'),
        'advanced_to_stage2': bool(candidate.get('advanced_to_stage2')),
        'recommendation_tier': candidate.get('recommendation_tier'),
        'recommendation_book': candidate.get('recommendation_book'),
        'execution_lane': candidate.get('execution_lane'),
        'touch_window_band': candidate.get('touch_window_band'),
        'mover_rank': candidate.get('mover_rank'),
        'intraday_pct_gain': candidate.get('intraday_pct_gain'),
        'total_score': candidate.get('total_score'),
        'current_price': candidate.get('current_price'),
        'relative_volume': candidate.get('relative_volume'),
        'range_classification': metrics.get('range_classification'),
        'range_classification_code': metrics.get('range_classification_code'),
        'score_cap_reason': metrics.get('score_cap_reason'),
        'exclusion_reason': candidate.get('exclusion_reason'),
        'distance_to_entry_pct': metrics.get('distance_to_entry_pct'),
        'range_current_location': metrics.get('range_current_location'),
        'range_band_width_pct': metrics.get('range_band_width_pct'),
        'range_containment_ratio': metrics.get('range_containment_ratio'),
        'breakout_close_ratio': metrics.get('breakout_close_ratio'),
        'wickiness_ratio': metrics.get('wickiness_ratio'),
        'recent_breakout_close_ratio': metrics.get('recent_breakout_close_ratio'),
        'recent_directional_efficiency': metrics.get('recent_directional_efficiency'),
        'recent_wickiness_ratio': metrics.get('recent_wickiness_ratio'),
        'width_retention_ratio': metrics.get('width_retention_ratio'),
        'cycle_persistence_ratio': metrics.get('cycle_persistence_ratio'),
        'cycle_durability_score': metrics.get('cycle_durability_score'),
        'bounce_quality_score': metrics.get('bounce_quality_score'),
        'recent_completed_cycles_observed': metrics.get('recent_completed_cycles_observed'),
        'recent_lower_zone_touch_count': metrics.get('recent_lower_zone_touch_count'),
        'recent_upper_zone_touch_count': metrics.get('recent_upper_zone_touch_count'),
        'recent_bounce_event_count': metrics.get('recent_bounce_event_count'),
        'recent_bounce_observation_confidence': metrics.get('recent_bounce_observation_confidence'),
        'within_range_target_possible': metrics.get('within_range_target_possible'),
    }


def _sample_rejected_classification_c(candidates: dict[str, dict[str, Any]], *, limit: int, exclude_symbols: set[str]) -> list[dict[str, Any]]:
    ranked = [
        candidate
        for symbol, candidate in candidates.items()
        if symbol not in exclude_symbols
        and not bool(candidate.get('advanced_to_stage2'))
        and str((candidate.get('metrics') or {}).get('range_classification_code') or '') == 'C'
    ]
    ranked.sort(
        key=lambda candidate: (
            -(float(candidate.get('total_score') or 0.0)),
            int(candidate.get('mover_rank') or 999999),
            str(candidate.get('symbol') or ''),
        )
    )
    return ranked[: max(int(limit), 0)]


def _symbol_metric_snapshot(trading_day: str, offset: int, reason: str, candidate: dict[str, Any]) -> dict[str, Any]:
    metrics = dict(candidate.get('metrics') or {})
    row = {
        'trading_day': trading_day,
        'scan_offset_minutes': int(offset),
        'audit_reason': reason,
        'symbol': candidate.get('symbol'),
    }
    for key, value in metrics.items():
        if isinstance(value, (dict, list)):
            continue
        row[key] = value
    return row


def _predicate_snapshot_rows(
    settings: Settings,
    trading_day: str,
    symbol: str,
    early_offset: int,
    early_candidate: dict[str, Any],
    late_offset: int,
    late_candidate: dict[str, Any],
    *,
    reason: str,
) -> list[dict[str, Any]]:
    rows = _gate_predicate_rows(settings, trading_day, symbol, early_offset, early_candidate, late_offset, late_candidate)
    for row in rows:
        row['audit_reason'] = reason
    return rows


def _metric_delta_rows_with_reason(
    trading_day: str,
    symbol: str,
    early_offset: int,
    early_candidate: dict[str, Any],
    late_offset: int,
    late_candidate: dict[str, Any],
    *,
    reason: str,
) -> list[dict[str, Any]]:
    rows = _metric_delta_rows(trading_day, symbol, early_offset, early_candidate, late_offset, late_candidate)
    for row in rows:
        row['audit_reason'] = reason
    return rows


def _normalise_timestamp_column(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    bars = frame.copy()
    if 'timestamp' not in bars.columns:
        bars = bars.reset_index()
        if 'index' in bars.columns and 'timestamp' not in bars.columns:
            bars = bars.rename(columns={'index': 'timestamp'})
    bars['timestamp'] = pd.to_datetime(bars['timestamp'], utc=True, errors='coerce')
    bars = bars[bars['timestamp'].notna()].copy()
    return bars.sort_values('timestamp').reset_index(drop=True)


def _intraday_bar_rows(
    bars_map: dict[str, pd.DataFrame],
    *,
    trading_day: str,
    offset_minutes: list[int],
) -> list[dict[str, Any]]:
    checkpoints = {offset: _session_bounds_for_regular_day(trading_day, offset)[2] for offset in offset_minutes}
    rows: list[dict[str, Any]] = []
    for symbol, frame in sorted(bars_map.items()):
        bars = _normalise_timestamp_column(frame)
        if bars.empty:
            continue
        for bar in bars.itertuples(index=False):
            timestamp = pd.Timestamp(getattr(bar, 'timestamp')).to_pydatetime().astimezone(timezone.utc)
            row = {
                'trading_day': trading_day,
                'symbol': symbol,
                'timestamp_utc': timestamp.isoformat(),
                'open': float(getattr(bar, 'open')),
                'high': float(getattr(bar, 'high')),
                'low': float(getattr(bar, 'low')),
                'close': float(getattr(bar, 'close')),
                'volume': float(getattr(bar, 'volume')),
            }
            for offset, checkpoint in checkpoints.items():
                row[f'at_or_before_{offset}m_checkpoint'] = timestamp <= checkpoint
            rows.append(row)
    return rows


def build_classifier_audit_pack(
    settings: Settings,
    db: Database | RepositoryBundle,
    alpaca,
    *,
    days: int = 5,
    offsets: list[int] | None = None,
    rejected_sample_per_offset: int = DEFAULT_REJECT_SAMPLE_PER_OFFSET,
) -> dict[str, bytes]:
    repos = ensure_repository_bundle(db)
    requested_offsets = sorted({int(value) for value in (offsets or LATEST_DEFAULT_OFFSETS) if int(value) > 0}) or list(LATEST_DEFAULT_OFFSETS)
    selected_days, chosen_scans = _select_recent_scans(repos, days=days, offsets=requested_offsets)
    latest_day = selected_days[0] if selected_days else ''
    latest_offsets = sorted({offset for day, offset in chosen_scans if day == latest_day})
    if len(latest_offsets) < 2:
        latest_offsets = requested_offsets
    early_offset = latest_offsets[0] if latest_offsets else requested_offsets[0]
    late_offset = latest_offsets[-1] if latest_offsets else requested_offsets[-1]

    scan_rows: list[dict[str, Any]] = []
    for (day, offset), scan in sorted(chosen_scans.items()):
        scan_view = build_scan_view(scan, alpaca_data_feed=settings.alpaca_data_feed) or scan
        candidates = list(_candidate_maps(repos, int(scan.get('id') or 0)).values())
        scan_rows.append(_surface_row(scan_view, candidates))

    audited_rows: list[dict[str, Any]] = []
    metric_snapshots: list[dict[str, Any]] = []
    metric_deltas: list[dict[str, Any]] = []
    predicate_rows: list[dict[str, Any]] = []
    audited_symbols: set[str] = set()
    classification_c_samples_by_offset: dict[int, list[str]] = {}

    if latest_day and (latest_day, early_offset) in chosen_scans and (latest_day, late_offset) in chosen_scans:
        early_scan = chosen_scans[(latest_day, early_offset)]
        late_scan = chosen_scans[(latest_day, late_offset)]
        early_candidates = _candidate_maps(repos, int(early_scan.get('id') or 0))
        late_candidates = _candidate_maps(repos, int(late_scan.get('id') or 0))

        advanced_symbols = sorted(
            symbol
            for symbol, candidate in early_candidates.items()
            if bool(candidate.get('advanced_to_stage2')) and symbol in late_candidates
        )
        for symbol in advanced_symbols:
            early_candidate = early_candidates[symbol]
            late_candidate = late_candidates[symbol]
            audited_symbols.add(symbol)
            audited_rows.append(_candidate_audit_row(trading_day=latest_day, offset=early_offset, reason='advanced_at_early_checkpoint', candidate=early_candidate))
            audited_rows.append(_candidate_audit_row(trading_day=latest_day, offset=late_offset, reason='advanced_then_late_snapshot', candidate=late_candidate))
            metric_snapshots.append(_symbol_metric_snapshot(latest_day, early_offset, 'advanced_at_early_checkpoint', early_candidate))
            metric_snapshots.append(_symbol_metric_snapshot(latest_day, late_offset, 'advanced_then_late_snapshot', late_candidate))
            metric_deltas.extend(_metric_delta_rows_with_reason(latest_day, symbol, early_offset, early_candidate, late_offset, late_candidate, reason='advanced_symbol_regression_check'))
            predicate_rows.extend(_predicate_snapshot_rows(settings, latest_day, symbol, early_offset, early_candidate, late_offset, late_candidate, reason='advanced_symbol_regression_check'))

        for offset, candidates in ((early_offset, early_candidates), (late_offset, late_candidates)):
            sample = _sample_rejected_classification_c(candidates, limit=rejected_sample_per_offset, exclude_symbols=audited_symbols)
            classification_c_samples_by_offset[offset] = [str(candidate.get('symbol') or '') for candidate in sample]
            paired_candidates = late_candidates if offset == early_offset else early_candidates
            paired_offset = late_offset if offset == early_offset else early_offset
            for candidate in sample:
                symbol = str(candidate.get('symbol') or '')
                audited_symbols.add(symbol)
                reason = f'rejected_classification_c_sample_{offset}m'
                audited_rows.append(_candidate_audit_row(trading_day=latest_day, offset=offset, reason=reason, candidate=candidate))
                metric_snapshots.append(_symbol_metric_snapshot(latest_day, offset, reason, candidate))
                counterpart = paired_candidates.get(symbol)
                if counterpart:
                    metric_snapshots.append(_symbol_metric_snapshot(latest_day, paired_offset, f'paired_snapshot_{paired_offset}m', counterpart))
                    metric_deltas.extend(_metric_delta_rows_with_reason(latest_day, symbol, early_offset if offset == early_offset else paired_offset, early_candidates.get(symbol, counterpart), late_offset if offset == early_offset else offset, late_candidates.get(symbol, candidate), reason=reason))
                    predicate_rows.extend(_predicate_snapshot_rows(
                        settings,
                        latest_day,
                        symbol,
                        early_offset,
                        early_candidates.get(symbol, candidate),
                        late_offset,
                        late_candidates.get(symbol, candidate),
                        reason=reason,
                    ))

    intraday_rows: list[dict[str, Any]] = []
    if latest_day and audited_symbols and getattr(alpaca, 'has_credentials', lambda: False)():
        market_open, market_close, _ = _session_bounds_for_regular_day(latest_day, late_offset)
        bars_map = alpaca.fetch_bars(sorted(audited_symbols), '1Min', _iso_z(market_open), _iso_z(market_close))
        intraday_rows = _intraday_bar_rows(bars_map, trading_day=latest_day, offset_minutes=[early_offset, late_offset])

    summary = {
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'app_version': VERSION,
        'selected_days': selected_days,
        'latest_trading_day': latest_day,
        'early_offset_minutes': early_offset,
        'late_offset_minutes': late_offset,
        'audited_symbol_count': len(audited_symbols),
        'advanced_symbol_count': len({row.get('symbol') for row in audited_rows if row.get('audit_reason') == 'advanced_at_early_checkpoint'}),
        'classification_c_sample_symbols_by_offset': classification_c_samples_by_offset,
        'classification_c_counts_by_offset': {
            int(row.get('scan_offset_minutes') or 0): int(row.get('classification_c_count') or 0)
            for row in scan_rows
            if str(row.get('trading_day') or '') == latest_day
        },
        'intraday_bars_included': bool(intraday_rows),
        'decision_rule': 'Use this pack to test whether classification-C domination is mostly correct or too strict before any new product surface or threshold change.',
        'freeze_recommendation': [
            'Do not add new presentation features until classifier evidence is clearer.',
            'Do not retune thresholds before checking whether classification-C rejects are visually correct.',
            'Do not redesign stage-1 while shortlist alignment is retaining a large liquid pool.',
        ],
    }

    report_lines = [
        '# Classifier audit evidence pack',
        '',
        f"Generated at: {summary['generated_at_utc']}",
        f'App version: {VERSION}',
        f"Latest trading day audited: {latest_day or 'None'}",
        f'Early/late offsets compared: {early_offset} -> {late_offset}',
        '',
        '## Why this pack exists',
        '- The clean live bottleneck is now classification-C domination and 120→150 regression, not shortlist liquidity starvation.',
        '- This pack is for evidence gathering, not for adding new product surface complexity.',
        '',
        '## What is inside',
        f"- Audited symbols: {', '.join(sorted(audited_symbols)) or 'None'}",
        f"- Classification-C sample at {early_offset}m: {', '.join(classification_c_samples_by_offset.get(early_offset) or []) or 'None'}",
        f"- Classification-C sample at {late_offset}m: {', '.join(classification_c_samples_by_offset.get(late_offset) or []) or 'None'}",
        f"- Exact intraday bars included: {'Yes' if intraday_rows else 'No'}",
        '',
        '## Freeze guidance',
    ]
    report_lines.extend(f'- {line}' for line in summary['freeze_recommendation'])

    manifest = {
        'bundle_type': 'classifier_audit_pack',
        'bundle_contract_version': '1.0',
        'app_version': VERSION,
        'generated_at_utc': summary['generated_at_utc'],
        'days_requested': int(days),
        'offsets_requested': requested_offsets,
        'latest_trading_day': latest_day,
        'settings_snapshot': settings.public_snapshot(),
    }

    return {
        'MANIFEST.json': _json_bytes(manifest),
        'settings_snapshot.json': _json_bytes(settings.public_snapshot()),
        'contract_health.json': _json_bytes(build_contract_health(repos.db)),
        'classifier_audit_summary.json': _json_bytes(summary),
        'classifier_audit_scan_rollup.csv': _rows_to_csv(scan_rows).encode('utf-8'),
        'classifier_audit_symbols.csv': _rows_to_csv(audited_rows).encode('utf-8'),
        'classifier_audit_metric_snapshots.csv': _rows_to_csv(metric_snapshots).encode('utf-8'),
        'classifier_audit_metric_deltas.csv': _rows_to_csv(metric_deltas).encode('utf-8'),
        'classifier_audit_gate_snapshot.csv': _rows_to_csv(predicate_rows).encode('utf-8'),
        'classifier_audit_intraday_bars.csv': _rows_to_csv(intraday_rows).encode('utf-8'),
        'report.md': '\n'.join(report_lines).encode('utf-8'),
    }
