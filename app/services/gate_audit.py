from __future__ import annotations

from typing import Dict, List, Tuple


def _advanced_key_set(rows: List[Dict[str, object]]) -> set[Tuple[str, str]]:
    return {
        (str(row.get('trading_day') or ''), str(row.get('symbol') or ''))
        for row in rows
        if bool(row.get('advanced_to_stage2'))
    }


def _row_reason(row: Dict[str, object]) -> str:
    return str(
        row.get('stage2_exclusion_reason')
        or row.get('exclusion_reason')
        or 'No explicit exclusion reason recorded.'
    )


def _utility_score(summary: Dict[str, object]) -> float:
    funnel = dict(summary.get('stage_funnel_summary') or {})
    advanced_rows = int(summary.get('advanced_stage2_total') or 0)
    actionable_lane = int(funnel.get('actionable_lane') or 0)
    precision_at_10 = float(summary.get('precision_at_10') or 0.0)
    conditional_precision_at_10 = float(summary.get('conditional_precision_at_10_entry_touched') or 0.0)
    entry_touch_rate = float(summary.get('entry_touch_rate_stage2') or 0.0)
    sample_score = min(advanced_rows / 20.0, 1.5) / 1.5
    actionable_score = min(actionable_lane / 8.0, 1.0)
    precision_score = min(max(precision_at_10, 0.0), 1.0)
    conditional_score = min(max(conditional_precision_at_10, 0.0), 1.0)
    entry_touch_score = min(max(entry_touch_rate, 0.0), 1.0)
    return round(precision_score * 0.42 + conditional_score * 0.16 + sample_score * 0.22 + actionable_score * 0.12 + entry_touch_score * 0.08, 4)


def build_gate_audit_row(
    scenario_name: str,
    payload: Dict[str, object],
    *,
    baseline_payload: Dict[str, object],
    overrides: Dict[str, object],
) -> Dict[str, object]:
    summary = dict(payload.get('summary') or {})
    funnel = dict(summary.get('stage_funnel_summary') or {})
    rows = list(payload.get('rows') or [])
    baseline_rows = list(baseline_payload.get('rows') or [])
    baseline_summary = dict(baseline_payload.get('summary') or {})
    baseline_funnel = dict(baseline_summary.get('stage_funnel_summary') or {})

    advanced_keys = _advanced_key_set(rows)
    baseline_advanced_keys = _advanced_key_set(baseline_rows)
    newly_admitted_keys = advanced_keys - baseline_advanced_keys
    newly_admitted_rows = [
        row for row in rows
        if bool(row.get('advanced_to_stage2')) and (str(row.get('trading_day') or ''), str(row.get('symbol') or '')) in newly_admitted_keys
    ]
    baseline_lookup = {
        (str(row.get('trading_day') or ''), str(row.get('symbol') or '')): row
        for row in baseline_rows
    }
    newly_admitted_reason_counts: Dict[str, int] = {}
    for row in newly_admitted_rows:
        key = (str(row.get('trading_day') or ''), str(row.get('symbol') or ''))
        base_row = baseline_lookup.get(key) or {}
        reason = _row_reason(base_row)
        newly_admitted_reason_counts[reason] = newly_admitted_reason_counts.get(reason, 0) + 1
    top_reasons = [
        {'reason': reason, 'count': count}
        for reason, count in sorted(newly_admitted_reason_counts.items(), key=lambda item: (-item[1], item[0]))[:5]
    ]

    advanced_stage2_total = int(summary.get('advanced_stage2_total') or 0)
    actionable_lane = int(funnel.get('actionable_lane') or 0)
    baseline_advanced_stage2_total = int(baseline_summary.get('advanced_stage2_total') or 0)
    baseline_actionable_lane = int(baseline_funnel.get('actionable_lane') or 0)
    precision_at_10 = round(float(summary.get('precision_at_10') or 0.0), 4)
    baseline_precision_at_10 = round(float(baseline_summary.get('precision_at_10') or 0.0), 4)
    conditional_precision_at_10 = round(float(summary.get('conditional_precision_at_10_entry_touched') or 0.0), 4)
    entry_touch_rate_stage2 = round(float(summary.get('entry_touch_rate_stage2') or 0.0), 4)
    overall_hit_rate = round(float(summary.get('overall_hit_rate') or 0.0), 4)

    newly_admitted_hit_rate = round(
        sum(1 for row in newly_admitted_rows if bool(row.get('hit_target'))) / max(len(newly_admitted_rows), 1), 4
    ) if newly_admitted_rows else None
    newly_admitted_entry_touch_rate = round(
        sum(1 for row in newly_admitted_rows if bool(row.get('entry_touched'))) / max(len(newly_admitted_rows), 1), 4
    ) if newly_admitted_rows else None

    utility_score = _utility_score(summary)
    baseline_utility_score = _utility_score(baseline_summary)

    return {
        'scenario_name': scenario_name,
        'validation_id': int(payload['id']),
        'overrides': overrides,
        'days': int(summary.get('days') or 0),
        'advanced_stage2_total': advanced_stage2_total,
        'actionable_lane': actionable_lane,
        'precision_at_10': precision_at_10,
        'conditional_precision_at_10_entry_touched': conditional_precision_at_10,
        'overall_hit_rate': overall_hit_rate,
        'entry_touch_rate_stage2': entry_touch_rate_stage2,
        'utility_score': utility_score,
        'delta_advanced_stage2_total': advanced_stage2_total - baseline_advanced_stage2_total,
        'delta_actionable_lane': actionable_lane - baseline_actionable_lane,
        'delta_precision_at_10': round(precision_at_10 - baseline_precision_at_10, 4),
        'delta_overall_hit_rate': round(overall_hit_rate - round(float(baseline_summary.get('overall_hit_rate') or 0.0), 4), 4),
        'delta_utility_score': round(utility_score - baseline_utility_score, 4),
        'newly_admitted_stage2': int(len(newly_admitted_rows)),
        'newly_admitted_hit_rate': newly_admitted_hit_rate,
        'newly_admitted_entry_touch_rate': newly_admitted_entry_touch_rate,
        'newly_admitted_baseline_reasons': top_reasons,
        'baseline_precision_at_10': baseline_precision_at_10,
    }


