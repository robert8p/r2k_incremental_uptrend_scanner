from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

import numpy as np
import pandas as pd


@dataclass
class AdaptiveRangeResult:
    classification: str
    classification_label: str
    band_low: float
    band_high: float
    band_mid: float
    band_width_pct: float
    current_location: float
    lower_zone_low: float
    lower_zone_high: float
    upper_zone_low: float
    upper_zone_high: float
    slope_bps_per_bar: float
    upper_slope_bps_per_bar: float
    lower_slope_bps_per_bar: float
    containment_ratio: float
    band_step_change_pct: float
    higher_low_ratio: float
    higher_high_ratio: float
    reversals: int
    lower_zone_touch_count: int
    upper_zone_touch_count: int
    zone_transition_count: int
    completed_cycles: int
    breakout_close_ratio: float
    directional_efficiency: float
    wickiness_ratio: float
    recent_lower_zone_touch_count: int
    recent_upper_zone_touch_count: int
    recent_zone_transition_count: int
    recent_completed_cycles: int
    width_retention_ratio: float
    cycle_persistence_ratio: float
    recent_breakout_close_ratio: float
    recent_directional_efficiency: float
    recent_wickiness_ratio: float
    recent_close_location_bias: float
    bounce_event_count: int
    median_bounce_mfe_pct: float
    median_bounce_mae_pct: float
    bounce_payoff_ratio: float
    bounce_edge_pct: float
    target_excess_pct: float
    upper_reach_ratio: float
    target_hit_ratio: float
    recent_bounce_event_count: int
    recent_bounce_observation_confidence: float
    recent_median_bounce_mfe_pct: float
    recent_median_bounce_mae_pct: float
    recent_bounce_payoff_ratio: float
    recent_bounce_edge_pct: float
    recent_target_excess_pct: float
    recent_shrunk_upper_reach_ratio: float
    recent_shrunk_target_hit_ratio: float
    recent_upper_reach_ratio: float
    recent_target_hit_ratio: float
    bounce_quality_score: float
    cycle_durability_score: float
    degradation_score: float
    dynamic_range_score: float
    details: Dict[str, float]


def _safe_slope(series: pd.Series) -> float:
    if len(series) < 3:
        return 0.0
    x = np.arange(len(series))
    y = series.astype(float).to_numpy()
    slope, _ = np.polyfit(x, y, 1)
    return float(slope)


def _compress_zone_states(states: List[str]) -> List[str]:
    compressed: List[str] = []
    for state in states:
        if state not in {'L', 'U'}:
            continue
        if compressed and compressed[-1] == state:
            continue
        compressed.append(state)
    return compressed


def _touch_event_indices(mask: pd.Series, min_gap: int = 2) -> List[int]:
    indices: List[int] = []
    last_idx = -10_000
    for idx, touched in enumerate(mask.astype(bool).tolist()):
        if not touched:
            continue
        if idx - last_idx <= min_gap:
            continue
        indices.append(idx)
        last_idx = idx
    return indices



def _confidence_score(sample_size: float) -> float:
    return float(np.clip((float(sample_size) - 0.5) * 30.0, 0.0, 100.0))


def _shrink_ratio(observed_ratio: float, sample_size: float, prior_mean: float, prior_strength: float) -> float:
    numerator = observed_ratio * max(sample_size, 0.0) + prior_mean * prior_strength
    denominator = max(sample_size, 0.0) + prior_strength
    return float(numerator / max(denominator, 1e-9))


