from __future__ import annotations

import io
import json
import zipfile

from fastapi.testclient import TestClient

from app.config import Settings
from app.factory import create_app
from app.runtime import AppRuntime
from app.services.evidence_pack import pack_to_zip_bytes
from app.services.replay_bottleneck_pack import build_replay_bottleneck_pack


class DummyAlpaca:
    def __init__(self, has_creds: bool = True):
        self._has_creds = has_creds

    def has_credentials(self) -> bool:
        return self._has_creds


class DummyDB:
    def __init__(self, path: str):
        self.path = path


def _csv_bytes(rows: list[dict[str, object]]) -> bytes:
    if not rows:
        return b''
    headers = list(rows[0].keys())
    lines = [','.join(headers)]
    for row in rows:
        lines.append(','.join(str(row.get(h, '')) for h in headers))
    return ('\n'.join(lines) + '\n').encode('utf-8')


def test_build_replay_bottleneck_pack_splits_offset_and_failure_paths(monkeypatch, tmp_path):
    settings = Settings(data_dir=str(tmp_path), database_path=str(tmp_path / 'replay-bottleneck.db'))
    replay_pack = {
        'historical_replay_shadow_summary.json': json.dumps(
            {
                'generated_at_utc': '2026-04-04T00:00:00+00:00',
                'app_version': '1.39.14',
                'overall_verdict': 'historical_replay_no_clear_candidate',
                'overall_reason': 'No clear candidate yet.',
                'lookback_days_requested': 90,
                'lookback_days_effective': 80,
                'offsets_requested': [120, 150],
                'recommended_profile': {
                    'profile_name': 'soft_cycle_durability',
                    'flagged_tradeable': 3,
                    'tradeable_share': 0.4286,
                },
            }
        ).encode('utf-8'),
        'historical_replay_shadow_profile_rows.csv': _csv_bytes(
            [
                {
                    'trading_day': '2026-04-01',
                    'scan_offset_minutes': 120,
                    'symbol': 'AAA',
                    'entry_touched': 'True',
                    'hit_target': 'True',
                    'minutes_to_entry': '5',
                    'minutes_to_target': '10',
                    'mover_rank': '4',
                    'intraday_pct_gain': '8.0',
                    'distance_to_entry_pct': '-0.5',
                    'width_retention_ratio': '0.7',
                    'cycle_persistence_ratio': '0.9',
                    'bounce_quality_score': '40',
                    'cycle_durability_score': '31',
                    'profile_name': 'soft_cycle_durability',
                    'would_pass_shadow_profile': 'True',
                    'shadow_tradeable_if_admitted': 'True',
                },
                {
                    'trading_day': '2026-04-01',
                    'scan_offset_minutes': 120,
                    'symbol': 'BBB',
                    'entry_touched': 'True',
                    'hit_target': 'False',
                    'minutes_to_entry': '12',
                    'minutes_to_target': '',
                    'mover_rank': '14',
                    'intraday_pct_gain': '7.1',
                    'distance_to_entry_pct': '-0.4',
                    'width_retention_ratio': '0.8',
                    'cycle_persistence_ratio': '1.0',
                    'bounce_quality_score': '42',
                    'cycle_durability_score': '28',
                    'profile_name': 'soft_cycle_durability',
                    'would_pass_shadow_profile': 'True',
                    'shadow_tradeable_if_admitted': 'False',
                },
                {
                    'trading_day': '2026-04-01',
                    'scan_offset_minutes': 150,
                    'symbol': 'CCC',
                    'entry_touched': 'False',
                    'hit_target': 'False',
                    'minutes_to_entry': '',
                    'minutes_to_target': '',
                    'mover_rank': '24',
                    'intraday_pct_gain': '6.4',
                    'distance_to_entry_pct': '-1.0',
                    'width_retention_ratio': '0.9',
                    'cycle_persistence_ratio': '1.2',
                    'bounce_quality_score': '38',
                    'cycle_durability_score': '35',
                    'profile_name': 'soft_cycle_durability',
                    'would_pass_shadow_profile': 'True',
                    'shadow_tradeable_if_admitted': 'False',
                },
                {
                    'trading_day': '2026-04-01',
                    'scan_offset_minutes': 150,
                    'symbol': 'DDD',
                    'entry_touched': 'True',
                    'hit_target': 'True',
                    'minutes_to_entry': '7',
                    'minutes_to_target': '14',
                    'mover_rank': '9',
                    'intraday_pct_gain': '9.0',
                    'distance_to_entry_pct': '-0.2',
                    'width_retention_ratio': '0.7',
                    'cycle_persistence_ratio': '1.1',
                    'bounce_quality_score': '45',
                    'cycle_durability_score': '33',
                    'profile_name': 'soft_cycle_durability',
                    'would_pass_shadow_profile': 'True',
                    'shadow_tradeable_if_admitted': 'True',
                },
                {
                    'trading_day': '2026-04-01',
                    'scan_offset_minutes': 150,
                    'symbol': 'EEE',
                    'entry_touched': 'True',
                    'hit_target': 'False',
                    'minutes_to_entry': '22',
                    'minutes_to_target': '',
                    'mover_rank': '31',
                    'intraday_pct_gain': '5.0',
                    'distance_to_entry_pct': '-0.9',
                    'width_retention_ratio': '0.8',
                    'cycle_persistence_ratio': '1.0',
                    'bounce_quality_score': '36',
                    'cycle_durability_score': '29',
                    'profile_name': 'soft_cycle_durability',
                    'would_pass_shadow_profile': 'True',
                    'shadow_tradeable_if_admitted': 'False',
                },
            ]
        ),
    }
    raw = pack_to_zip_bytes(replay_pack)
    monkeypatch.setattr('app.services.replay_bottleneck_pack.get_or_build_historical_replay_shadow_zip', lambda *args, **kwargs: raw)

    pack = build_replay_bottleneck_pack(settings, object(), DummyAlpaca(), lookback_days=90, offsets=[120, 150])
    summary = json.loads(pack['replay_bottleneck_summary.json'])

    assert summary['focus_profile_name'] == 'soft_cycle_durability'
    assert summary['best_offset_by_tradeable_share']['scan_offset_minutes'] == 120
    assert summary['worst_offset_by_tradeable_share']['scan_offset_minutes'] == 150
    assert summary['dominant_failure_path']['failure_path'] in {'entry_never_touched', 'entry_touched_no_target'}
    assert 'recommended_profile_offset_rollup.csv' in pack