def recommend_gate_audit_scenario(rows: List[Dict[str, object]]) -> Dict[str, object]:
    if not rows:
        return {
            'recommended_scenario': 'baseline',
            'recommendation_reason': 'No audit rows were produced.',
            'should_consider_change': False,
        }
    baseline = next((row for row in rows if row.get('scenario_name') == 'baseline'), rows[0])
    challengers = [row for row in rows if row.get('scenario_name') != 'baseline']
    acceptable = [
        row for row in challengers
        if float(row.get('precision_at_10') or 0.0) >= float(baseline.get('precision_at_10') or 0.0) - 0.03
        and float(row.get('conditional_precision_at_10_entry_touched') or 0.0) >= float(baseline.get('conditional_precision_at_10_entry_touched') or 0.0) - 0.05
        and int(row.get('newly_admitted_stage2') or 0) > 0
    ]
    if not acceptable:
        return {
            'recommended_scenario': str(baseline.get('scenario_name') or 'baseline'),
            'recommended_validation_id': int(baseline.get('validation_id') or 0),
            'recommendation_reason': 'No relaxed scenario admitted additional stage-2 names without materially degrading validation quality.',
            'should_consider_change': False,
        }
    best = sorted(
        acceptable,
        key=lambda row: (
            float(row.get('delta_utility_score') or 0.0),
            int(row.get('newly_admitted_stage2') or 0),
            float(row.get('newly_admitted_hit_rate') or 0.0),
        ),
        reverse=True,
    )[0]
    return {
        'recommended_scenario': str(best.get('scenario_name') or ''),
        'recommended_validation_id': int(best.get('validation_id') or 0),
        'recommendation_reason': 'This relaxed scenario adds incremental stage-2 names while keeping core precision close enough to baseline to merit live consideration.',
        'should_consider_change': True,
    }
