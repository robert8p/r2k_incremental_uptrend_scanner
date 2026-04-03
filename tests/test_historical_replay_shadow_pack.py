from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import datetime, timezone

from app.config import Settings
from app.services.evidence_pack import pack_to_zip_bytes
from app.services.historical_replay_shadow_pack import (
    build_historical_replay_shadow_pack,
    get_or_build_historical_replay_shadow_zip,
    read_cached_historical_replay_summary,
)

UTC = timezone.utc


class DummyAlpaca:
    def __init__(self, has_creds: bool = True):
        self._has_creds = has_creds

    def has_credentials(self) -> bool:
        return self._has_creds


def _row(symbol: str, trading_day: str, *, offset: int, advanced: bool, hit: bool, touched: bool, range_code: str = 'C', bounce: float = 20.0, durability: float = 20.0, persistence: float = 0.2, width_retention: float = 0.6) -> dict[str, object]:
    return {
        'trading_day': trading_day,
        'symbol': symbol,
        'advanced_to_stage2': advanced,
        'baseline_eligible': True,
        'scored_for_replay': True,
        'entry_touched': touched,
        'hit_target': hit,
        'minutes_to_entry': 5 if touched else None,
        'minutes_to_target': 20 if hit else None,
        'range_classification_code': range_code,
        'mover_rank': 3,
        'intraday_pct_gain': 11.2,
        'metrics': {
            'distance_to_entry_pct': -0.2,
            'width_retention_ratio': width_retention,
            'cycle_persistence_ratio': persistence,
            'bounce_quality_score': bounce,
            'cycle_durability_score': durability,
            'within_range_target_possible': True,
        },
    }


def test_historical_replay_shadow_pack_builds_profile_rollup(monkeypatch, tmp_path):
    settings = Settings(data_dir=str(tmp_path), database_path=str(tmp_path / 'replay.db'))
    payloads = {
        120: {
            'rows': [
                _row('BASE', '2026-04-02', offset=120, advanced=True, hit=True, touched=True, range_code='A', bounce=65.0, durability=70.0, persistence=0.9, width_retention=0.8),
                _row('AIRS', '2026-04-02', offset=120, advanced=False, hit=True, touched=True, bounce=35.0, durability=22.0, persistence=0.2, width_retention=0.6),
                _row('SATL', '2026-04-03', offset=120, advanced=False, hit=True, touched=True, bounce=32.0, durability=24.0, persistence=0.2, width_retention=0.6),
                _row('ORIC', '2026-04-03', offset=120, advanced=False, hit=True, touched=True, bounce=33.0, durability=23.0, persistence=0.2, width_retention=0.6),
                _row('JUNK', '2026-04-03', offset=120, advanced=False, hit=False, touched=False, bounce=10.0, durability=10.0, persistence=0.1, width_retention=0.3),
            ]
        },
        150: {
            'rows': [
                _row('BASE2', '2026-04-02', offset=150, advanced=True, hit=False, touched=False, range_code='A', bounce=60.0, durability=65.0, persistence=0.8, width_retention=0.8),
                _row('LUNR', '2026-04-02', offset=150, advanced=False, hit=True, touched=True, bounce=31.0, durability=21.0, persistence=0.15, width_retention=0.65),
            ]
        },
    }

    monkeypatch.setattr(
        'app.services.historical_replay_shadow_pack._select_replay_window',
        lambda settings, lookback_days: ('2026-04-01', '2026-04-03', ['2026-04-01', '2026-04-02', '2026-04-03'], 3),
    )
    monkeypatch.setattr(
        'app.services.historical_replay_shadow_pack._build_validation_payload',
        lambda settings, db, alpaca, start_date, end_date, scan_offset_minutes: payloads[int(scan_offset_minutes)],
    )

    pack = build_historical_replay_shadow_pack(settings, object(), DummyAlpaca(), lookback_days=90, offsets=[120, 150])
    summary = json.loads(pack['historical_replay_shadow_summary.json'])
    rollup_csv = pack['historical_replay_shadow_profile_rollup.csv'].decode('utf-8')

    assert summary['overall_verdict'] == 'historical_replay_supports_candidate_profile'
    assert summary['recommended_profile']['profile_name'] in {'soft_bounce_quality', 'combined_soft_structure'}
    assert 'soft_bounce_quality' in rollup_csv
    assert 'historical_replay_shadow_daily_rollup.csv' in pack


def test_historical_replay_shadow_zip_build_uses_cache(monkeypatch, tmp_path):
    settings = Settings(data_dir=str(tmp_path), database_path=str(tmp_path / 'replay-cache.db'))
    monkeypatch.setattr(
        'app.services.historical_replay_shadow_pack.build_historical_replay_shadow_pack',
        lambda *args, **kwargs: {
            'historical_replay_shadow_summary.json': json.dumps({'overall_verdict': 'historical_replay_supports_candidate_profile'}).encode('utf-8')
        },
    )
    raw = get_or_build_historical_replay_shadow_zip(settings, object(), DummyAlpaca(), prefer_cache=False)
    with zipfile.ZipFile(io.BytesIO(raw), 'r') as zf:
        assert 'historical_replay_shadow_summary.json' in zf.namelist()
    summary = read_cached_historical_replay_summary(settings)
    assert summary['overall_verdict'] == 'historical_replay_supports_candidate_profile'
