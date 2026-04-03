from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from statistics import mean, pstdev
from typing import Any, Dict, Iterable, List, Tuple

import pandas as pd

from app.config import Settings
from app.services.calibration import calibrate_rows
from app.services.validation_engine import ValidationRunRequest, execute_validation_run


@dataclass
class GoalSeekWindow:
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    embargo_days: int
    fold_index: int


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if number != number or number in {float('inf'), float('-inf')}:
        return float(default)
    return float(number)


def _settings_with_overrides(settings: Settings, overrides: Dict[str, object] | None = None, weights: Dict[str, float] | None = None) -> Settings:
    payload = settings.model_dump(mode='python')
    payload.update(dict(overrides or {}))
    if weights:
        payload.update({
            'weight_target_strength': float(weights['target_strength']),
            'weight_liquidity': float(weights['liquidity']),
            'weight_volatility': float(weights['volatility_capacity']),
            'weight_dynamic_range': float(weights['dynamic_range']),
            'weight_range_position': float(weights['range_position']),
            'weight_time_feasibility': float(weights['time_feasibility']),
            'weight_execution_quality': float(weights['execution_quality']),
        })
    return Settings(**payload)


def build_walk_forward_windows(start_date: str, end_date: str, *, train_days: int, test_days: int, step_days: int, embargo_days: int = 1) -> List[GoalSeekWindow]:
    try:
        from app.services.market_time import list_trading_days
        days = list_trading_days(start_date, end_date)
    except Exception:
        days = [d.strftime('%Y-%m-%d') for d in pd.date_range(start=start_date, end=end_date, freq='B')]
    required = int(train_days) + int(embargo_days) + int(test_days)
    if len(days) < required:
        raise ValueError(
            f'Not enough trading days for walk-forward validation. Need at least {required}, found {len(days)}.'
        )
    windows: List[GoalSeekWindow] = []
    fold_index = 1
    start_idx = 0
    while True:
        train_start_idx = start_idx
        train_end_idx = train_start_idx + int(train_days) - 1
        test_start_idx = train_end_idx + int(embargo_days) + 1
        test_end_idx = test_start_idx + int(test_days) - 1
        if test_end_idx >= len(days):
            break
        windows.append(
            GoalSeekWindow(
                train_start=days[train_start_idx],
                train_end=days[train_end_idx],
                test_start=days[test_start_idx],
                test_end=days[test_end_idx],
                embargo_days=int(embargo_days),
                fold_index=fold_index,
            )
        )
        fold_index += 1
        start_idx += int(step_days)
    if not windows:
        raise ValueError('Unable to build any walk-forward windows from the requested range.')
    return windows


