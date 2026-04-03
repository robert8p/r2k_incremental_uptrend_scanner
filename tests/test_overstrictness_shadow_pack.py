from __future__ import annotations

import json
import zipfile
from datetime import datetime, timezone
from io import BytesIO

import pandas as pd

from app.config import Settings
from app.db import Database
from app.services.evidence_pack import pack_to_zip_bytes
from app.services.overstrictness_shadow_pack import build_overstrictness_shadow_pack


class DummyAlpaca:
    def has_credentials(self) -> bool:
        return True

    def fetch_bars(self, symbols, timeframe, start_iso, end_iso):
        assert timeframe == '1Min'
        frames = {
            'AEHR': pd.DataFrame(
                [
                    {'timestamp': '2026-04-02T14:30:00Z', 'open': 10.00, 'high': 10.02, 'low': 9.98, 'close': 10.01, 'volume': 1000},
                    {'timestamp': '2026-04-02T16:31:00Z', 'open': 10.03, 'high': 10.08, 'low': 10.00, 'close': 10.05, 'volume': 1200},
                    {'timestamp': '2026-04-02T16:35:00Z', 'open': 10.10, 'high': 10.23, 'low': 10.08, 'close': 10.22, 'volume': 1300},
                    {'timestamp': '2026-04-02T17:05:00Z', 'open': 10.21, 'high': 10.24, 'low': 10.18, 'close': 10.20, 'volume': 900},
                    {'timestamp': '2026-04-02T17:20:00Z', 'open': 10.18, 'high': 10.19, 'low': 10.10, 'close': 10.12, 'volume': 850},
                ]
            ),
            'ORIC': pd.DataFrame(
                [
                    {'timestamp': '2026-04-02T14:30:00Z', 'open': 30.00, 'high': 30.05, 'low': 29.95, 'close': 30.02, 'volume': 800},
                    {'timestamp': '2026-04-02T16:31:00Z', 'open': 30.02, 'high': 30.10, 'low': 30.00, 'close': 30.08, 'volume': 900},
                    {'timestamp': '2026-04-02T16:36:00Z', 'open': 30.08, 'high': 30.45, 'low': 30.05, 'close': 30.41, 'volume': 1100},
                    {'timestamp': '2026-04-02T17:00:00Z', 'open': 30.40, 'high': 30.43, 'low': 30.35, 'close': 30.38, 'volume': 700},
                ]
            ),
            'AAOI': pd.DataFrame(
                [
                    {'timestamp': '2026-04-02T14:30:00Z', 'open': 20.00, 'high': 20.05, 'low': 19.95, 'close': 20.01, 'volume': 1000},
                    {'timestamp': '2026-04-02T16:31:00Z', 'open': 20.30, 'high': 20.32, 'low': 20.25, 'close': 20.28, 'volume': 900},
                    {'timestamp': '2026-04-02T17:10:00Z', 'open': 20.35, 'high': 20.36, 'low': 20.30, 'close': 20.31, 'volume': 800},
                ]
            ),
        }
        return {symbol: frames[symbol].copy() for symbol in symbols if symbol in frames}


