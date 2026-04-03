from app.services.gate_audit import build_gate_audit_row, recommend_gate_audit_scenario


def _payload(validation_id: int, *, precision: float, conditional: float, advanced: list[tuple[str, str, bool]], actionable: int):
    rows = [
        {
            'trading_day': day,
            'symbol': symbol,
            'advanced_to_stage2': True,
            'hit_target': hit,
            'entry_touched': True,
        }
        for day, symbol, hit in advanced
    ]
    return {
        'id': validation_id,
        'rows': rows,
        'summary': {
            'days': 5,
            'advanced_stage2_total': len(rows),
            'precision_at_10': precision,
            'conditional_precision_at_10_entry_touched': conditional,
            'overall_hit_rate': precision,
            'entry_touch_rate_stage2': 0.8,
            'stage_funnel_summary': {'actionable_lane': actionable},
        },
    }


def test_build_gate_audit_row_counts_newly_admitted_and_reasons():
    baseline = {
        'id': 1,
        'rows': [
            {'trading_day': '2026-01-02', 'symbol': 'AAA', 'advanced_to_stage2': False, 'exclusion_reason': 'Average daily dollar volume below threshold.'},
            {'trading_day': '2026-01-03', 'symbol': 'BBB', 'advanced_to_stage2': True, 'hit_target': True, 'entry_touched': True},
        ],
        'summary': {
            'days': 2,
            'advanced_stage2_total': 1,
            'precision_at_10': 0.6,
            'conditional_precision_at_10_entry_touched': 0.7,
            'overall_hit_rate': 0.6,
            'entry_touch_rate_stage2': 0.8,
            'stage_funnel_summary': {'actionable_lane': 1},
        },
    }
    scenario = _payload(2, precision=0.62, conditional=0.69, advanced=[('2026-01-02', 'AAA', True), ('2026-01-03', 'BBB', True)], actionable=2)
    row = build_gate_audit_row('relaxed', scenario, baseline_payload=baseline, overrides={'min_avg_dollar_volume': 1500000})
    assert row['newly_admitted_stage2'] == 1
    assert row['newly_admitted_hit_rate'] == 1.0
    assert row['delta_advanced_stage2_total'] == 1
    assert row['newly_admitted_baseline_reasons'][0]['reason'] == 'Average daily dollar volume below threshold.'


def test_recommend_gate_audit_scenario_prefers_acceptable_relaxed_case():
    rows = [
        {
            'scenario_name': 'baseline',
            'validation_id': 1,
            'precision_at_10': 0.61,
            'conditional_precision_at_10_entry_touched': 0.74,
            'delta_utility_score': 0.0,
            'newly_admitted_stage2': 0,
        },
        {
            'scenario_name': 'relaxed',
            'validation_id': 2,
            'precision_at_10': 0.59,
            'conditional_precision_at_10_entry_touched': 0.72,
            'delta_utility_score': 0.02,
            'newly_admitted_stage2': 4,
            'newly_admitted_hit_rate': 0.5,
        },
    ]
    decision = recommend_gate_audit_scenario(rows)
    assert decision['recommended_scenario'] == 'relaxed'
    assert decision['should_consider_change'] is True