def _bounce_stats_for_segment(segment: pd.DataFrame, target_pct: float, horizon_bars: int = 15) -> Dict[str, float]:
    if segment.empty:
        return {
            'bounce_event_count': 0.0,
            'median_bounce_mfe_pct': 0.0,
            'median_bounce_mae_pct': -99.0,
            'bounce_payoff_ratio': 0.0,
            'bounce_edge_pct': 0.0,
            'target_excess_pct': -float(target_pct),
            'upper_reach_ratio': 0.0,
            'target_hit_ratio': 0.0,
        }

    lower_zone_low = segment['rolling_low'] + segment['band_width'] * 0.05
    lower_zone_high = segment['rolling_low'] + segment['band_width'] * 0.33
    upper_zone_low = segment['rolling_high'] - segment['band_width'] * 0.33
    lower_touch_mask = segment['low'] <= lower_zone_high
    touch_indices = _touch_event_indices(lower_touch_mask, min_gap=2)
    if not touch_indices:
        return {
            'bounce_event_count': 0.0,
            'median_bounce_mfe_pct': 0.0,
            'median_bounce_mae_pct': -99.0,
            'bounce_payoff_ratio': 0.0,
            'bounce_edge_pct': 0.0,
            'target_excess_pct': -float(target_pct),
            'upper_reach_ratio': 0.0,
            'target_hit_ratio': 0.0,
        }

    mfe_values: List[float] = []
    mae_values: List[float] = []
    upper_hits = 0
    target_hits = 0

    for pos, idx in enumerate(touch_indices):
        entry_price = float(np.clip(float(segment['close'].iloc[idx]), float(lower_zone_low.iloc[idx]), float(lower_zone_high.iloc[idx])))
        next_touch_idx = touch_indices[pos + 1] if pos + 1 < len(touch_indices) else len(segment) - 1
        horizon_end = min(next_touch_idx, idx + horizon_bars, len(segment) - 1)
        if horizon_end <= idx:
            continue
        future = segment.iloc[idx + 1 : horizon_end + 1]
        if future.empty:
            continue
        mfe_pct = float((future['high'].max() / max(entry_price, 1e-9) - 1.0) * 100.0)
        mae_pct = float((future['low'].min() / max(entry_price, 1e-9) - 1.0) * 100.0)
        reached_upper = bool((future['high'] >= upper_zone_low.iloc[idx + 1 : horizon_end + 1].to_numpy()).any())
        target_hit = mfe_pct >= float(target_pct)
        mfe_values.append(mfe_pct)
        mae_values.append(mae_pct)
        upper_hits += int(reached_upper)
        target_hits += int(target_hit)

    if not mfe_values:
        return {
            'bounce_event_count': 0.0,
            'median_bounce_mfe_pct': 0.0,
            'median_bounce_mae_pct': -99.0,
            'bounce_payoff_ratio': 0.0,
            'bounce_edge_pct': 0.0,
            'target_excess_pct': -float(target_pct),
            'upper_reach_ratio': 0.0,
            'target_hit_ratio': 0.0,
        }

    median_mfe = float(np.median(mfe_values))
    median_mae = float(np.median(mae_values))
    downside_ref = max(max(-median_mae, 0.0), 0.15)
    payoff_ratio = float(min(median_mfe / max(downside_ref, 1e-6), 8.0)) if median_mfe > 0 else 0.0
    bounce_edge_pct = float(median_mfe - max(-median_mae, 0.0))
    target_excess_pct = float(median_mfe - float(target_pct))
    return {
        'bounce_event_count': float(len(mfe_values)),
        'median_bounce_mfe_pct': median_mfe,
        'median_bounce_mae_pct': median_mae,
        'bounce_payoff_ratio': payoff_ratio,
        'bounce_edge_pct': bounce_edge_pct,
        'target_excess_pct': target_excess_pct,
        'upper_reach_ratio': float(upper_hits / max(len(mfe_values), 1)),
        'target_hit_ratio': float(target_hits / max(len(mfe_values), 1)),
    }


