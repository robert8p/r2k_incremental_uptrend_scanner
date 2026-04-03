from __future__ import annotations

from typing import Dict, List

import numpy as np
import pandas as pd

from app.config import Settings


def spread_bps(df: pd.DataFrame) -> float:
    if df.empty:
        return 999.0
    recent = df.tail(min(10, len(df)))
    avg_range = ((recent['high'] - recent['low']) / recent['close'].replace(0, np.nan) * 10000.0).mean()
    return float(np.clip(avg_range * 0.35, 2.0, 150.0))


def average_daily_dollar_volume(daily_bars: pd.DataFrame) -> float:
    if daily_bars.empty or not {'close', 'volume'}.issubset(daily_bars.columns):
        return 0.0
    tail = daily_bars.tail(min(20, len(daily_bars)))
    if tail.empty:
        return 0.0
    return float(tail['close'].mean() * tail['volume'].mean())


def quality_filter_reason(row: pd.Series, settings: Settings, avg_daily_dollar_volume: float, spread_bps_value: float) -> str | None:
    current_price = float(row['current_price'])
    intraday_dollar = current_price * float(row['cum_volume'])
    if not bool(row.get('tradable', True)):
        return 'Not marked tradable.'
    if current_price < settings.low_price_hard_floor:
        return 'Below hard price floor.'
    if current_price < settings.min_price:
        return 'Below preferred minimum price.'
    if avg_daily_dollar_volume < settings.min_avg_dollar_volume:
        return 'Average daily dollar volume below threshold.'
    if intraday_dollar < settings.min_intraday_dollar_volume:
        return 'Intraday dollar volume below threshold.'
    if spread_bps_value > 150:
        return 'Spread proxy too wide.'
    return None


def stage1_alignment_filter_reason(row: pd.Series, settings: Settings, avg_daily_dollar_volume_value: float) -> str | None:
    current_price = float(row['current_price'])
    intraday_dollar = current_price * float(row['cum_volume'])
    if not bool(row.get('tradable', True)):
        return 'Not marked tradable.'
    if current_price < settings.low_price_hard_floor:
        return 'Below hard price floor.'
    if current_price < settings.min_price:
        return 'Below preferred minimum price.'
    if avg_daily_dollar_volume_value < settings.min_avg_dollar_volume:
        return 'Average daily dollar volume below threshold.'
    if intraday_dollar < settings.min_intraday_dollar_volume:
        return 'Intraday dollar volume below threshold.'
    return None


def _prepare_positive_movers_frame(stage1_records: List[Dict[str, object]]) -> pd.DataFrame:
    stage1 = pd.DataFrame(stage1_records)
    if stage1.empty:
        return stage1
    stage1 = stage1[stage1['intraday_pct_gain'] > 0].sort_values(['intraday_pct_gain', 'cum_volume'], ascending=[False, False]).reset_index(drop=True)
    if stage1.empty:
        return stage1
    stage1['raw_mover_rank'] = np.arange(1, len(stage1) + 1)
    return stage1


def _finalize_stage1_frame(stage1: pd.DataFrame) -> pd.DataFrame:
    if stage1.empty:
        return stage1
    stage1 = stage1.copy().reset_index(drop=True)
    stage1['mover_rank'] = np.arange(1, len(stage1) + 1)
    leader_gain = float(stage1['intraday_pct_gain'].iloc[0])
    stage1['distance_from_leader_pct'] = leader_gain - stage1['intraday_pct_gain']
    stage1['remains_day_strong'] = stage1['mover_rank'] <= max(10, min(len(stage1), 20))
    return stage1


def build_stage1_target_group(stage1_records: List[Dict[str, object]], top_mover_count: int) -> pd.DataFrame:
    stage1 = _prepare_positive_movers_frame(stage1_records)
    if stage1.empty:
        return stage1
    stage1 = stage1.head(int(top_mover_count)).copy()
    return _finalize_stage1_frame(stage1)


def build_stage1_target_group_with_alignment(
    stage1_records: List[Dict[str, object]],
    *,
    settings: Settings,
    avg_daily_dollar_volume_lookup: Dict[str, float] | None = None,
) -> Dict[str, object]:
    positive = _prepare_positive_movers_frame(stage1_records)
    diagnostics: Dict[str, object] = {
        'alignment_enabled': bool(settings.stage1_alignment_enabled),
        'raw_positive_mover_count': int(len(positive)),
        'alignment_pool_size': 0,
        'alignment_prefilter_kept_count': 0,
        'prefilter_rejection_counts': {},
        'selection_mode': 'raw_positive_movers',
    }
    if positive.empty:
        return {
            'stage1': positive,
            'positive_movers': positive,
            'diagnostics': diagnostics,
        }

    raw_stage1 = _finalize_stage1_frame(positive.head(int(settings.top_mover_count)).copy())
    if not settings.stage1_alignment_enabled:
        diagnostics['selected_stage1_count'] = int(len(raw_stage1))
        return {
            'stage1': raw_stage1,
            'positive_movers': positive,
            'diagnostics': diagnostics,
        }

    pool_size = min(
        len(positive),
        max(
            int(settings.top_mover_count) * max(int(settings.stage1_alignment_pool_multiplier), 1),
            max(int(settings.stage1_alignment_min_pool_size), int(settings.top_mover_count)),
        ),
    )
    pool = positive.head(int(pool_size)).copy()
    avg_daily_dollar_volume_lookup = avg_daily_dollar_volume_lookup or {}
    reasons: List[str | None] = []
    rejection_counts: Dict[str, int] = {}
    for _, row in pool.iterrows():
        reason = stage1_alignment_filter_reason(
            row,
            settings,
            float(avg_daily_dollar_volume_lookup.get(str(row['symbol']), 0.0) or 0.0),
        )
        reasons.append(reason)
        if reason:
            rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
    pool['stage1_prefilter_reason'] = reasons
    filtered = pool[pool['stage1_prefilter_reason'].isna()].copy()

    diagnostics.update({
        'alignment_pool_size': int(len(pool)),
        'alignment_prefilter_kept_count': int(len(filtered)),
        'prefilter_rejection_counts': rejection_counts,
    })

    if filtered.empty:
        diagnostics['selection_mode'] = 'raw_positive_fallback_no_prefilter_matches'
        diagnostics['selected_stage1_count'] = int(len(raw_stage1))
        return {
            'stage1': raw_stage1,
            'positive_movers': positive,
            'alignment_pool': pool,
            'diagnostics': diagnostics,
        }

    selected = _finalize_stage1_frame(filtered.head(int(settings.top_mover_count)).copy())
    diagnostics['selection_mode'] = 'aligned_prefilter_pool'
    diagnostics['selected_stage1_count'] = int(len(selected))
    return {
        'stage1': selected,
        'positive_movers': positive,
        'alignment_pool': pool,
        'diagnostics': diagnostics,
    }
