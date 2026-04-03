from __future__ import annotations

import zipfile
from datetime import datetime, timezone
from io import BytesIO

from app.config import Settings
from app.db import Database
from app.services.evidence_pack import pack_to_zip_bytes
from app.services.recent_scan_export import build_recent_scan_export_pack


def _candidate(symbol: str) -> dict:
    return {
        'symbol': symbol,
        'advanced_to_stage2': True,
        'mover_rank': 1,
        'intraday_pct_gain': 5.0,
        'total_score': 80.0,
        'recommendation_tier': 'near_ready',
        'recommendation_book': 'actionable_now',
        'entry_low': 10.0,
        'entry_high': 10.2,
        'target_price': 10.3,
        'stop_price': 9.8,
        'metrics': {'spread_bps': 12.0},
    }


def test_recent_scan_export_pack_includes_latest_requested_pairs(tmp_path):
    db = Database(str(tmp_path / 'recent.db'))
    settings = Settings()
    for trading_day, offset, symbol in [
        ('2026-03-24', 120, 'AAA'),
        ('2026-03-24', 150, 'AAB'),
        ('2026-03-25', 120, 'BBB'),
        ('2026-03-25', 150, 'BBC'),
        ('2026-03-26', 120, 'CCC'),
        ('2026-03-26', 150, 'CCD'),
    ]:
        db.insert_scan(
            {
                'created_at': datetime.now(timezone.utc).isoformat(),
                'trading_day': trading_day,
                'scan_offset_minutes': offset,
                'scan_timestamp': datetime.now(timezone.utc).isoformat(),
                'status': 'ok',
                'mode': 'scan_only',
                'universe_count': 1000,
                'stage1_count': 50,
                'stage2_count': 1,
                'summary': {'goal': 'test', 'advanced_count': 1},
            },
            [_candidate(symbol)],
        )

    pack = build_recent_scan_export_pack(settings, db, days=2, offsets=[120, 150])
    zip_bytes = pack_to_zip_bytes(pack)

    with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
        names = set(zf.namelist())

    assert 'MANIFEST.json' in names
    assert any(name.endswith('offset_120_scan_5_summary.json') or name.endswith('offset_120_scan_3_summary.json') for name in names)
    assert any(name.endswith('offset_150_scan_6_summary.json') or name.endswith('offset_150_scan_4_summary.json') for name in names)
    assert any(name.endswith('_candidates.csv') for name in names)