def _segment_cycle_stats(segment: pd.DataFrame, target_pct: float) -> Dict[str, float]:
    if segment.empty:
        return {
            'lower_zone_touch_count': 0.0,
            'upper_zone_touch_count': 0.0,
            'zone_transition_count': 0.0,
            'completed_cycles': 0.0,
            'breakout_close_ratio': 1.0,
            'directional_efficiency': 1.0,
            'wickiness_ratio': 99.0,
            'avg_width_pct': 0.0,
            'close_location_bias': 0.5,
            'bounce_event_count': 0.0,
            'median_bounce_mfe_pct': 0.0,
            'median_bounce_mae_pct': -99.0,
            'bounce_payoff_ratio': 0.0,
            'bounce_edge_pct': 0.0,
            'target_excess_pct': -float(target_pct),
            'upper_reach_ratio': 0.0,
            'target_hit_ratio': 0.0,
        }

    lower_zone_low = segment['rolling_low'] + segment['band_width'] * 0.05
    lower_zone_high = segment['rolling_low'] + segment['band_width'] * 0.33
    upper_zone_low = segment['rolling_high'] - segment['band_width'] * 0.33
    upper_zone_high = segment['rolling_high'] - segment['band_width'] * 0.05

    lower_touch_indices = _touch_event_indices(segment['low'] <= lower_zone_high, min_gap=2)
    upper_touch_indices = _touch_event_indices(segment['high'] >= upper_zone_low, min_gap=2)
    lower_zone_touch_count = len(lower_touch_indices)
    upper_zone_touch_count = len(upper_touch_indices)

    loc_series = ((segment['close'] - segment['rolling_low']) / segment['band_width']).clip(-0.2, 1.2)
    raw_states = ['L' if loc <= 0.35 else 'U' if loc >= 0.65 else 'M' for loc in loc_series]
    compressed_states = _compress_zone_states(raw_states)
    zone_transition_count = max(len(compressed_states) - 1, 0)
    completed_cycles = 0
    for prev_state, next_state in zip(compressed_states, compressed_states[1:]):
        if prev_state == 'L' and next_state == 'U':
            completed_cycles += 1

    breakout_close_ratio = float(((segment['close'] > segment['rolling_high']) | (segment['close'] < segment['rolling_low'])).mean())
    abs_moves = segment['close'].diff().abs().fillna(0.0)
    directional_efficiency = float(abs(segment['close'].iloc[-1] - segment['close'].iloc[0]) / max(abs_moves.sum(), 1e-9))
    body = (segment['close'] - segment['open']).abs()
    floor = max(float(segment['close'].abs().median()) * 0.0005, 1e-6)
    wickiness_ratio = float(((segment['high'] - segment['low']) / body.clip(lower=floor)).mean())
    avg_width_pct = float(segment['band_width_pct'].mean())
    close_location_bias = float(loc_series.mean())
    bounce_stats = _bounce_stats_for_segment(segment, target_pct=target_pct)

    return {
        'lower_zone_touch_count': float(lower_zone_touch_count),
        'upper_zone_touch_count': float(upper_zone_touch_count),
        'zone_transition_count': float(zone_transition_count),
        'completed_cycles': float(completed_cycles),
        'breakout_close_ratio': breakout_close_ratio,
        'directional_efficiency': directional_efficiency,
        'wickiness_ratio': wickiness_ratio,
        'avg_width_pct': avg_width_pct,
        'close_location_bias': close_location_bias,
        **bounce_stats,
    }