def prioritized_goal_seek_configs(settings: Settings, scope: str = 'full') -> List[Dict[str, Any]]:
    current = {
        'min_avg_dollar_volume': float(settings.min_avg_dollar_volume),
        'min_price': float(settings.min_price),
        'low_price_hard_floor': float(settings.low_price_hard_floor),
        'min_completed_cycles_observed': int(settings.min_completed_cycles_observed),
        'max_breakout_close_ratio': float(settings.max_breakout_close_ratio),
        'max_wickiness_ratio': float(settings.max_wickiness_ratio),
        'max_directional_efficiency': float(settings.max_directional_efficiency),
    }
    configs = [
        {'name': 'incumbent', 'overrides': dict(current), 'rationale': 'Current deployed champion.'},
        {'name': 'moderate_relaxation', 'overrides': {**current, 'min_avg_dollar_volume': 1_250_000.0, 'min_price': 1.75, 'low_price_hard_floor': 1.0}, 'rationale': 'Milder liquidity and price relaxation.'},
        {'name': 'strong_relaxation', 'overrides': {**current, 'min_avg_dollar_volume': 1_000_000.0, 'min_price': 1.5, 'low_price_hard_floor': 0.75}, 'rationale': 'Historical gate-audit winner.'},
        {'name': 'liquidity_only_relaxed', 'overrides': {**current, 'min_avg_dollar_volume': 1_000_000.0}, 'rationale': 'Tests whether liquidity alone explains under-selection.'},
        {'name': 'price_only_relaxed', 'overrides': {**current, 'min_price': 1.5, 'low_price_hard_floor': 0.75}, 'rationale': 'Tests whether preferred price floor is overly strict.'},
        {'name': 'strong_plus_cycles_relaxed', 'overrides': {**current, 'min_avg_dollar_volume': 1_000_000.0, 'min_price': 1.5, 'low_price_hard_floor': 0.75, 'min_completed_cycles_observed': 1}, 'rationale': 'Checks whether one-cycle setups improve surfaceability without collapsing quality.'},
        {'name': 'strong_plus_breakout_tighter', 'overrides': {**current, 'min_avg_dollar_volume': 1_000_000.0, 'min_price': 1.5, 'low_price_hard_floor': 0.75, 'max_breakout_close_ratio': 0.10}, 'rationale': 'Recovers quality with stricter breakout control.'},
        {'name': 'strong_plus_chaos_tighter', 'overrides': {**current, 'min_avg_dollar_volume': 1_000_000.0, 'min_price': 1.5, 'low_price_hard_floor': 0.75, 'max_wickiness_ratio': 6.0}, 'rationale': 'Recovers quality with stricter wickiness control.'},
        {'name': 'balanced_quality_relaxed', 'overrides': {**current, 'min_avg_dollar_volume': 1_250_000.0, 'min_price': 1.5, 'low_price_hard_floor': 0.75, 'max_breakout_close_ratio': 0.10}, 'rationale': 'Compromise between candidate flow and breakout discipline.'},
        {'name': 'balanced_cycles_relaxed', 'overrides': {**current, 'min_avg_dollar_volume': 1_250_000.0, 'min_price': 1.5, 'low_price_hard_floor': 0.75, 'min_completed_cycles_observed': 1}, 'rationale': 'Compromise between candidate flow and cycle strictness.'},
    ]
    normalized_scope = str(scope or 'full').strip().lower().replace('-', '_')
    if normalized_scope == 'focused_liquidity':
        keep = {
            'incumbent',
            'moderate_relaxation',
            'strong_relaxation',
            'liquidity_only_relaxed',
            'price_only_relaxed',
            'strong_plus_cycles_relaxed',
        }
        configs = [config for config in configs if config['name'] in keep]
    deduped: List[Dict[str, Any]] = []
    seen: set[Tuple[Tuple[str, object], ...]] = set()
    for config in configs:
        key = tuple(sorted(config['overrides'].items()))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(config)
    return deduped


def _daily_groups(rows: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row['trading_day'])].append(row)
    return grouped


def _average_daily_precision(rows: List[Dict[str, Any]], top_n: int) -> float:
    grouped = _daily_groups([row for row in rows if row.get('advanced_to_stage2')])
    scores: List[float] = []
    for day_rows in grouped.values():
        ranked = sorted(day_rows, key=lambda row: _safe_float(row.get('total_score'), -1.0), reverse=True)[:top_n]
        if ranked:
            scores.append(sum(1 for row in ranked if row.get('hit_target')) / len(ranked))
    return round(mean(scores), 4) if scores else 0.0


def _coverage_score(summary: Dict[str, Any]) -> float:
    days = max(int(summary.get('days') or 0), 1)
    daily = list(summary.get('daily_summaries') or [])
    active_days = sum(1 for row in daily if int(row.get('advanced_count') or 0) > 0)
    active_day_ratio = active_days / days
    avg_advanced_per_active_day = (_safe_float(summary.get('advanced_stage2_total'), 0.0) / active_days) if active_days else 0.0
    active_ratio_score = min(active_day_ratio / 0.35, 1.0)
    density_score = min(avg_advanced_per_active_day / 2.0, 1.0)
    return round((active_ratio_score * 0.65) + (density_score * 0.35), 4)


