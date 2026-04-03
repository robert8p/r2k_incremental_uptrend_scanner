from __future__ import annotations

import json
import zipfile
from datetime import datetime, timezone
from io import BytesIO

from app.config import Settings
from app.db import Database
from app.services.evidence_pack import pack_to_zip_bytes
from app.services.stage2_regression_pack import build_stage2_regression_pack


def _candidate(symbol: str, *, advanced: bool, classification_code: str, classification_label: str, total_score: float, cycle_durability: float, width_retention: float, exclusion_reason: str | None = None) -> dict:
    metrics = {
        'range_classification_code': classification_code,
        'range_classification': classification_label,
        'within_range_target_possible': True,
        'trade_window_minutes_remaining': 120,
        'breakout_close_ratio': 0.0,
        'wickiness_ratio': 2.0,
        'range_containment_ratio': 0.8,
        'recent_breakout_close_ratio': 0.0,
        'recent_directional_efficiency': 0.2,
        'recent_lower_zone_touch_count': 2,
        'recent_upper_zone_touch_count': 2,
        'recent_completed_cycles_observed': 1,
        'recent_bounce_event_count': 2,
        'width_retention_ratio': width_retention,
        'cycle_persistence_ratio': 0.6,
        'bounce_quality_score': 55.0,
        'cycle_durability_score': cycle_durability,
        'distance_to_entry_pct': -0.5,
        'range_current_location': 0.4,
        'range_band_width_pct': 2.1,
        'completed_cycles_observed': 2,
        'structural_score': total_score + 10.0,
        'expected_actionability_score': total_score + 5.0,
        'actionability_score': total_score + 2.0,
        'execution_readiness_score': total_score + 1.0,
        'follow_through_confidence_score': total_score + 3.0,
        'recommendation_tier': 'watchlist' if advanced else 'rejected',
        'recommendation_book': 'touch_soon_queue' if advanced else 'rejected',
        'execution_lane': 'monitor_5m' if advanced else 'passive_watchlist',
        'touch_window_band': 'touch_viable' if advanced else 'unlikely_in_window',
        'score_cap_reason': None if advanced else 'Unstable non-range cap applied.',
    }
    return {
        'symbol': symbol,
        'company_name': f'{symbol} Inc',
        'advanced_to_stage2': advanced,
        'mover_rank': 1,
        'intraday_pct_gain': 12.0,
        'total_score': total_score,
        'recommendation_tier': 'watchlist' if advanced else 'rejected',
        'recommendation_book': 'touch_soon_queue' if advanced else 'rejected',
        'execution_lane': 'monitor_5m' if advanced else 'passive_watchlist',
        'touch_window_band': 'touch_viable' if advanced else 'unlikely_in_window',
        'monitor_cadence_minutes': 5 if advanced else 0,
        'metrics': metrics,
        'exclusion_reason': exclusion_reason,
    }


def _insert_scan(db: Database, trading_day: str, offset: int, stage2_count: int, candidates: list[dict]) -> int:
    return db.insert_scan(
        {
            'created_at': datetime.now(timezone.utc).isoformat(),
            'trading_day': trading_day,
            'scan_offset_minutes': offset,
            'scan_timestamp': datetime.now(timezone.utc).isoformat(),
            'status': 'ok',
            'mode': 'scan_only',
            'universe_count': 1000,
            'stage1_count': 50,
            'stage2_count': stage2_count,
            'summary': {
                'goal': 'test',
                'advanced_count': stage2_count,
                'shortlist_alignment': {
                    'enabled': True,
                    'selection_mode': 'aligned_prefilter_pool',
                    'alignment_pool_size': 200,
                    'alignment_prefilter_kept_count': 177,
                },
            },
        },
        candidates,
    )


def test_stage2_regression_pack_tracks_regression_and_gate_flips(tmp_path):
    db = Database(str(tmp_path / 'stage2_regression.db'))
    settings = Settings()
    _insert_scan(
        db,
        '2026-04-02',
        120,
        1,
        [
            _candidate('AEHR', advanced=True, classification_code='A', classification_label='Incrementally upward-shifting range', total_score=49.19, cycle_durability=58.0, width_retention=0.62),
            _candidate('AAOI', advanced=False, classification_code='C', classification_label='Unstable non-range behaviour', total_score=14.99, cycle_durability=12.0, width_retention=0.40, exclusion_reason='Unstable non-range behaviour; excluded by the range-cycling thesis gate.'),
        ],
    )
    _insert_scan(
        db,
        '2026-04-02',
        150,
        0,
        [
            _candidate('AEHR', advanced=False, classification_code='C', classification_label='Unstable non-range behaviour', total_score=7.40, cycle_durability=22.0, width_retention=0.43, exclusion_reason='Unstable non-range behaviour; excluded by the range-cycling thesis gate.'),
            _candidate('AAOI', advanced=False, classification_code='C', classification_label='Unstable non-range behaviour', total_score=14.99, cycle_durability=12.0, width_retention=0.40, exclusion_reason='Unstable non-range behaviour; excluded by the range-cycling thesis gate.'),
        ],
    )

    pack = build_stage2_regression_pack(settings, db, days=1, offsets=[120, 150])
    zip_bytes = pack_to_zip_bytes(pack)

    with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
        names = set(zf.namelist())
        assert 'regression_summary.json' in names
        assert 'regression_candidates.csv' in names
        assert 'regression_metric_deltas.csv' in names
        assert 'regression_gate_status.csv' in names
        assert 'latest_day_surface_breakdown.csv' in names
        summary = json.loads(zf.read('regression_summary.json'))
        candidate_csv = zf.read('regression_candidates.csv').decode('utf-8')
        delta_csv = zf.read('regression_metric_deltas.csv').decode('utf-8')
        gate_csv = zf.read('regression_gate_status.csv').decode('utf-8')

    assert summary['regressed_symbol_count'] == 1
    assert 'AEHR' in summary['regressed_symbols']
    assert 'regressed_after_early_advance' in candidate_csv
    assert 'cycle_durability_score' in delta_csv
    assert 'stable_range_width_retention' in gate_csv
    assert 'range_classification_not_unstable' in gate_csv