def analyze_incremental_range(df: pd.DataFrame, price_col: str = 'close', target_pct: float = 1.0) -> AdaptiveRangeResult:
    if df.empty:
        raise ValueError('No bars available for adaptive range analysis.')

    window = max(10, min(24, len(df)))
    lookback = min(max(window * 4, 48), len(df))
    working = df.copy().tail(lookback).reset_index(drop=True)
    working['rolling_low'] = working['low'].rolling(window=window, min_periods=max(5, window // 2)).min()
    working['rolling_high'] = working['high'].rolling(window=window, min_periods=max(5, window // 2)).max()
    working['band_mid'] = (working['rolling_low'] + working['rolling_high']) / 2.0
    working['band_width'] = (working['rolling_high'] - working['rolling_low']).clip(lower=1e-9)
    working['band_width_pct'] = working['band_width'] / working['band_mid'].replace(0, np.nan) * 100.0
    working = working.dropna().reset_index(drop=True)
    if working.empty:
        working = df.copy().tail(window).reset_index(drop=True)
        working['rolling_low'] = working['low'].expanding().min()
        working['rolling_high'] = working['high'].expanding().max()
        working['band_mid'] = (working['rolling_low'] + working['rolling_high']) / 2.0
        working['band_width'] = (working['rolling_high'] - working['rolling_low']).clip(lower=1e-9)
        working['band_width_pct'] = working['band_width'] / working['band_mid'].replace(0, np.nan) * 100.0

    band_low = float(working['rolling_low'].iloc[-1])
    band_high = float(working['rolling_high'].iloc[-1])
    band_mid = float(working['band_mid'].iloc[-1])
    width = max(band_high - band_low, 1e-9)
    current_price = float(df[price_col].iloc[-1])
    current_location = float(np.clip((current_price - band_low) / width, 0.0, 1.2))

    mid_slope = _safe_slope(working['band_mid'])
    low_slope = _safe_slope(working['rolling_low'])
    high_slope = _safe_slope(working['rolling_high'])
    scale = max(current_price, 1e-9)
    slope_bps_per_bar = mid_slope / scale * 10_000.0
    lower_slope_bps_per_bar = low_slope / scale * 10_000.0
    upper_slope_bps_per_bar = high_slope / scale * 10_000.0

    containment = ((working['close'] >= working['rolling_low']) & (working['close'] <= working['rolling_high'])).mean()
    breakout_close_ratio = ((working['close'] > working['rolling_high']) | (working['close'] < working['rolling_low'])).mean()
    band_step_change = (
        working['rolling_low'].diff().abs().fillna(0).add(working['rolling_high'].diff().abs().fillna(0))
        / working['band_mid'].replace(0, np.nan)
    ).mean() * 100.0
    higher_low_ratio = (working['rolling_low'].diff() > 0).mean()
    higher_high_ratio = (working['rolling_high'].diff() > 0).mean()

    direction = np.sign(df['close'].diff().fillna(0.0))
    reversals = int((direction.shift(1) * direction < 0).sum())

    full_stats = _segment_cycle_stats(working, target_pct=target_pct)
    split_idx = max(len(working) // 2, 1)
    early_half = working.iloc[:split_idx].copy()
    recent_half = working.iloc[split_idx:].copy() if split_idx < len(working) else working.iloc[-max(len(working)//2,1):].copy()
    if recent_half.empty:
        recent_half = working.copy()
    early_stats = _segment_cycle_stats(early_half, target_pct=target_pct)
    recent_stats = _segment_cycle_stats(recent_half, target_pct=target_pct)

    width_retention_ratio = float(recent_stats['avg_width_pct'] / max(early_stats['avg_width_pct'], 1e-6)) if early_stats['avg_width_pct'] > 0 else 0.0
    early_transition_density = early_stats['zone_transition_count'] / max(len(early_half), 1)
    recent_transition_density = recent_stats['zone_transition_count'] / max(len(recent_half), 1)
    cycle_persistence_ratio = float(recent_transition_density / max(early_transition_density, 1e-6)) if early_transition_density > 0 else 0.0
    recent_close_location_bias = float(recent_stats['close_location_bias'])
    recent_bounce_confidence = _confidence_score(recent_stats['bounce_event_count'])
    recent_shrunk_upper_reach_ratio = _shrink_ratio(float(recent_stats['upper_reach_ratio']), float(recent_stats['bounce_event_count']), prior_mean=0.45, prior_strength=4.0)
    recent_shrunk_target_hit_ratio = _shrink_ratio(float(recent_stats['target_hit_ratio']), float(recent_stats['bounce_event_count']), prior_mean=0.30, prior_strength=4.0)

    width_score = float(np.clip(100.0 - abs(float(working['band_width_pct'].iloc[-1]) - 3.0) * 18.0, 0.0, 100.0))
    containment_score = float(np.clip(containment * 135.0 - 25.0, 0.0, 100.0))
    cycle_score = float(np.clip(full_stats['completed_cycles'] * 34.0 + full_stats['zone_transition_count'] * 8.0, 0.0, 100.0))
    touch_score = float(np.clip((min(full_stats['lower_zone_touch_count'], full_stats['upper_zone_touch_count']) - 1.0) * 32.0, 0.0, 100.0))
    efficiency_score = float(np.clip(100.0 - abs(full_stats['directional_efficiency'] - 0.35) * 180.0, 0.0, 100.0))
    breakout_penalty = float(np.clip(full_stats['breakout_close_ratio'] * 260.0, 0.0, 65.0))
    wick_penalty = float(np.clip(max(full_stats['wickiness_ratio'] - 5.0, 0.0) * 8.0, 0.0, 55.0))
    step_penalty = float(np.clip(band_step_change * 20.0, 0.0, 55.0))
    slope_penalty = float(np.clip(max(abs(slope_bps_per_bar) - 18.0, 0.0) * 1.8, 0.0, 35.0))

    bounce_mfe_score = float(np.clip((recent_stats['median_bounce_mfe_pct'] - target_pct * 0.75) * 70.0, 0.0, 100.0))
    bounce_mae_score = float(np.clip((1.05 - max(-recent_stats['median_bounce_mae_pct'], 0.0)) / 1.05 * 100.0, 0.0, 100.0))
    upper_reach_score = float(np.clip(recent_shrunk_upper_reach_ratio * 120.0, 0.0, 100.0))
    target_hit_score = float(np.clip(recent_shrunk_target_hit_ratio * 150.0, 0.0, 100.0))
    payoff_score = float(np.clip((recent_stats['bounce_payoff_ratio'] - 1.0) * 35.0, 0.0, 100.0))
    edge_score = float(np.clip((recent_stats['bounce_edge_pct'] - 0.35) * 55.0, 0.0, 100.0))
    target_excess_score = float(np.clip((recent_stats['target_excess_pct'] + 0.10) * 70.0, 0.0, 100.0))
    confidence_score = float(np.clip(recent_bounce_confidence, 0.0, 100.0))
    bounce_quality_score = float(np.clip(
        bounce_mfe_score * 0.16
        + bounce_mae_score * 0.12
        + upper_reach_score * 0.14
        + target_hit_score * 0.16
        + payoff_score * 0.08
        + edge_score * 0.14
        + target_excess_score * 0.10
        + confidence_score * 0.10,
        0.0,
        100.0,
    ))

    durability_base = (
        np.clip((width_retention_ratio - 0.55) * 120.0, 0.0, 100.0) * 0.18
        + np.clip((cycle_persistence_ratio - 0.45) * 100.0, 0.0, 100.0) * 0.18
        + np.clip((recent_stats['completed_cycles'] - 0.5) * 70.0, 0.0, 100.0) * 0.12
        + np.clip((min(recent_stats['lower_zone_touch_count'], recent_stats['upper_zone_touch_count']) - 0.5) * 55.0, 0.0, 100.0) * 0.10
        + np.clip((0.60 - recent_stats['directional_efficiency']) * 180.0, 0.0, 100.0) * 0.12
        + bounce_quality_score * 0.24
        + np.clip(recent_bounce_confidence, 0.0, 100.0) * 0.06
    )
    deterioration_penalty = (
        np.clip((recent_stats['breakout_close_ratio'] - full_stats['breakout_close_ratio']) * 320.0, 0.0, 35.0)
        + np.clip((recent_stats['directional_efficiency'] - full_stats['directional_efficiency']) * 140.0, 0.0, 30.0)
        + np.clip((0.75 - width_retention_ratio) * 110.0, 0.0, 30.0)
        + np.clip((0.70 - cycle_persistence_ratio) * 85.0, 0.0, 28.0)
        + np.clip(abs(recent_close_location_bias - 0.50) * 60.0 - 8.0, 0.0, 20.0)
        + np.clip((target_pct * 0.90 - recent_stats['median_bounce_mfe_pct']) * 45.0, 0.0, 30.0)
        + np.clip((abs(recent_stats['median_bounce_mae_pct']) - 1.00) * 35.0, 0.0, 24.0)
        + np.clip((1.10 - recent_stats['bounce_payoff_ratio']) * 35.0, 0.0, 24.0)
        + np.clip((0.42 - recent_shrunk_upper_reach_ratio) * 45.0, 0.0, 18.0)
        + np.clip((0.38 - recent_shrunk_target_hit_ratio) * 60.0, 0.0, 24.0)
        + np.clip((45.0 - recent_bounce_confidence) * 0.35, 0.0, 18.0)
    )
    cycle_durability_score = float(np.clip(durability_base - deterioration_penalty, 0.0, 100.0))
    degradation_score = float(np.clip(100.0 - cycle_durability_score, 0.0, 100.0))

    dynamic_score = (
        width_score * 0.11
        + containment_score * 0.14
        + cycle_score * 0.13
        + touch_score * 0.10
        + efficiency_score * 0.08
        + bounce_quality_score * 0.22
        + cycle_durability_score * 0.22
        - breakout_penalty
        - wick_penalty
        - step_penalty
        - slope_penalty
    )
    dynamic_score = float(np.clip(dynamic_score, 0.0, 100.0))

    stable_range = (
        containment >= 0.58
        and breakout_close_ratio <= 0.12
        and band_step_change <= 0.9
        and full_stats['wickiness_ratio'] <= 8.5
        and full_stats['lower_zone_touch_count'] >= 2
        and full_stats['upper_zone_touch_count'] >= 2
        and full_stats['completed_cycles'] >= 1
        and recent_stats['lower_zone_touch_count'] >= 1
        and recent_stats['upper_zone_touch_count'] >= 1
        and recent_stats['completed_cycles'] >= 1
        and recent_stats['bounce_event_count'] >= 1
        and width_retention_ratio >= 0.55
        and cycle_persistence_ratio >= 0.40
        and recent_stats['breakout_close_ratio'] <= 0.18
        and recent_stats['directional_efficiency'] <= 0.72
        and bounce_quality_score >= 40.0
        and cycle_durability_score >= 35.0
    )
    if stable_range and slope_bps_per_bar >= 0.5 and slope_bps_per_bar <= 14.0 and full_stats['directional_efficiency'] <= 0.72 and bounce_quality_score >= 55.0 and cycle_durability_score >= 55.0:
        classification = 'A'
        label = 'Incrementally upward-shifting range'
    elif stable_range and abs(slope_bps_per_bar) <= 6.0 and full_stats['directional_efficiency'] <= 0.68 and bounce_quality_score >= 50.0 and cycle_durability_score >= 50.0:
        classification = 'B'
        label = 'Static sideways range'
    else:
        classification = 'C'
        label = 'Unstable non-range behaviour'

    lower_zone_low = band_low + width * 0.05
    lower_zone_high = band_low + width * 0.33
    upper_zone_low = band_high - width * 0.33
    upper_zone_high = band_high - width * 0.05

    return AdaptiveRangeResult(
        classification=classification,
        classification_label=label,
        band_low=band_low,
        band_high=band_high,
        band_mid=band_mid,
        band_width_pct=float((width / max(band_mid, 1e-9)) * 100.0),
        current_location=current_location,
        lower_zone_low=float(lower_zone_low),
        lower_zone_high=float(lower_zone_high),
        upper_zone_low=float(upper_zone_low),
        upper_zone_high=float(upper_zone_high),
        slope_bps_per_bar=float(slope_bps_per_bar),
        upper_slope_bps_per_bar=float(upper_slope_bps_per_bar),
        lower_slope_bps_per_bar=float(lower_slope_bps_per_bar),
        containment_ratio=float(containment),
        band_step_change_pct=float(band_step_change),
        higher_low_ratio=float(higher_low_ratio),
        higher_high_ratio=float(higher_high_ratio),
        reversals=reversals,
        lower_zone_touch_count=int(full_stats['lower_zone_touch_count']),
        upper_zone_touch_count=int(full_stats['upper_zone_touch_count']),
        zone_transition_count=int(full_stats['zone_transition_count']),
        completed_cycles=int(full_stats['completed_cycles']),
        breakout_close_ratio=float(full_stats['breakout_close_ratio']),
        directional_efficiency=float(full_stats['directional_efficiency']),
        wickiness_ratio=float(full_stats['wickiness_ratio']),
        recent_lower_zone_touch_count=int(recent_stats['lower_zone_touch_count']),
        recent_upper_zone_touch_count=int(recent_stats['upper_zone_touch_count']),
        recent_zone_transition_count=int(recent_stats['zone_transition_count']),
        recent_completed_cycles=int(recent_stats['completed_cycles']),
        width_retention_ratio=float(width_retention_ratio),
        cycle_persistence_ratio=float(cycle_persistence_ratio),
        recent_breakout_close_ratio=float(recent_stats['breakout_close_ratio']),
        recent_directional_efficiency=float(recent_stats['directional_efficiency']),
        recent_wickiness_ratio=float(recent_stats['wickiness_ratio']),
        recent_close_location_bias=float(recent_close_location_bias),
        bounce_event_count=int(full_stats['bounce_event_count']),
        median_bounce_mfe_pct=float(full_stats['median_bounce_mfe_pct']),
        median_bounce_mae_pct=float(full_stats['median_bounce_mae_pct']),
        bounce_payoff_ratio=float(full_stats['bounce_payoff_ratio']),
        bounce_edge_pct=float(full_stats['bounce_edge_pct']),
        target_excess_pct=float(full_stats['target_excess_pct']),
        upper_reach_ratio=float(full_stats['upper_reach_ratio']),
        target_hit_ratio=float(full_stats['target_hit_ratio']),
        recent_bounce_event_count=int(recent_stats['bounce_event_count']),
        recent_bounce_observation_confidence=float(recent_bounce_confidence),
        recent_median_bounce_mfe_pct=float(recent_stats['median_bounce_mfe_pct']),
        recent_median_bounce_mae_pct=float(recent_stats['median_bounce_mae_pct']),
        recent_bounce_payoff_ratio=float(recent_stats['bounce_payoff_ratio']),
        recent_bounce_edge_pct=float(recent_stats['bounce_edge_pct']),
        recent_target_excess_pct=float(recent_stats['target_excess_pct']),
        recent_shrunk_upper_reach_ratio=float(recent_shrunk_upper_reach_ratio),
        recent_shrunk_target_hit_ratio=float(recent_shrunk_target_hit_ratio),
        recent_upper_reach_ratio=float(recent_stats['upper_reach_ratio']),
        recent_target_hit_ratio=float(recent_stats['target_hit_ratio']),
        bounce_quality_score=float(bounce_quality_score),
        cycle_durability_score=float(cycle_durability_score),
        degradation_score=float(degradation_score),
        dynamic_range_score=dynamic_score,
        details={
            'width_score': round(width_score, 2),
            'containment_score': round(containment_score, 2),
            'cycle_score': round(cycle_score, 2),
            'touch_score': round(touch_score, 2),
            'efficiency_score': round(efficiency_score, 2),
            'bounce_quality_score': round(bounce_quality_score, 2),
            'breakout_penalty': round(breakout_penalty, 2),
            'wick_penalty': round(wick_penalty, 2),
            'step_penalty': round(step_penalty, 2),
            'slope_penalty': round(slope_penalty, 2),
            'cycle_durability_score': round(cycle_durability_score, 2),
            'degradation_score': round(degradation_score, 2),
            'width_retention_ratio': round(width_retention_ratio, 3),
            'cycle_persistence_ratio': round(cycle_persistence_ratio, 3),
            'recent_breakout_close_ratio': round(float(recent_stats['breakout_close_ratio']), 3),
            'recent_directional_efficiency': round(float(recent_stats['directional_efficiency']), 3),
            'recent_wickiness_ratio': round(float(recent_stats['wickiness_ratio']), 3),
            'recent_close_location_bias': round(float(recent_close_location_bias), 3),
            'median_bounce_mfe_pct': round(float(full_stats['median_bounce_mfe_pct']), 3),
            'median_bounce_mae_pct': round(float(full_stats['median_bounce_mae_pct']), 3),
            'bounce_payoff_ratio': round(float(full_stats['bounce_payoff_ratio']), 3),
            'upper_reach_ratio': round(float(full_stats['upper_reach_ratio']), 3),
            'target_hit_ratio': round(float(full_stats['target_hit_ratio']), 3),
            'recent_median_bounce_mfe_pct': round(float(recent_stats['median_bounce_mfe_pct']), 3),
            'recent_median_bounce_mae_pct': round(float(recent_stats['median_bounce_mae_pct']), 3),
            'recent_bounce_payoff_ratio': round(float(recent_stats['bounce_payoff_ratio']), 3),
            'recent_upper_reach_ratio': round(float(recent_stats['upper_reach_ratio']), 3),
            'recent_target_hit_ratio': round(float(recent_stats['target_hit_ratio']), 3),
            'recent_shrunk_upper_reach_ratio': round(float(recent_shrunk_upper_reach_ratio), 3),
            'recent_shrunk_target_hit_ratio': round(float(recent_shrunk_target_hit_ratio), 3),
            'recent_bounce_event_count': int(recent_stats['bounce_event_count']),
            'recent_bounce_observation_confidence': round(float(recent_bounce_confidence), 3),
            'recent_bounce_edge_pct': round(float(recent_stats['bounce_edge_pct']), 3),
            'recent_target_excess_pct': round(float(recent_stats['target_excess_pct']), 3),
        },
    )
