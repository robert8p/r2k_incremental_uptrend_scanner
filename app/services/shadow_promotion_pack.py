from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from io import StringIO
from typing import Any

from app.config import Settings
from app.db import Database
from app.repositories import RepositoryBundle
from app.services.overstrictness_shadow_pack import build_overstrictness_shadow_pack
from app.version import VERSION

UTC = timezone.utc

MIN_CLEAN_DAYS_FOR_TRIAL = 5
MIN_POSSIBLE_OVERSTRICT_ROWS = 8
MIN_PROFILE_CAPTURE_ROWS = 4
MIN_PRECISION_LIKE = 0.70
MAX_FALSE_POSITIVE_RATE = 0.10


def _read_json_bytes(raw: bytes) -> dict[str, Any]:
    if not raw:
        return {}
    return dict(json.loads(raw.decode('utf-8')))


def _read_csv_bytes(raw: bytes) -> list[dict[str, Any]]:
    if not raw:
        return []
    reader = csv.DictReader(StringIO(raw.decode('utf-8')))
    return [dict(row) for row in reader]


def _to_int(value: Any) -> int:
    try:
        if value in (None, ''):
            return 0
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _to_float(value: Any) -> float | None:
    try:
        if value in (None, '', 'None'):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


PROMOTION_RULES = {
    'min_clean_days_for_trial': MIN_CLEAN_DAYS_FOR_TRIAL,
    'min_possible_overstrict_rows': MIN_POSSIBLE_OVERSTRICT_ROWS,
    'min_profile_capture_rows': MIN_PROFILE_CAPTURE_ROWS,
    'min_precision_like_overstrict_share': MIN_PRECISION_LIKE,
    'max_false_positive_rate_on_correct_rejects': MAX_FALSE_POSITIVE_RATE,
}


