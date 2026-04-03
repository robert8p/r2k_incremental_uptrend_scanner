from __future__ import annotations

import json
import zipfile
from datetime import datetime, timezone
from io import BytesIO

import pandas as pd

from app.config import Settings
from app.db import Database
from app.services.classifier_audit_pack import build_classifier_audit_pack
from app.services.evidence_pack import pack_to_zip_bytes


class DummyAlpaca:
    def has_credentials(self) -> bool:
        return True

    def fetch_bars(self, symbols, timeframe, start_iso, end_iso):
        assert timeframe == '1Min'
        frame = pd.DataFrame(
            [
                {'timestamp': '2026-04-02T14:30:00Z', 'open': 10.0, 'high': 10.2, 'low': 9.9, 'close': 10.1, 'volume': 1000},
                {'timestamp': '2026-04-02T16:30:00Z', 'open': 10.1, 'high': 10.3, 'low': 10.0, 'close': 10.2, 'volume': 1200},
                {'timestamp': '2026-04-02T17:00:00Z', 'open': 10.2, 'high': 10.25, 'low': 10.05, 'close': 10.08, 'volume': 1100},
            ]
        )
        return {symbol: frame.copy() for symbol in symbols}


def _candidate(symbol: str, *, advanced: bool, classification_code: str, total_score: float, cycle_durability: float, bounce_quality: float, exclusion_reason: str | None = None) -> dict:
    metrics = {
        'range_classification_code': classification_code,
        'range_classification': 'Stable range' if classification_code != 'C' else 'Unstable non-range behaviour',
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
        'width_retention_ratio': 0.62 if advanced else 0.4,
        'cycle_persistence_ratio': 0.65 if advanced else 0.3,
        'bounce_quality_score': bounce_quality,
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


def test_classifier_audit_pack_contains_intraday_bars_and_samples(tmp_path):
    db = Database(str(tmp_path / 'classifier_audit.db'))
    settings = Settings()
    _insert_scan(
        db,
        '2026-04-02',
        120,
        1,
        [
            _candidate('AEHR', advanced=True, classification_code='A', total_score=49.19, cycle_durability=58.0, bounce_quality=58.344),
            _candidate('AAOI', advanced=False, classification_code='C', total_score=14.99, cycle_durability=12.0, bounce_quality=28.0, exclusion_reason='Unstable non-range behaviour; excluded by the range-cycling thesis gate.'),
        ],
    )
    _insert_scan(
        db,
        '2026-04-02',
        150,
        0,
        [
            _candidate('AEHR', advanced=False, classification_code='C', total_score=7.40, cycle_durability=0.0, bounce_quality=28.792, exclusion_reason='Unstable non-range behaviour; excluded by the range-cycling thesis gate.'),
            _candidate('AAOI', advanced=False, classification_code='C', total_score=14.99, cycle_durability=12.0, bounce_quality=28.0, exclusion_reason='Unstable non-range behaviour; excluded by the range-cycling thesis gate.'),
        ],
    )

    pack = build_classifier_audit_pack(settings, db, DummyAlpaca(), days=1, offsets=[120, 150], rejected_sample_per_offset=1)
    zip_bytes = pack_to_zip_bytes(pack)

    with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
        names = set(zf.namelist())
        summary = json.loads(zf.read('classifier_audit_summary.json'))
        symbols_csv = zf.read('classifier_audit_symbols.csv').decode('utf-8')
        gate_csv = zf.read('classifier_audit_gate_snapshot.csv').decode('utf-8')
        bars_csv = zf.read('classifier_audit_intraday_bars.csv').decode('utf-8')

    assert 'classifier_audit_scan_rollup.csv' in names
    assert 'classifier_audit_metric_snapshots.csv' in names
    assert 'classifier_audit_metric_deltas.csv' in names
    assert 'classifier_audit_gate_snapshot.csv' in names
    assert 'classifier_audit_intraday_bars.csv' in names
    assert summary['audited_symbol_count'] == 2
    assert summary['intraday_bars_included'] is True
    assert 'AEHR' in symbols_csv
    assert 'AAOI' in symbols_csv
    assert 'range_classification_not_unstable' in gate_csv
    assert 'at_or_before_120m_checkpoint' in bars_csv