def _candidate(
    symbol: str,
    *,
    advanced: bool,
    classification_code: str,
    total_score: float,
    entry_low: float,
    entry_high: float,
    width_retention: float = 0.60,
    cycle_persistence: float = 0.50,
    bounce_quality: float = 45.0,
    cycle_durability: float = 40.0,
    exclusion_reason: str | None = None,
) -> dict:
    metrics = {
        'range_classification_code': classification_code,
        'range_classification': 'Stable range' if classification_code != 'C' else 'Unstable non-range behaviour',
        'spread_bps': 5.0,
        'distance_to_entry_pct': -0.5,
        'range_current_location': 0.4,
        'effective_headroom_pct': 1.8,
        'within_range_target_possible': True,
        'score_cap_reason': None if advanced else 'Unstable non-range cap applied.',
        'trade_window_minutes_remaining': 120,
        'breakout_close_ratio': 0.0,
        'wickiness_ratio': 3.0,
        'range_containment_ratio': 1.0,
        'recent_breakout_close_ratio': 0.0,
        'recent_directional_efficiency': 0.2,
        'recent_lower_zone_touch_count': 2,
        'recent_upper_zone_touch_count': 2,
        'recent_completed_cycles_observed': 1,
        'recent_bounce_event_count': 2,
        'width_retention_ratio': width_retention,
        'cycle_persistence_ratio': cycle_persistence,
        'bounce_quality_score': bounce_quality,
        'cycle_durability_score': cycle_durability,
    }
    zone_mid = round((entry_low + entry_high) / 2.0, 4)
    return {
        'symbol': symbol,
        'company_name': f'{symbol} Inc',
        'advanced_to_stage2': advanced,
        'mover_rank': 1,
        'intraday_pct_gain': 10.0,
        'total_score': total_score,
        'recommendation_tier': 'watchlist' if advanced else 'rejected',
        'recommendation_book': 'touch_soon_queue' if advanced else 'rejected',
        'execution_lane': 'monitor_5m' if advanced else 'passive_watchlist',
        'touch_window_band': 'touch_viable' if advanced else 'unlikely_in_window',
        'monitor_cadence_minutes': 5 if advanced else 0,
        'metrics': metrics,
        'exclusion_reason': exclusion_reason,
        'entry_low': entry_low,
        'entry_high': entry_high,
        'target_price': round(zone_mid * 1.01, 4),
        'stop_price': round(zone_mid * 0.99, 4),
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


def test_overstrictness_shadow_pack_accumulates_verdicts_and_profiles(tmp_path):
    db = Database(str(tmp_path / 'overstrictness_shadow.db'))
    settings = Settings()
    _insert_scan(
        db,
        '2026-04-02',
        120,
        1,
        [
            _candidate('AEHR', advanced=True, classification_code='A', total_score=49.19, entry_low=10.00, entry_high=10.10),
            _candidate('ORIC', advanced=False, classification_code='C', total_score=22.0, entry_low=30.00, entry_high=30.10, width_retention=0.52, cycle_persistence=0.35, bounce_quality=36.0, cycle_durability=30.0, exclusion_reason='Unstable non-range behaviour; excluded by the range-cycling thesis gate.'),
            _candidate('AAOI', advanced=False, classification_code='C', total_score=14.99, entry_low=19.80, entry_high=19.90, width_retention=0.30, cycle_persistence=0.10, bounce_quality=20.0, cycle_durability=10.0, exclusion_reason='Unstable non-range behaviour; excluded by the range-cycling thesis gate.'),
        ],
    )
    _insert_scan(
        db,
        '2026-04-02',
        150,
        0,
        [
            _candidate('AEHR', advanced=False, classification_code='C', total_score=7.40, entry_low=10.30, entry_high=10.40, width_retention=0.40, cycle_persistence=0.20, bounce_quality=25.0, cycle_durability=0.0, exclusion_reason='Unstable non-range behaviour; excluded by the range-cycling thesis gate.'),
            _candidate('ORIC', advanced=False, classification_code='C', total_score=12.0, entry_low=30.20, entry_high=30.30, width_retention=0.45, cycle_persistence=0.25, bounce_quality=28.0, cycle_durability=20.0, exclusion_reason='Unstable non-range behaviour; excluded by the range-cycling thesis gate.'),
            _candidate('AAOI', advanced=False, classification_code='C', total_score=10.0, entry_low=20.20, entry_high=20.30, width_retention=0.25, cycle_persistence=0.08, bounce_quality=18.0, cycle_durability=8.0, exclusion_reason='Unstable non-range behaviour; excluded by the range-cycling thesis gate.'),
        ],
    )

    pack = build_overstrictness_shadow_pack(settings, db, DummyAlpaca(), days=3, offsets=[120, 150], rejected_sample_per_offset=2)
    zip_bytes = pack_to_zip_bytes(pack)

    with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
        names = set(zf.namelist())
        summary = json.loads(zf.read('overstrictness_shadow_summary.json'))
        profile_rollup_csv = zf.read('shadow_threshold_profile_rollup.csv').decode('utf-8')
        predicate_fail_csv = zf.read('overstrictness_predicate_fail_rollup.csv').decode('utf-8')
        rows_csv = zf.read('overstrictness_shadow_rows.csv').decode('utf-8')

    assert 'overstrictness_shadow_rows.csv' in names
    assert 'shadow_threshold_profile_rollup.csv' in names
    assert 'overstrictness_predicate_fail_rollup.csv' in names
    assert 'overstrictness_intraday_bars.csv' in names
    assert summary['clean_day_count'] == 1
    assert summary['verdict_counts']['possible_classifier_overstrict'] >= 1
    assert summary['verdict_counts']['classifier_correct_reject'] >= 1
    assert 'combined_soft_structure' in profile_rollup_csv
    assert 'flagged_possible_classifier_overstrict' in profile_rollup_csv
    assert 'stable_range_bounce_quality' in predicate_fail_csv or 'stable_range_cycle_durability' in predicate_fail_csv
    assert 'possible_classifier_overstrict' in rows_csv
    assert 'classifier_correct_reject' in rows_csv