def _regime_proxy(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    grouped = _daily_groups([row for row in rows if row.get('advanced_to_stage2')])
    if len(grouped) < 4:
        return {'available': False}
    day_leader = []
    for day, day_rows in grouped.items():
        leader_gain = max((_safe_float(row.get('intraday_pct_gain'), 0.0) for row in day_rows), default=0.0)
        day_leader.append((day, leader_gain))
    if not day_leader:
        return {'available': False}
    leader_values = sorted(v for _, v in day_leader)
    median_gain = leader_values[len(leader_values) // 2]
    high_days = {day for day, gain in day_leader if gain >= median_gain}
    low_days = {day for day, gain in day_leader if gain < median_gain}

    def _subset_precision(day_set: set[str]) -> float:
        subset = [row for row in rows if row.get('advanced_to_stage2') and str(row['trading_day']) in day_set]
        return _average_daily_precision(subset, 10)

    high_p10 = _subset_precision(high_days)
    low_p10 = _subset_precision(low_days)
    return {
        'available': True,
        'high_opportunity_precision_at_10': high_p10,
        'low_opportunity_precision_at_10': low_p10,
        'regime_gap': round(abs(high_p10 - low_p10), 4),
        'median_leader_gain_pct': round(median_gain, 3),
    }


def _score_distribution(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    advanced_rows = [row for row in rows if row.get('advanced_to_stage2')]
    scores = sorted(_safe_float(row.get('total_score'), 0.0) for row in advanced_rows)
    if not scores:
        return {'available': False, 'mean': None, 'p90': None}
    p90_idx = min(max(int(round(0.9 * (len(scores) - 1))), 0), len(scores) - 1)
    return {
        'available': True,
        'mean': round(mean(scores), 4),
        'p90': round(scores[p90_idx], 4),
        'count': len(scores),
    }


def build_validation_scorecard(summary: Dict[str, Any], rows: List[Dict[str, Any]], *, train_summary: Dict[str, Any] | None = None, train_rows: List[Dict[str, Any]] | None = None) -> Dict[str, Any]:
    baseline = ((summary.get('baseline_comparison') or {}).get('mover_rank_only') or {})
    precision_at_5 = _safe_float(summary.get('precision_at_5'), 0.0)
    precision_at_10 = _safe_float(summary.get('precision_at_10'), 0.0)
    precision_at_20 = _safe_float(summary.get('precision_at_20'), 0.0)
    overall_hit_rate = _safe_float(summary.get('overall_hit_rate'), 0.0)
    conditional_precision_at_10 = _safe_float(summary.get('conditional_precision_at_10_entry_touched'), 0.0)
    conditional_hit_rate = _safe_float(summary.get('conditional_hit_rate_entry_touched'), 0.0)
    entry_touch_rate = _safe_float(summary.get('entry_touch_rate_stage2'), 0.0)
    monotonicity_ratio = _safe_float((summary.get('score_bucket_monotonicity') or {}).get('ratio'), 0.0)
    mover_rank_p10 = _safe_float(baseline.get('precision_at_10'), 0.0)
    mover_rank_lift = round(precision_at_10 - mover_rank_p10, 4)
    top_rank_superiority = round(max(precision_at_5 - overall_hit_rate, 0.0), 4)
    coverage_score = _coverage_score(summary)
    regime = _regime_proxy(rows)
    score_distribution = _score_distribution(rows)
    train_distribution = _score_distribution(train_rows or []) if train_rows else {'available': False}
    train_precision_at_10 = _safe_float((train_summary or {}).get('precision_at_10'), 0.0)
    train_test_precision_gap = round(abs(train_precision_at_10 - precision_at_10), 4) if train_summary else None
    score_mean_gap = None
    if train_distribution.get('available') and score_distribution.get('available'):
        score_mean_gap = round(abs(_safe_float(train_distribution.get('mean')) - _safe_float(score_distribution.get('mean'))), 4)

    penalties = {
        'train_test_precision_gap': min((train_test_precision_gap or 0.0) / 0.20, 1.0),
        'score_mean_gap': min((score_mean_gap or 0.0) / 20.0, 1.0),
        'regime_gap': min((_safe_float(regime.get('regime_gap'), 0.0)) / 0.20, 1.0) if regime.get('available') else 0.0,
    }
    positives = {
        'precision_at_10': precision_at_10,
        'conditional_precision_at_10_entry_touched': conditional_precision_at_10,
        'mover_rank_lift': max(mover_rank_lift, 0.0),
        'overall_hit_rate': overall_hit_rate,
        'top_rank_superiority': top_rank_superiority,
        'entry_touch_rate': entry_touch_rate,
        'monotonicity_ratio': monotonicity_ratio,
        'coverage_score': coverage_score,
    }
    goal_score = (
        positives['precision_at_10'] * 0.28
        + positives['conditional_precision_at_10_entry_touched'] * 0.20
        + positives['mover_rank_lift'] * 0.14
        + positives['overall_hit_rate'] * 0.10
        + positives['top_rank_superiority'] * 0.08
        + positives['entry_touch_rate'] * 0.08
        + positives['monotonicity_ratio'] * 0.07
        + positives['coverage_score'] * 0.05
        - penalties['train_test_precision_gap'] * 0.05
        - penalties['score_mean_gap'] * 0.02
        - penalties['regime_gap'] * 0.03
    )
    hard_requirements = {
        'beats_mover_rank_p10_by_2pts': precision_at_10 >= mover_rank_p10 + 0.02,
        'tail_reliability_above_0_40': conditional_precision_at_10 >= 0.40,
        'entry_touch_rate_above_0_30': entry_touch_rate >= 0.30,
        'monotonicity_ratio_above_0_60': monotonicity_ratio >= 0.60,
        'minimum_stage2_sample': int(summary.get('advanced_stage2_total') or 0) >= max(10, int(summary.get('days') or 0) // 2),
    }
    return {
        'days': int(summary.get('days') or 0),
        'advanced_stage2_total': int(summary.get('advanced_stage2_total') or 0),
        'precision_at_5': round(precision_at_5, 4),
        'precision_at_10': round(precision_at_10, 4),
        'precision_at_20': round(precision_at_20, 4),
        'overall_hit_rate': round(overall_hit_rate, 4),
        'conditional_hit_rate_entry_touched': round(conditional_hit_rate, 4),
        'conditional_precision_at_10_entry_touched': round(conditional_precision_at_10, 4),
        'entry_touch_rate_stage2': round(entry_touch_rate, 4),
        'mover_rank_precision_at_10': round(mover_rank_p10, 4),
        'mover_rank_lift_precision_at_10': round(mover_rank_lift, 4),
        'top_rank_superiority': round(top_rank_superiority, 4),
        'score_monotonicity_ratio': round(monotonicity_ratio, 4),
        'coverage_score': round(coverage_score, 4),
        'goal_score': round(goal_score, 6),
        'hard_requirements': hard_requirements,
        'hard_requirement_pass_rate': round(sum(1 for ok in hard_requirements.values() if ok) / max(len(hard_requirements), 1), 4),
        'train_test_precision_gap': train_test_precision_gap,
        'score_distribution': score_distribution,
        'score_mean_gap': score_mean_gap,
        'regime_proxy': regime,
    }


def _aggregate_fold_rows(fold_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    goal_scores = [_safe_float(row.get('goal_score'), 0.0) for row in fold_rows]
    p10s = [_safe_float(row.get('precision_at_10'), 0.0) for row in fold_rows]
    cond_p10s = [_safe_float(row.get('conditional_precision_at_10_entry_touched'), 0.0) for row in fold_rows]
    mover_lifts = [_safe_float(row.get('mover_rank_lift_precision_at_10'), 0.0) for row in fold_rows]
    entry_touches = [_safe_float(row.get('entry_touch_rate_stage2'), 0.0) for row in fold_rows]
    failures = [row for row in fold_rows if row.get('hard_requirement_pass_rate', 0.0) < 1.0]
    fold_failure_rate = len(failures) / max(len(fold_rows), 1)
    aggregate_goal_score = (
        mean(goal_scores) if goal_scores else 0.0
    ) - (pstdev(goal_scores) if len(goal_scores) > 1 else 0.0) * 0.15 - fold_failure_rate * 0.10
    return {
        'mean_goal_score': round(mean(goal_scores), 6) if goal_scores else 0.0,
        'aggregate_goal_score': round(aggregate_goal_score, 6),
        'goal_score_std': round(pstdev(goal_scores), 6) if len(goal_scores) > 1 else 0.0,
        'min_goal_score': round(min(goal_scores), 6) if goal_scores else 0.0,
        'mean_precision_at_10': round(mean(p10s), 4) if p10s else 0.0,
        'mean_conditional_precision_at_10_entry_touched': round(mean(cond_p10s), 4) if cond_p10s else 0.0,
        'mean_mover_rank_lift_precision_at_10': round(mean(mover_lifts), 4) if mover_lifts else 0.0,
        'mean_entry_touch_rate_stage2': round(mean(entry_touches), 4) if entry_touches else 0.0,
        'fold_failure_rate': round(fold_failure_rate, 4),
        'folds_with_any_failure': len(failures),
        'fold_count': len(fold_rows),
    }


def _run_validation(settings: Settings, db, alpaca, start_date: str, end_date: str, scan_offset_minutes: int, *, progress_callback=None) -> Dict[str, Any]:
    request = ValidationRunRequest(
        start_date=start_date,
        end_date=end_date,
        scan_offset_minutes=int(scan_offset_minutes),
        cache_history=True,
        persist=False,
    )
    return execute_validation_run(settings, db, alpaca, request, progress_callback=progress_callback)


def run_goal_seek_optimization(run_id: int, settings: Settings, db, alpaca, params: Dict[str, Any], progress_callback) -> Dict[str, Any]:
    offsets = sorted(set(int(v) for v in params.get('offsets') or [120, 150] if int(v) > 0))
    windows = build_walk_forward_windows(
        params['start_date'],
        params['end_date'],
        train_days=int(params['train_days']),
        test_days=int(params['test_days']),
        step_days=int(params['step_days']),
        embargo_days=int(params.get('embargo_days') or 1),
    )
    config_scope = str(params.get('config_scope') or 'full').strip() or 'full'
    configs = prioritized_goal_seek_configs(settings, scope=config_scope)
    experiment_rows: List[Dict[str, Any]] = []
    total_runs = max(len(configs) * len(windows) * max(len(offsets), 1), 1)
    completed = 0
    champion: Dict[str, Any] | None = None

    for config_index, config in enumerate(configs, start=1):
        config_result = {
            'config_name': config['name'],
            'rationale': config['rationale'],
            'overrides': config['overrides'],
            'offset_results': [],
        }
        for offset in offsets:
            fold_results = []
            for window in windows:
                fold_progress_start = 0.02 + (completed / total_runs) * 0.92
                fold_progress_end = 0.02 + ((completed + 1) / total_runs) * 0.92

                def _fold_progress(stage_start: float, stage_end: float, stage_label: str):
                    def _callback(stage_progress: float, stage_message: str) -> None:
                        bounded = max(0.0, min(float(stage_progress), 1.0))
                        scaled = stage_start + ((stage_end - stage_start) * bounded)
                        progress_callback(
                            scaled,
                            f"{config['name']} @ {offset}m — fold {window.fold_index}/{len(windows)} — {stage_label}: {stage_message}",
                        )
                    return _callback

                progress_callback(fold_progress_start, f"{config['name']} @ {offset}m — fold {window.fold_index}/{len(windows)} — train replay starting")
                scenario_settings = _settings_with_overrides(settings, config['overrides'])
                train_payload = _run_validation(
                    scenario_settings,
                    db,
                    alpaca,
                    window.train_start,
                    window.train_end,
                    offset,
                    progress_callback=_fold_progress(fold_progress_start, fold_progress_start + ((fold_progress_end - fold_progress_start) * 0.45), 'train'),
                )
                progress_callback(fold_progress_start + ((fold_progress_end - fold_progress_start) * 0.50), f"{config['name']} @ {offset}m — fold {window.fold_index}/{len(windows)} — calibrating on train window")
                calibration = calibrate_rows(
                    train_payload['rows'],
                    scenario_settings.weights,
                    settings.calibration_min_improvement,
                    mover_rank_baseline_precision_at_10=_safe_float((((train_payload.get('summary') or {}).get('baseline_comparison') or {}).get('mover_rank_only') or {}).get('precision_at_10'), 0.0),
                )
                calibrated_weights = calibration.get('recommended', {}).get('weights') if calibration.get('eligible') and calibration.get('recommended') else None
                test_settings = _settings_with_overrides(scenario_settings, weights=calibrated_weights if calibration.get('should_apply') else None)
                progress_callback(fold_progress_start + ((fold_progress_end - fold_progress_start) * 0.55), f"{config['name']} @ {offset}m — fold {window.fold_index}/{len(windows)} — test replay starting")
                test_payload = _run_validation(
                    test_settings,
                    db,
                    alpaca,
                    window.test_start,
                    window.test_end,
                    offset,
                    progress_callback=_fold_progress(fold_progress_start + ((fold_progress_end - fold_progress_start) * 0.55), fold_progress_start + ((fold_progress_end - fold_progress_start) * 0.95), 'test'),
                )
                progress_callback(fold_progress_start + ((fold_progress_end - fold_progress_start) * 0.97), f"{config['name']} @ {offset}m — fold {window.fold_index}/{len(windows)} — scoring fold results")
                scorecard = build_validation_scorecard(
                    test_payload['summary'],
                    test_payload['rows'],
                    train_summary=train_payload['summary'],
                    train_rows=train_payload['rows'],
                )
                fold_row = {
                    'fold_index': window.fold_index,
                    'train_start': window.train_start,
                    'train_end': window.train_end,
                    'test_start': window.test_start,
                    'test_end': window.test_end,
                    'offset': int(offset),
                    'config_name': config['name'],
                    'calibration_applied': bool(calibration.get('should_apply')),
                    'calibration_improvement': round(_safe_float(calibration.get('improvement'), 0.0), 6),
                    'calibration_reason': calibration.get('reason'),
                    **scorecard,
                }
                fold_results.append(fold_row)
                completed += 1
            offset_aggregate = _aggregate_fold_rows(fold_results)
            config_result['offset_results'].append({
                'scan_offset_minutes': int(offset),
                'folds': fold_results,
                'aggregate': offset_aggregate,
            })
        all_fold_rows = [row for offset_result in config_result['offset_results'] for row in offset_result['folds']]
        config_result['aggregate'] = _aggregate_fold_rows(all_fold_rows)
        config_result['wins'] = {
            'mean_goal_score': config_result['aggregate']['mean_goal_score'],
            'aggregate_goal_score': config_result['aggregate']['aggregate_goal_score'],
            'mean_precision_at_10': config_result['aggregate']['mean_precision_at_10'],
            'mean_conditional_precision_at_10_entry_touched': config_result['aggregate']['mean_conditional_precision_at_10_entry_touched'],
            'mean_mover_rank_lift_precision_at_10': config_result['aggregate']['mean_mover_rank_lift_precision_at_10'],
            'mean_entry_touch_rate_stage2': config_result['aggregate']['mean_entry_touch_rate_stage2'],
            'goal_score_std': config_result['aggregate']['goal_score_std'],
            'fold_failure_rate': config_result['aggregate']['fold_failure_rate'],
        }
        experiment_rows.append(config_result)
        if champion is None or config_result['aggregate']['aggregate_goal_score'] > champion['aggregate']['aggregate_goal_score'] + 1e-9:
            champion = config_result

    ranked = sorted(experiment_rows, key=lambda row: row['aggregate']['aggregate_goal_score'], reverse=True)
    champion = ranked[0] if ranked else None
    goal_standard = {
        'mean_precision_at_10_min': 0.45,
        'mean_conditional_precision_at_10_entry_touched_min': 0.45,
        'mean_mover_rank_lift_precision_at_10_min': 0.02,
        'mean_entry_touch_rate_stage2_min': 0.30,
        'fold_failure_rate_max': 0.34,
    }
    goal_standard_met = bool(
        champion
        and champion['aggregate']['mean_precision_at_10'] >= goal_standard['mean_precision_at_10_min']
        and champion['aggregate']['mean_conditional_precision_at_10_entry_touched'] >= goal_standard['mean_conditional_precision_at_10_entry_touched_min']
        and champion['aggregate']['mean_mover_rank_lift_precision_at_10'] >= goal_standard['mean_mover_rank_lift_precision_at_10_min']
        and champion['aggregate']['mean_entry_touch_rate_stage2'] >= goal_standard['mean_entry_touch_rate_stage2_min']
        and champion['aggregate']['fold_failure_rate'] <= goal_standard['fold_failure_rate_max']
    )
    architecture_blocker = not goal_standard_met and bool(champion) and champion['aggregate']['mean_precision_at_10'] < 0.35
    recommendation = {
        'config_name': champion['config_name'] if champion else None,
        'overrides': champion['overrides'] if champion else {},
        'rationale': champion['rationale'] if champion else 'No champion produced.',
        'goal_standard_met': goal_standard_met,
        'architecture_blocker': architecture_blocker,
        'next_action': 'promote_champion_to_live_trial' if goal_standard_met else 'hold_live_policy_and_simplify_scoring' if architecture_blocker else 'review_best_config_and_continue_offline_search',
    }
    progress_callback(0.98, 'Assembling champion–challenger report.')
    return {
        'mode': 'offline_goal_seek_optimization',
        'summary': {
            'window_count': len(windows),
            'offsets': offsets,
            'config_count': len(configs),
            'config_scope': config_scope,
            'goal_standard': goal_standard,
            'goal_standard_met': goal_standard_met,
            'architecture_blocker': architecture_blocker,
            'best_config_name': champion['config_name'] if champion else None,
            'best_aggregate_goal_score': champion['aggregate']['aggregate_goal_score'] if champion else None,
        },
        'goal_scorecard_definition': {
            'load_bearing_metrics': [
                'precision_at_10',
                'conditional_precision_at_10_entry_touched',
                'mover_rank_lift_precision_at_10',
                'entry_touch_rate_stage2',
                'score_monotonicity_ratio',
                'coverage_score',
                'train_test_precision_gap',
                'regime_proxy.regime_gap',
            ],
            'composite_goal_score_formula': '0.28*p10 + 0.20*conditional_p10_touch + 0.14*positive_mover_lift + 0.10*overall_hit + 0.08*top_rank_superiority + 0.08*entry_touch + 0.07*monotonicity + 0.05*coverage - 0.05*train_test_gap - 0.02*score_mean_gap - 0.03*regime_gap',
        },
        'walk_forward_windows': [window.__dict__ for window in windows],
        'ranked_configs': ranked,
        'champion_config': champion,
        'recommended_configuration': recommendation,
    }
