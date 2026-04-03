from __future__ import annotations

import csv
import io
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

import numpy as np
import pandas as pd

from app.config import Settings
from app.db import Database
from app.services.adaptive_range import analyze_incremental_range
from app.services.alpaca_client import AlpacaClient
from app.services.history_cache import fetch_or_cache_frame_map
from app.services.market_time import get_session_for_day, iso_z, list_trading_days
from app.services.scoring import build_candidate_score
from app.services.shared_logic import average_daily_dollar_volume, build_stage1_target_group, build_stage1_target_group_with_alignment, quality_filter_reason, spread_bps
from app.services.universe import load_universe

logger = logging.getLogger(__name__)
ProgressCallback = Optional[Callable[[float, str], None]]


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if number != number or number in {float('inf'), float('-inf')}:
        return float(default)
    return float(number)


def _safe_optional_float(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in {float('inf'), float('-inf')}:
        return None
    return float(number)


def _numeric_list(values: List[object]) -> List[float]:
    output: List[float] = []
    for value in values:
        number = _safe_optional_float(value)
        if number is not None:
            output.append(number)
    return output


def _ensure_timestamp_column(df: pd.DataFrame | None) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df
    working = df.copy()
    if 'timestamp' in working.columns and not pd.api.types.is_datetime64_any_dtype(working['timestamp']):
        working['timestamp'] = pd.to_datetime(working['timestamp'], utc=True, errors='coerce')
        working = working.dropna(subset=['timestamp']).reset_index(drop=True)
    return working


def _score_bucket(score: float) -> str:
    bounded = min(max(_safe_float(score, 0.0), 0.0), 99.999)
    lower = int(bounded // 20) * 20
    upper = min(lower + 20, 100)
    return f'{lower}-{upper}'


def _mover_bucket(rank: int) -> str:
    if rank <= 5:
        return '1-5'
    if rank <= 10:
        return '6-10'
    if rank <= 20:
        return '11-20'
    if rank <= 35:
        return '21-35'
    return '36-50'


def _row_rank_score(row: Dict[str, object]) -> float:
    book = row.get('recommendation_book') or (row.get('metrics') or {}).get('recommendation_book')
    if book == 'pullback_queue':
        if row.get('pullback_queue_rank_score') is not None:
            return _safe_float(row.get('pullback_queue_rank_score'), -1.0)
        metrics = row.get('metrics') or {}
        if metrics.get('pullback_queue_rank_score') is not None:
            return _safe_float(metrics.get('pullback_queue_rank_score'), -1.0)
    if row.get('headline_rank_score') is not None:
        return _safe_float(row.get('headline_rank_score'), -1.0)
    metrics = row.get('metrics') or {}
    if metrics.get('headline_rank_score') is not None:
        return _safe_float(metrics.get('headline_rank_score'), -1.0)
    return _safe_float(row.get('total_score'), -1.0)


def _trade_window_deadline(checkpoint_ts: pd.Timestamp, minutes_remaining_to_close: int, buffer_minutes_before_close: int) -> pd.Timestamp:
    tradable_minutes = max(int(minutes_remaining_to_close) - int(buffer_minutes_before_close), 0)
    return checkpoint_ts + pd.Timedelta(minutes=tradable_minutes)


def _find_entry_touch(full_day_bars: pd.DataFrame, checkpoint_ts: pd.Timestamp, entry_low: float, entry_high: float, deadline_ts: pd.Timestamp, settings: Settings) -> Dict[str, object]:
    after = full_day_bars[(full_day_bars['timestamp'] > checkpoint_ts) & (full_day_bars['timestamp'] <= deadline_ts)].copy()
    if after.empty:
        return {
            'entry_touched': False,
            'entry_timestamp': None,
            'entry_price': None,
            'minutes_to_entry': None,
            'entry_fill_method': 'no_post_scan_bars_in_trade_window',
        }

    zone_low = float(min(entry_low, entry_high))
    zone_high = float(max(entry_low, entry_high))
    zone_mid = (zone_low + zone_high) / 2.0
    fill_mode = str(settings.replay_entry_fill_mode or 'zone_mid').strip().lower()

    for _, row in after.iterrows():
        bar_low = float(row['low'])
        bar_high = float(row['high'])
        if bar_low > zone_high or bar_high < zone_low:
            continue

        if fill_mode == 'bar_typical_clipped':
            typical = (float(row['open']) + float(row['high']) + float(row['low']) + float(row['close'])) / 4.0
            fill_price = float(np.clip(typical, zone_low, zone_high))
            fill_method = 'bar_typical_clipped_to_zone'
        else:
            fill_price = zone_mid
            fill_method = 'zone_mid_conservative_fill'

        entry_ts = pd.Timestamp(row['timestamp'])
        return {
            'entry_touched': True,
            'entry_timestamp': entry_ts,
            'entry_price': round(float(fill_price), 4),
            'minutes_to_entry': int((entry_ts - checkpoint_ts).total_seconds() // 60),
            'entry_fill_method': fill_method,
        }
    return {
        'entry_touched': False,
        'entry_timestamp': None,
        'entry_price': None,
        'minutes_to_entry': None,
        'entry_fill_method': 'zone_not_touched_within_trade_window',
    }

def _post_entry_outcome(full_day_bars: pd.DataFrame, entry_timestamp: pd.Timestamp, entry_price: float, target_pct: float, deadline_ts: pd.Timestamp, settings: Settings, spread_bps_value: float) -> Dict[str, object]:
    future = full_day_bars[(full_day_bars['timestamp'] > entry_timestamp) & (full_day_bars['timestamp'] <= deadline_ts)].copy()
    if future.empty:
        return {
            'hit_target': False,
            'minutes_to_target': None,
            'mfe_pct': None,
            'mae_pct': None,
            'end_of_window_return_pct': None,
            'net_end_of_window_return_pct': None,
            'round_trip_cost_bps': round(float(spread_bps_value * float(settings.replay_spread_cost_multiplier) + float(settings.replay_slippage_bps_per_side) * 2.0), 3),
            'target_timestamp': None,
            'target_fill_method': str(settings.replay_target_hit_mode or 'close_confirmed'),
        }
    highs = future['high'] / max(entry_price, 1e-9) - 1.0
    lows = future['low'] / max(entry_price, 1e-9) - 1.0
    closes = future['close'] / max(entry_price, 1e-9) - 1.0
    target_ratio = target_pct / 100.0
    close_buffer = float(settings.replay_target_close_buffer_bps) / 10000.0
    hit_mode = str(settings.replay_target_hit_mode or 'close_confirmed').strip().lower()
    if hit_mode == 'close_with_buffer':
        qualifying = closes[closes >= max(target_ratio - close_buffer, 0.0)]
        target_fill_method = 'close_with_buffer'
    else:
        qualifying = closes[closes >= target_ratio]
        target_fill_method = 'close_confirmed'
    hit = not qualifying.empty
    target_ts = None
    minutes_to_target = None
    if hit:
        target_ts = pd.Timestamp(future.loc[qualifying.index[0], 'timestamp'])
        minutes_to_target = int((target_ts - entry_timestamp).total_seconds() // 60)

    round_trip_cost_bps = float(spread_bps_value * float(settings.replay_spread_cost_multiplier) + float(settings.replay_slippage_bps_per_side) * 2.0)
    round_trip_cost_pct = round_trip_cost_bps / 100.0
    end_of_window_return_pct = float((future['close'].iloc[-1] / max(entry_price, 1e-9) - 1.0) * 100.0)
    return {
        'hit_target': hit,
        'minutes_to_target': minutes_to_target,
        'mfe_pct': round(float(highs.max() * 100.0), 3),
        'mae_pct': round(float(lows.min() * 100.0), 3),
        'end_of_window_return_pct': round(end_of_window_return_pct, 3),
        'net_end_of_window_return_pct': round(end_of_window_return_pct - round_trip_cost_pct, 3),
        'round_trip_cost_bps': round(round_trip_cost_bps, 3),
        'target_timestamp': target_ts.isoformat() if target_ts is not None else None,
        'target_fill_method': target_fill_method,
    }


def _post_scan_structural_label(post_scan_bars: pd.DataFrame, settings: Settings, target_pct: float) -> Dict[str, object]:
    if post_scan_bars is None or len(post_scan_bars) < 15:
        return {
            'structural_tradability_label': False,
            'post_scan_structure_reason': 'Too few post-scan bars to evaluate structural tradability.',
            'post_scan_range_classification': None,
            'post_scan_completed_cycles': 0,
            'post_scan_containment_ratio': None,
        }
    try:
        result = analyze_incremental_range(post_scan_bars.reset_index(drop=True), target_pct=target_pct)
        label = bool(
            result.classification in {'A', 'B'}
            and int(result.completed_cycles) >= int(settings.structural_post_scan_min_completed_cycles)
            and float(result.containment_ratio) >= float(settings.structural_post_scan_min_containment_ratio)
        )
        reason = None if label else 'Post-scan range did not remain structurally tradable enough under the stricter post-scan label.'
        return {
            'structural_tradability_label': label,
            'post_scan_structure_reason': reason,
            'post_scan_range_classification': result.classification,
            'post_scan_completed_cycles': int(result.completed_cycles),
            'post_scan_containment_ratio': round(float(result.containment_ratio), 3),
        }
    except Exception:
        return {
            'structural_tradability_label': False,
            'post_scan_structure_reason': 'Post-scan structural analysis failed.',
            'post_scan_range_classification': None,
            'post_scan_completed_cycles': 0,
            'post_scan_containment_ratio': None,
        }

def average_daily_precision(rows: List[Dict[str, object]], n: int, score_key: str = 'total_score') -> float:
    if not rows:
        return 0.0
    grouped: Dict[str, List[Dict[str, object]]] = defaultdict(list)
    for row in rows:
        grouped[str(row['trading_day'])].append(row)
    daily_scores = []
    for day_rows in grouped.values():
        ranked = sorted(day_rows, key=lambda x: _row_rank_score(x) if score_key == 'total_score' else _safe_float(x.get(score_key), -1.0), reverse=True)[:n]
        if not ranked:
            continue
        daily_scores.append(sum(1 for row in ranked if row.get('hit_target')) / len(ranked))
    return round(float(np.mean(daily_scores)), 4) if daily_scores else 0.0


def average_daily_precision_by_rank(rows: List[Dict[str, object]], n: int, rank_key: str = 'mover_rank') -> float:
    if not rows:
        return 0.0
    grouped: Dict[str, List[Dict[str, object]]] = defaultdict(list)
    for row in rows:
        grouped[str(row['trading_day'])].append(row)
    daily_scores = []
    for day_rows in grouped.values():
        ranked = sorted(day_rows, key=lambda x: int(_safe_float(x.get(rank_key), 999999)))[:n]
        if not ranked:
            continue
        daily_scores.append(sum(1 for row in ranked if row.get('hit_target')) / len(ranked))
    return round(float(np.mean(daily_scores)), 4) if daily_scores else 0.0


def average_daily_precision_conditioned(rows: List[Dict[str, object]], n: int, score_key: str = 'total_score') -> float:
    touched_rows = [row for row in rows if row.get('entry_touched')]
    if not touched_rows:
        return 0.0
    return average_daily_precision(touched_rows, n, score_key=score_key)


def _hit_rate_conditioned(rows: List[Dict[str, object]]) -> float:
    touched_rows = [row for row in rows if row.get('entry_touched')]
    if not touched_rows:
        return 0.0
    return round(sum(1 for row in touched_rows if row.get('hit_target')) / len(touched_rows), 4)


def _hit_rate(rows: List[Dict[str, object]]) -> float:
    if not rows:
        return 0.0
    return round(sum(1 for row in rows if row.get('hit_target')) / len(rows), 4)


def _validation_verdict(summary: Dict[str, object]) -> Dict[str, object]:
    checks = []
    advanced_total = int(summary.get('advanced_stage2_total') or 0)
    overall_hit = float(summary.get('overall_hit_rate') or 0.0)
    p10 = float(summary.get('precision_at_10') or 0.0)
    monotonicity = summary.get('score_bucket_monotonicity') or {}
    baseline = (summary.get('baseline_comparison') or {}).get('mover_rank_only') or {}
    entry_touch_rate = float(summary.get('entry_touch_rate_stage2') or 0.0)
    avg_cycles = float(summary.get('avg_completed_cycles_stage2') or 0.0)

    checks.append({'name': 'enough_sample', 'ok': advanced_total >= 50, 'detail': f'Advanced rows={advanced_total} (need at least 50).'})
    checks.append({'name': 'signal_above_noise', 'ok': p10 >= max(overall_hit, 0.2), 'detail': f'Precision@10={p10:.3f}, overall hit rate={overall_hit:.3f}.'})
    checks.append({'name': 'score_monotonicity', 'ok': bool(monotonicity.get('ok')), 'detail': f"Bucket monotonicity ratio={float(monotonicity.get('ratio') or 0.0):.3f}."})
    checks.append({'name': 'beats_mover_rank_baseline', 'ok': p10 > float(baseline.get('precision_at_10') or 0.0), 'detail': f"Stage-2 P@10={p10:.3f}, mover-rank-only P@10={float(baseline.get('precision_at_10') or 0.0):.3f}."})
    checks.append({'name': 'post_scan_entry_touch_exists', 'ok': entry_touch_rate >= 0.30, 'detail': f'Entry touch rate among stage-2 names={entry_touch_rate:.3f}; target credit only starts after a post-scan zone touch.'})
    checks.append({'name': 'repeatability_observed', 'ok': avg_cycles >= 2.0, 'detail': f'Average completed low-to-high cycles observed before scan={avg_cycles:.2f}.'})

    ok_count = sum(1 for item in checks if item['ok'])
    critical_fail = not checks[2]['ok'] or not checks[3]['ok']
    if ok_count == len(checks):
        verdict = 'PASS'
    elif ok_count >= 4 and not critical_fail:
        verdict = 'REVIEW'
    else:
        verdict = 'FAIL'
    return {'verdict': verdict, 'checks': checks}


def summarize_validation_rows(rows: List[Dict[str, object]], settings: Settings) -> Dict[str, object]:
    scored_rows = [row for row in rows if row.get('scored_for_replay')]
    advanced_rows = [row for row in scored_rows if row.get('advanced_to_stage2')]
    baseline_rows = [row for row in scored_rows if row.get('baseline_eligible', True)]

    if advanced_rows:
        by_score_bucket = defaultdict(list)
        by_mover_bucket = defaultdict(list)
        by_day = defaultdict(list)
        minutes_to_entry = []
        minutes_to_target = []
        false_positives = []
        for row in advanced_rows:
            by_score_bucket[row['score_bucket']].append(row)
            by_mover_bucket[row['mover_bucket']].append(row)
            by_day[str(row['trading_day'])].append(row)
            if row.get('minutes_to_entry') is not None:
                minutes_to_entry.append(row['minutes_to_entry'])
            if row.get('minutes_to_target') is not None:
                minutes_to_target.append(row['minutes_to_target'])
            if not row.get('hit_target'):
                false_positives.append(row)

        score_bucket_summary = [
            {'bucket': bucket, 'count': len(bucket_rows), 'hit_rate': round(sum(1 for row in bucket_rows if row['hit_target']) / len(bucket_rows), 4)}
            for bucket, bucket_rows in sorted(by_score_bucket.items())
        ]
        mover_bucket_order = ['1-5', '6-10', '11-20', '21-35', '36-50']
        mover_bucket_summary = [
            {'bucket': bucket, 'count': len(by_mover_bucket[bucket]), 'hit_rate': round(sum(1 for row in by_mover_bucket[bucket] if row['hit_target']) / len(by_mover_bucket[bucket]), 4)}
            for bucket in mover_bucket_order if bucket in by_mover_bucket
        ]
        daily_summaries = []
        for trading_day, day_rows in sorted(by_day.items()):
            ranked = sorted(day_rows, key=_row_rank_score, reverse=True)
            daily_summaries.append({
                'trading_day': trading_day,
                'advanced_count': len(day_rows),
                'overall_hit_rate': round(sum(1 for row in day_rows if row['hit_target']) / len(day_rows), 4),
                'precision_at_5': round(sum(1 for row in ranked[:5] if row['hit_target']) / max(len(ranked[:5]), 1), 4),
                'precision_at_10': round(sum(1 for row in ranked[:10] if row['hit_target']) / max(len(ranked[:10]), 1), 4),
                'entry_touch_rate': round(sum(1 for row in day_rows if row.get('entry_touched')) / len(day_rows), 4),
            })

        monotonic_pairs_tested = max(len(score_bucket_summary) - 1, 0)
        monotonic_pairs_non_decreasing = 0
        for idx in range(len(score_bucket_summary) - 1):
            if score_bucket_summary[idx + 1]['hit_rate'] + 1e-9 >= score_bucket_summary[idx]['hit_rate']:
                monotonic_pairs_non_decreasing += 1
        monotonicity_ratio = round(monotonic_pairs_non_decreasing / monotonic_pairs_tested, 3) if monotonic_pairs_tested else 0.0
    else:
        score_bucket_summary = []
        mover_bucket_summary = []
        daily_summaries = []
        minutes_to_entry = []
        minutes_to_target = []
        monotonic_pairs_tested = 0
        monotonic_pairs_non_decreasing = 0
        monotonicity_ratio = 0.0
        false_positives = []

    baseline_summary = {
        'mover_rank_only': {
            'precision_at_5': average_daily_precision_by_rank(baseline_rows, 5),
            'precision_at_10': average_daily_precision_by_rank(baseline_rows, 10),
            'precision_at_20': average_daily_precision_by_rank(baseline_rows, 20),
            'overall_hit_rate': _hit_rate(baseline_rows),
            'eligible_rows': len(baseline_rows),
        },
        'stage2_delta_vs_mover_rank': {
            'precision_at_5': round(average_daily_precision(advanced_rows, 5) - average_daily_precision_by_rank(baseline_rows, 5), 4),
            'precision_at_10': round(average_daily_precision(advanced_rows, 10) - average_daily_precision_by_rank(baseline_rows, 10), 4),
            'precision_at_20': round(average_daily_precision(advanced_rows, 20) - average_daily_precision_by_rank(baseline_rows, 20), 4),
        },
    }

    structural_positive_rows = [row for row in scored_rows if row.get('structural_tradability_label')]
    entry_accessible_rows = [row for row in advanced_rows if row.get('entry_accessibility_label')]
    entry_touched_rows = [row for row in advanced_rows if row.get('entry_touched')]
    follow_through_rows = [row for row in entry_touched_rows if row.get('follow_through_quality_label')]
    tier_rows = defaultdict(list)
    book_rows = defaultdict(list)
    lane_rows = defaultdict(list)
    for row in advanced_rows:
        tier_rows[str(row.get('recommendation_tier') or row.get('metrics', {}).get('recommendation_tier') or 'unknown')].append(row)
        book_rows[str(row.get('recommendation_book') or row.get('metrics', {}).get('recommendation_book') or 'unknown')].append(row)
        lane_rows[str(row.get('execution_lane') or row.get('metrics', {}).get('execution_lane') or 'unknown')].append(row)

    headline_rows = [row for row in advanced_rows if str(row.get('recommendation_tier') or row.get('metrics', {}).get('recommendation_tier') or '') == 'headline_shortlist']
    ready_rows = [row for row in advanced_rows if str(row.get('recommendation_tier') or '') == 'ready_now']
    near_ready_rows = [row for row in advanced_rows if str(row.get('recommendation_tier') or '') == 'near_ready']
    watchlist_rows = [row for row in advanced_rows if str(row.get('recommendation_tier') or '') == 'watchlist']
    touch_soon_queue_rows = [row for row in advanced_rows if str(row.get('recommendation_book') or row.get('metrics', {}).get('recommendation_book') or '') == 'touch_soon_queue']
    touch_later_queue_rows = [row for row in advanced_rows if str(row.get('recommendation_book') or row.get('metrics', {}).get('recommendation_book') or '') == 'touch_later_queue']
    structural_watchlist_rows = [row for row in advanced_rows if str(row.get('recommendation_book') or row.get('metrics', {}).get('recommendation_book') or '') == 'structural_watchlist']
    actionable_lane_rows = [row for row in advanced_rows if str(row.get('execution_lane') or row.get('metrics', {}).get('execution_lane') or '') == 'actionable_now']
    monitor_5m_rows = [row for row in advanced_rows if str(row.get('execution_lane') or row.get('metrics', {}).get('execution_lane') or '') == 'monitor_5m']
    monitor_15m_rows = [row for row in advanced_rows if str(row.get('execution_lane') or row.get('metrics', {}).get('execution_lane') or '') == 'monitor_15m']
    passive_watchlist_rows = [row for row in advanced_rows if str(row.get('execution_lane') or row.get('metrics', {}).get('execution_lane') or '') == 'passive_watchlist']
    touch_window_rows = defaultdict(list)
    for row in advanced_rows:
        touch_window_rows[str(row.get('touch_window_band') or row.get('metrics', {}).get('touch_window_band') or 'unknown')].append(row)
    stage_funnel_summary = {
        'scored_rows': len(scored_rows),
        'advanced_stage2': len(advanced_rows),
        'headline_shortlist': len(headline_rows),
        'ready_now': len(ready_rows),
        'near_ready': len(near_ready_rows),
        'watchlist': len(watchlist_rows),
        'pullback_queue': len(touch_soon_queue_rows) + len(touch_later_queue_rows),
        'touch_soon_queue': len(touch_soon_queue_rows),
        'touch_later_queue': len(touch_later_queue_rows),
        'structural_watchlist': len(structural_watchlist_rows),
        'actionable_lane': len(actionable_lane_rows),
        'monitor_5m': len(monitor_5m_rows),
        'monitor_15m': len(monitor_15m_rows),
        'passive_watchlist': len(passive_watchlist_rows),
        'entry_accessible': len(entry_accessible_rows),
        'entry_touched': len(entry_touched_rows),
        'follow_through_positive': len(follow_through_rows),
        'rates': {
            'advanced_from_scored': round(len(advanced_rows) / max(len(scored_rows), 1), 4) if scored_rows else 0.0,
            'headline_from_advanced': round(len(headline_rows) / max(len(advanced_rows), 1), 4) if advanced_rows else 0.0,
            'ready_now_from_advanced': round(len(ready_rows) / max(len(advanced_rows), 1), 4) if advanced_rows else 0.0,
            'near_ready_from_advanced': round(len(near_ready_rows) / max(len(advanced_rows), 1), 4) if advanced_rows else 0.0,
            'pullback_queue_from_advanced': round((len(touch_soon_queue_rows) + len(touch_later_queue_rows)) / max(len(advanced_rows), 1), 4) if advanced_rows else 0.0,
            'touch_soon_queue_from_advanced': round(len(touch_soon_queue_rows) / max(len(advanced_rows), 1), 4) if advanced_rows else 0.0,
            'touch_later_queue_from_advanced': round(len(touch_later_queue_rows) / max(len(advanced_rows), 1), 4) if advanced_rows else 0.0,
            'structural_watchlist_from_advanced': round(len(structural_watchlist_rows) / max(len(advanced_rows), 1), 4) if advanced_rows else 0.0,
            'actionable_lane_from_advanced': round(len(actionable_lane_rows) / max(len(advanced_rows), 1), 4) if advanced_rows else 0.0,
            'monitor_5m_from_advanced': round(len(monitor_5m_rows) / max(len(advanced_rows), 1), 4) if advanced_rows else 0.0,
            'monitor_15m_from_advanced': round(len(monitor_15m_rows) / max(len(advanced_rows), 1), 4) if advanced_rows else 0.0,
            'passive_watchlist_from_advanced': round(len(passive_watchlist_rows) / max(len(advanced_rows), 1), 4) if advanced_rows else 0.0,
            'entry_accessible_from_advanced': round(len(entry_accessible_rows) / max(len(advanced_rows), 1), 4) if advanced_rows else 0.0,
            'entry_touched_from_advanced': round(len(entry_touched_rows) / max(len(advanced_rows), 1), 4) if advanced_rows else 0.0,
            'follow_through_from_entry_touched': round(len(follow_through_rows) / max(len(entry_touched_rows), 1), 4) if entry_touched_rows else 0.0,
        },
    }

    range_class_counts = defaultdict(int)
    range_exclusions = defaultdict(int)
    for row in scored_rows:
        code = str(row.get('range_classification_code') or 'unknown')
        range_class_counts[code] += 1
        if not row.get('advanced_to_stage2') and row.get('stage2_exclusion_reason'):
            range_exclusions[code] += 1

    mfe_values = _numeric_list([row.get('mfe_pct') for row in advanced_rows])
    mae_values = _numeric_list([row.get('mae_pct') for row in advanced_rows])
    completed_cycles_values = _numeric_list([row.get('metrics', {}).get('completed_cycles_observed') for row in advanced_rows])
    lower_touch_values = _numeric_list([row.get('metrics', {}).get('lower_zone_touch_count') for row in advanced_rows])
    upper_touch_values = _numeric_list([row.get('metrics', {}).get('upper_zone_touch_count') for row in advanced_rows])

    summary = {
        'days': len({row['trading_day'] for row in rows}),
        'top50_stage1_per_day': settings.top_mover_count,
        'scored_replay_rows_total': len(scored_rows),
        'advanced_stage2_total': len(advanced_rows),
        'overall_hit_rate': _hit_rate(advanced_rows),
        'precision_at_5': average_daily_precision(advanced_rows, 5),
        'precision_at_10': average_daily_precision(advanced_rows, 10),
        'precision_at_20': average_daily_precision(advanced_rows, 20),
        'conditional_hit_rate_entry_touched': _hit_rate_conditioned(advanced_rows),
        'conditional_precision_at_5_entry_touched': average_daily_precision_conditioned(advanced_rows, 5),
        'conditional_precision_at_10_entry_touched': average_daily_precision_conditioned(advanced_rows, 10),
        'conditional_precision_at_20_entry_touched': average_daily_precision_conditioned(advanced_rows, 20),
        'avg_mfe_pct': round(float(np.mean(mfe_values)), 3) if mfe_values else None,
        'avg_mae_pct': round(float(np.mean(mae_values)), 3) if mae_values else None,
        'median_minutes_to_entry': int(np.median(minutes_to_entry)) if minutes_to_entry else None,
        'median_minutes_to_target': int(np.median(minutes_to_target)) if minutes_to_target else None,
        'entry_touch_rate_stage2': round(sum(1 for row in advanced_rows if row.get('entry_touched')) / len(advanced_rows), 4) if advanced_rows else 0.0,
        'entry_touch_rate_scored': round(sum(1 for row in scored_rows if row.get('entry_touched')) / len(scored_rows), 4) if scored_rows else 0.0,
        'avg_completed_cycles_stage2': round(float(np.mean(completed_cycles_values)), 3) if completed_cycles_values else 0.0,
        'avg_lower_zone_touches_stage2': round(float(np.mean(lower_touch_values)), 3) if lower_touch_values else 0.0,
        'avg_upper_zone_touches_stage2': round(float(np.mean(upper_touch_values)), 3) if upper_touch_values else 0.0,
        'stage_label_summary': {
            'structural_tradability_positive_count': len(structural_positive_rows),
            'entry_accessibility_positive_count': len(entry_accessible_rows),
            'follow_through_quality_positive_count': len(follow_through_rows),
            'structural_tradability_positive_rate_scored': round(len(structural_positive_rows) / max(len(scored_rows), 1), 4) if scored_rows else 0.0,
            'entry_accessibility_positive_rate_stage2': round(len(entry_accessible_rows) / max(len(advanced_rows), 1), 4) if advanced_rows else 0.0,
            'follow_through_quality_positive_rate_entry_touched': round(len(follow_through_rows) / max(len(entry_touched_rows), 1), 4) if entry_touched_rows else 0.0,
        },
        'tier_summary': [
            {
                'tier': tier,
                'count': len(rows_in_tier),
                'hit_rate': round(sum(1 for row in rows_in_tier if row.get('hit_target')) / len(rows_in_tier), 4),
                'entry_touch_rate': round(sum(1 for row in rows_in_tier if row.get('entry_touched')) / len(rows_in_tier), 4),
                'conditional_hit_rate_entry_touched': round(sum(1 for row in rows_in_tier if row.get('hit_target') and row.get('entry_touched')) / max(sum(1 for row in rows_in_tier if row.get('entry_touched')), 1), 4) if any(row.get('entry_touched') for row in rows_in_tier) else 0.0,
                'avg_distance_to_entry_pct': round(float(np.mean(_numeric_list([row.get('metrics', {}).get('distance_to_entry_pct', row.get('distance_to_entry_pct')) for row in rows_in_tier]))), 3) if _numeric_list([row.get('metrics', {}).get('distance_to_entry_pct', row.get('distance_to_entry_pct')) for row in rows_in_tier]) else None,
                'avg_execution_readiness_score': round(float(np.mean(_numeric_list([row.get('execution_readiness_score', row.get('metrics', {}).get('execution_readiness_score')) for row in rows_in_tier]))), 3) if _numeric_list([row.get('execution_readiness_score', row.get('metrics', {}).get('execution_readiness_score')) for row in rows_in_tier]) else None,
                'avg_entry_touch_likelihood_score': round(float(np.mean(_numeric_list([row.get('entry_touch_likelihood_score', row.get('metrics', {}).get('entry_touch_likelihood_score')) for row in rows_in_tier]))), 3) if _numeric_list([row.get('entry_touch_likelihood_score', row.get('metrics', {}).get('entry_touch_likelihood_score')) for row in rows_in_tier]) else None,
                'avg_follow_through_confidence_score': round(float(np.mean(_numeric_list([row.get('follow_through_confidence_score', row.get('metrics', {}).get('follow_through_confidence_score')) for row in rows_in_tier]))), 3) if _numeric_list([row.get('follow_through_confidence_score', row.get('metrics', {}).get('follow_through_confidence_score')) for row in rows_in_tier]) else None,
                'avg_expected_actionability_score': round(float(np.mean(_numeric_list([row.get('expected_actionability_score', row.get('metrics', {}).get('expected_actionability_score')) for row in rows_in_tier]))), 3) if _numeric_list([row.get('expected_actionability_score', row.get('metrics', {}).get('expected_actionability_score')) for row in rows_in_tier]) else None,
                'avg_actionability_score': round(float(np.mean(_numeric_list([row.get('actionability_score', row.get('metrics', {}).get('actionability_score')) for row in rows_in_tier]))), 3) if _numeric_list([row.get('actionability_score', row.get('metrics', {}).get('actionability_score')) for row in rows_in_tier]) else None,
                'avg_headline_rank_score': round(float(np.mean(_numeric_list([row.get('headline_rank_score', row.get('metrics', {}).get('headline_rank_score')) for row in rows_in_tier]))), 3) if _numeric_list([row.get('headline_rank_score', row.get('metrics', {}).get('headline_rank_score')) for row in rows_in_tier]) else None,
                'avg_touch_urgency_score': round(float(np.mean(_numeric_list([row.get('touch_urgency_score', row.get('metrics', {}).get('touch_urgency_score')) for row in rows_in_tier]))), 3) if _numeric_list([row.get('touch_urgency_score', row.get('metrics', {}).get('touch_urgency_score')) for row in rows_in_tier]) else None,
                'avg_expected_minutes_to_touch': round(float(np.mean(_numeric_list([row.get('expected_minutes_to_touch', row.get('metrics', {}).get('expected_minutes_to_touch')) for row in rows_in_tier]))), 3) if _numeric_list([row.get('expected_minutes_to_touch', row.get('metrics', {}).get('expected_minutes_to_touch')) for row in rows_in_tier]) else None,
            }
            for tier, rows_in_tier in sorted(tier_rows.items())
        ],
        'book_summary': [
            {
                'book': book,
                'count': len(rows_in_book),
                'hit_rate': round(sum(1 for row in rows_in_book if row.get('hit_target')) / len(rows_in_book), 4),
                'entry_touch_rate': round(sum(1 for row in rows_in_book if row.get('entry_touched')) / len(rows_in_book), 4),
                'conditional_hit_rate_entry_touched': round(sum(1 for row in rows_in_book if row.get('hit_target') and row.get('entry_touched')) / max(sum(1 for row in rows_in_book if row.get('entry_touched')), 1), 4) if any(row.get('entry_touched') for row in rows_in_book) else 0.0,
                'avg_distance_to_entry_pct': round(float(np.mean(_numeric_list([row.get('metrics', {}).get('distance_to_entry_pct') for row in rows_in_book]))), 3) if _numeric_list([row.get('metrics', {}).get('distance_to_entry_pct') for row in rows_in_book]) else None,
                'avg_pullback_queue_rank_score': round(float(np.mean(_numeric_list([row.get('metrics', {}).get('pullback_queue_rank_score') for row in rows_in_book]))), 3) if _numeric_list([row.get('metrics', {}).get('pullback_queue_rank_score') for row in rows_in_book]) else None,
                'avg_headline_rank_score': round(float(np.mean(_numeric_list([row.get('metrics', {}).get('headline_rank_score') for row in rows_in_book]))), 3) if _numeric_list([row.get('metrics', {}).get('headline_rank_score') for row in rows_in_book]) else None,
                'avg_touch_urgency_score': round(float(np.mean(_numeric_list([row.get('metrics', {}).get('touch_urgency_score') for row in rows_in_book]))), 3) if _numeric_list([row.get('metrics', {}).get('touch_urgency_score') for row in rows_in_book]) else None,
                'avg_expected_minutes_to_touch': round(float(np.mean(_numeric_list([row.get('metrics', {}).get('expected_minutes_to_touch') for row in rows_in_book]))), 3) if _numeric_list([row.get('metrics', {}).get('expected_minutes_to_touch') for row in rows_in_book]) else None,
                'avg_queue_actionability_score': round(float(np.mean(_numeric_list([row.get('metrics', {}).get('queue_actionability_score') for row in rows_in_book]))), 3) if _numeric_list([row.get('metrics', {}).get('queue_actionability_score') for row in rows_in_book]) else None,
            }
            for book, rows_in_book in sorted(book_rows.items())
        ],
        'touch_window_summary': [
            {
                'touch_window_band': band,
                'count': len(rows_in_band),
                'hit_rate': round(sum(1 for row in rows_in_band if row.get('hit_target')) / len(rows_in_band), 4),
                'entry_touch_rate': round(sum(1 for row in rows_in_band if row.get('entry_touched')) / len(rows_in_band), 4),
                'conditional_hit_rate_entry_touched': round(sum(1 for row in rows_in_band if row.get('hit_target') and row.get('entry_touched')) / max(sum(1 for row in rows_in_band if row.get('entry_touched')), 1), 4) if any(row.get('entry_touched') for row in rows_in_band) else 0.0,
                'avg_expected_minutes_to_touch': round(float(np.mean(_numeric_list([row.get('expected_minutes_to_touch', row.get('metrics', {}).get('expected_minutes_to_touch')) for row in rows_in_band]))), 3) if _numeric_list([row.get('expected_minutes_to_touch', row.get('metrics', {}).get('expected_minutes_to_touch')) for row in rows_in_band]) else None,
                'avg_touch_urgency_score': round(float(np.mean(_numeric_list([row.get('touch_urgency_score', row.get('metrics', {}).get('touch_urgency_score')) for row in rows_in_band]))), 3) if _numeric_list([row.get('touch_urgency_score', row.get('metrics', {}).get('touch_urgency_score')) for row in rows_in_band]) else None,
            }
            for band, rows_in_band in sorted(touch_window_rows.items())
        ],
        'stage_funnel_summary': stage_funnel_summary,
        'entry_methodology': {
            'requires_post_scan_touch': True,
            'disallows_same_bar_target_credit': True,
            'description': f'A validation hit only counts after the post-scan price action touches the suggested lower-band entry zone using the configured conservative fill method ({settings.replay_entry_fill_mode}), and the target must be confirmed using {settings.replay_target_hit_mode} before the {settings.trade_window_end_buffer_minutes_before_close}-minutes-before-close cutoff.',
        },
        'replay_honesty': {
            'entry_fill_mode': settings.replay_entry_fill_mode,
            'target_hit_mode': settings.replay_target_hit_mode,
            'target_close_buffer_bps': float(settings.replay_target_close_buffer_bps),
            'spread_cost_multiplier': float(settings.replay_spread_cost_multiplier),
            'slippage_bps_per_side': float(settings.replay_slippage_bps_per_side),
            'entry_accessibility_minutes': int(settings.entry_accessibility_minutes),
            'follow_through_max_adverse_excursion_pct': float(settings.follow_through_max_adverse_excursion_pct),
        },
        'score_bucket_summary': score_bucket_summary,
        'mover_bucket_summary': mover_bucket_summary,
        'false_positives_sample': sorted(false_positives, key=_row_rank_score, reverse=True)[:15],
        'daily_summaries': daily_summaries,
        'score_bucket_monotonicity': {
            'ok': monotonicity_ratio >= 0.6 if monotonic_pairs_tested else False,
            'ratio': monotonicity_ratio,
            'pairs_tested': monotonic_pairs_tested,
            'pairs_non_decreasing': monotonic_pairs_non_decreasing,
        },
        'baseline_comparison': baseline_summary,
        'range_structure_distribution': {
            'A': range_class_counts.get('A', 0),
            'B': range_class_counts.get('B', 0),
            'C': range_class_counts.get('C', 0),
            'excluded_by_structure': {'A': range_exclusions.get('A', 0), 'B': range_exclusions.get('B', 0), 'C': range_exclusions.get('C', 0)},
        },
    }
    summary['validation_verdict'] = _validation_verdict(summary)
    return summary


def _report_progress(progress_callback: ProgressCallback, progress: float, message: str) -> None:
    if progress_callback:
        progress_callback(float(progress), message)


def run_validation(settings: Settings, db: Database, alpaca: AlpacaClient, start_date: str, end_date: str, scan_offset_minutes: int, *, progress_callback: ProgressCallback = None, cache_history: bool = False, persist: bool = True) -> Dict[str, object]:
    if not alpaca.has_credentials():
        raise RuntimeError('Alpaca credentials are required for validation.')

    trading_days = list_trading_days(start_date, end_date)
    if not trading_days:
        raise ValueError('No NYSE trading days in the selected date range.')
    if len(trading_days) > settings.max_validation_days:
        raise ValueError(f'Validation range exceeds MAX_VALIDATION_DAYS={settings.max_validation_days}.')

    universe = load_universe(settings, db, force_refresh=False)
    universe_rows = universe['symbols']
    symbols = [row['symbol'] for row in universe_rows if row.get('tradable', True) or row.get('asset_status') == 'unknown']
    company_lookup = {row['symbol']: row.get('company_name') for row in universe_rows}
    tradable_lookup = {row['symbol']: bool(row.get('tradable', True)) for row in universe_rows}

    validation_rows: List[Dict[str, object]] = []
    cache_stats = {'cache_hits': 0, 'cache_misses': 0, 'downloaded_segments': 0}

    for idx, trading_day in enumerate(trading_days, start=1):
        logger.info('Validation replay for %s', trading_day)
        progress_floor = (idx - 1) / max(len(trading_days), 1)
        _report_progress(progress_callback, progress_floor, f'Preparing replay for {trading_day} ({idx}/{len(trading_days)}).')
        session = get_session_for_day(trading_day, scan_offset_minutes)

        stage1_key = f'{trading_day}_{scan_offset_minutes}_stage1'
        if cache_history:
            stage1_bars_map, meta = fetch_or_cache_frame_map(settings, category='validation_stage1', key=stage1_key, loader=lambda: alpaca.fetch_bars(symbols, '1Min', iso_z(session.market_open), iso_z(session.checkpoint)))
            cache_stats['cache_hits' if meta['cache_hit'] else 'cache_misses'] += 1
            if not meta['cache_hit']:
                cache_stats['downloaded_segments'] += 1
        else:
            stage1_bars_map = alpaca.fetch_bars(symbols, '1Min', iso_z(session.market_open), iso_z(session.checkpoint))

        stage1_records = []
        for symbol in symbols:
            bars = _ensure_timestamp_column(stage1_bars_map.get(symbol))
            if bars is None or bars.empty:
                continue
            open_price = float(bars['open'].iloc[0])
            current_price = float(bars['close'].iloc[-1])
            if open_price <= 0:
                continue
            stage1_records.append({
                'symbol': symbol,
                'company_name': company_lookup.get(symbol),
                'tradable': tradable_lookup.get(symbol),
                'session_open': open_price,
                'current_price': current_price,
                'intraday_pct_gain': (current_price / open_price - 1.0) * 100.0,
                'cum_volume': float(bars['volume'].sum()),
                'day_range_pct': float((bars['high'].max() / max(bars['low'].min(), 1e-9) - 1.0) * 100.0),
                'bid': np.nan,
                'ask': np.nan,
            })
        stage1_preview = build_stage1_target_group(
            stage1_records,
            max(
                int(settings.top_mover_count) * max(int(settings.stage1_alignment_pool_multiplier), 1),
                max(int(settings.stage1_alignment_min_pool_size), int(settings.top_mover_count)),
            ),
        )
        stage1_pool_symbols = stage1_preview['symbol'].tolist() if not stage1_preview.empty else []
        daily_key = f'{trading_day}_{scan_offset_minutes}_daily'
        if stage1_pool_symbols:
            if cache_history:
                stage1_daily_bars_map, meta = fetch_or_cache_frame_map(
                    settings,
                    category='validation_daily',
                    key=daily_key,
                    loader=lambda: alpaca.fetch_daily_bars(stage1_pool_symbols, iso_z(session.market_open - timedelta(days=60)), iso_z(session.market_open)),
                )
                cache_stats['cache_hits' if meta['cache_hit'] else 'cache_misses'] += 1
                if not meta['cache_hit']:
                    cache_stats['downloaded_segments'] += 1
            else:
                stage1_daily_bars_map = alpaca.fetch_daily_bars(stage1_pool_symbols, iso_z(session.market_open - timedelta(days=60)), iso_z(session.market_open))
        else:
            stage1_daily_bars_map = {}

        avg_daily_dollar_volume_lookup = {
            symbol: average_daily_dollar_volume(frame)
            for symbol, frame in stage1_daily_bars_map.items()
        }
        stage1_selection = build_stage1_target_group_with_alignment(
            stage1_records,
            settings=settings,
            avg_daily_dollar_volume_lookup=avg_daily_dollar_volume_lookup,
        )
        stage1 = stage1_selection['stage1']
        if stage1.empty:
            continue

        top_symbols = stage1['symbol'].tolist()
        full_key = f'{trading_day}_{scan_offset_minutes}_full'
        if cache_history:
            full_day_bars_map, meta = fetch_or_cache_frame_map(settings, category='validation_full_day', key=full_key, loader=lambda: alpaca.fetch_bars(top_symbols, '1Min', iso_z(session.market_open), iso_z(session.market_close)))
            cache_stats['cache_hits' if meta['cache_hit'] else 'cache_misses'] += 1
            if not meta['cache_hit']:
                cache_stats['downloaded_segments'] += 1
        else:
            full_day_bars_map = alpaca.fetch_bars(top_symbols, '1Min', iso_z(session.market_open), iso_z(session.market_close))
        daily_bars_map = {symbol: stage1_daily_bars_map.get(symbol, pd.DataFrame()) for symbol in top_symbols}

        day_rows = []
        for _, row in stage1.iterrows():
            symbol = row['symbol']
            bars = _ensure_timestamp_column(full_day_bars_map.get(symbol))
            daily_bars = _ensure_timestamp_column(daily_bars_map.get(symbol, pd.DataFrame()))
            if bars is None or bars.empty:
                day_rows.append({'trading_day': trading_day, 'symbol': symbol, 'mover_rank': int(row['mover_rank']), 'intraday_pct_gain': round(float(row['intraday_pct_gain']), 3), 'advanced_to_stage2': False, 'baseline_eligible': False, 'scored_for_replay': False, 'exclusion_reason': 'Missing full-day bars for replay symbol.'})
                continue
            checkpoint_ts = pd.Timestamp(session.checkpoint.astimezone(session.market_open.tzinfo))
            checkpoint_bars = bars[bars['timestamp'] <= checkpoint_ts]
            if checkpoint_bars.empty:
                day_rows.append({'trading_day': trading_day, 'symbol': symbol, 'mover_rank': int(row['mover_rank']), 'intraday_pct_gain': round(float(row['intraday_pct_gain']), 3), 'advanced_to_stage2': False, 'baseline_eligible': False, 'scored_for_replay': False, 'exclusion_reason': 'No bars at checkpoint.'})
                continue

            spread_bps_value = spread_bps(checkpoint_bars)
            avg_daily_dollar_volume = float(daily_bars['close'].tail(min(20, len(daily_bars))).mean() * daily_bars['volume'].tail(min(20, len(daily_bars))).mean()) if not daily_bars.empty and {'close', 'volume'}.issubset(daily_bars.columns) else 0.0
            exclusion_reason = quality_filter_reason(row, settings, avg_daily_dollar_volume, spread_bps_value)
            if exclusion_reason:
                day_rows.append({'trading_day': trading_day, 'symbol': symbol, 'mover_rank': int(row['mover_rank']), 'intraday_pct_gain': round(float(row['intraday_pct_gain']), 3), 'advanced_to_stage2': False, 'baseline_eligible': False, 'scored_for_replay': False, 'exclusion_reason': exclusion_reason})
                continue

            scored = build_candidate_score(row=row, top_stage1=stage1, intraday_bars=checkpoint_bars, daily_bars=daily_bars, spread_bps=spread_bps_value, minutes_remaining=session.minutes_until_close_checkpoint, settings=settings)
            deadline_ts = _trade_window_deadline(checkpoint_ts, session.minutes_until_close_checkpoint, settings.trade_window_end_buffer_minutes_before_close)
            if scored['advanced_to_stage2']:
                entry = _find_entry_touch(bars, checkpoint_ts, float(scored['entry_low']), float(scored['entry_high']), deadline_ts, settings)
                if entry['entry_touched']:
                    outcome = _post_entry_outcome(bars, pd.Timestamp(entry['entry_timestamp']), float(entry['entry_price']), settings.target_pct, deadline_ts, settings, spread_bps_value)
                else:
                    outcome = {'hit_target': False, 'minutes_to_target': None, 'mfe_pct': None, 'mae_pct': None, 'end_of_window_return_pct': None, 'net_end_of_window_return_pct': None, 'round_trip_cost_bps': round(float(spread_bps_value * float(settings.replay_spread_cost_multiplier) + float(settings.replay_slippage_bps_per_side) * 2.0), 3), 'target_timestamp': None, 'target_fill_method': str(settings.replay_target_hit_mode or 'close_confirmed')}
            else:
                entry = {'entry_touched': False, 'entry_timestamp': None, 'entry_price': None, 'minutes_to_entry': None, 'entry_fill_method': 'stage2_gate_not_passed'}
                outcome = {'hit_target': False, 'minutes_to_target': None, 'mfe_pct': None, 'mae_pct': None, 'end_of_window_return_pct': None, 'net_end_of_window_return_pct': None, 'round_trip_cost_bps': round(float(spread_bps_value * float(settings.replay_spread_cost_multiplier) + float(settings.replay_slippage_bps_per_side) * 2.0), 3), 'target_timestamp': None, 'target_fill_method': str(settings.replay_target_hit_mode or 'close_confirmed')}

            post_scan_bars = bars[(bars['timestamp'] > checkpoint_ts) & (bars['timestamp'] <= deadline_ts)].copy()
            structural_label = _post_scan_structural_label(post_scan_bars, settings, settings.target_pct)
            entry_accessibility_label = bool(entry.get('entry_touched') and entry.get('minutes_to_entry') is not None and int(entry['minutes_to_entry']) <= int(settings.entry_accessibility_minutes))
            follow_through_quality_label = bool(entry.get('entry_touched') and outcome.get('hit_target') and (outcome.get('mae_pct') is None or float(outcome.get('mae_pct')) > -float(settings.follow_through_max_adverse_excursion_pct)))

            merged = {
                'trading_day': trading_day,
                'symbol': symbol,
                'mover_rank': int(row['mover_rank']),
                'intraday_pct_gain': round(float(row['intraday_pct_gain']), 3),
                'advanced_to_stage2': bool(scored['advanced_to_stage2']),
                'stage2_exclusion_reason': scored.get('exclusion_reason'),
                'baseline_eligible': True,
                'scored_for_replay': True,
                'range_classification_code': str(scored['metrics'].get('range_classification_code') or 'unknown'),
                'total_score': _safe_float(scored.get('total_score'), 0.0),
                'headline_rank_score': _safe_float(scored.get('headline_rank_score'), _safe_float(scored.get('total_score'), 0.0)),
                'recommendation_tier': scored.get('recommendation_tier'),
                'structural_score': _safe_float(scored.get('structural_score'), 0.0),
                'entry_touch_likelihood_score': _safe_float(scored.get('entry_touch_likelihood_score'), 0.0),
                'follow_through_confidence_score': _safe_float(scored.get('follow_through_confidence_score'), 0.0),
                'expected_actionability_score': _safe_float(scored.get('expected_actionability_score'), 0.0),
                'actionability_score': _safe_float(scored.get('actionability_score'), 0.0),
                'score_bucket': _score_bucket(scored.get('headline_rank_score', scored.get('total_score', 0.0))),
                'mover_bucket': _mover_bucket(int(row['mover_rank'])),
                'entry_price_proxy': entry['entry_price'],
                'entry_touched': bool(entry['entry_touched']),
                'entry_timestamp': entry['entry_timestamp'].isoformat() if entry['entry_timestamp'] is not None else None,
                'entry_fill_method': entry['entry_fill_method'],
                'minutes_to_entry': entry['minutes_to_entry'],
                'target_price': float(scored['target_price']),
                'stretch_target_price': float(scored['stretch_target_price']),
                'hit_target': bool(outcome['hit_target']),
                'minutes_to_target': outcome['minutes_to_target'],
                'mfe_pct': outcome['mfe_pct'],
                'mae_pct': outcome['mae_pct'],
                'end_of_window_return_pct': outcome['end_of_window_return_pct'],
                'target_timestamp': outcome['target_timestamp'],
                'target_fill_method': outcome['target_fill_method'],
                'round_trip_cost_bps': outcome['round_trip_cost_bps'],
                'net_end_of_window_return_pct': outcome['net_end_of_window_return_pct'],
                'structural_tradability_label': structural_label['structural_tradability_label'],
                'post_scan_structure_reason': structural_label['post_scan_structure_reason'],
                'post_scan_range_classification': structural_label['post_scan_range_classification'],
                'post_scan_completed_cycles': structural_label['post_scan_completed_cycles'],
                'post_scan_containment_ratio': structural_label['post_scan_containment_ratio'],
                'entry_accessibility_label': entry_accessibility_label,
                'follow_through_quality_label': follow_through_quality_label,
                'component_scores': scored['component_scores'],
                'metrics': scored['metrics'],
                'rationale': scored['rationale'],
                'replay_method': 'post_scan_touch_then_target_before_two_hours_to_close',
            }
            day_rows.append(merged)

        validation_rows.extend(day_rows)
        _report_progress(progress_callback, idx / max(len(trading_days), 1), f'Finished replay for {trading_day} ({idx}/{len(trading_days)}).')

    summary = summarize_validation_rows(validation_rows, settings)
    summary['download_summary'] = {
        'cache_enabled': cache_history,
        'cache_hits': cache_stats['cache_hits'],
        'cache_misses': cache_stats['cache_misses'],
        'downloaded_segments': cache_stats['downloaded_segments'],
        'days_requested': len(trading_days),
    }

    payload = {'created_at': datetime.now(timezone.utc).isoformat(), 'start_date': start_date, 'end_date': end_date, 'scan_offset_minutes': scan_offset_minutes, 'status': 'ok', 'summary': summary}
    if persist:
        validation_id = db.insert_validation_run(payload, validation_rows)
        payload['id'] = validation_id
    else:
        payload['id'] = 0
    payload['rows'] = validation_rows
    return payload


def validation_rows_to_csv(rows: List[Dict[str, object]]) -> str:
    if not rows:
        return ''
    flat_rows = []
    for row in rows:
        base = {k: v for k, v in row.items() if k not in {'component_scores', 'metrics'}}
        for k, v in (row.get('component_scores') or {}).items():
            base[f'component_{k}'] = v
        metrics = row.get('metrics') or {}
        for k, v in metrics.items():
            if isinstance(v, (dict, list)):
                continue
            base[f'metric_{k}'] = v
        flat_rows.append(base)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=sorted({key for row in flat_rows for key in row.keys()}))
    writer.writeheader()
    writer.writerows(flat_rows)
    return buffer.getvalue()
