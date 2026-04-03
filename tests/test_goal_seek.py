from app.config import Settings
from app.services.goal_seek import build_walk_forward_windows, build_validation_scorecard, prioritized_goal_seek_configs


def test_build_walk_forward_windows_creates_temporal_slices():
    windows = build_walk_forward_windows('2025-10-01', '2026-03-26', train_days=40, test_days=15, step_days=15, embargo_days=1)
    assert windows
    first = windows[0]
    assert first.train_start < first.train_end < first.test_start < first.test_end
    assert first.embargo_days == 1


def test_build_validation_scorecard_outputs_goal_score_and_requirements():
    summary = {
        'days': 20,
        'advanced_stage2_total': 18,
        'precision_at_5': 0.6,
        'precision_at_10': 0.5,
        'precision_at_20': 0.42,
        'overall_hit_rate': 0.35,
        'conditional_hit_rate_entry_touched': 0.52,
        'conditional_precision_at_10_entry_touched': 0.55,
        'entry_touch_rate_stage2': 0.4,
        'score_bucket_monotonicity': {'ratio': 0.8},
        'baseline_comparison': {'mover_rank_only': {'precision_at_10': 0.32}},
        'daily_summaries': [
            {'trading_day': '2026-01-01', 'advanced_count': 1},
            {'trading_day': '2026-01-02', 'advanced_count': 2},
            {'trading_day': '2026-01-03', 'advanced_count': 0},
            {'trading_day': '2026-01-04', 'advanced_count': 1},
        ],
    }
    rows = [
        {'trading_day': '2026-01-01', 'advanced_to_stage2': True, 'hit_target': True, 'intraday_pct_gain': 5.0, 'total_score': 72.0},
        {'trading_day': '2026-01-02', 'advanced_to_stage2': True, 'hit_target': False, 'intraday_pct_gain': 7.0, 'total_score': 64.0},
        {'trading_day': '2026-01-02', 'advanced_to_stage2': True, 'hit_target': True, 'intraday_pct_gain': 6.0, 'total_score': 78.0},
        {'trading_day': '2026-01-04', 'advanced_to_stage2': True, 'hit_target': False, 'intraday_pct_gain': 3.0, 'total_score': 55.0},
    ]
    scorecard = build_validation_scorecard(summary, rows, train_summary={'precision_at_10': 0.48}, train_rows=rows)
    assert scorecard['goal_score'] > 0
    assert scorecard['mover_rank_lift_precision_at_10'] == 0.18
    assert scorecard['hard_requirements']['beats_mover_rank_p10_by_2pts'] is True


def test_prioritized_goal_seek_configs_can_be_narrowed_to_focused_liquidity_scope():
    settings = Settings()
    full = prioritized_goal_seek_configs(settings, scope='full')
    focused = prioritized_goal_seek_configs(settings, scope='focused_liquidity')

    assert len(focused) < len(full)
    assert [config['name'] for config in focused] == [
        'incumbent',
        'moderate_relaxation',
        'strong_relaxation',
        'liquidity_only_relaxed',
        'price_only_relaxed',
        'strong_plus_cycles_relaxed',
    ]
