from __future__ import annotations

import json
import zipfile
from datetime import datetime, timezone
from io import BytesIO

from app.config import Settings
from app.db import Database
from app.services.evidence_pack import pack_to_zip_bytes
from app.services.stage2_sanity_pack import build_stage2_sanity_pack


def _candidate(symbol: str, *, advanced: bool, tier: str | None = None, book: str | None = None, lane: str | None = None, exclusion_reason: str | None = None) -> dict:
    effective_tier = tier if tier is not None else ('watchlist' if advanced else 'rejected')
    effective_book = book if book is not None else ('touch_soon_queue' if advanced else 'rejected')
    effective_lane = lane if lane is not None else ('monitor_5m' if advanced else 'passive_watchlist')
    metrics = {
        'range_classification': 'Stable range' if advanced else 'Unstable non-range behaviour',
        'distance_to_entry_pct': -0.2 if advanced else -1.5,
        'recommendation_tier': effective_tier,
        'recommendation_book': effective_book,
        'execution_lane': effective_lane,
        'touch_window_band': 'touch_soon' if advanced else 'unlikely_in_window',
        'monitor_cadence_minutes': 5 if advanced else 0,
        'execution_readiness_score': 42.0 if advanced else 0.0,
        'follow_through_confidence_score': 58.0 if advanced else 0.0,
        'expected_actionability_score': 44.0 if advanced else 0.0,
        'actionability_score': 36.0 if advanced else 0.0,
        'headline_rank_score': 22.0 if advanced else 0.0,
        'structural_score': 55.0 if advanced else 14.0,
        'score_cap_reason': None if advanced else 'Unstable non-range cap applied.',
    }
    return {
        'symbol': symbol,
        'company_name': f'{symbol} Corp',
        'advanced_to_stage2': advanced,
        'mover_rank': 1,
        'intraday_pct_gain': 5.0,
        'total_score': 55.0 if advanced else 14.0,
        'recommendation_tier': effective_tier,
        'recommendation_book': effective_book,
        'execution_lane': effective_lane,
        'touch_window_band': 'touch_soon' if advanced else 'unlikely_in_window',
        'monitor_cadence_minutes': 5 if advanced else 0,
        'exclusion_reason': exclusion_reason,
        'entry_low': 10.0,
        'entry_high': 10.2,
        'target_price': 10.3,
        'stop_price': 9.8,
        'metrics': metrics,
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


def test_stage2_sanity_pack_includes_progression_and_advanced_rows(tmp_path):
    db = Database(str(tmp_path / 'stage2_sanity.db'))
    settings = Settings()
    scan_120 = _insert_scan(db, '2026-04-02', 120, 2, [
        _candidate('FC', advanced=True),
        _candidate('AEHR', advanced=True),
        _candidate('AAOI', advanced=False, exclusion_reason='Unstable non-range behaviour; excluded by the range-cycling thesis gate.'),
    ])
    _insert_scan(db, '2026-04-02', 150, 0, [
        _candidate('FC', advanced=False, exclusion_reason='Unstable non-range behaviour; excluded by the range-cycling thesis gate.'),
        _candidate('AEHR', advanced=False, exclusion_reason='Unstable non-range behaviour; excluded by the range-cycling thesis gate.'),
        _candidate('AAOI', advanced=False, exclusion_reason='Unstable non-range behaviour; excluded by the range-cycling thesis gate.'),
    ])
    db.update_live_candidate_outcome(scan_120, 'FC', evaluation_status='evaluated', entry_touched=1, hit_target=0)

    pack = build_stage2_sanity_pack(settings, db, days=1, offsets=[120, 150])
    zip_bytes = pack_to_zip_bytes(pack)

    with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
        names = set(zf.namelist())
        assert 'audit_summary.json' in names
        assert 'advanced_candidates.csv' in names
        assert 'latest_day_progression.csv' in names
        audit_summary = json.loads(zf.read('audit_summary.json'))
        progression_csv = zf.read('latest_day_progression.csv').decode('utf-8')
        advanced_csv = zf.read('advanced_candidates.csv').decode('utf-8')

    assert audit_summary['pipeline_proof_exists'] is True
    assert 'regressed_after_early_advance' in progression_csv
    assert 'FC' in progression_csv
    assert 'touch_soon_queue' in advanced_csv
    assert 'monitor_5m' in advanced_csv