def _profile_readiness_rows(summary: dict[str, Any], rollup_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    clean_day_count = _to_int(summary.get('clean_day_count'))
    verdict_counts = dict(summary.get('verdict_counts') or {})
    possible_total = _to_int(verdict_counts.get('possible_classifier_overstrict'))
    correct_total = _to_int(verdict_counts.get('classifier_correct_reject'))

    rows: list[dict[str, Any]] = []
    for row in rollup_rows:
        flagged_total = _to_int(row.get('flagged_total'))
        flagged_overstrict = _to_int(row.get('flagged_possible_classifier_overstrict'))
        flagged_correct = _to_int(row.get('flagged_classifier_correct_reject'))
        precision_like = _to_float(row.get('precision_like_overstrict_share'))
        false_positive_rate = _to_float(row.get('false_positive_rate_on_correct_rejects'))
        capture_rate = _to_float(row.get('capture_rate_of_possible_overstrict'))

        promising_profile = (
            flagged_overstrict >= MIN_PROFILE_CAPTURE_ROWS
            and (precision_like is not None and precision_like >= MIN_PRECISION_LIKE)
            and (false_positive_rate is not None and false_positive_rate <= MAX_FALSE_POSITIVE_RATE)
        )
        evidence_ready = clean_day_count >= MIN_CLEAN_DAYS_FOR_TRIAL and possible_total >= MIN_POSSIBLE_OVERSTRICT_ROWS
        if evidence_ready and promising_profile:
            verdict = 'eligible_for_narrow_live_trial'
        elif promising_profile:
            verdict = 'shadow_profile_promising_but_early'
        elif clean_day_count < MIN_CLEAN_DAYS_FOR_TRIAL:
            verdict = 'insufficient_clean_days'
        elif possible_total < MIN_POSSIBLE_OVERSTRICT_ROWS:
            verdict = 'insufficient_possible_overstrict_rows'
        else:
            verdict = 'not_promotion_ready'

        rows.append(
            {
                'profile_name': row.get('profile_name'),
                'clean_day_count': clean_day_count,
                'possible_classifier_overstrict_total': possible_total,
                'classifier_correct_reject_total': correct_total,
                'flagged_total': flagged_total,
                'flagged_possible_classifier_overstrict': flagged_overstrict,
                'flagged_classifier_correct_reject': flagged_correct,
                'precision_like_overstrict_share': precision_like,
                'capture_rate_of_possible_overstrict': capture_rate,
                'false_positive_rate_on_correct_rejects': false_positive_rate,
                'promotion_readiness_verdict': verdict,
            }
        )
    return rows


def _promotion_decision_summary(summary: dict[str, Any], readiness_rows: list[dict[str, Any]]) -> dict[str, Any]:
    priority = {
        'eligible_for_narrow_live_trial': 4,
        'shadow_profile_promising_but_early': 3,
        'insufficient_possible_overstrict_rows': 2,
        'insufficient_clean_days': 1,
        'not_promotion_ready': 0,
    }
    sorted_rows = sorted(
        readiness_rows,
        key=lambda row: (
            priority.get(str(row.get('promotion_readiness_verdict') or ''), -1),
            _to_int(row.get('flagged_possible_classifier_overstrict')),
            _to_float(row.get('precision_like_overstrict_share')) or -1.0,
            -(_to_float(row.get('false_positive_rate_on_correct_rejects')) or 999.0),
        ),
        reverse=True,
    )
    recommended = sorted_rows[0] if sorted_rows else None
    if not recommended:
        overall = 'insufficient_evidence'
        reason = 'No shadow profile rows were available.'
    else:
        overall = str(recommended.get('promotion_readiness_verdict') or 'insufficient_evidence')
        reason = (
            f"Best profile={recommended.get('profile_name')}, "
            f"flagged_possible_overstrict={recommended.get('flagged_possible_classifier_overstrict')}, "
            f"precision_like={recommended.get('precision_like_overstrict_share')}, "
            f"false_positive_rate={recommended.get('false_positive_rate_on_correct_rejects')}."
        )
    return {
        'generated_at_utc': datetime.now(UTC).isoformat(),
        'app_version': VERSION,
        'promotion_rules': PROMOTION_RULES,
        'source_clean_day_count': _to_int(summary.get('clean_day_count')),
        'source_verdict_counts': dict(summary.get('verdict_counts') or {}),
        'overall_promotion_readiness': overall,
        'overall_reason': reason,
        'recommended_profile': recommended,
        'all_profile_readiness_rows': readiness_rows,
        'decision_rule': (
            'Keep live thresholds unchanged until a shadow profile remains promising across multiple clean days, '
            'maintains separation from correct rejects, and meets the minimum overstrict row count.'
        ),
    }


def build_shadow_promotion_pack(
    settings: Settings,
    db: Database | RepositoryBundle,
    alpaca,
    *,
    days: int = 10,
    offsets: list[int] | None = None,
) -> dict[str, bytes]:
    base_pack = build_overstrictness_shadow_pack(settings, db, alpaca, days=days, offsets=offsets)
    shadow_summary = _read_json_bytes(base_pack.get('overstrictness_shadow_summary.json', b''))
    shadow_rollup = _read_csv_bytes(base_pack.get('shadow_threshold_profile_rollup.csv', b''))
    readiness_rows = _profile_readiness_rows(shadow_summary, shadow_rollup)
    decision_summary = _promotion_decision_summary(shadow_summary, readiness_rows)

    report_lines = [
        '# Shadow promotion readiness',
        '',
        f"Generated at: {decision_summary['generated_at_utc']}",
        f"App version: {VERSION}",
        f"Overall readiness: {decision_summary['overall_promotion_readiness']}",
        decision_summary['overall_reason'],
        '',
        '## Promotion rules',
    ]
    for key, value in PROMOTION_RULES.items():
        report_lines.append(f'- {key}: {value}')
    report_lines.extend(['', '## Profile readiness'])
    for row in readiness_rows:
        report_lines.append(
            f"- {row['profile_name']}: verdict={row['promotion_readiness_verdict']}, "
            f"flagged_overstrict={row['flagged_possible_classifier_overstrict']}, "
            f"flagged_correct={row['flagged_classifier_correct_reject']}, "
            f"precision_like={row['precision_like_overstrict_share']}, "
            f"capture_rate={row['capture_rate_of_possible_overstrict']}, "
            f"false_positive_rate={row['false_positive_rate_on_correct_rejects']}"
        )

    manifest = {
        'bundle_type': 'shadow_promotion_pack',
        'bundle_contract_version': '1.0',
        'app_version': VERSION,
        'generated_at_utc': decision_summary['generated_at_utc'],
        'days_requested': int(days),
        'offsets_requested': list(offsets or [120, 150]),
        'promotion_rules': PROMOTION_RULES,
        'overall_promotion_readiness': decision_summary['overall_promotion_readiness'],
    }

    pack = dict(base_pack)
    pack.update(
        {
            'MANIFEST.json': json.dumps(manifest, indent=2).encode('utf-8'),
            'shadow_promotion_summary.json': json.dumps(decision_summary, indent=2).encode('utf-8'),
            'shadow_promotion_readiness_rows.csv': _csv_bytes(readiness_rows),
            'report.md': '\n'.join(report_lines).encode('utf-8'),
        }
    )
    return pack


def _csv_bytes(rows: list[dict[str, Any]]) -> bytes:
    if not rows:
        return b''
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode('utf-8')