def test_replay_bottleneck_route_returns_zip(monkeypatch, tmp_path):
    settings = Settings(database_path=str(tmp_path / 'route.db'), enable_scheduler=False, auth_token='', data_dir=str(tmp_path))
    runtime = AppRuntime(initial_settings=settings, settings_loader=lambda: settings, db_factory=DummyDB, alpaca_factory=lambda s: DummyAlpaca())
    app = create_app(runtime)

    monkeypatch.setattr(
        'app.services.replay_bottleneck_pack.build_replay_bottleneck_pack',
        lambda *args, **kwargs: {
            'MANIFEST.json': json.dumps({'bundle_type': 'replay_bottleneck_pack'}).encode('utf-8'),
            'replay_bottleneck_summary.json': json.dumps({'focus_profile_name': 'soft_cycle_durability'}).encode('utf-8'),
        },
    )

    client = TestClient(app)
    response = client.get('/diagnostics/replay-bottleneck-pack.zip')
    assert response.status_code == 200
    assert response.headers['content-type'] == 'application/zip'
    assert 'replay_bottleneck_pack_last_90_days.zip' in response.headers['content-disposition']

    with zipfile.ZipFile(io.BytesIO(response.content), 'r') as zf:
        assert 'replay_bottleneck_summary.json' in zf.namelist()
