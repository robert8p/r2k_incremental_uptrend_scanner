from __future__ import annotations

import csv
import json
from io import StringIO

from app.config import Settings
from app.services.shadow_promotion_pack import _profile_readiness_rows, build_shadow_promotion_pack


def _csv_bytes(rows: list[dict]) -> bytes:
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode('utf-8')


def test_profile_readiness_marks_promising_profile_as_early_when_clean_days_insufficient():
    summary = {
        'clean_day_count': 1,
        'verdict_counts': {
            'possible_classifier_overstrict': 8,
            'classifier_correct_reject': 4,
        },
    }
    rollup_rows = [
        {
            'profile_name': 'soft_bounce_quality',
            'flagged_total': 4,
            'flagged_possible_classifier_overstrict': 4,
            'flagged_classifier_correct_reject': 0,
            'precision_like_overstrict_share': 1.0,
            'capture_rate_of_possible_overstrict': 0.5,
            'false_positive_rate_on_correct_rejects': 0.0,
        }
    ]
    rows = _profile_readiness_rows(summary, rollup_rows)
    assert rows[0]['promotion_readiness_verdict'] == 'shadow_profile_promising_but_early'


def test_build_shadow_promotion_pack_emits_summary_and_rows(monkeypatch):
    summary = {
        'clean_day_count': 1,
        'verdict_counts': {
            'possible_classifier_overstrict': 8,
            'classifier_correct_reject': 4,
        },
    }
    rollup_rows = [
        {
            'profile_name': 'soft_bounce_quality',
            'flagged_total': 4,
            'flagged_possible_classifier_overstrict': 4,
            'flagged_classifier_correct_reject': 0,
            'precision_like_overstrict_share': 1.0,
            'capture_rate_of_possible_overstrict': 0.5,
            'false_positive_rate_on_correct_rejects': 0.0,
        },
        {
            'profile_name': 'combined_soft_structure',
            'flagged_total': 4,
            'flagged_possible_classifier_overstrict': 4,
            'flagged_classifier_correct_reject': 0,
            'precision_like_overstrict_share': 1.0,
            'capture_rate_of_possible_overstrict': 0.5,
            'false_positive_rate_on_correct_rejects': 0.0,
        },
    ]

    def fake_base_pack(*args, **kwargs):
        return {
            'MANIFEST.json': b'{}',
            'overstrictness_shadow_summary.json': json.dumps(summary).encode('utf-8'),
            'shadow_threshold_profile_rollup.csv': _csv_bytes(rollup_rows),
            'report.md': b'old report',
        }

    monkeypatch.setattr('app.services.shadow_promotion_pack.build_overstrictness_shadow_pack', fake_base_pack)

    pack = build_shadow_promotion_pack(Settings(), None, None, days=10, offsets=[120, 150])
    summary_json = json.loads(pack['shadow_promotion_summary.json'])
    rows_csv = pack['shadow_promotion_readiness_rows.csv'].decode('utf-8')

    assert summary_json['overall_promotion_readiness'] == 'shadow_profile_promising_but_early'
    assert summary_json['recommended_profile']['profile_name'] in {'soft_bounce_quality', 'combined_soft_structure'}
    assert 'shadow_profile_promising_but_early' in rows_csv
