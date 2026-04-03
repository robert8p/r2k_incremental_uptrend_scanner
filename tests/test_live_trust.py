from __future__ import annotations

from datetime import datetime, timezone

from app.config import Settings
from app.db import Database
from app.services.live_trust import build_live_trust_snapshot


def _advanced_candidate(symbol: str = 'ABCD') -> dict:
    return {
        'symbol': symbol,
        'advanced_to_stage2': True,
        'mover_rank': 1,
        'intraday_pct_gain': 4.2,
        'total_score': 87.5,
        'recommendation_tier': 'near_ready',
        'recommendation_book': 'actionable_now',
        'entry_low': 10.0,
        'entry_high': 10.2,
        'target_price': 10.3,
        'stop_price': 9.8,
        'metrics': {'spread_bps': 12.0},
    }


def test_insert_scan_seeds_pending_live_outcomes(tmp_path):
    db = Database(str(tmp_path / 'trust.db'))
    scan_id = db.insert_scan(
        {
            'created_at': datetime.now(timezone.utc).isoformat(),
            'trading_day': '2026-03-20',
            'scan_offset_minutes': 120,
            'scan_timestamp': datetime.now(timezone.utc).isoformat(),
            'status': 'ok',
            'mode': 'scan_only',
            'universe_count': 1000,
            'stage1_count': 50,
            'stage2_count': 1,
            'summary': {'goal': 'test'},
        },
        [_advanced_candidate(), {'symbol': 'MISS', 'advanced_to_stage2': False}],
    )

    rows = db.list_live_candidate_outcomes_for_scan(scan_id)
    assert len(rows) == 1
    assert rows[0]['symbol'] == 'ABCD'
    assert rows[0]['evaluation_status'] == 'pending'


def test_live_trust_snapshot_surfaces_schedule_alignment_and_drift(tmp_path):
    db = Database(str(tmp_path / 'trust_snapshot.db'))
    settings = Settings(scheduled_scan_offsets='30,60,90,120')

    scan_id = db.insert_scan(
        {
            'created_at': datetime.now(timezone.utc).isoformat(),
            'trading_day': '2026-03-20',
            'scan_offset_minutes': 120,
            'scan_timestamp': datetime.now(timezone.utc).isoformat(),
            'status': 'ok',
            'mode': 'scan_only',
            'universe_count': 1000,
            'stage1_count': 50,
            'stage2_count': 1,
            'summary': {'goal': 'test'},
        },
        [_advanced_candidate()],
    )
    db.update_live_candidate_outcome(
        scan_id,
        'ABCD',
        evaluation_status='evaluated',
        evaluated_at=datetime.now(timezone.utc).isoformat(),
        entry_touched=1,
        hit_target=1,
    )

    run_id = db.insert_research_run(
        {
            'created_at': datetime.now(timezone.utc).isoformat(),
            'mode': 'three_month_offset_ladder_research',
        },
        status='completed',
        message='done',
    )
    db.update_research_run(
        run_id,
        status='completed',
        progress=1.0,
        result={
            'validation_id': 23,
            'best_validation_id': 23,
            'offset_ladder_summary': [
                {
                    'scan_offset_minutes': 120,
                    'overall_hit_rate': 0.55,
                    'entry_touch_rate_stage2': 0.65,
                    'conditional_precision_at_10_entry_touched': 0.7,
                    'recommended': True,
                }
            ],
            'recommended_live_schedule': {
                'suggested_scheduled_scan_offsets': '120,150',
                'default_scan_offset_minutes': 150,
                'schedule_should_apply': True,
                'all_offsets_failed_gates': False,
            },
        },
    )

    snapshot = build_live_trust_snapshot(settings, db)

    assert snapshot['latest_research_run_id'] == run_id
    assert snapshot['schedule_alignment_ok'] is False
    assert snapshot['offset_rows'][0]['scan_offset_minutes'] == 120
    assert snapshot['offset_rows'][0]['live_overall_hit_rate'] == 1.0
    assert snapshot['offset_rows'][0]['expected_overall_hit_rate'] == 0.55
