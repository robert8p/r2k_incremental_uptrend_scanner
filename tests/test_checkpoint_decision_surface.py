from __future__ import annotations

import json
import zipfile
from datetime import datetime, timezone
from io import BytesIO

from app.config import Settings
from app.db import Database
from app.services.checkpoint_decision_surface import build_checkpoint_decision_pack, build_checkpoint_decision_surface
from app.services.evidence_pack import pack_to_zip_bytes


def _candidate(symbol: str, *, advanced: bool, total_score: float, tier: str = 'watchlist', exclusion_reason: str | None = None, classification: str = 'Stable range', classification_code: str = 'A') -> dict:
    metrics = {
        'range_classification': classification,
        'range_classification_code': classification_code,
        'score_cap_reason': None if advanced else 'Unstable non-range cap applied.',
        'actionability_score': total_score - 5.0,
        'expected_actionability_score': total_score - 2.0,
        'execution_readiness_score': total_score - 10.0,
        'follow_through_confidence_score': total_score - 7.0,
        'distance_to_entry_pct': -0.3,
    }
    return {
        'symbol': symbol,
        'company_name': f'{symbol} Inc',
        'advanced_to_stage2': advanced,
        'mover_rank': 1,
        'intraday_pct_gain': 10.0,
        'total_score': total_score,
        'recommendation_tier': tier if advanced else 'rejected',
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
                'leader_symbol': 'AAA',
                'leader_gain_pct': 9.9,
                'shortlist_alignment': {
                    'enabled': True,
                    'selection_mode': 'aligned_prefilter_pool',
                    'alignment_pool_size': 200,
                    'alignment_prefilter_kept_count': 175,
                },
            },
        },
        candidates,
    )


def test_checkpoint_decision_surface_tracks_current_and_regressed_candidates(tmp_path):
    db = Database(str(tmp_path / 'checkpoint.db'))
    settings = Settings()
    _insert_scan(
        db,
        '2026-04-02',
        120,
        2,
        [
            _candidate('AEHR', advanced=True, total_score=49.19),
            _candidate('FC', advanced=True, total_score=44.99),
        ],
    )
    _insert_scan(
        db,
        '2026-04-02',
        150,
        1,
        [
            _candidate('AEHR', advanced=False, total_score=7.40, exclusion_reason='Unstable non-range behaviour; excluded by the range-cycling thesis gate.', classification='Unstable non-range behaviour', classification_code='C'),
            _candidate('FC', advanced=True, total_score=31.00, tier='near_ready'),
        ],
    )

    surface = build_checkpoint_decision_surface(settings, db, trading_day='2026-04-02', offsets=[120, 150])

    assert surface['summary']['unique_symbols_advanced_any_checkpoint'] == 2
    assert surface['summary']['currently_valid_now_count'] == 1
    assert surface['summary']['regressed_after_earlier_validity_count'] == 1
    assert surface['summary']['best_checkpoint_offset_minutes'] == 120
    assert [row['symbol'] for row in surface['current_valid_now']] == ['FC']
    assert [row['symbol'] for row in surface['regressed_candidates']] == ['AEHR']


def test_checkpoint_decision_pack_contains_expected_files(tmp_path):
    db = Database(str(tmp_path / 'checkpoint_pack.db'))
    settings = Settings()
    _insert_scan(
        db,
        '2026-04-02',
        120,
        1,
        [_candidate('AEHR', advanced=True, total_score=49.19)],
    )
    _insert_scan(
        db,
        '2026-04-02',
        150,
        0,
        [_candidate('AEHR', advanced=False, total_score=7.40, exclusion_reason='Unstable non-range behaviour; excluded by the range-cycling thesis gate.', classification='Unstable non-range behaviour', classification_code='C')],
    )

    pack = build_checkpoint_decision_pack(settings, db, trading_day='2026-04-02', offsets=[120, 150])
    zip_bytes = pack_to_zip_bytes(pack)

    with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
        names = set(zf.namelist())
        summary = json.loads(zf.read('checkpoint_decision_summary.json'))
        regressed_csv = zf.read('checkpoint_regressed_candidates.csv').decode('utf-8')

    assert 'checkpoint_scan_summary.csv' in names
    assert 'checkpoint_current_valid_now.csv' in names
    assert 'checkpoint_regressed_candidates.csv' in names
    assert 'checkpoint_best_candidates.csv' in names
    assert summary['regressed_after_earlier_validity_count'] == 1
    assert 'AEHR' in regressed_csv
