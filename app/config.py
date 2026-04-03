from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Tuple

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _safe_float(value: object, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if number != number or number in {float('inf'), float('-inf')}:
        return float(default)
    return float(number)


OVERRIDABLE_FIELDS = {
    'trading_mode',
    'default_notional_usd',
    'min_price',
    'min_avg_dollar_volume',
    'min_intraday_dollar_volume',
    'low_price_hard_floor',
    'target_pct',
    'stretch_target_pct',
    'default_scan_offset_minutes',
    'scheduled_scan_offsets',
    'weight_target_strength',
    'weight_liquidity',
    'weight_volatility',
    'weight_dynamic_range',
    'weight_range_position',
    'weight_time_feasibility',
    'weight_execution_quality',
}

POLICY_FIELD_GROUPS = {
    'product_policy': [
        'min_price',
        'min_avg_dollar_volume',
        'min_intraday_dollar_volume',
        'low_price_hard_floor',
        'top_mover_count',
        'target_pct',
        'stretch_target_pct',
        'trade_window_end_buffer_minutes_before_close',
    ],
    'scoring_policy': [
        'weights',
        'weights_raw',
        'min_completed_cycles_observed',
        'min_lower_zone_touches',
        'min_upper_zone_touches',
        'max_breakout_close_ratio',
        'max_wickiness_ratio',
        'max_directional_efficiency',
        'min_estimated_cycles_remaining',
        'ready_now_min_execution_readiness_score',
        'headline_min_entry_touch_likelihood_score',
        'headline_min_expected_actionability_score',
        'headline_min_actionability_score',
        'ready_now_min_entry_touch_likelihood_score',
        'ready_now_min_expected_actionability_score',
        'ready_now_min_actionability_score',
        'near_ready_min_entry_touch_likelihood_score',
        'near_ready_min_expected_actionability_score',
        'near_ready_min_actionability_score',
        'final_score_actionability_weight',
        'final_score_structural_weight',
        'final_score_entry_touch_weight',
        'final_score_expected_actionability_weight',
        'final_score_readiness_weight',
        'headline_rank_actionability_weight',
        'headline_rank_readiness_weight',
        'headline_rank_entry_touch_weight',
        'headline_rank_follow_through_weight',
        'headline_rank_structural_weight',
        'pullback_queue_rank_actionability_weight',
        'pullback_queue_rank_entry_touch_weight',
        'pullback_queue_rank_follow_through_weight',
        'pullback_queue_rank_structural_weight',
        'pullback_queue_rank_distance_weight',
        'pullback_queue_rank_touch_urgency_weight',
    ],
    'research_policy': [
        'default_validation_lookback_days',
        'max_validation_days',
        'research_lookback_months',
        'calibration_min_improvement',
        'research_offset_ladder',
        'research_offset_values',
        'ladder_min_precision_at_10',
        'ladder_min_advanced_rows',
        'replay_entry_fill_mode',
        'replay_target_hit_mode',
        'replay_target_close_buffer_bps',
        'replay_spread_cost_multiplier',
        'replay_slippage_bps_per_side',
    ],
    'operational_policy': [
        'app_env',
        'trading_mode',
        'enable_live_trading',
        'auth_enabled',
        'alpaca_data_feed',
        'default_scan_offset_minutes',
        'scheduled_offsets',
        'default_notional_usd',
        'enable_scheduler',
        'universe_cache_ttl_hours',
        'stage1_alignment_enabled',
        'stage1_alignment_pool_multiplier',
        'stage1_alignment_min_pool_size',
    ],
}

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore', populate_by_name=True)

    app_name: str = Field(default='Russell 2000 Intraday 1% Range-Cycling Scanner', alias='APP_NAME')
    app_env: str = Field(default='development', alias='APP_ENV')
    app_host: str = Field(default='0.0.0.0', alias='APP_HOST')
    app_port: int = Field(default=8000, alias='APP_PORT')
    log_level: str = Field(default='INFO', alias='LOG_LEVEL')
    auth_token: str = Field(default='', alias='AUTH_TOKEN')

    data_dir: str = Field(default='./data', alias='DATA_DIR')
    database_path: str = Field(default='./data/scanner.db', alias='DATABASE_PATH')
    settings_override_path: str = Field(default='./data/settings_override.json', alias='SETTINGS_OVERRIDE_PATH')

    alpaca_api_key: str = Field(default='', alias='ALPACA_API_KEY')
    alpaca_secret_key: str = Field(default='', alias='ALPACA_SECRET_KEY')
    alpaca_data_feed: str = Field(default='iex', alias='ALPACA_DATA_FEED')
    alpaca_request_timeout_seconds: int = Field(default=30, alias='ALPACA_REQUEST_TIMEOUT_SECONDS')
    alpaca_account_type: str = Field(default='paper', alias='ALPACA_ACCOUNT_TYPE')
    alpaca_paper_base_url: str = Field(default='https://paper-api.alpaca.markets', alias='ALPACA_PAPER_BASE_URL')
    alpaca_live_base_url: str = Field(default='https://api.alpaca.markets', alias='ALPACA_LIVE_BASE_URL')
    alpaca_data_base_url: str = Field(default='https://data.alpaca.markets', alias='ALPACA_DATA_BASE_URL')

    trading_mode: str = Field(default='scan_only', alias='TRADING_MODE')
    enable_live_trading: bool = Field(default=False, alias='ENABLE_LIVE_TRADING')
    default_notional_usd: float = Field(default=1000.0, alias='DEFAULT_NOTIONAL_USD')

    universe_cache_ttl_hours: int = Field(default=24, alias='UNIVERSE_CACHE_TTL_HOURS')
    universe_holdings_url: str = Field(
        default='https://www.ishares.com/ch/professionals/en/products/239710/ishares-russell-2000-etf/1495092304805.ajax?dataType=fund&fileName=IWM_holdings&fileType=csv',
        alias='UNIVERSE_HOLDINGS_URL',
    )
    stage1_alignment_enabled: bool = Field(default=True, alias='STAGE1_ALIGNMENT_ENABLED')
    stage1_alignment_pool_multiplier: int = Field(default=4, alias='STAGE1_ALIGNMENT_POOL_MULTIPLIER')
    stage1_alignment_min_pool_size: int = Field(default=150, alias='STAGE1_ALIGNMENT_MIN_POOL_SIZE')

    min_price: float = Field(default=2.0, alias='MIN_PRICE')
    min_avg_dollar_volume: float = Field(default=2_000_000.0, alias='MIN_AVG_DOLLAR_VOLUME')
    min_intraday_dollar_volume: float = Field(default=500_000.0, alias='MIN_INTRADAY_DOLLAR_VOLUME')
    low_price_hard_floor: float = Field(default=1.0, alias='LOW_PRICE_HARD_FLOOR')
    top_mover_count: int = Field(default=50, alias='TOP_MOVER_COUNT')
    target_pct: float = Field(default=1.0, alias='TARGET_PCT')
    stretch_target_pct: float = Field(default=2.0, alias='STRETCH_TARGET_PCT')
    default_scan_offset_minutes: int = Field(default=120, alias='DEFAULT_SCAN_OFFSET_MINUTES')
    scheduled_scan_offsets: str = Field(default='120', alias='SCHEDULED_SCAN_OFFSETS')
    enable_scheduler: bool = Field(default=True, alias='ENABLE_SCHEDULER')
    trade_window_end_buffer_minutes_before_close: int = Field(default=120, alias='TRADE_WINDOW_END_BUFFER_MINUTES_BEFORE_CLOSE')

    default_validation_lookback_days: int = Field(default=20, alias='DEFAULT_VALIDATION_LOOKBACK_DAYS')
    max_validation_days: int = Field(default=80, alias='MAX_VALIDATION_DAYS')
    research_lookback_months: int = Field(default=3, alias='RESEARCH_LOOKBACK_MONTHS')
    calibration_min_improvement: float = Field(default=0.015, alias='CALIBRATION_MIN_IMPROVEMENT')
    research_offset_ladder: str = Field(default='90,120,150,180', alias='RESEARCH_OFFSET_LADDER')
    ladder_min_precision_at_10: float = Field(default=0.50, alias='LADDER_MIN_PRECISION_AT_10')
    ladder_min_advanced_rows: int = Field(default=20, alias='LADDER_MIN_ADVANCED_ROWS')


    structural_post_scan_min_completed_cycles: int = Field(default=2, alias='STRUCTURAL_POST_SCAN_MIN_COMPLETED_CYCLES')
    structural_post_scan_min_containment_ratio: float = Field(default=0.65, alias='STRUCTURAL_POST_SCAN_MIN_CONTAINMENT_RATIO')
    entry_accessibility_minutes: int = Field(default=45, alias='ENTRY_ACCESSIBILITY_MINUTES')
    follow_through_max_adverse_excursion_pct: float = Field(default=0.5, alias='FOLLOW_THROUGH_MAX_ADVERSE_EXCURSION_PCT')
    replay_entry_fill_mode: str = Field(default='zone_mid', alias='REPLAY_ENTRY_FILL_MODE')
    replay_target_hit_mode: str = Field(default='close_confirmed', alias='REPLAY_TARGET_HIT_MODE')
    replay_target_close_buffer_bps: float = Field(default=0.0, alias='REPLAY_TARGET_CLOSE_BUFFER_BPS')
    replay_spread_cost_multiplier: float = Field(default=2.0, alias='REPLAY_SPREAD_COST_MULTIPLIER')
    replay_slippage_bps_per_side: float = Field(default=5.0, alias='REPLAY_SLIPPAGE_BPS_PER_SIDE')

    min_completed_cycles_observed: int = Field(default=2, alias='MIN_COMPLETED_CYCLES_OBSERVED')
    min_lower_zone_touches: int = Field(default=2, alias='MIN_LOWER_ZONE_TOUCHES')
    min_upper_zone_touches: int = Field(default=2, alias='MIN_UPPER_ZONE_TOUCHES')
    max_breakout_close_ratio: float = Field(default=0.12, alias='MAX_BREAKOUT_CLOSE_RATIO')
    max_wickiness_ratio: float = Field(default=8.0, alias='MAX_WICKINESS_RATIO')
    max_directional_efficiency: float = Field(default=0.72, alias='MAX_DIRECTIONAL_EFFICIENCY')
    min_estimated_cycles_remaining: float = Field(default=0.75, alias='MIN_ESTIMATED_CYCLES_REMAINING')
    min_recent_completed_cycles: int = Field(default=1, alias='MIN_RECENT_COMPLETED_CYCLES')
    min_recent_zone_touches_each: int = Field(default=1, alias='MIN_RECENT_ZONE_TOUCHES_EACH')
    min_width_retention_ratio: float = Field(default=0.72, alias='MIN_WIDTH_RETENTION_RATIO')
    min_cycle_persistence_ratio: float = Field(default=0.65, alias='MIN_CYCLE_PERSISTENCE_RATIO')
    max_recent_breakout_close_ratio: float = Field(default=0.08, alias='MAX_RECENT_BREAKOUT_CLOSE_RATIO')
    max_recent_directional_efficiency: float = Field(default=0.58, alias='MAX_RECENT_DIRECTIONAL_EFFICIENCY')
    min_cycle_durability_score: float = Field(default=55.0, alias='MIN_CYCLE_DURABILITY_SCORE')
    min_recent_bounce_mfe_pct: float = Field(default=0.9, alias='MIN_RECENT_BOUNCE_MFE_PCT')
    max_recent_bounce_mae_pct: float = Field(default=1.35, alias='MAX_RECENT_BOUNCE_MAE_PCT')
    min_recent_bounce_payoff_ratio: float = Field(default=1.15, alias='MIN_RECENT_BOUNCE_PAYOFF_RATIO')
    min_recent_upper_reach_ratio: float = Field(default=0.20, alias='MIN_RECENT_UPPER_REACH_RATIO')
    min_recent_target_hit_ratio: float = Field(default=0.25, alias='MIN_RECENT_TARGET_HIT_RATIO')
    min_bounce_quality_score: float = Field(default=55.0, alias='MIN_BOUNCE_QUALITY_SCORE')
    min_recent_bounce_event_count: int = Field(default=2, alias='MIN_RECENT_BOUNCE_EVENT_COUNT')
    min_recent_bounce_confidence_score: float = Field(default=40.0, alias='MIN_RECENT_BOUNCE_CONFIDENCE_SCORE')
    ready_now_max_range_location: float = Field(default=0.60, alias='READY_NOW_MAX_RANGE_LOCATION')
    ready_now_min_recent_bounce_confidence_score: float = Field(default=65.0, alias='READY_NOW_MIN_RECENT_BOUNCE_CONFIDENCE_SCORE')
    ready_now_min_recent_bounce_event_count: int = Field(default=2, alias='READY_NOW_MIN_RECENT_BOUNCE_EVENT_COUNT')
    ready_now_min_recent_shrunk_target_hit_ratio: float = Field(default=0.43, alias='READY_NOW_MIN_RECENT_SHRUNK_TARGET_HIT_RATIO')
    ready_now_min_recent_shrunk_upper_reach_ratio: float = Field(default=0.45, alias='READY_NOW_MIN_RECENT_SHRUNK_UPPER_REACH_RATIO')
    ready_now_min_trade_liquidity_confidence: float = Field(default=45.0, alias='READY_NOW_MIN_TRADE_LIQUIDITY_CONFIDENCE')
    ready_now_min_execution_readiness_score: float = Field(default=62.0, alias='READY_NOW_MIN_EXECUTION_READINESS_SCORE')
    ready_now_max_distance_to_entry_pct: float = Field(default=0.25, alias='READY_NOW_MAX_DISTANCE_TO_ENTRY_PCT')
    ready_now_max_expected_minutes_to_touch: float = Field(default=20.0, alias='READY_NOW_MAX_EXPECTED_MINUTES_TO_TOUCH')
    near_ready_max_range_location: float = Field(default=0.78, alias='NEAR_READY_MAX_RANGE_LOCATION')
    near_ready_min_execution_readiness_score: float = Field(default=46.0, alias='NEAR_READY_MIN_EXECUTION_READINESS_SCORE')
    near_ready_max_distance_to_entry_pct: float = Field(default=0.85, alias='NEAR_READY_MAX_DISTANCE_TO_ENTRY_PCT')
    near_ready_max_expected_minutes_to_touch: float = Field(default=42.0, alias='NEAR_READY_MAX_EXPECTED_MINUTES_TO_TOUCH')
    near_ready_min_recent_target_excess_pct: float = Field(default=-0.05, alias='NEAR_READY_MIN_RECENT_TARGET_EXCESS_PCT')
    headline_max_range_location: float = Field(default=0.50, alias='HEADLINE_MAX_RANGE_LOCATION')
    headline_min_execution_readiness_score: float = Field(default=72.0, alias='HEADLINE_MIN_EXECUTION_READINESS_SCORE')
    headline_max_distance_to_entry_pct: float = Field(default=0.35, alias='HEADLINE_MAX_DISTANCE_TO_ENTRY_PCT')
    headline_max_expected_minutes_to_touch: float = Field(default=12.0, alias='HEADLINE_MAX_EXPECTED_MINUTES_TO_TOUCH')
    headline_min_recent_target_excess_pct: float = Field(default=0.12, alias='HEADLINE_MIN_RECENT_TARGET_EXCESS_PCT')
    headline_min_follow_through_confidence_score: float = Field(default=60.0, alias='HEADLINE_MIN_FOLLOW_THROUGH_CONFIDENCE_SCORE')
    headline_min_entry_touch_likelihood_score: float = Field(default=72.0, alias='HEADLINE_MIN_ENTRY_TOUCH_LIKELIHOOD_SCORE')
    headline_min_expected_actionability_score: float = Field(default=74.0, alias='HEADLINE_MIN_EXPECTED_ACTIONABILITY_SCORE')
    headline_min_actionability_score: float = Field(default=72.0, alias='HEADLINE_MIN_ACTIONABILITY_SCORE')
    ready_now_min_entry_touch_likelihood_score: float = Field(default=58.0, alias='READY_NOW_MIN_ENTRY_TOUCH_LIKELIHOOD_SCORE')
    ready_now_min_expected_actionability_score: float = Field(default=63.0, alias='READY_NOW_MIN_EXPECTED_ACTIONABILITY_SCORE')
    ready_now_min_actionability_score: float = Field(default=62.0, alias='READY_NOW_MIN_ACTIONABILITY_SCORE')
    near_ready_min_entry_touch_likelihood_score: float = Field(default=42.0, alias='NEAR_READY_MIN_ENTRY_TOUCH_LIKELIHOOD_SCORE')
    near_ready_min_expected_actionability_score: float = Field(default=50.0, alias='NEAR_READY_MIN_EXPECTED_ACTIONABILITY_SCORE')
    near_ready_min_actionability_score: float = Field(default=50.0, alias='NEAR_READY_MIN_ACTIONABILITY_SCORE')
    final_score_actionability_weight: float = Field(default=0.46, alias='FINAL_SCORE_ACTIONABILITY_WEIGHT')
    final_score_structural_weight: float = Field(default=0.54, alias='FINAL_SCORE_STRUCTURAL_WEIGHT')
    final_score_entry_touch_weight: float = Field(default=0.30, alias='FINAL_SCORE_ENTRY_TOUCH_WEIGHT')
    final_score_expected_actionability_weight: float = Field(default=0.42, alias='FINAL_SCORE_EXPECTED_ACTIONABILITY_WEIGHT')

    headline_rank_actionability_weight: float = Field(default=0.40, alias='HEADLINE_RANK_ACTIONABILITY_WEIGHT')
    headline_rank_readiness_weight: float = Field(default=0.22, alias='HEADLINE_RANK_READINESS_WEIGHT')
    headline_rank_entry_touch_weight: float = Field(default=0.18, alias='HEADLINE_RANK_ENTRY_TOUCH_WEIGHT')
    headline_rank_follow_through_weight: float = Field(default=0.15, alias='HEADLINE_RANK_FOLLOW_THROUGH_WEIGHT')
    headline_rank_structural_weight: float = Field(default=0.05, alias='HEADLINE_RANK_STRUCTURAL_WEIGHT')
    final_score_headline_rank_blend: float = Field(default=0.65, alias='FINAL_SCORE_HEADLINE_RANK_BLEND')
    headline_cap_score: float = Field(default=100.0, alias='HEADLINE_CAP_SCORE')
    ready_now_cap_score: float = Field(default=84.99, alias='READY_NOW_CAP_SCORE')
    final_score_readiness_weight: float = Field(default=0.30, alias='FINAL_SCORE_READINESS_WEIGHT')
    final_score_follow_through_weight: float = Field(default=0.24, alias='FINAL_SCORE_FOLLOW_THROUGH_WEIGHT')
    headline_bonus_points: float = Field(default=8.0, alias='HEADLINE_BONUS_POINTS')
    ready_now_bonus_points: float = Field(default=4.0, alias='READY_NOW_BONUS_POINTS')
    near_ready_bonus_points: float = Field(default=1.5, alias='NEAR_READY_BONUS_POINTS')
    watchlist_penalty_points: float = Field(default=2.5, alias='WATCHLIST_PENALTY_POINTS')
    near_ready_cap_score: float = Field(default=69.99, alias='NEAR_READY_CAP_SCORE')
    watchlist_cap_score: float = Field(default=44.99, alias='WATCHLIST_CAP_SCORE')
    pullback_queue_max_distance_to_entry_pct: float = Field(default=2.50, alias='PULLBACK_QUEUE_MAX_DISTANCE_TO_ENTRY_PCT')
    pullback_queue_max_expected_minutes_to_touch: float = Field(default=70.0, alias='PULLBACK_QUEUE_MAX_EXPECTED_MINUTES_TO_TOUCH')
    pullback_queue_min_entry_touch_likelihood_score: float = Field(default=36.0, alias='PULLBACK_QUEUE_MIN_ENTRY_TOUCH_LIKELIHOOD_SCORE')
    pullback_queue_min_follow_through_confidence_score: float = Field(default=56.0, alias='PULLBACK_QUEUE_MIN_FOLLOW_THROUGH_CONFIDENCE_SCORE')
    pullback_queue_min_expected_actionability_score: float = Field(default=52.0, alias='PULLBACK_QUEUE_MIN_EXPECTED_ACTIONABILITY_SCORE')
    pullback_queue_rank_actionability_weight: float = Field(default=0.32, alias='PULLBACK_QUEUE_RANK_ACTIONABILITY_WEIGHT')
    pullback_queue_rank_entry_touch_weight: float = Field(default=0.22, alias='PULLBACK_QUEUE_RANK_ENTRY_TOUCH_WEIGHT')
    pullback_queue_rank_follow_through_weight: float = Field(default=0.22, alias='PULLBACK_QUEUE_RANK_FOLLOW_THROUGH_WEIGHT')
    pullback_queue_rank_structural_weight: float = Field(default=0.14, alias='PULLBACK_QUEUE_RANK_STRUCTURAL_WEIGHT')
    pullback_queue_rank_distance_weight: float = Field(default=0.10, alias='PULLBACK_QUEUE_RANK_DISTANCE_WEIGHT')
    pullback_queue_rank_touch_urgency_weight: float = Field(default=0.14, alias='PULLBACK_QUEUE_RANK_TOUCH_URGENCY_WEIGHT')
    pullback_queue_cap_score: float = Field(default=49.99, alias='PULLBACK_QUEUE_CAP_SCORE')
    touch_soon_queue_max_expected_minutes_to_touch: float = Field(default=30.0, alias='TOUCH_SOON_QUEUE_MAX_EXPECTED_MINUTES_TO_TOUCH')
    touch_soon_queue_min_entry_touch_likelihood_score: float = Field(default=46.0, alias='TOUCH_SOON_QUEUE_MIN_ENTRY_TOUCH_LIKELIHOOD_SCORE')
    touch_soon_queue_min_expected_actionability_score: float = Field(default=56.0, alias='TOUCH_SOON_QUEUE_MIN_EXPECTED_ACTIONABILITY_SCORE')
    touch_later_queue_max_expected_minutes_to_touch: float = Field(default=70.0, alias='TOUCH_LATER_QUEUE_MAX_EXPECTED_MINUTES_TO_TOUCH')
    touch_later_queue_min_entry_touch_likelihood_score: float = Field(default=36.0, alias='TOUCH_LATER_QUEUE_MIN_ENTRY_TOUCH_LIKELIHOOD_SCORE')
    touch_later_queue_min_expected_actionability_score: float = Field(default=52.0, alias='TOUCH_LATER_QUEUE_MIN_EXPECTED_ACTIONABILITY_SCORE')
    min_recent_shrunk_target_hit_ratio: float = Field(default=0.40, alias='MIN_RECENT_SHRUNK_TARGET_HIT_RATIO')
    min_recent_shrunk_upper_reach_ratio: float = Field(default=0.45, alias='MIN_RECENT_SHRUNK_UPPER_REACH_RATIO')
    min_cycle_trade_avg_daily_dollar_volume: float = Field(default=5_000_000.0, alias='MIN_CYCLE_TRADE_AVG_DAILY_DOLLAR_VOLUME')
    min_cycle_trade_relative_volume: float = Field(default=1.5, alias='MIN_CYCLE_TRADE_RELATIVE_VOLUME')

    weight_target_strength: float = Field(default=0.04, alias='WEIGHT_TARGET_STRENGTH')
    weight_liquidity: float = Field(default=0.12, alias='WEIGHT_LIQUIDITY')
    weight_volatility: float = Field(default=0.08, alias='WEIGHT_VOLATILITY')
    weight_dynamic_range: float = Field(default=0.24, alias='WEIGHT_DYNAMIC_RANGE')
    weight_range_position: float = Field(default=0.28, alias='WEIGHT_RANGE_POSITION')
    weight_time_feasibility: float = Field(default=0.17, alias='WEIGHT_TIME_FEASIBILITY')
    weight_execution_quality: float = Field(default=0.07, alias='WEIGHT_EXECUTION_QUALITY')

    @property
    def scheduled_offsets(self) -> List[int]:
        values = []
        for raw in self.scheduled_scan_offsets.split(','):
            raw = raw.strip()
            if raw:
                values.append(int(raw))
        return sorted(set(values))

    @property
    def research_offset_values(self) -> List[int]:
        values: List[int] = []
        for raw in self.research_offset_ladder.split(','):
            raw = raw.strip()
            if raw:
                try:
                    values.append(int(raw))
                except ValueError:
                    continue
        return sorted(set(v for v in values if v > 0)) or [self.default_scan_offset_minutes]

    @property
    def raw_weights(self) -> Dict[str, float]:
        return {
            'target_strength': _safe_float(self.weight_target_strength, 0.04),
            'liquidity': _safe_float(self.weight_liquidity, 0.12),
            'volatility_capacity': _safe_float(self.weight_volatility, 0.08),
            'dynamic_range': _safe_float(self.weight_dynamic_range, 0.24),
            'range_position': _safe_float(self.weight_range_position, 0.28),
            'time_feasibility': _safe_float(self.weight_time_feasibility, 0.17),
            'execution_quality': _safe_float(self.weight_execution_quality, 0.07),
        }

    @property
    def weights(self) -> Dict[str, float]:
        raw = self.raw_weights
        total = sum(max(v, 0.0) for v in raw.values())
        if total <= 0:
            equal = 1.0 / len(raw)
            return {k: round(equal, 6) for k in raw}
        return {k: round(max(v, 0.0) / total, 6) for k, v in raw.items()}

    @property
    def effective_alpaca_account_type(self) -> str:
        mode = (self.trading_mode or '').strip().lower()
        if mode in {'paper', 'live'}:
            return mode
        account_type = (self.alpaca_account_type or 'paper').strip().lower()
        return 'live' if account_type == 'live' else 'paper'

    @property
    def alpaca_trading_base_url(self) -> str:
        return self.alpaca_live_base_url if self.effective_alpaca_account_type == 'live' else self.alpaca_paper_base_url

    def ensure_directories(self) -> None:
        Path(self.data_dir).mkdir(parents=True, exist_ok=True)
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.settings_override_path).parent.mkdir(parents=True, exist_ok=True)

    def _snapshot_value(self, field_name: str):
        if field_name == 'auth_enabled':
            return bool((self.auth_token or '').strip())
        if field_name == 'weights_raw':
            return self.raw_weights
        if field_name == 'weights':
            return self.weights
        return getattr(self, field_name)

    def policy_snapshot(self) -> Dict[str, Dict[str, object]]:
        grouped: Dict[str, Dict[str, object]] = {}
        for group_name, field_names in POLICY_FIELD_GROUPS.items():
            group_payload: Dict[str, object] = {}
            for field_name in field_names:
                value = self._snapshot_value(field_name)
                group_payload[field_name] = value
            grouped[group_name] = group_payload
        return grouped

    def policy_summary(self) -> Dict[str, object]:
        groups = self.policy_snapshot()
        return {
            'group_names': list(groups.keys()),
            'field_counts': {name: len(values) for name, values in groups.items()},
            'overrideable_fields': sorted(OVERRIDABLE_FIELDS),
        }

    def public_snapshot(self) -> Dict[str, object]:
        return {
            'app_name': self.app_name,
            'app_env': self.app_env,
            'trading_mode': self.trading_mode,
            'enable_live_trading': self.enable_live_trading,
            'alpaca_data_feed': self.alpaca_data_feed,
            'auth_enabled': bool((self.auth_token or '').strip()),
            'min_price': self.min_price,
            'min_avg_dollar_volume': self.min_avg_dollar_volume,
            'min_intraday_dollar_volume': self.min_intraday_dollar_volume,
            'low_price_hard_floor': self.low_price_hard_floor,
            'top_mover_count': self.top_mover_count,
            'target_pct': self.target_pct,
            'stretch_target_pct': self.stretch_target_pct,
            'default_scan_offset_minutes': self.default_scan_offset_minutes,
            'scheduled_offsets': self.scheduled_offsets,
            'trade_window_end_buffer_minutes_before_close': self.trade_window_end_buffer_minutes_before_close,
            'default_notional_usd': self.default_notional_usd,
            'stage1_alignment_enabled': self.stage1_alignment_enabled,
            'stage1_alignment_pool_multiplier': self.stage1_alignment_pool_multiplier,
            'stage1_alignment_min_pool_size': self.stage1_alignment_min_pool_size,
            'weights': self.weights,
            'weights_raw': self.raw_weights,
            'max_validation_days': self.max_validation_days,
            'research_lookback_months': self.research_lookback_months,
            'calibration_min_improvement': self.calibration_min_improvement,
            'research_offset_ladder': self.research_offset_ladder,
            'research_offset_values': self.research_offset_values,
            'ladder_min_precision_at_10': self.ladder_min_precision_at_10,
            'ladder_min_advanced_rows': self.ladder_min_advanced_rows,
            'structural_post_scan_min_completed_cycles': self.structural_post_scan_min_completed_cycles,
            'structural_post_scan_min_containment_ratio': self.structural_post_scan_min_containment_ratio,
            'entry_accessibility_minutes': self.entry_accessibility_minutes,
            'follow_through_max_adverse_excursion_pct': self.follow_through_max_adverse_excursion_pct,
            'replay_entry_fill_mode': self.replay_entry_fill_mode,
            'replay_target_hit_mode': self.replay_target_hit_mode,
            'replay_target_close_buffer_bps': self.replay_target_close_buffer_bps,
            'replay_spread_cost_multiplier': self.replay_spread_cost_multiplier,
            'replay_slippage_bps_per_side': self.replay_slippage_bps_per_side,
            'min_completed_cycles_observed': self.min_completed_cycles_observed,
            'min_lower_zone_touches': self.min_lower_zone_touches,
            'min_upper_zone_touches': self.min_upper_zone_touches,
            'max_breakout_close_ratio': self.max_breakout_close_ratio,
            'max_wickiness_ratio': self.max_wickiness_ratio,
            'max_directional_efficiency': self.max_directional_efficiency,
            'min_estimated_cycles_remaining': self.min_estimated_cycles_remaining,
            'min_recent_completed_cycles': self.min_recent_completed_cycles,
            'min_recent_zone_touches_each': self.min_recent_zone_touches_each,
            'min_width_retention_ratio': self.min_width_retention_ratio,
            'min_cycle_persistence_ratio': self.min_cycle_persistence_ratio,
            'max_recent_breakout_close_ratio': self.max_recent_breakout_close_ratio,
            'max_recent_directional_efficiency': self.max_recent_directional_efficiency,
            'min_cycle_durability_score': self.min_cycle_durability_score,
            'min_recent_bounce_mfe_pct': self.min_recent_bounce_mfe_pct,
            'max_recent_bounce_mae_pct': self.max_recent_bounce_mae_pct,
            'min_recent_bounce_payoff_ratio': self.min_recent_bounce_payoff_ratio,
            'min_recent_upper_reach_ratio': self.min_recent_upper_reach_ratio,
            'min_recent_target_hit_ratio': self.min_recent_target_hit_ratio,
            'min_bounce_quality_score': self.min_bounce_quality_score,
            'min_recent_bounce_event_count': self.min_recent_bounce_event_count,
            'min_recent_bounce_confidence_score': self.min_recent_bounce_confidence_score,
            'min_recent_shrunk_target_hit_ratio': self.min_recent_shrunk_target_hit_ratio,
            'min_recent_shrunk_upper_reach_ratio': self.min_recent_shrunk_upper_reach_ratio,
            'min_cycle_trade_avg_daily_dollar_volume': self.min_cycle_trade_avg_daily_dollar_volume,
            'min_cycle_trade_relative_volume': self.min_cycle_trade_relative_volume,
            'ready_now_min_execution_readiness_score': self.ready_now_min_execution_readiness_score,
            'headline_min_entry_touch_likelihood_score': self.headline_min_entry_touch_likelihood_score,
            'headline_min_expected_actionability_score': self.headline_min_expected_actionability_score,
            'headline_min_actionability_score': self.headline_min_actionability_score,
            'headline_max_expected_minutes_to_touch': self.headline_max_expected_minutes_to_touch,
            'ready_now_min_entry_touch_likelihood_score': self.ready_now_min_entry_touch_likelihood_score,
            'ready_now_min_expected_actionability_score': self.ready_now_min_expected_actionability_score,
            'ready_now_min_actionability_score': self.ready_now_min_actionability_score,
            'ready_now_max_expected_minutes_to_touch': self.ready_now_max_expected_minutes_to_touch,
            'near_ready_min_entry_touch_likelihood_score': self.near_ready_min_entry_touch_likelihood_score,
            'near_ready_min_expected_actionability_score': self.near_ready_min_expected_actionability_score,
            'near_ready_min_actionability_score': self.near_ready_min_actionability_score,
            'near_ready_max_expected_minutes_to_touch': self.near_ready_max_expected_minutes_to_touch,
            'final_score_actionability_weight': self.final_score_actionability_weight,
            'final_score_structural_weight': self.final_score_structural_weight,
            'final_score_entry_touch_weight': self.final_score_entry_touch_weight,
            'final_score_expected_actionability_weight': self.final_score_expected_actionability_weight,
            'headline_rank_actionability_weight': self.headline_rank_actionability_weight,
            'headline_rank_readiness_weight': self.headline_rank_readiness_weight,
            'headline_rank_entry_touch_weight': self.headline_rank_entry_touch_weight,
            'headline_rank_follow_through_weight': self.headline_rank_follow_through_weight,
            'headline_rank_structural_weight': self.headline_rank_structural_weight,
            'final_score_headline_rank_blend': self.final_score_headline_rank_blend,
            'headline_cap_score': self.headline_cap_score,
            'ready_now_cap_score': self.ready_now_cap_score,
            'near_ready_max_range_location': self.near_ready_max_range_location,
            'near_ready_min_execution_readiness_score': self.near_ready_min_execution_readiness_score,
            'final_score_readiness_weight': self.final_score_readiness_weight,
            'ready_now_bonus_points': self.ready_now_bonus_points,
            'near_ready_bonus_points': self.near_ready_bonus_points,
            'watchlist_penalty_points': self.watchlist_penalty_points,
            'pullback_queue_max_distance_to_entry_pct': self.pullback_queue_max_distance_to_entry_pct,
            'pullback_queue_min_entry_touch_likelihood_score': self.pullback_queue_min_entry_touch_likelihood_score,
            'pullback_queue_min_follow_through_confidence_score': self.pullback_queue_min_follow_through_confidence_score,
            'pullback_queue_min_expected_actionability_score': self.pullback_queue_min_expected_actionability_score,
            'pullback_queue_max_expected_minutes_to_touch': self.pullback_queue_max_expected_minutes_to_touch,
            'pullback_queue_cap_score': self.pullback_queue_cap_score,
            'touch_soon_queue_max_expected_minutes_to_touch': self.touch_soon_queue_max_expected_minutes_to_touch,
            'touch_soon_queue_min_entry_touch_likelihood_score': self.touch_soon_queue_min_entry_touch_likelihood_score,
            'touch_soon_queue_min_expected_actionability_score': self.touch_soon_queue_min_expected_actionability_score,
            'touch_later_queue_max_expected_minutes_to_touch': self.touch_later_queue_max_expected_minutes_to_touch,
            'touch_later_queue_min_entry_touch_likelihood_score': self.touch_later_queue_min_entry_touch_likelihood_score,
            'touch_later_queue_min_expected_actionability_score': self.touch_later_queue_min_expected_actionability_score,
            'pullback_queue_max_distance_to_entry_pct': self.pullback_queue_max_distance_to_entry_pct,
            'pullback_queue_min_entry_touch_likelihood_score': self.pullback_queue_min_entry_touch_likelihood_score,
            'pullback_queue_min_follow_through_confidence_score': self.pullback_queue_min_follow_through_confidence_score,
            'pullback_queue_min_expected_actionability_score': self.pullback_queue_min_expected_actionability_score,
            'pullback_queue_max_expected_minutes_to_touch': self.pullback_queue_max_expected_minutes_to_touch,
            'pullback_queue_rank_actionability_weight': self.pullback_queue_rank_actionability_weight,
            'pullback_queue_rank_entry_touch_weight': self.pullback_queue_rank_entry_touch_weight,
            'pullback_queue_rank_follow_through_weight': self.pullback_queue_rank_follow_through_weight,
            'pullback_queue_rank_structural_weight': self.pullback_queue_rank_structural_weight,
            'pullback_queue_rank_distance_weight': self.pullback_queue_rank_distance_weight,
            'pullback_queue_rank_touch_urgency_weight': self.pullback_queue_rank_touch_urgency_weight,
            'pullback_queue_cap_score': self.pullback_queue_cap_score,
            'policy_groups': self.policy_snapshot(),
            'policy_summary': self.policy_summary(),
            'overrideable_fields': sorted(OVERRIDABLE_FIELDS),
        }


def _sanitize_override_payload(payload: Dict[str, object]) -> Tuple[Dict[str, object], List[str]]:
    clean = {k: v for k, v in payload.items() if k in OVERRIDABLE_FIELDS}
    dropped = sorted(k for k in payload.keys() if k not in OVERRIDABLE_FIELDS)
    return clean, dropped


def _read_override_payload(path: Path) -> Dict[str, object]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(raw, dict):
        raise ValueError('settings override payload must be a JSON object')
    clean, dropped = _sanitize_override_payload(raw)
    if dropped:
        path.write_text(json.dumps(clean, indent=2), encoding='utf-8')
    return clean


def load_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    override_path = Path(settings.settings_override_path)
    if override_path.exists():
        data = _read_override_payload(override_path)
        merged = settings.model_dump()
        merged.update(data)
        settings = Settings(**merged)
        settings.ensure_directories()
    return settings


def save_settings_override(settings: Settings, payload: Dict[str, object]) -> None:
    clean, _ = _sanitize_override_payload(payload)
    path = Path(settings.settings_override_path)
    current = _read_override_payload(path) if path.exists() else {}
    current.update(clean)
    path.write_text(json.dumps(current, indent=2), encoding='utf-8')
