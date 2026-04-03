from __future__ import annotations

from collections import defaultdict
from itertools import product
from typing import Dict, Iterable, List, Tuple

import numpy as np


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if number != number or number in {float("inf"), float("-inf")}:
        return float(default)
    return float(number)


COMPONENT_KEYS = [
    'target_strength',
    'liquidity',
    'volatility_capacity',
    'dynamic_range',
    'range_position',
    'time_feasibility',
    'execution_quality',
]


def _group_by_day(rows: Iterable[Dict[str, object]]) -> Dict[str, List[Dict[str, object]]]:
    grouped: Dict[str, List[Dict[str, object]]] = defaultdict(list)
    for row in rows:
        if row.get('advanced_to_stage2'):
            grouped[str(row['trading_day'])].append(row)
    return grouped


def average_daily_precision(rows: Iterable[Dict[str, object]], score_key: str, top_n: int) -> float:
    grouped = _group_by_day(rows)
    if not grouped:
        return 0.0
    daily_scores = []
    for day_rows in grouped.values():
        ranked = sorted(day_rows, key=lambda x: _safe_float(x.get(score_key), -1.0), reverse=True)[:top_n]
        if not ranked:
            continue
        daily_scores.append(sum(1 for row in ranked if row.get('hit_target')) / len(ranked))
    return round(float(np.mean(daily_scores)), 4) if daily_scores else 0.0


def overall_hit_rate(rows: Iterable[Dict[str, object]], score_key: str | None = None) -> float:
    advanced = [row for row in rows if row.get('advanced_to_stage2')]
    if not advanced:
        return 0.0
    return round(sum(1 for row in advanced if row.get('hit_target')) / len(advanced), 4)


