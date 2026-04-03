from __future__ import annotations

import json
import zipfile
from datetime import datetime, timezone
from io import BytesIO

import pandas as pd

from app.config import Settings
from app.db import Database
from app.services.evidence_pack import pack_to_zip_bytes
from app.services.outcome_adjudication_pack import build_outcome_adjudication_pack


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
            'AAOI': pd.DataFrame(
                [
                    {'timestamp': '2026-04-02T14:30:00Z', 'open': 20.00, 'high': 20.05, 'low': 19.95, 'close': 20.01, 'volume': 1000},
                    {'timestamp': '2026-04-02T16:31:00Z', 'open': 20.30, 'high': 20.32, 'low': 20.25, 'close': 20.28, 'volume': 900},
                    {'timestamp': '2026-04-02T17:10:00Z', 'open': 20.35, 'high': 20.36, 'low': 20.30, 'close': 20.31, 'volume': 800},
                ]
            ),
        }
        return {symbol: frames[symbol].copy() for symbol in symbols}


def _candidate(symbol: str, *, advanced: bool, classification_code: str, total_score: float, entry_low: float, entry_high: float, exclusion_reason: str | None = None) -> dict:
    metrics = {
        'range_classification_code': classification_code,
        'range_classification': 'Stable range' if classification_code != 'C' else 'Unstable non-range behaviour',
        'spread_bps': 5.0,
        'distance_to_entry_pct': -0.5,
        'range_current_location': 0.4,
        'effective_headroom_pct': 1.8,
        'within_range_target_possible': True,
        'score_cap_reason': None if advanced else 'Unstable non-range cap applied.',
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


def test_outcome_adjudication_pack_automates_tradeability_verdicts(tmp_path):
    db = Database(str(tmp_path / 'outcome_adjudication.db'))
    settings = Settings()
    _insert_scan(
        db,
        '2026-04-02',
        120,
        1,
        [
            _candidate('AEHR', advanced=True, classification_code='A', total_score=49.19, entry_low=10.00, entry_high=10.10),
            _candidate('AAOI', advanced=False, classification_code='C', total_score=14.99, entry_low=19.80, entry_high=19.90, exclusion_reason='Unstable non-range behaviour; excluded by the range-cycling thesis gate.'),
        ],
    )
    _insert_scan(
        db,
        '2026-04-02',
        150,
        0,
        [
            _candidate('AEHR', advanced=False, classification_code='C', total_score=7.40, entry_low=10.30, entry_high=10.40, exclusion_reason='Unstable non-range behaviour; excluded by the range-cycling thesis gate.'),
            _candidate('AAOI', advanced=False, classification_code='C', total_score=14.99, entry_low=19.80, entry_high=19.90, exclusion_reason='Unstable non-range behaviour; excluded by the range-cycling thesis gate.'),
        ],
    )

    pack = build_outcome_adjudication_pack(settings, db, DummyAlpaca(), days=1, offsets=[120, 150], rejected_sample_per_offset=1)
    zip_bytes = pack_to_zip_bytes(pack)

    with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
        names = set(zf.namelist())
        summary = json.loads(zf.read('outcome_adjudication_summary.json'))
        rows_csv = zf.read('outcome_adjudication_rows.csv').decode('utf-8')
        progression_csv = zf.read('outcome_adjudication_progression.csv').decode('utf-8')
        rollup_csv = zf.read('outcome_adjudication_verdict_rollup.csv').decode('utf-8')

    assert 'outcome_adjudication_rows.csv' in names
    assert 'outcome_adjudication_progression.csv' in names
    assert 'outcome_adjudication_verdict_rollup.csv' in names
    assert 'outcome_adjudication_intraday_bars.csv' in names
    assert summary['audited_symbol_count'] == 2
    assert summary['verdict_counts']['advanced_and_tradeable'] >= 1
    assert summary['verdict_counts']['classifier_correct_reject'] >= 1
    assert summary['pair_verdict_counts']['late_regression_was_correct'] == 1
    assert 'intrabar_target_reached' in rows_csv
    assert 'advanced_and_tradeable' in rows_csv
    assert 'classifier_correct_reject' in rows_csv
    assert 'late_regression_was_correct' in progression_csv
    assert 'share_of_audited_symbols' in rollup_csv
