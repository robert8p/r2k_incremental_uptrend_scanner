from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.config import Settings
from app.db import Database
from app.repositories import RepositoryBundle, ensure_repository_bundle
from app.services.checkpoint_decision_surface import (
    _candidate_map,
    _select_scans_for_day,
    build_checkpoint_decision_surface,
)
from app.services.decision_bundle import get_or_build_decision_state
from app.services.replay_bottleneck_pack import _to_float, _to_int
from app.services.evidence_pack import _rows_to_csv
from app.version import VERSION

UTC = timezone.utc
DEFAULT_OFFSETS = [120, 150]


def _metric_value(candidate: dict[str, Any], metric_name: str) -> Any:
    metrics = dict(candidate.get('metrics') or {})
    if metric_name in metrics:
        return metrics.get(metric_name)
    return candidate.get(metric_name)


def _gate_pass(value: Any, comparator: str, threshold: Any) -> bool:
    if value is None or value == '':
        return False
    comp = str(comparator or '')
    if comp == '==':
        return str(value) == str(threshold)
    value_num = _to_float(value)
    threshold_num = _to_float(threshold)
    if value_num is None or threshold_num is None:
        return False
    if comp == '>=':
        return value_num >= threshold_num
    if comp == '<=':
        return value_num <= threshold_num
    if comp == '>':
        return value_num > threshold_num
    if comp == '<':
        return value_num < threshold_num
    return False


def _candidate_shadow_row(
    *,
    candidate: dict[str, Any],
    scan_id: int,
    offset: int,
    metric_name: str,
    comparator: str,
    threshold: Any,
    supported_advanced_symbols: set[str],
) -> dict[str, Any]:
    metrics = dict(candidate.get('metrics') or {})
    symbol = str(candidate.get('symbol') or '')
    metric_value = _metric_value(candidate, metric_name)
    advanced_now = bool(candidate.get('advanced_to_stage2'))
    pass_gate = _gate_pass(metric_value, comparator, threshold)
    return {
        'scan_id': int(scan_id),
        'scan_offset_minutes': int(offset),
        'symbol': symbol,
        'company_name': candidate.get('company_name'),
        'advanced_to_stage2': advanced_now,
        'recommendation_tier': candidate.get('recommendation_tier'),
        'recommendation_book': candidate.get('recommendation_book'),
        'execution_lane': candidate.get('execution_lane'),
        'mover_rank': candidate.get('mover_rank'),
        'intraday_pct_gain': candidate.get('intraday_pct_gain'),
        'total_score': candidate.get('total_score'),
        'current_price': candidate.get('current_price'),
        'range_classification_code': metrics.get('range_classification_code'),
        'within_range_target_possible': metrics.get('within_range_target_possible'),
        'distance_to_entry_pct': metrics.get('distance_to_entry_pct'),
        'width_retention_ratio': metrics.get('width_retention_ratio'),
        'cycle_persistence_ratio': metrics.get('cycle_persistence_ratio'),
        'bounce_quality_score': metrics.get('bounce_quality_score'),
        'cycle_durability_score': metrics.get('cycle_durability_score'),
        'gate_metric_name': metric_name,
        'gate_metric_value': metric_value,
        'gate_comparator': comparator,
        'gate_threshold_value': threshold,
        'passes_shadow_gate': pass_gate,
        'advanced_at_supported_checkpoint': symbol in supported_advanced_symbols,
        'regressed_from_supported_checkpoint': (symbol in supported_advanced_symbols) and (not advanced_now),
        'exclusion_reason': candidate.get('exclusion_reason'),
        'score_cap_reason': metrics.get('score_cap_reason'),
    }