def bucket_monotonicity(rows: Iterable[Dict[str, object]], score_key: str) -> Dict[str, object]:
    advanced = [row for row in rows if row.get('advanced_to_stage2')]
    if not advanced:
        return {'ok': False, 'pairs_tested': 0, 'pairs_non_decreasing': 0, 'ratio': 0.0}
    buckets: Dict[str, List[Dict[str, object]]] = defaultdict(list)
    for row in advanced:
        score = _safe_float(row.get(score_key), 0.0)
        lower = int(score // 20) * 20
        upper = min(lower + 20, 100)
        bucket = f'{lower}-{upper}'
        buckets[bucket].append(row)
    ordered = []
    for bucket in sorted(buckets.keys(), key=lambda x: int(x.split('-')[0])):
        rows_in_bucket = buckets[bucket]
        ordered.append((bucket, sum(1 for row in rows_in_bucket if row.get('hit_target')) / len(rows_in_bucket)))
    pairs_tested = max(len(ordered) - 1, 0)
    non_decreasing = 0
    for idx in range(len(ordered) - 1):
        if ordered[idx + 1][1] + 1e-9 >= ordered[idx][1]:
            non_decreasing += 1
    ratio = non_decreasing / pairs_tested if pairs_tested else 0.0
    return {'ok': ratio >= 0.6 if pairs_tested else False, 'pairs_tested': pairs_tested, 'pairs_non_decreasing': non_decreasing, 'ratio': round(ratio, 3)}


def evaluate_weight_vector(rows: List[Dict[str, object]], weights: Dict[str, float], *, score_key: str) -> Dict[str, object]:
    for row in rows:
        if not row.get('advanced_to_stage2'):
            continue
        component_scores = row.get('component_scores') or {}
        row[score_key] = round(sum(_safe_float(component_scores.get(k), 0.0) * _safe_float(weights.get(k), 0.0) for k in COMPONENT_KEYS), 4)
    p5 = average_daily_precision(rows, score_key, 5)
    p10 = average_daily_precision(rows, score_key, 10)
    p20 = average_daily_precision(rows, score_key, 20)
    hit = overall_hit_rate(rows, score_key)
    mono = bucket_monotonicity(rows, score_key)
    objective = round(p10 * 0.40 + p5 * 0.25 + p20 * 0.15 + hit * 0.05 + float(mono['ratio']) * 0.15, 6)
    return {
        'objective': objective,
        'precision_at_5': p5,
        'precision_at_10': p10,
        'precision_at_20': p20,
        'overall_hit_rate': hit,
        'bucket_monotonicity': mono,
    }


def _normalize_weights(weights: Dict[str, float]) -> Dict[str, float]:
    total = sum(max(_safe_float(v, 0.0), 0.0) for v in weights.values())
    if total <= 0:
        equal = 1.0 / len(COMPONENT_KEYS)
        return {k: equal for k in COMPONENT_KEYS}
    return {k: round(max(_safe_float(weights.get(k), 0.0), 0.0) / total, 6) for k in COMPONENT_KEYS}


def _candidate_weight_vectors(base_weights: Dict[str, float]) -> Iterable[Dict[str, float]]:
    base = [_safe_float(base_weights.get(k), 0.0) for k in COMPONENT_KEYS]
    multipliers = (0.8, 1.0, 1.2)
    seen: set[Tuple[float, ...]] = set()
    for combo in product(multipliers, repeat=len(COMPONENT_KEYS)):
        proposal = {k: base[idx] * combo[idx] for idx, k in enumerate(COMPONENT_KEYS)}
        normalized = _normalize_weights(proposal)
        key = tuple(normalized[k] for k in COMPONENT_KEYS)
        if key in seen:
            continue
        seen.add(key)
        yield normalized


def calibrate_rows(
    rows: List[Dict[str, object]],
    current_weights: Dict[str, float],
    min_improvement: float,
    *,
    mover_rank_baseline_precision_at_10: float = 0.0,
) -> Dict[str, object]:
    advanced_count = sum(1 for row in rows if row.get('advanced_to_stage2'))
    if advanced_count < 25:
        baseline_rows = [dict(row) for row in rows]
        baseline = evaluate_weight_vector(baseline_rows, _normalize_weights(current_weights), score_key='baseline_score')
        return {
            'eligible': False,
            'reason': 'Not enough advanced stage-2 rows for a trustworthy calibration sweep.',
            'baseline': baseline,
            'recommended': None,
            'should_apply': False,
            'improvement': 0.0,
        }

    normalized_current = _normalize_weights(current_weights)
    baseline_rows = [dict(row) for row in rows]
    baseline = evaluate_weight_vector(baseline_rows, normalized_current, score_key='baseline_score')

    best_weights = normalized_current
    best_metrics = baseline

    for proposal in _candidate_weight_vectors(normalized_current):
        trial_rows = [dict(row) for row in rows]
        metrics = evaluate_weight_vector(trial_rows, proposal, score_key='trial_score')
        better_objective = metrics['objective'] > best_metrics['objective'] + 1e-9
        same_objective_better_p10 = abs(metrics['objective'] - best_metrics['objective']) <= 1e-9 and metrics['precision_at_10'] > best_metrics['precision_at_10']
        if better_objective or same_objective_better_p10:
            best_metrics = metrics
            best_weights = proposal

    improvement = round(float(best_metrics['objective'] - baseline['objective']), 6)
    deltas = {k: round(best_weights[k] - normalized_current[k], 6) for k in COMPONENT_KEYS}
    sorted_deltas = sorted(deltas.items(), key=lambda x: abs(x[1]), reverse=True)
    major_shifts = [{'component': k, 'delta': v} for k, v in sorted_deltas[:3] if abs(v) >= 0.01]

    best_beats_baseline = float(best_metrics['precision_at_10']) > float(mover_rank_baseline_precision_at_10)
    best_monotonic = bool(best_metrics['bucket_monotonicity']['ok'])
    should_apply = improvement >= float(min_improvement) and best_beats_baseline and best_monotonic

    blockers = []
    if improvement < float(min_improvement):
        blockers.append('improvement below minimum threshold')
    if not best_beats_baseline:
        blockers.append('recommended weights still do not beat mover-rank-only baseline at Precision@10')
    if not best_monotonic:
        blockers.append('recommended weights still fail score monotonicity')

    return {
        'eligible': True,
        'reason': '; '.join(blockers) if blockers else None,
        'baseline': baseline,
        'recommended': {
            'weights': best_weights,
            'metrics': best_metrics,
            'major_weight_shifts': major_shifts,
        },
        'should_apply': should_apply,
        'improvement': improvement,
        'baseline_gate': {
            'mover_rank_only_precision_at_10': round(float(mover_rank_baseline_precision_at_10), 4),
            'best_beats_baseline': best_beats_baseline,
            'best_monotonic': best_monotonic,
        },
    }
