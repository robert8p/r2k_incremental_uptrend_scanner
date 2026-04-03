from __future__ import annotations

from typing import Dict, Tuple

import numpy as np
import pandas as pd

from app.config import Settings
from app.services.adaptive_range import AdaptiveRangeResult, analyze_incremental_range


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if number != number or number in {float('inf'), float('-inf')}:
        return float(default)
    return float(number)


def _safe_round(value: object, digits: int = 4, default: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in {float('inf'), float('-inf')}:
        return default
    return round(number, digits)


def _safe_weighted_total(component_scores: Dict[str, float], weights: Dict[str, float]) -> float:
    total = 0.0
    for key, component in component_scores.items():
        total += _safe_float(component, 0.0) * _safe_float(weights.get(key, 0.0), 0.0)
    return float(total)


def _compute_atr_like(df: pd.DataFrame) -> float:
    prev_close = df['close'].shift(1)
    tr = pd.concat([
        df['high'] - df['low'],
        (df['high'] - prev_close).abs(),
        (df['low'] - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.tail(min(14, len(tr))).mean()
    current_price = float(df['close'].iloc[-1])
    return float((atr / max(current_price, 1e-9)) * 100.0)


def _score_target_group_membership(row: pd.Series, top_stage1: pd.DataFrame) -> Tuple[float, Dict[str, float]]:
    leader = float(top_stage1['intraday_pct_gain'].max()) if not top_stage1.empty else max(float(row['intraday_pct_gain']), 1.0)
    rank_pct = 1.0 - ((float(row['mover_rank']) - 1.0) / max(len(top_stage1) - 1, 1))
    gain_score = np.clip((float(row['intraday_pct_gain']) / max(leader, 1e-9)) * 100.0, 0.0, 100.0)
    rank_score = np.clip(rank_pct * 100.0, 0.0, 100.0)
    score = np.clip(gain_score * 0.35 + rank_score * 0.65, 0.0, 100.0)
    return float(score), {
        'gain_score': round(float(gain_score), 2),
        'rank_score': round(float(rank_score), 2),
        'distance_from_leader_pct': round(float(row['distance_from_leader_pct']), 3),
    }


def _score_liquidity(current_price: float, cum_volume: float, avg_daily_volume: float, avg_daily_dollar_volume: float, spread_bps: float, settings: Settings) -> Tuple[float, Dict[str, float]]:
    intraday_dollar = current_price * cum_volume
    rvol = cum_volume / max(avg_daily_volume / 6.5, 1.0)
    adv_score = np.clip(np.log10(max(avg_daily_dollar_volume, 1.0)) * 15.0 - 55.0, 0.0, 100.0)
    intra_score = np.clip(np.log10(max(intraday_dollar, 1.0)) * 15.0 - 50.0, 0.0, 100.0)
    rvol_score = np.clip((rvol - 0.8) * 35.0, 0.0, 100.0)
    spread_penalty = np.clip((spread_bps - 15.0) * 1.6, 0.0, 55.0)
    low_price_penalty = 25.0 if current_price < settings.min_price else 0.0
    trade_adv_confidence = np.clip((np.log10(max(avg_daily_dollar_volume, 1.0)) - 6.7) * 70.0, 0.0, 100.0)
    trade_rvol_confidence = np.clip((rvol - 1.2) * 32.0, 0.0, 100.0)
    trade_liquidity_confidence = np.clip(trade_adv_confidence * 0.65 + trade_rvol_confidence * 0.35 - spread_penalty * 0.25, 0.0, 100.0)
    score = np.clip(adv_score * 0.33 + intra_score * 0.22 + rvol_score * 0.30 + trade_liquidity_confidence * 0.15 - spread_penalty - low_price_penalty, 0.0, 100.0)
    return float(score), {
        'relative_volume': round(float(rvol), 3),
        'intraday_dollar_volume': round(float(intraday_dollar), 2),
        'spread_penalty': round(float(spread_penalty), 2),
        'low_price_penalty': round(float(low_price_penalty), 2),
        'adv_score': round(float(adv_score), 2),
        'intraday_score': round(float(intra_score), 2),
        'rvol_score': round(float(rvol_score), 2),
        'trade_liquidity_confidence': round(float(trade_liquidity_confidence), 2),
    }


def _score_volatility(df: pd.DataFrame, atr_pct: float, range_result: AdaptiveRangeResult) -> Tuple[float, Dict[str, float]]:
    returns = df['close'].pct_change().fillna(0.0)
    realized_vol = returns.std() * np.sqrt(max(len(df), 1)) * 100.0
    session_range_pct = (df['high'].max() / max(df['low'].min(), 1e-9) - 1.0) * 100.0
    mean_bar_range_pct = ((df['high'] - df['low']) / df['close'].replace(0, np.nan) * 100.0).tail(min(15, len(df))).mean()
    oscillation_score = np.clip(mean_bar_range_pct * 55.0, 0.0, 100.0)
    realized_score = np.clip(realized_vol * 16.0, 0.0, 100.0)
    atr_score = np.clip(atr_pct * 16.0, 0.0, 100.0)
    width_score = np.clip((range_result.band_width_pct - 0.8) * 28.0, 0.0, 100.0)
    chaos_penalty = np.clip(max(range_result.wickiness_ratio - 6.0, 0.0) * 7.0 + range_result.breakout_close_ratio * 180.0, 0.0, 45.0)
    bounce_bonus = np.clip(range_result.bounce_quality_score * 0.25, 0.0, 20.0)
    score = np.clip(oscillation_score * 0.22 + realized_score * 0.18 + atr_score * 0.18 + width_score * 0.32 + bounce_bonus - chaos_penalty, 0.0, 100.0)
    return float(score), {
        'realized_vol_pct': round(float(realized_vol), 3),
        'session_range_pct': round(float(session_range_pct), 3),
        'mean_bar_range_pct': round(float(mean_bar_range_pct), 3),
        'oscillation_score': round(float(oscillation_score), 2),
        'realized_score': round(float(realized_score), 2),
        'atr_score': round(float(atr_score), 2),
        'width_score': round(float(width_score), 2),
        'bounce_bonus': round(float(bounce_bonus), 2),
        'chaos_penalty': round(float(chaos_penalty), 2),
    }


def _score_range_position(range_result: AdaptiveRangeResult) -> Tuple[float, Dict[str, float]]:
    loc = range_result.current_location
    base = 100.0 - abs(loc - 0.24) * 190.0
    if loc > 0.42:
        base -= (loc - 0.42) * 130.0
    score = np.clip(base, 0.0, 100.0)
    return float(score), {'location_within_band': round(float(loc), 3)}


def _score_range_tradability(range_result: AdaptiveRangeResult, entry_reference: float, target_pct: float, spread_bps: float) -> Tuple[float, Dict[str, float]]:
    gross_headroom_pct = (range_result.band_high / max(entry_reference, 1e-9) - 1.0) * 100.0
    trading_friction_pct = max(spread_bps / 10000.0 * 100.0 * 1.5, 0.05)
    effective_headroom_pct = max(gross_headroom_pct - trading_friction_pct, 0.0)
    width_multiple = range_result.band_width_pct / max(target_pct, 1e-9)
    width_score = np.clip((width_multiple - 1.0) * 50.0, 0.0, 100.0)
    headroom_score = np.clip((effective_headroom_pct - target_pct * 0.9) * 60.0, 0.0, 100.0)
    cycle_score = np.clip((range_result.completed_cycles - 1.0) * 42.0, 0.0, 100.0)
    touch_score = np.clip((min(range_result.lower_zone_touch_count, range_result.upper_zone_touch_count) - 1.0) * 35.0, 0.0, 100.0)
    containment_score = np.clip((range_result.containment_ratio - 0.45) * 150.0, 0.0, 100.0)
    efficiency_score = np.clip(100.0 - abs(range_result.directional_efficiency - 0.35) * 170.0, 0.0, 100.0)
    durability_score = np.clip(range_result.cycle_durability_score, 0.0, 100.0)
    bounce_quality_score = np.clip(range_result.bounce_quality_score, 0.0, 100.0)
    bounce_mfe_score = np.clip((range_result.recent_median_bounce_mfe_pct - target_pct * 0.70) * 70.0, 0.0, 100.0)
    bounce_payoff_score = np.clip((range_result.recent_bounce_payoff_ratio - 1.0) * 35.0, 0.0, 100.0)
    upper_reach_score = np.clip(range_result.recent_shrunk_upper_reach_ratio * 120.0, 0.0, 100.0)
    target_hit_score = np.clip(range_result.recent_shrunk_target_hit_ratio * 150.0, 0.0, 100.0)
    confidence_score = np.clip(range_result.recent_bounce_observation_confidence, 0.0, 100.0)
    edge_score = np.clip((range_result.recent_bounce_edge_pct - 0.35) * 55.0, 0.0, 100.0)
    target_excess_score = np.clip((range_result.recent_target_excess_pct + 0.10) * 70.0, 0.0, 100.0)
    downside_penalty = np.clip((max(-range_result.recent_median_bounce_mae_pct, 0.0) - 0.8) * 45.0, 0.0, 45.0)
    breakout_penalty = np.clip(range_result.breakout_close_ratio * 260.0 + range_result.recent_breakout_close_ratio * 180.0, 0.0, 70.0)
    wick_penalty = np.clip(max(range_result.wickiness_ratio - 5.0, 0.0) * 8.0 + max(range_result.recent_wickiness_ratio - 5.0, 0.0) * 6.0, 0.0, 60.0)
    degradation_penalty = np.clip(range_result.degradation_score * 0.45, 0.0, 45.0)
    score = np.clip(
        width_score * 0.12
        + headroom_score * 0.14
        + cycle_score * 0.14
        + touch_score * 0.11
        + containment_score * 0.08
        + efficiency_score * 0.07
        + durability_score * 0.20
        + bounce_quality_score * 0.14
        - breakout_penalty
        - wick_penalty
        - degradation_penalty
        - downside_penalty,
        0.0,
        100.0,
    )
    return float(score), {
        'gross_headroom_pct': round(float(gross_headroom_pct), 3),
        'effective_headroom_pct': round(float(effective_headroom_pct), 3),
        'within_range_target_possible': effective_headroom_pct >= target_pct,
        'width_multiple_vs_target': round(float(width_multiple), 3),
        'width_score': round(float(width_score), 2),
        'headroom_score': round(float(headroom_score), 2),
        'cycle_score': round(float(cycle_score), 2),
        'touch_score': round(float(touch_score), 2),
        'containment_score': round(float(containment_score), 2),
        'efficiency_score': round(float(efficiency_score), 2),
        'durability_score': round(float(durability_score), 2),
        'bounce_quality_score': round(float(bounce_quality_score), 2),
        'bounce_mfe_score': round(float(bounce_mfe_score), 2),
        'bounce_payoff_score': round(float(bounce_payoff_score), 2),
        'upper_reach_score': round(float(upper_reach_score), 2),
        'target_hit_score': round(float(target_hit_score), 2),
        'confidence_score': round(float(confidence_score), 2),
        'edge_score': round(float(edge_score), 2),
        'target_excess_score': round(float(target_excess_score), 2),
        'downside_penalty': round(float(downside_penalty), 2),
        'breakout_penalty': round(float(breakout_penalty), 2),
        'wick_penalty': round(float(wick_penalty), 2),
        'degradation_penalty': round(float(degradation_penalty), 2),
    }


def _score_time_feasibility(df: pd.DataFrame, range_result: AdaptiveRangeResult, target_pct: float, minutes_remaining_to_close: int, buffer_before_close: int) -> Tuple[float, Dict[str, float]]:
    trade_window_remaining = max(int(minutes_remaining_to_close) - int(buffer_before_close), 0)
    observed_minutes = max(len(df) - 1, 1)
    minutes_per_completed_cycle = observed_minutes / max(range_result.completed_cycles, 1)
    estimated_cycles_remaining = trade_window_remaining / max(minutes_per_completed_cycle, 1.0)
    mean_abs_close_move_pct = float(df['close'].pct_change().abs().tail(min(20, len(df))).mean() * 100.0)
    required_pace = target_pct / max(trade_window_remaining, 1)
    pace_ratio = mean_abs_close_move_pct / max(required_pace, 1e-6)
    runway_score = np.clip((trade_window_remaining - 40.0) * 0.8, 0.0, 100.0)
    cycle_score = np.clip((estimated_cycles_remaining - 0.8) * 60.0, 0.0, 100.0)
    pace_score = np.clip(pace_ratio * 20.0, 0.0, 100.0)
    score = np.clip(runway_score * 0.30 + cycle_score * 0.50 + pace_score * 0.20, 0.0, 100.0)
    return float(score), {
        'trade_window_minutes_remaining': int(trade_window_remaining),
        'minutes_per_completed_cycle': round(float(minutes_per_completed_cycle), 3),
        'estimated_cycles_remaining': round(float(estimated_cycles_remaining), 3),
        'mean_abs_close_move_pct': round(float(mean_abs_close_move_pct), 4),
        'required_pace_pct_per_min': round(float(required_pace), 5),
        'pace_ratio': round(float(pace_ratio), 3),
        'runway_score': round(float(runway_score), 2),
        'cycle_score': round(float(cycle_score), 2),
        'pace_score': round(float(pace_score), 2),
    }


def _score_execution_quality(spread_bps: float, rvol: float, current_location: float, range_result: AdaptiveRangeResult) -> Tuple[float, Dict[str, float]]:
    spread_score = np.clip(100.0 - max(spread_bps - 5.0, 0.0) * 2.4, 0.0, 100.0)
    rvol_score = np.clip((rvol - 0.8) * 30.0, 0.0, 100.0)
    chase_penalty = np.clip(max(current_location - 0.65, 0.0) * 95.0, 0.0, 50.0)
    breakout_penalty = np.clip(range_result.breakout_close_ratio * 220.0, 0.0, 55.0)
    wick_penalty = np.clip(max(range_result.wickiness_ratio - 6.0, 0.0) * 7.0, 0.0, 45.0)
    bounce_penalty = np.clip((1.0 - range_result.recent_bounce_payoff_ratio) * 18.0 + (0.38 - range_result.recent_shrunk_target_hit_ratio) * 50.0 + (45.0 - range_result.recent_bounce_observation_confidence) * 0.25, 0.0, 40.0)
    classification_penalty = 0.0 if range_result.classification in {'A', 'B'} else 40.0
    score = np.clip(
        spread_score * 0.48 + rvol_score * 0.34 + np.clip(range_result.dynamic_range_score, 0.0, 100.0) * 0.18
        - chase_penalty - breakout_penalty - wick_penalty - classification_penalty,
        0.0,
        100.0,
    )
    return float(score), {
        'spread_score': round(float(spread_score), 2),
        'rvol_score': round(float(rvol_score), 2),
        'chase_penalty': round(float(chase_penalty), 2),
        'breakout_penalty': round(float(breakout_penalty), 2),
        'wick_penalty': round(float(wick_penalty), 2),
        'bounce_penalty': round(float(bounce_penalty), 2),
        'classification_penalty': round(float(classification_penalty), 2),
    }



def _entry_proximity_score(distance_to_entry_pct: float) -> float:
    # distance_to_entry_pct is negative when current price sits above the preferred entry.
    # We want to heavily favor names already at the entry zone, mildly penalize small stretch,
    # and sharply demote names that would require a larger pullback first.
    if distance_to_entry_pct >= 0:
        return 100.0
    stretch = abs(float(distance_to_entry_pct))
    if stretch <= 0.20:
        return float(np.clip(100.0 - stretch * 120.0, 0.0, 100.0))
    return float(np.clip(76.0 - (stretch - 0.20) * 90.0, 0.0, 100.0))



def _score_entry_touch_likelihood(metrics: Dict[str, object]) -> float:
    distance_pct = _safe_float(metrics.get('distance_to_entry_pct'), -99.0)
    proximity_score = _entry_proximity_score(distance_pct)
    location = _safe_float(metrics.get('range_current_location'), 1.0)
    location_score = np.clip(100.0 - abs(location - 0.22) * 170.0, 0.0, 100.0)
    lower_touch_score = np.clip((_safe_float(metrics.get('recent_lower_zone_touch_count'), 0.0) - 0.5) * 45.0, 0.0, 100.0)
    transition_score = np.clip((_safe_float(metrics.get('recent_zone_transition_count'), 0.0) - 0.5) * 35.0, 0.0, 100.0)
    bias = _safe_float(metrics.get('recent_close_location_bias'), 0.5)
    bias_score = np.clip(100.0 - max(bias - 0.52, 0.0) * 150.0 - abs(min(bias - 0.15, 0.0)) * 70.0, 0.0, 100.0)
    cycle_time = _safe_float(metrics.get('minutes_per_completed_cycle'), 999.0)
    cycle_time_score = np.clip((45.0 - cycle_time) * 3.0, 0.0, 100.0)
    cycles_remaining_score = np.clip((_safe_float(metrics.get('estimated_cycles_remaining'), 0.0) - 0.6) * 55.0, 0.0, 100.0)
    confidence_score = np.clip(_safe_float(metrics.get('recent_bounce_observation_confidence'), 0.0), 0.0, 100.0)
    liquidity_score = np.clip(_safe_float(metrics.get('trade_liquidity_confidence'), 0.0), 0.0, 100.0)
    score = np.clip(
        proximity_score * 0.34
        + location_score * 0.18
        + lower_touch_score * 0.10
        + transition_score * 0.08
        + bias_score * 0.10
        + cycle_time_score * 0.06
        + cycles_remaining_score * 0.06
        + confidence_score * 0.04
        + liquidity_score * 0.04,
        0.0,
        100.0,
    )
    return float(score)


def _estimate_minutes_to_touch(metrics: Dict[str, object]) -> float:
    distance_pct = _safe_float(metrics.get('distance_to_entry_pct'), -99.0)
    if distance_pct >= 0:
        return 0.0
    stretch = abs(distance_pct)
    cycle_minutes = max(_safe_float(metrics.get('minutes_per_completed_cycle'), 30.0), 5.0)
    width_pct = max(_safe_float(metrics.get('range_band_width_pct'), 0.60), 0.05)
    mean_bar_range_pct = max(_safe_float(metrics.get('mean_bar_range_pct'), width_pct / 6.0), 0.03)
    location = _safe_float(metrics.get('range_current_location'), 0.50)
    recent_lower = max(_safe_float(metrics.get('recent_lower_zone_touch_count'), 0.0), 0.0)
    bias = _safe_float(metrics.get('recent_close_location_bias'), 0.5)

    stretch_fraction = stretch / max(width_pct * 0.45, 0.08)
    bar_factor = np.clip(stretch / max(mean_bar_range_pct, 0.03), 0.50, 4.00)
    recency_factor = np.clip(1.10 - recent_lower * 0.08, 0.72, 1.08)
    location_factor = np.clip(0.82 + max(location - 0.24, 0.0) * 0.85, 0.82, 1.35)
    bias_factor = np.clip(0.88 + max(bias - 0.48, 0.0) * 0.70, 0.88, 1.22)

    expected = max(cycle_minutes * stretch_fraction * 0.55 + cycle_minutes * 0.20 + bar_factor * 2.5, 1.0)
    expected *= recency_factor * location_factor * bias_factor
    return float(np.clip(expected, 0.0, 999.0))


def _score_touch_urgency(metrics: Dict[str, object], entry_touch_score: float) -> float:
    expected_minutes = _estimate_minutes_to_touch(metrics)
    trade_window = max(_safe_float(metrics.get('trade_window_minutes_remaining'), 0.0), 0.0)
    proximity_score = _entry_proximity_score(_safe_float(metrics.get('distance_to_entry_pct'), -99.0))
    eta_score = np.clip((48.0 - expected_minutes) * 2.2, 0.0, 100.0)
    viability_score = np.clip(((trade_window - expected_minutes) / max(trade_window, 1.0)) * 140.0, 0.0, 100.0) if trade_window > 0 else 0.0
    score = np.clip(
        eta_score * 0.42
        + viability_score * 0.28
        + proximity_score * 0.18
        + np.clip(entry_touch_score, 0.0, 100.0) * 0.12,
        0.0,
        100.0,
    )
    return float(score)


def _touch_window_band(expected_minutes: float, trade_window_minutes_remaining: float) -> str:
    if expected_minutes <= 2.0:
        return 'at_entry'
    if expected_minutes <= 15.0:
        return 'touch_soon'
    if expected_minutes <= 35.0:
        return 'touch_viable'
    if expected_minutes <= max(trade_window_minutes_remaining, 0.0):
        return 'touch_late'
    return 'unlikely_in_window'


def _score_queue_priority(expected_actionability_score: float, touch_urgency_score: float, follow_through_score: float, structural_score: float) -> float:
    return float(np.clip(
        np.clip(expected_actionability_score, 0.0, 100.0) * 0.42
        + np.clip(touch_urgency_score, 0.0, 100.0) * 0.30
        + np.clip(follow_through_score, 0.0, 100.0) * 0.18
        + np.clip(structural_score, 0.0, 100.0) * 0.10,
        0.0,
        100.0,
    ))


def _execution_lane_for_book(recommendation_book: str) -> tuple[str, int]:
    book = str(recommendation_book or '').strip().lower()
    if book == 'actionable_now':
        return 'actionable_now', 0
    if book == 'touch_soon_queue':
        return 'monitor_5m', 5
    if book == 'touch_later_queue':
        return 'monitor_15m', 15
    if book in {'structural_watchlist', 'rejected'}:
        return 'passive_watchlist', 0
    return 'passive_watchlist', 0


def _score_expected_actionability(entry_touch_score: float, follow_through_score: float, structural_score: float, touch_urgency_score: float, metrics: Dict[str, object]) -> float:
    entry_prob = np.clip(entry_touch_score / 100.0, 0.0, 1.0)
    follow_prob = np.clip(follow_through_score / 100.0, 0.0, 1.0)
    conversion_score = np.sqrt(entry_prob * follow_prob) * 100.0
    edge_score = np.clip((_safe_float(metrics.get('recent_target_excess_pct'), -1.0) + 0.10) * 70.0, 0.0, 100.0)
    payoff_score = np.clip((_safe_float(metrics.get('recent_bounce_payoff_ratio'), 0.0) - 1.0) * 35.0, 0.0, 100.0)
    net_score = np.clip(_safe_float(metrics.get('net_headroom_score_for_tier'), 0.0), 0.0, 100.0)
    score = np.clip(
        conversion_score * 0.44
        + edge_score * 0.18
        + payoff_score * 0.12
        + entry_touch_score * 0.10
        + follow_through_score * 0.08
        + np.clip(touch_urgency_score, 0.0, 100.0) * 0.10
        + np.clip(structural_score, 0.0, 100.0) * 0.04,
        0.0,
        100.0,
    )
    return float((score * 0.85) + net_score * 0.15)


def _score_follow_through_confidence(metrics: Dict[str, object]) -> float:
    target_excess_score = np.clip((_safe_float(metrics.get('recent_target_excess_pct'), -1.0) + 0.10) * 70.0, 0.0, 100.0)
    target_hit_score = np.clip(_safe_float(metrics.get('recent_shrunk_target_hit_ratio'), 0.0) * 150.0, 0.0, 100.0)
    upper_reach_score = np.clip(_safe_float(metrics.get('recent_shrunk_upper_reach_ratio'), 0.0) * 120.0, 0.0, 100.0)
    bounce_mfe_score = np.clip((_safe_float(metrics.get('recent_median_bounce_mfe_pct'), 0.0) - 0.70) * 70.0, 0.0, 100.0)
    bounce_drawdown_score = np.clip((1.05 - max(abs(_safe_float(metrics.get('recent_median_bounce_mae_pct'), 0.0)), 0.0)) / 1.05 * 100.0, 0.0, 100.0)
    confidence_score = np.clip(_safe_float(metrics.get('recent_bounce_observation_confidence'), 0.0), 0.0, 100.0)
    time_score = np.clip((_safe_float(metrics.get('estimated_cycles_remaining'), 0.0) - 0.8) * 55.0, 0.0, 100.0)
    score = np.clip(
        target_excess_score * 0.20
        + target_hit_score * 0.20
        + upper_reach_score * 0.14
        + bounce_mfe_score * 0.18
        + bounce_drawdown_score * 0.10
        + confidence_score * 0.10
        + time_score * 0.08,
        0.0,
        100.0,
    )
    return float(score)


def _score_actionability(execution_readiness_score: float, follow_through_score: float, structural_score: float) -> float:
    score = np.clip(
        execution_readiness_score * 0.54
        + follow_through_score * 0.34
        + np.clip(structural_score, 0.0, 100.0) * 0.12,
        0.0,
        100.0,
    )
    return float(score)




def _score_pullback_queue_rank(metrics: Dict[str, object], structural_score: float, entry_touch_score: float, follow_through_score: float, expected_actionability_score: float, touch_urgency_score: float, settings: Settings) -> float:
    distance_pct = _safe_float(metrics.get('distance_to_entry_pct'), -99.0)
    if distance_pct >= 0:
        queue_distance_score = 100.0
    else:
        stretch = abs(distance_pct)
        queue_distance_score = float(np.clip(100.0 - max(stretch - 0.15, 0.0) * 32.0, 0.0, 100.0))
    score = (
        float(settings.pullback_queue_rank_actionability_weight) * np.clip(expected_actionability_score, 0.0, 100.0)
        + float(settings.pullback_queue_rank_entry_touch_weight) * np.clip(entry_touch_score, 0.0, 100.0)
        + float(settings.pullback_queue_rank_follow_through_weight) * np.clip(follow_through_score, 0.0, 100.0)
        + float(settings.pullback_queue_rank_structural_weight) * np.clip(structural_score, 0.0, 100.0)
        + float(settings.pullback_queue_rank_distance_weight) * queue_distance_score
        + float(settings.pullback_queue_rank_touch_urgency_weight) * np.clip(touch_urgency_score, 0.0, 100.0)
    )
    return float(np.clip(score, 0.0, 100.0))
def _determine_recommendation_tier(metrics: Dict[str, object], settings: Settings, advanced_to_stage2: bool) -> tuple[str, str, float, float, float, float, float, float, float, str, float]:
    if not advanced_to_stage2:
        return 'rejected', 'rejected', 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 999.0, 'unlikely_in_window', 0.0
    entry_touch_likelihood_score = _score_entry_touch_likelihood(metrics)
    expected_minutes_to_touch = _estimate_minutes_to_touch(metrics)
    touch_urgency_score = _score_touch_urgency(metrics, entry_touch_likelihood_score)
    entry_proximity_score = _entry_proximity_score(_safe_float(metrics.get('distance_to_entry_pct'), -99.0))
    trade_window_remaining = _safe_float(metrics.get('trade_window_minutes_remaining'), 0.0)
    touch_window_band = _touch_window_band(expected_minutes_to_touch, trade_window_remaining)
    readiness_score = float(np.clip(
        _safe_float(metrics.get('range_position_score_for_tier'), 0.0) * 0.30
        + entry_proximity_score * 0.28
        + entry_touch_likelihood_score * 0.24
        + _safe_float(metrics.get('recent_bounce_observation_confidence'), 0.0) * 0.04
        + _safe_float(metrics.get('recent_shrunk_target_hit_ratio'), 0.0) * 100.0 * 0.06
        + _safe_float(metrics.get('trade_liquidity_confidence'), 0.0) * 0.04
        + _safe_float(metrics.get('recent_target_excess_pct'), -1.0) * 6.0,
        0.0,
        100.0,
    ))
    follow_through_score = _score_follow_through_confidence(metrics)
    structural_score = _safe_float(metrics.get('structural_score'), 0.0)
    expected_actionability_score = _score_expected_actionability(entry_touch_likelihood_score, follow_through_score, structural_score, touch_urgency_score, metrics)
    actionability_score = float(np.clip(
        expected_actionability_score * 0.68
        + readiness_score * 0.14
        + np.clip(touch_urgency_score, 0.0, 100.0) * 0.10
        + np.clip(structural_score, 0.0, 100.0) * 0.08,
        0.0,
        100.0,
    ))
    pullback_queue_rank = _score_pullback_queue_rank(metrics, structural_score, entry_touch_likelihood_score, follow_through_score, expected_actionability_score, touch_urgency_score, settings)
    queue_priority_score = _score_queue_priority(expected_actionability_score, touch_urgency_score, follow_through_score, structural_score)

    headline = (
        _safe_float(metrics.get('range_current_location'), 1.0) <= float(settings.headline_max_range_location)
        and _safe_float(metrics.get('distance_to_entry_pct'), -99.0) >= -float(settings.headline_max_distance_to_entry_pct)
        and _safe_float(metrics.get('recent_target_excess_pct'), -99.0) >= float(settings.headline_min_recent_target_excess_pct)
        and expected_minutes_to_touch <= float(settings.headline_max_expected_minutes_to_touch)
        and readiness_score >= float(settings.headline_min_execution_readiness_score)
        and follow_through_score >= float(settings.headline_min_follow_through_confidence_score)
        and entry_touch_likelihood_score >= float(settings.headline_min_entry_touch_likelihood_score)
        and expected_actionability_score >= float(settings.headline_min_expected_actionability_score)
        and actionability_score >= float(settings.headline_min_actionability_score)
    )
    if headline:
        return 'headline_shortlist', 'actionable_now', readiness_score, follow_through_score, actionability_score, entry_touch_likelihood_score, touch_urgency_score, pullback_queue_rank, expected_minutes_to_touch, touch_window_band, queue_priority_score

    ready_now = (
        _safe_float(metrics.get('range_current_location'), 1.0) <= float(settings.ready_now_max_range_location)
        and _safe_float(metrics.get('distance_to_entry_pct'), -99.0) >= -float(settings.ready_now_max_distance_to_entry_pct)
        and expected_minutes_to_touch <= float(settings.ready_now_max_expected_minutes_to_touch)
        and _safe_float(metrics.get('recent_bounce_observation_confidence'), 0.0) >= float(settings.ready_now_min_recent_bounce_confidence_score)
        and int(_safe_float(metrics.get('recent_bounce_event_count'), 0.0)) >= int(settings.ready_now_min_recent_bounce_event_count)
        and _safe_float(metrics.get('recent_shrunk_target_hit_ratio'), 0.0) >= float(settings.ready_now_min_recent_shrunk_target_hit_ratio)
        and _safe_float(metrics.get('recent_shrunk_upper_reach_ratio'), 0.0) >= float(settings.ready_now_min_recent_shrunk_upper_reach_ratio)
        and _safe_float(metrics.get('trade_liquidity_confidence'), 0.0) >= float(settings.ready_now_min_trade_liquidity_confidence)
        and readiness_score >= float(settings.ready_now_min_execution_readiness_score)
        and entry_touch_likelihood_score >= float(settings.ready_now_min_entry_touch_likelihood_score)
        and expected_actionability_score >= float(settings.ready_now_min_expected_actionability_score)
        and actionability_score >= float(settings.ready_now_min_actionability_score)
    )
    if ready_now:
        return 'ready_now', 'actionable_now', readiness_score, follow_through_score, actionability_score, entry_touch_likelihood_score, touch_urgency_score, pullback_queue_rank, expected_minutes_to_touch, touch_window_band, queue_priority_score

    near_ready = (
        _safe_float(metrics.get('range_current_location'), 1.0) <= float(settings.near_ready_max_range_location)
        and _safe_float(metrics.get('distance_to_entry_pct'), -99.0) >= -float(settings.near_ready_max_distance_to_entry_pct)
        and expected_minutes_to_touch <= float(settings.near_ready_max_expected_minutes_to_touch)
        and _safe_float(metrics.get('recent_target_excess_pct'), -99.0) >= float(settings.near_ready_min_recent_target_excess_pct)
        and readiness_score >= float(settings.near_ready_min_execution_readiness_score)
        and entry_touch_likelihood_score >= float(settings.near_ready_min_entry_touch_likelihood_score)
        and expected_actionability_score >= float(settings.near_ready_min_expected_actionability_score)
        and actionability_score >= float(settings.near_ready_min_actionability_score)
    )
    if near_ready:
        return 'near_ready', 'actionable_now', readiness_score, follow_through_score, actionability_score, entry_touch_likelihood_score, touch_urgency_score, pullback_queue_rank, expected_minutes_to_touch, touch_window_band, queue_priority_score

    touch_soon_queue = (
        _safe_float(metrics.get('distance_to_entry_pct'), -99.0) >= -float(settings.pullback_queue_max_distance_to_entry_pct)
        and expected_minutes_to_touch <= float(settings.touch_soon_queue_max_expected_minutes_to_touch)
        and entry_touch_likelihood_score >= float(settings.touch_soon_queue_min_entry_touch_likelihood_score)
        and expected_actionability_score >= float(settings.touch_soon_queue_min_expected_actionability_score)
        and follow_through_score >= float(settings.pullback_queue_min_follow_through_confidence_score)
    )
    if touch_soon_queue:
        return 'watchlist', 'touch_soon_queue', readiness_score, follow_through_score, actionability_score, entry_touch_likelihood_score, touch_urgency_score, pullback_queue_rank, expected_minutes_to_touch, touch_window_band, queue_priority_score

    touch_later_queue = (
        _safe_float(metrics.get('distance_to_entry_pct'), -99.0) >= -float(settings.pullback_queue_max_distance_to_entry_pct)
        and expected_minutes_to_touch <= float(settings.touch_later_queue_max_expected_minutes_to_touch)
        and entry_touch_likelihood_score >= float(settings.touch_later_queue_min_entry_touch_likelihood_score)
        and expected_actionability_score >= float(settings.touch_later_queue_min_expected_actionability_score)
        and follow_through_score >= float(settings.pullback_queue_min_follow_through_confidence_score)
    )
    if touch_later_queue:
        return 'watchlist', 'touch_later_queue', readiness_score, follow_through_score, actionability_score, entry_touch_likelihood_score, touch_urgency_score, pullback_queue_rank, expected_minutes_to_touch, touch_window_band, queue_priority_score

    return 'watchlist', 'structural_watchlist', readiness_score, follow_through_score, actionability_score, entry_touch_likelihood_score, touch_urgency_score, pullback_queue_rank, expected_minutes_to_touch, touch_window_band, queue_priority_score


def _compute_headline_rank_score(
    recommendation_tier: str,
    structural_score: float,
    execution_readiness_score: float,
    follow_through_score: float,
    actionability_score: float,
    entry_touch_likelihood_score: float,
    settings: Settings,
) -> float:
    raw = (
        float(settings.headline_rank_actionability_weight) * actionability_score
        + float(settings.headline_rank_readiness_weight) * execution_readiness_score
        + float(settings.headline_rank_entry_touch_weight) * entry_touch_likelihood_score
        + float(settings.headline_rank_follow_through_weight) * follow_through_score
        + float(settings.headline_rank_structural_weight) * structural_score
    )
    if recommendation_tier == 'headline_shortlist':
        raw += float(settings.headline_bonus_points)
        cap = float(settings.headline_cap_score)
    elif recommendation_tier == 'ready_now':
        raw += float(settings.ready_now_bonus_points)
        cap = min(float(settings.ready_now_cap_score), 89.99)
    elif recommendation_tier == 'near_ready':
        raw += float(settings.near_ready_bonus_points)
        cap = min(float(settings.near_ready_cap_score), 74.99)
    elif recommendation_tier == 'watchlist':
        raw -= float(settings.watchlist_penalty_points)
        cap = min(float(settings.watchlist_cap_score), 59.99)
    else:
        cap = 24.99
    return float(np.clip(raw, 0.0, cap))

def _build_stage2_gate_reason(metrics: Dict[str, object], settings: Settings) -> str | None:
    if metrics['range_classification_code'] == 'C':
        return 'Unstable non-range behaviour; excluded by the range-cycling thesis gate.'
    if not bool(metrics['within_range_target_possible']):
        return 'Current band does not offer a realistic +1% in-range headroom from the preferred entry zone.'
    if int(metrics['trade_window_minutes_remaining']) <= 0:
        return 'The valid trading window ends two hours before the close and has already expired.'
    if float(metrics['breakout_close_ratio']) > float(settings.max_breakout_close_ratio):
        return 'Too many closes are breaking outside the band, which makes repeatable in-range exits unreliable.'
    if float(metrics['wickiness_ratio']) > float(settings.max_wickiness_ratio):
        return 'Range behaviour is too wick-driven and chaotic to trust for repeated in-band trades.'
    return None


def build_candidate_score(
    row: pd.Series,
    top_stage1: pd.DataFrame,
    intraday_bars: pd.DataFrame,
    daily_bars: pd.DataFrame,
    spread_bps: float,
    minutes_remaining: int,
    settings: Settings,
) -> Dict[str, object]:
    intraday = intraday_bars.copy().reset_index(drop=True)
    intraday['cum_volume'] = intraday['volume'].cumsum()
    intraday['typical_price'] = (intraday['high'] + intraday['low'] + intraday['close']) / 3.0
    intraday['cum_dollar'] = (intraday['typical_price'] * intraday['volume']).cumsum()
    intraday['anchored_vwap'] = intraday['cum_dollar'] / intraday['cum_volume'].replace(0, np.nan)

    avg_daily_volume = float(daily_bars['volume'].tail(min(20, len(daily_bars))).mean()) if not daily_bars.empty else float(row['cum_volume'])
    avg_daily_close = float(daily_bars['close'].tail(min(20, len(daily_bars))).mean()) if not daily_bars.empty else float(row['current_price'])
    avg_daily_dollar_volume = avg_daily_volume * avg_daily_close
    atr_pct = _compute_atr_like(intraday)
    range_result = analyze_incremental_range(intraday, target_pct=settings.target_pct)

    entry_low = range_result.lower_zone_low
    entry_high = range_result.lower_zone_high
    if float(row['current_price']) > entry_high:
        entry_reference = (entry_low + entry_high) / 2.0
    elif float(row['current_price']) < entry_low:
        entry_reference = entry_low
    else:
        entry_reference = float(np.clip(float(row['current_price']), entry_low, entry_high))
    target_price = round(entry_reference * (1.0 + settings.target_pct / 100.0), 4)
    stretch_target_price = round(entry_reference * (1.0 + settings.stretch_target_pct / 100.0), 4)
    stop_price = round(range_result.band_low - max((range_result.band_high - range_result.band_low) * 0.20, float(row['current_price']) * 0.0035), 4)

    target_group_score, target_group_meta = _score_target_group_membership(row, top_stage1)
    liquidity_score, liquidity_meta = _score_liquidity(float(row['current_price']), float(row['cum_volume']), avg_daily_volume, avg_daily_dollar_volume, spread_bps, settings)
    volatility_score, volatility_meta = _score_volatility(intraday, atr_pct, range_result)
    range_tradability_score, range_tradability_meta = _score_range_tradability(range_result, entry_reference, settings.target_pct, spread_bps)
    range_position_score, range_position_meta = _score_range_position(range_result)
    time_feasibility_score, time_meta = _score_time_feasibility(intraday, range_result, settings.target_pct, minutes_remaining, settings.trade_window_end_buffer_minutes_before_close)
    execution_score, execution_meta = _score_execution_quality(spread_bps, liquidity_meta['relative_volume'], range_result.current_location, range_result)

    component_scores = {
        'target_strength': round(target_group_score, 2),
        'liquidity': round(liquidity_score, 2),
        'volatility_capacity': round(volatility_score, 2),
        'dynamic_range': round(range_tradability_score, 2),
        'range_position': round(range_position_score, 2),
        'time_feasibility': round(time_feasibility_score, 2),
        'execution_quality': round(execution_score, 2),
    }

    total_score = _safe_weighted_total(component_scores, settings.weights)
    total_score = float(np.clip(total_score, 0.0, 100.0))
    score_cap_reason = None
    score_penalties = []
    if range_result.classification == 'C':
        total_score = min(total_score, 24.99)
        score_cap_reason = 'Unstable non-range cap applied.'
    elif int(range_result.completed_cycles) < int(settings.min_completed_cycles_observed):
        total_score = min(total_score, 44.99)
        score_cap_reason = 'Completed-cycle cap applied.'
    elif min(range_result.lower_zone_touch_count, range_result.upper_zone_touch_count) < min(settings.min_lower_zone_touches, settings.min_upper_zone_touches):
        total_score = min(total_score, 49.99)
        score_cap_reason = 'Zone-touch cap applied.'
    elif not bool(range_tradability_meta['within_range_target_possible']):
        total_score = min(total_score, 44.99)
        score_cap_reason = 'Range headroom cap applied because +1% is not available inside the current band.'
    elif float(range_result.breakout_close_ratio) > float(settings.max_breakout_close_ratio):
        total_score = min(total_score, 34.99)
        score_cap_reason = 'Breakout-prone cap applied.'
    elif float(range_result.wickiness_ratio) > float(settings.max_wickiness_ratio):
        total_score = min(total_score, 34.99)
        score_cap_reason = 'Chaos cap applied.'
    elif float(range_result.directional_efficiency) > float(settings.max_directional_efficiency):
        total_score = min(total_score, 39.99)
        score_cap_reason = 'Directional-drift cap applied.'

    if float(range_result.recent_breakout_close_ratio) > float(settings.max_recent_breakout_close_ratio):
        penalty = min(14.0, max((float(range_result.recent_breakout_close_ratio) - float(settings.max_recent_breakout_close_ratio)) * 120.0, 4.0))
        total_score -= penalty
        score_penalties.append(f'recent_breakout:{penalty:.1f}')
    if float(range_result.recent_directional_efficiency) > float(settings.max_recent_directional_efficiency):
        penalty = min(10.0, max((float(range_result.recent_directional_efficiency) - float(settings.max_recent_directional_efficiency)) * 45.0, 3.0))
        total_score -= penalty
        score_penalties.append(f'recent_directionality:{penalty:.1f}')
    if float(range_result.cycle_durability_score) < float(settings.min_cycle_durability_score):
        penalty = min(10.0, max((float(settings.min_cycle_durability_score) - float(range_result.cycle_durability_score)) * 0.22, 2.0))
        total_score -= penalty
        score_penalties.append(f'cycle_durability:{penalty:.1f}')
    if float(range_result.recent_median_bounce_mfe_pct) < float(settings.min_recent_bounce_mfe_pct):
        penalty = min(10.0, max((float(settings.min_recent_bounce_mfe_pct) - float(range_result.recent_median_bounce_mfe_pct)) * 8.0, 2.0))
        total_score -= penalty
        score_penalties.append(f'recent_bounce_upside:{penalty:.1f}')
    if abs(float(range_result.recent_median_bounce_mae_pct)) > float(settings.max_recent_bounce_mae_pct):
        penalty = min(10.0, max((abs(float(range_result.recent_median_bounce_mae_pct)) - float(settings.max_recent_bounce_mae_pct)) * 5.0, 2.0))
        total_score -= penalty
        score_penalties.append(f'recent_bounce_drawdown:{penalty:.1f}')
    if int(range_result.recent_bounce_event_count) < int(settings.min_recent_bounce_event_count):
        penalty = 3.5
        total_score -= penalty
        score_penalties.append(f'recent_bounce_events:{penalty:.1f}')
    if float(range_result.recent_bounce_observation_confidence) < float(settings.min_recent_bounce_confidence_score):
        penalty = min(5.0, max((float(settings.min_recent_bounce_confidence_score) - float(range_result.recent_bounce_observation_confidence)) * 0.08, 1.5))
        total_score -= penalty
        score_penalties.append(f'recent_bounce_confidence:{penalty:.1f}')
    if float(range_result.recent_shrunk_upper_reach_ratio) < float(settings.min_recent_shrunk_upper_reach_ratio):
        penalty = min(5.0, max((float(settings.min_recent_shrunk_upper_reach_ratio) - float(range_result.recent_shrunk_upper_reach_ratio)) * 12.0, 1.5))
        total_score -= penalty
        score_penalties.append(f'shrunk_upper_reach:{penalty:.1f}')
    if float(range_result.recent_shrunk_target_hit_ratio) < float(settings.min_recent_shrunk_target_hit_ratio):
        penalty = min(6.0, max((float(settings.min_recent_shrunk_target_hit_ratio) - float(range_result.recent_shrunk_target_hit_ratio)) * 15.0, 1.5))
        total_score -= penalty
        score_penalties.append(f'shrunk_target_hit:{penalty:.1f}')
    if float(liquidity_meta['trade_liquidity_confidence']) < float(settings.min_cycle_trade_relative_volume * 20.0):
        penalty = min(4.5, max((float(settings.min_cycle_trade_relative_volume) * 20.0 - float(liquidity_meta['trade_liquidity_confidence'])) * 0.06, 1.0))
        total_score -= penalty
        score_penalties.append(f'trade_liquidity_confidence:{penalty:.1f}')

    total_score = round(float(np.clip(total_score, 0.0, 100.0)), 2)
    structural_score = float(total_score)

    metrics = {
        'session_open': _safe_round(row['session_open'], 4, 0.0),
        'current_price': _safe_round(row['current_price'], 4, 0.0),
        'intraday_pct_gain': round(float(row['intraday_pct_gain']), 3),
        'mover_rank': int(row['mover_rank']),
        'distance_from_leader_pct': round(float(row['distance_from_leader_pct']), 3),
        'current_cum_volume': round(float(row['cum_volume']), 2),
        'avg_daily_volume': round(float(avg_daily_volume), 2),
        'avg_daily_dollar_volume': round(float(avg_daily_dollar_volume), 2),
        'relative_volume': round(float(liquidity_meta['relative_volume']), 3),
        'trade_liquidity_confidence': float(liquidity_meta['trade_liquidity_confidence']),
        'range_position_score_for_tier': float(range_position_score),
        'spread_bps': round(float(spread_bps), 3),
        'atr_pct': round(float(atr_pct), 3),
        'anchored_vwap': _safe_round(intraday['anchored_vwap'].iloc[-1], 4, None),
        'realized_vol_pct': volatility_meta['realized_vol_pct'],
        'session_range_pct': volatility_meta['session_range_pct'],
        'range_classification': range_result.classification_label,
        'range_classification_code': range_result.classification,
        'thesis_structure_ok': range_result.classification in {'A', 'B'},
        'range_band_low': round(float(range_result.band_low), 4),
        'range_band_high': round(float(range_result.band_high), 4),
        'range_band_mid': round(float(range_result.band_mid), 4),
        'range_band_width_pct': round(float(range_result.band_width_pct), 3),
        'range_current_location': round(float(range_result.current_location), 3),
        'range_slope_bps_per_bar': round(float(range_result.slope_bps_per_bar), 3),
        'range_upper_slope_bps_per_bar': round(float(range_result.upper_slope_bps_per_bar), 3),
        'range_lower_slope_bps_per_bar': round(float(range_result.lower_slope_bps_per_bar), 3),
        'range_containment_ratio': round(float(range_result.containment_ratio), 3),
        'range_band_step_change_pct': round(float(range_result.band_step_change_pct), 3),
        'higher_low_ratio': round(float(range_result.higher_low_ratio), 3),
        'higher_high_ratio': round(float(range_result.higher_high_ratio), 3),
        'reversals': int(range_result.reversals),
        'lower_zone_touch_count': int(range_result.lower_zone_touch_count),
        'upper_zone_touch_count': int(range_result.upper_zone_touch_count),
        'zone_transition_count': int(range_result.zone_transition_count),
        'completed_cycles_observed': int(range_result.completed_cycles),
        'recent_completed_cycles_observed': int(range_result.recent_completed_cycles),
        'recent_lower_zone_touch_count': int(range_result.recent_lower_zone_touch_count),
        'recent_upper_zone_touch_count': int(range_result.recent_upper_zone_touch_count),
        'recent_zone_transition_count': int(range_result.recent_zone_transition_count),
        'width_retention_ratio': round(float(range_result.width_retention_ratio), 3),
        'cycle_persistence_ratio': round(float(range_result.cycle_persistence_ratio), 3),
        'cycle_durability_score': round(float(range_result.cycle_durability_score), 3),
        'degradation_score': round(float(range_result.degradation_score), 3),
        'breakout_close_ratio': round(float(range_result.breakout_close_ratio), 3),
        'recent_breakout_close_ratio': round(float(range_result.recent_breakout_close_ratio), 3),
        'directional_efficiency': round(float(range_result.directional_efficiency), 3),
        'recent_directional_efficiency': round(float(range_result.recent_directional_efficiency), 3),
        'wickiness_ratio': round(float(range_result.wickiness_ratio), 3),
        'recent_wickiness_ratio': round(float(range_result.recent_wickiness_ratio), 3),
        'recent_close_location_bias': round(float(range_result.recent_close_location_bias), 3),
        'median_bounce_mfe_pct': round(float(range_result.median_bounce_mfe_pct), 3),
        'median_bounce_mae_pct': round(float(range_result.median_bounce_mae_pct), 3),
        'bounce_payoff_ratio': round(float(range_result.bounce_payoff_ratio), 3),
        'upper_reach_ratio': round(float(range_result.upper_reach_ratio), 3),
        'target_hit_ratio': round(float(range_result.target_hit_ratio), 3),
        'recent_median_bounce_mfe_pct': round(float(range_result.recent_median_bounce_mfe_pct), 3),
        'recent_median_bounce_mae_pct': round(float(range_result.recent_median_bounce_mae_pct), 3),
        'recent_bounce_event_count': int(range_result.recent_bounce_event_count),
        'recent_bounce_observation_confidence': round(float(range_result.recent_bounce_observation_confidence), 3),
        'recent_bounce_payoff_ratio': round(float(range_result.recent_bounce_payoff_ratio), 3),
        'recent_bounce_edge_pct': round(float(range_result.recent_bounce_edge_pct), 3),
        'recent_target_excess_pct': round(float(range_result.recent_target_excess_pct), 3),
        'recent_shrunk_upper_reach_ratio': round(float(range_result.recent_shrunk_upper_reach_ratio), 3),
        'recent_shrunk_target_hit_ratio': round(float(range_result.recent_shrunk_target_hit_ratio), 3),
        'recent_upper_reach_ratio': round(float(range_result.recent_upper_reach_ratio), 3),
        'recent_target_hit_ratio': round(float(range_result.recent_target_hit_ratio), 3),
        'bounce_quality_score': round(float(range_result.bounce_quality_score), 3),
        'minutes_remaining_to_close': int(minutes_remaining),
        'trade_window_minutes_remaining': int(time_meta['trade_window_minutes_remaining']),
        'minutes_per_completed_cycle': float(time_meta['minutes_per_completed_cycle']),
        'estimated_cycles_remaining': float(time_meta['estimated_cycles_remaining']),
        'entry_low': round(float(entry_low), 4),
        'entry_high': round(float(entry_high), 4),
        'entry_reference': round(float(entry_reference), 4),
        'distance_to_entry_pct': round(((entry_reference / max(float(row['current_price']), 1e-9)) - 1.0) * 100.0, 3),
        'entry_proximity_score': round(_entry_proximity_score(((entry_reference / max(float(row['current_price']), 1e-9)) - 1.0) * 100.0), 3),
        'gross_headroom_pct': float(range_tradability_meta['gross_headroom_pct']),
        'effective_headroom_pct': float(range_tradability_meta['effective_headroom_pct']),
        'net_headroom_score_for_tier': float(np.clip((float(range_tradability_meta['effective_headroom_pct']) - settings.target_pct * 0.75) * 55.0, 0.0, 100.0)),
        'within_range_target_possible': bool(range_tradability_meta['within_range_target_possible']),
        'width_multiple_vs_target': float(range_tradability_meta['width_multiple_vs_target']),
        'target_price': target_price,
        'stretch_target_price': stretch_target_price,
        'stop_price': stop_price,
        'target_group_meta': target_group_meta,
        'liquidity_meta': liquidity_meta,
        'volatility_meta': volatility_meta,
        'dynamic_range_meta': {**range_result.details, **range_tradability_meta},
        'range_position_meta': range_position_meta,
        'time_meta': time_meta,
        'execution_meta': execution_meta,
        'score_cap_reason': score_cap_reason,
        'valid_exit_deadline_rule': f'Target should be achievable before {settings.trade_window_end_buffer_minutes_before_close} minutes before the close.',
    }

    stage2_gate_reason = _build_stage2_gate_reason(metrics, settings)
    advanced_to_stage2 = stage2_gate_reason is None
    metrics['structural_score'] = round(float(structural_score), 3)
    recommendation_tier, recommendation_book, execution_readiness_score, follow_through_confidence_score, actionability_score, entry_touch_likelihood_score, touch_urgency_score, pullback_queue_rank_score, expected_minutes_to_touch, touch_window_band, queue_priority_score = _determine_recommendation_tier(metrics, settings, advanced_to_stage2)
    execution_lane, monitor_cadence_minutes = _execution_lane_for_book(recommendation_book)
    queue_escalation_score = float(np.clip(queue_priority_score * 0.55 + touch_urgency_score * 0.25 + follow_through_confidence_score * 0.20, 0.0, 100.0))
    headline_rank_score = _compute_headline_rank_score(
        recommendation_tier,
        structural_score,
        execution_readiness_score,
        follow_through_confidence_score,
        actionability_score,
        entry_touch_likelihood_score,
        settings,
    )
    queue_actionability_score = float(np.clip(pullback_queue_rank_score * 0.38 + touch_urgency_score * 0.18 + queue_priority_score * 0.44, 0.0, 100.0))
    if advanced_to_stage2:
        structural_weight = float(np.clip(float(settings.final_score_structural_weight), 0.10, 0.90))
        actionability_weight = float(np.clip(float(settings.final_score_actionability_weight), 0.10, 0.90))
        total_weight = structural_weight + actionability_weight
        structural_weight /= total_weight
        actionability_weight /= total_weight
        if recommendation_tier == 'headline_shortlist':
            total_score = structural_score * 0.20 + actionability_score * 0.80
            total_score += float(settings.headline_bonus_points)
            total_score = max(total_score, actionability_score * 0.96)
            total_score = min(total_score, float(settings.headline_cap_score))
        elif recommendation_tier == 'ready_now':
            total_score = structural_score * 0.28 + actionability_score * 0.72
            total_score += float(settings.ready_now_bonus_points)
            total_score = max(total_score, actionability_score * 0.88)
            total_score = min(total_score, float(settings.ready_now_cap_score))
        elif recommendation_tier == 'near_ready':
            total_score = structural_score * 0.40 + actionability_score * 0.60
            total_score += float(settings.near_ready_bonus_points)
            total_score = max(total_score, structural_score * 0.58)
            total_score = min(total_score, float(settings.near_ready_cap_score))
        else:
            if recommendation_book == 'touch_soon_queue':
                total_score = structural_score * 0.34 + actionability_score * 0.14 + queue_actionability_score * 0.24 + queue_escalation_score * 0.28
                total_score -= max(float(settings.watchlist_penalty_points) - 1.75, 0.0)
                total_score = min(total_score, 56.99)
            elif recommendation_book == 'touch_later_queue':
                total_score = structural_score * 0.44 + actionability_score * 0.10 + queue_actionability_score * 0.24 + queue_escalation_score * 0.22
                total_score -= max(float(settings.watchlist_penalty_points) - 0.25, 0.0)
                total_score = min(total_score, 50.99)
            else:
                total_score = structural_score * 0.70 + actionability_score * 0.30
                total_score -= float(settings.watchlist_penalty_points)
                total_score = min(total_score, float(settings.watchlist_cap_score))
        headline_blend = float(np.clip(float(settings.final_score_headline_rank_blend), 0.0, 1.0))
        total_score = (1.0 - headline_blend) * total_score + headline_blend * headline_rank_score
        total_score = round(float(np.clip(total_score, 0.0, 100.0)), 2)

    metrics['structural_score'] = round(float(structural_score), 3)
    metrics['entry_touch_likelihood_score'] = round(float(entry_touch_likelihood_score), 3)
    metrics['execution_readiness_score'] = round(float(execution_readiness_score), 3)
    metrics['touch_urgency_score'] = round(float(touch_urgency_score), 3)
    metrics['expected_minutes_to_touch'] = round(float(expected_minutes_to_touch), 3)
    metrics['follow_through_confidence_score'] = round(float(follow_through_confidence_score), 3)
    metrics['expected_actionability_score'] = round(float(_score_expected_actionability(entry_touch_likelihood_score, follow_through_confidence_score, structural_score, touch_urgency_score, metrics)), 3)
    metrics['actionability_score'] = round(float(actionability_score), 3)
    metrics['headline_rank_score'] = round(float(headline_rank_score), 3)
    metrics['pullback_queue_rank_score'] = round(float(pullback_queue_rank_score), 3)
    metrics['queue_actionability_score'] = round(float(queue_actionability_score), 3)
    metrics['queue_priority_score'] = round(float(queue_priority_score), 3)
    metrics['queue_escalation_score'] = round(float(queue_escalation_score), 3)
    metrics['touch_window_band'] = touch_window_band
    metrics['execution_lane'] = execution_lane
    metrics['monitor_cadence_minutes'] = monitor_cadence_minutes
    metrics['recommendation_tier'] = recommendation_tier
    metrics['recommendation_book'] = recommendation_book
    metrics['score_penalties_applied'] = score_penalties

    rationale_bits = []
    if metrics['completed_cycles_observed'] >= settings.min_completed_cycles_observed:
        rationale_bits.append('the range has already completed multiple low-to-high cycles before the scan')
    if component_scores['dynamic_range'] >= 65:
        rationale_bits.append('the current band is broad enough and active enough for a 1% in-range trade')
    if component_scores['range_position'] >= 65:
        rationale_bits.append('price is near the lower part of the active range')
    if time_meta['estimated_cycles_remaining'] >= 1.0:
        rationale_bits.append('observed cycle speed implies room for more than one cycle before the cutoff')
    if range_result.cycle_durability_score >= 65.0:
        rationale_bits.append('the recent cycles still look durable rather than used up')
    if range_result.bounce_quality_score >= 60.0:
        rationale_bits.append('recent lower-band bounces have shown favorable upside-versus-downside asymmetry')
    if range_result.recent_bounce_observation_confidence >= 55.0:
        rationale_bits.append('recent lower-band bounce evidence is based on more than a token sample')
    if touch_urgency_score >= 70.0:
        rationale_bits.append('the preferred entry looks likely to be reached soon enough to treat this as an actively actionable idea rather than only a queue candidate')
    elif recommendation_book in {'touch_soon_queue', 'touch_later_queue'}:
        rationale_bits.append('the preferred entry still requires a pullback, so this belongs in the pullback queue rather than the immediate-action shortlist')
    if recommendation_tier == 'headline_shortlist':
        rationale_bits.append('this looks like a headline candidate: structurally tradable, close to entry, and backed by enough recent follow-through evidence')
    elif recommendation_tier == 'ready_now':
        rationale_bits.append('this looks like a ready-now lower-band entry rather than only a structural watchlist name')
    elif recommendation_tier == 'near_ready':
        rationale_bits.append('this looks structurally tradable and close enough to the preferred entry to stay on the actionable shortlist')
    elif recommendation_tier == 'watchlist':
        if recommendation_book in {'touch_soon_queue', 'touch_later_queue'}:
            if recommendation_book == 'touch_soon_queue':
                rationale_bits.append('this is better treated as a touch-soon pullback candidate: structurally tradable with decent conditional edge if entry is reached, but not a headline setup now')
            else:
                rationale_bits.append('this is better treated as a touch-later pullback candidate: structurally tradable with decent conditional edge if entry is reached, but likely to need more time before entry is reached')
        else:
            rationale_bits.append('this looks structurally tradable but is still too stretched above the preferred entry to treat as an actionable setup now')
    if actionability_score >= 70.0:
        rationale_bits.append('the immediate actionability profile is strong enough to prioritize above the broader watchlist')
    if headline_rank_score >= 75.0:
        rationale_bits.append('the combined headline rank score is strong enough for top-of-list treatment rather than passive monitoring')
    elif actionability_score < 45.0:
        rationale_bits.append('the structure may be valid, but immediate execution readiness and follow-through quality are still mediocre')
    if range_result.recent_shrunk_target_hit_ratio >= 0.45:
        rationale_bits.append('even after shrinking for limited evidence, recent lower-band touches still look productive enough')
    if not metrics['within_range_target_possible']:
        rationale_bits.append('the current band does not yet offer a full 1% net headroom from the preferred entry zone')
    if range_result.recent_bounce_payoff_ratio < 1.0:
        rationale_bits.append('recent lower-band bounces have offered poor payoff asymmetry')
    if range_result.recent_bounce_observation_confidence < 40.0:
        rationale_bits.append('recent bounce evidence is still thin, so the apparent edge may not be robust')
    if range_result.classification == 'B':
        rationale_bits.append('the band is static, which is acceptable for repeated buy-low / sell-high trades')
    elif range_result.classification == 'A':
        rationale_bits.append('the band is gently rising but still looks tradable inside the range')
    elif range_result.classification == 'C':
        rationale_bits.append('price behaviour is too unstable to trust for range cycling')
    if range_result.cycle_durability_score < 50.0:
        rationale_bits.append('the most recent oscillations look degraded relative to earlier in the session')
    if stage2_gate_reason:
        rationale_bits.append(stage2_gate_reason)
    rationale = '; '.join(rationale_bits) if rationale_bits else 'balanced range-cycling profile with no single dominant edge.'

    chart_context = {
        'band_low': _safe_round(range_result.band_low, 4, 0.0),
        'band_high': _safe_round(range_result.band_high, 4, 0.0),
        'entry_low': _safe_round(entry_low, 4, 0.0),
        'entry_high': _safe_round(entry_high, 4, 0.0),
        'target_price': _safe_round(target_price, 4, 0.0),
        'stop_price': _safe_round(stop_price, 4, 0.0),
    }

    return {
        'symbol': row['symbol'],
        'company_name': row.get('company_name'),
        'mover_rank': int(row['mover_rank']),
        'intraday_pct_gain': round(float(row['intraday_pct_gain']), 3),
        'advanced_to_stage2': advanced_to_stage2,
        'exclusion_reason': stage2_gate_reason,
        'current_price': round(float(row['current_price']), 4),
        'current_cum_volume': round(float(row['cum_volume']), 2),
        'relative_volume': round(float(liquidity_meta['relative_volume']), 3),
        'total_score': total_score,
        'recommendation_tier': recommendation_tier,
        'recommendation_book': recommendation_book,
        'structural_score': round(float(structural_score), 3),
        'entry_touch_likelihood_score': round(float(entry_touch_likelihood_score), 3),
        'touch_urgency_score': round(float(touch_urgency_score), 3),
        'expected_minutes_to_touch': round(float(expected_minutes_to_touch), 3),
        'follow_through_confidence_score': round(float(follow_through_confidence_score), 3),
        'expected_actionability_score': round(float(metrics['expected_actionability_score']), 3),
        'actionability_score': round(float(actionability_score), 3),
        'headline_rank_score': round(float(headline_rank_score), 3),
        'pullback_queue_rank_score': round(float(pullback_queue_rank_score), 3),
        'queue_actionability_score': round(float(queue_actionability_score), 3),
        'queue_escalation_score': round(float(queue_escalation_score), 3),
        'execution_lane': execution_lane,
        'monitor_cadence_minutes': monitor_cadence_minutes,
        'queue_priority_score': round(float(queue_priority_score), 3),
        'touch_window_band': touch_window_band,
        'component_scores': component_scores,
        'metrics': metrics,
        'rationale': rationale,
        'entry_low': round(float(entry_low), 4),
        'entry_high': round(float(entry_high), 4),
        'target_price': target_price,
        'stretch_target_price': stretch_target_price,
        'stop_price': stop_price,
        'chart_context': chart_context,
    }