def build_weaker_checkpoint_gate_shadow_pack(
    settings: Settings,
    db: Database | RepositoryBundle,
    alpaca,
    *,
    offsets: list[int] | None = None,
) -> dict[str, bytes]:
    requested_offsets = list(offsets or DEFAULT_OFFSETS)
    decision_state = get_or_build_decision_state(settings, db, alpaca, offsets=requested_offsets, prefer_cache=True)
    compatibility = dict(decision_state.get('historical_replay_checkpoint_compatibility') or {})
    gate = dict(compatibility.get('best_gate_weaker_offset_only') or {}) or dict(compatibility.get('best_gate_weaker_offset_total') or {})
    supported_offset = _to_int(compatibility.get('supported_offset_minutes'))
    weaker_offset = _to_int(compatibility.get('weaker_offset_minutes'))
    recommendation_code = str(decision_state.get('decision_recommendation_code') or '')

    surface = build_checkpoint_decision_surface(settings, db, trading_day=decision_state.get('latest_selected_day'), offsets=requested_offsets)
    selected_day = str((surface.get('summary') or {}).get('selected_day') or decision_state.get('latest_selected_day') or '')

    repos = ensure_repository_bundle(db)
    _, chosen_scans = _select_scans_for_day(repos, trading_day=selected_day, offsets=requested_offsets)
    supported_scan = chosen_scans.get(supported_offset)
    weaker_scan = chosen_scans.get(weaker_offset)
    supported_candidates = _candidate_map(repos, int(supported_scan.get('id') or 0)) if supported_scan else {}
    weaker_candidates = _candidate_map(repos, int(weaker_scan.get('id') or 0)) if weaker_scan else {}
    supported_advanced_symbols = {symbol for symbol, candidate in supported_candidates.items() if candidate.get('advanced_to_stage2')}

    metric_name = str(gate.get('metric_name') or '')
    comparator = str(gate.get('comparator') or '')
    threshold = gate.get('threshold_value')

    weaker_rows = [
        _candidate_shadow_row(
            candidate=candidate,
            scan_id=int(weaker_scan.get('id') or 0),
            offset=weaker_offset,
            metric_name=metric_name,
            comparator=comparator,
            threshold=threshold,
            supported_advanced_symbols=supported_advanced_symbols,
        )
        for candidate in weaker_candidates.values()
    ] if weaker_scan and gate else []
    weaker_rows.sort(key=lambda row: (not bool(row.get('passes_shadow_gate')), -(float(row.get('total_score') or 0.0)), str(row.get('symbol') or '')))

    gate_pass_rows = [row for row in weaker_rows if row.get('passes_shadow_gate')]
    gate_fail_rows = [row for row in weaker_rows if not row.get('passes_shadow_gate')]
    regressed_gate_pass_rows = [row for row in gate_pass_rows if row.get('regressed_from_supported_checkpoint')]

    summary = {
        'generated_at_utc': datetime.now(UTC).isoformat(),
        'app_version': VERSION,
        'bundle_type': 'weaker_checkpoint_gate_shadow_pack',
        'decision_recommendation_code': recommendation_code,
        'selected_day': selected_day,
        'requested_offsets': requested_offsets,
        'available_offsets': (surface.get('summary') or {}).get('available_offsets') or [],
        'supported_offset_minutes': supported_offset,
        'weaker_offset_minutes': weaker_offset,
        'supported_scan_id': int(supported_scan.get('id') or 0) if supported_scan else None,
        'weaker_scan_id': int(weaker_scan.get('id') or 0) if weaker_scan else None,
        'focus_profile_name': compatibility.get('focus_profile_name') or decision_state.get('historical_replay_shadow', {}).get('recommended_profile', {}).get('profile_name'),
        'gate_metric_name': metric_name,
        'gate_comparator': comparator,
        'gate_threshold_value': threshold,
        'gate_tradeable_share_from_replay': gate.get('tradeable_share'),
        'gate_flagged_total_from_replay': gate.get('flagged_total'),
        'weaker_checkpoint_candidate_count': len(weaker_rows),
        'weaker_checkpoint_gate_pass_count': len(gate_pass_rows),
        'weaker_checkpoint_gate_fail_count': len(gate_fail_rows),
        'weaker_checkpoint_gate_pass_share_of_candidates': round(len(gate_pass_rows) / len(weaker_rows), 4) if weaker_rows else None,
        'weaker_checkpoint_gate_pass_advanced_to_stage2_count': sum(1 for row in gate_pass_rows if row.get('advanced_to_stage2')),
        'weaker_checkpoint_gate_pass_rejected_count': sum(1 for row in gate_pass_rows if not row.get('advanced_to_stage2')),
        'supported_checkpoint_advanced_symbol_count': len(supported_advanced_symbols),
        'weaker_checkpoint_gate_pass_supported_advanced_count': sum(1 for row in gate_pass_rows if row.get('advanced_at_supported_checkpoint')),
        'weaker_checkpoint_gate_pass_regressed_from_supported_count': len(regressed_gate_pass_rows),
        'currently_valid_now_count': _to_int(decision_state.get('currently_valid_now_count')),
        'regressed_after_earlier_validity_count': _to_int(decision_state.get('regressed_after_earlier_validity_count')),
        'surface_message': (
            f'Shadow-test the weaker checkpoint gate on {metric_name} {comparator} {threshold}; keep live behavior frozen.'
            if gate else
            'No weaker-checkpoint gate candidate was available in the current decision state.'
        ),
    }

    report_lines = [
        '# Weaker checkpoint gate shadow pack',
        '',
        'Purpose: apply the current weaker-checkpoint replay gate candidate to the latest weaker checkpoint in shadow, without changing live behavior.',
        '',
        f"Generated at: {summary['generated_at_utc']}",
        f"App version: {VERSION}",
        f"Selected trading day: {selected_day or 'None'}",
        f"Decision recommendation: {recommendation_code or 'None'}",
        f"Supported offset: {supported_offset}",
        f"Weaker offset: {weaker_offset}",
        f"Gate candidate: {metric_name or 'None'} {comparator or ''} {threshold if threshold is not None else ''}",
        f"Replay tradeable share for gate: {gate.get('tradeable_share') if gate else None}",
        '',
        '## Current weaker-checkpoint shadow overlay',
        f"- Weaker-checkpoint candidates: {summary['weaker_checkpoint_candidate_count']}",
        f"- Gate-pass candidates: {summary['weaker_checkpoint_gate_pass_count']}",
        f"- Gate-pass advanced to stage 2: {summary['weaker_checkpoint_gate_pass_advanced_to_stage2_count']}",
        f"- Gate-pass rejected: {summary['weaker_checkpoint_gate_pass_rejected_count']}",
        f"- Gate-pass regressed from supported checkpoint: {summary['weaker_checkpoint_gate_pass_regressed_from_supported_count']}",
        '',
        '## Interpretation',
        '- Use this pack to see whether the replay-backed weaker-checkpoint gate candidate is surfacing a coherent shadow subset on the latest weaker checkpoint.',
        '- Do not change live thresholds from this pack alone.',
        '- A good shadow signal is a gate-pass subset that meaningfully trims weaker-checkpoint clutter while still retaining supported-regressed names worth watching.',
    ]

    manifest = {
        'bundle_type': 'weaker_checkpoint_gate_shadow_pack',
        'generated_at_utc': summary['generated_at_utc'],
        'app_version': VERSION,
        'files': [
            'MANIFEST.json',
            'weaker_checkpoint_gate_shadow_summary.json',
            'weaker_checkpoint_all_candidates.csv',
            'weaker_checkpoint_gate_pass_candidates.csv',
            'weaker_checkpoint_gate_fail_candidates.csv',
            'weaker_checkpoint_gate_regressed_from_supported.csv',
            'checkpoint_scan_summary.csv',
            'report.md',
        ],
    }

    return {
        'MANIFEST.json': json.dumps(manifest, indent=2).encode('utf-8'),
        'weaker_checkpoint_gate_shadow_summary.json': json.dumps(summary, indent=2).encode('utf-8'),
        'weaker_checkpoint_all_candidates.csv': _rows_to_csv(weaker_rows).encode('utf-8'),
        'weaker_checkpoint_gate_pass_candidates.csv': _rows_to_csv(gate_pass_rows).encode('utf-8'),
        'weaker_checkpoint_gate_fail_candidates.csv': _rows_to_csv(gate_fail_rows).encode('utf-8'),
        'weaker_checkpoint_gate_regressed_from_supported.csv': _rows_to_csv(regressed_gate_pass_rows).encode('utf-8'),
        'checkpoint_scan_summary.csv': _rows_to_csv(surface.get('scan_rows') or []).encode('utf-8'),
        'report.md': ('\n'.join(report_lines).strip() + '\n').encode('utf-8'),
    }
