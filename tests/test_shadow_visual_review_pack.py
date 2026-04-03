from __future__ import annotations

import csv
import json
from io import StringIO
from types import SimpleNamespace

import pandas as pd

from app.config import Settings
from app.services.shadow_visual_review_pack import _visual_verdict, build_shadow_visual_review_pack


def _csv_bytes(rows: list[dict]) -> bytes:
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode('utf-8')


def test_visual_verdict_marks_range_like_when_tradeable_and_two_sided():
    postscan = pd.DataFrame(
        {
            'close': [10.0, 9.8, 10.15, 9.95, 10.25, 10.05],
            'high': [10.1, 9.9, 10.2, 10.0, 10.3, 10.1],
            'low': [9.9, 9.7, 10.0, 9.9, 10.15, 9.95],
        }
    )
    row = {
        'entry_touched': True,
        'hit_target': True,
        'intrabar_target_reached': True,
        'distance_to_entry_pct': 0.1,
    }
    candidate = {
        'entry_low': 9.95,
        'entry_high': 10.05,
        'metrics': {'range_band_low': 9.8, 'range_band_high': 10.3},
        'chart_context': {},
    }

    verdict, reason, features = _visual_verdict(row, postscan_bars=postscan, candidate=candidate)
    assert verdict == 'visually_supportive_range'
    assert features['postscan_direction_changes'] >= 2
    assert 'two-sided' in reason


def test_build_shadow_visual_review_pack_emits_chart_and_summary(monkeypatch):
    promotion_summary = {
        'overall_promotion_readiness': 'shadow_profile_promising_but_early',
        'recommended_profile': {'profile_name': 'soft_bounce_quality'},
    }
    shadow_rows = [
        {
            'trading_day': '2026-04-02',
            'scan_offset_minutes': 120,
            'symbol': 'AIRS',
            'company_name': 'AIRSCULPT TECHNOLOGIES INC',
            'verdict_bucket': 'possible_classifier_overstrict',
            'verdict_reason': 'Looks tradeable.',
            'entry_touched': True,
            'hit_target': True,
            'intrabar_target_reached': True,
            'distance_to_entry_pct': 0.088,
            'range_current_location': 0.0,
            'total_score': 23.99,
        }
    ]
    profile_rows = [
        {
            'profile_name': 'soft_bounce_quality',
            'trading_day': '2026-04-02',
            'scan_offset_minutes': 120,
            'symbol': 'AIRS',
            'verdict_bucket': 'possible_classifier_overstrict',
            'would_pass_shadow_profile_excluding_classifier_veto': True,
        }
    ]
    intraday_rows = [
        {'trading_day': '2026-04-02', 'symbol': 'AIRS', 'timestamp_utc': '2026-04-02T15:00:00+00:00', 'open': 2.84, 'high': 2.86, 'low': 2.83, 'close': 2.85, 'volume': 1000},
        {'trading_day': '2026-04-02', 'symbol': 'AIRS', 'timestamp_utc': '2026-04-02T15:01:00+00:00', 'open': 2.85, 'high': 2.87, 'low': 2.84, 'close': 2.86, 'volume': 1100},
        {'trading_day': '2026-04-02', 'symbol': 'AIRS', 'timestamp_utc': '2026-04-02T15:31:00+00:00', 'open': 2.86, 'high': 2.88, 'low': 2.85, 'close': 2.87, 'volume': 1200},
        {'trading_day': '2026-04-02', 'symbol': 'AIRS', 'timestamp_utc': '2026-04-02T15:35:00+00:00', 'open': 2.87, 'high': 2.90, 'low': 2.86, 'close': 2.89, 'volume': 1300},
        {'trading_day': '2026-04-02', 'symbol': 'AIRS', 'timestamp_utc': '2026-04-02T15:45:00+00:00', 'open': 2.89, 'high': 3.02, 'low': 2.88, 'close': 3.01, 'volume': 1400},
    ]

    def fake_shadow_promotion_pack(*args, **kwargs):
        return {
            'shadow_promotion_summary.json': json.dumps(promotion_summary).encode('utf-8'),
            'overstrictness_shadow_rows.csv': _csv_bytes(shadow_rows),
            'shadow_threshold_profile_rows.csv': _csv_bytes(profile_rows),
            'overstrictness_intraday_bars.csv': _csv_bytes(intraday_rows),
        }

    candidate_lookup = {
        ('2026-04-02', 120, 'AIRS'): {
            'symbol': 'AIRS',
            'entry_low': 2.8537,
            'entry_high': 2.8674,
            'target_price': 2.8892,
            'stop_price': 2.8413,
            'metrics': {'range_band_low': 2.8513, 'range_band_high': 2.90},
            'chart_context': {'band_low': 2.8513, 'band_high': 2.90},
        }
    }

    monkeypatch.setattr('app.services.shadow_visual_review_pack.build_shadow_promotion_pack', fake_shadow_promotion_pack)
    monkeypatch.setattr('app.services.shadow_visual_review_pack.ensure_repository_bundle', lambda db: SimpleNamespace(db=None))
    monkeypatch.setattr('app.services.shadow_visual_review_pack._candidate_lookup_from_recent_scans', lambda repos, days, offsets: candidate_lookup)
    monkeypatch.setattr('app.services.shadow_visual_review_pack.build_contract_health', lambda db: {'ok': True})

    pack = build_shadow_visual_review_pack(Settings(), None, None, days=10, offsets=[120, 150], review_limit=5)
    summary = json.loads(pack['shadow_visual_review_summary.json'])
    rows_csv = pack['shadow_visual_review_rows.csv'].decode('utf-8')

    assert summary['best_profile_name'] == 'soft_bounce_quality'
    assert summary['selected_review_count'] == 1
    assert 'AIRS' in rows_csv
    chart_files = [name for name in pack if name.startswith('charts/') and name.endswith('.svg')]
    assert chart_files
    assert 'shadow_visual_review.html' in pack
