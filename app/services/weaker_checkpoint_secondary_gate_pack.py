
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from typing import Any

from app.config import Settings
from app.db import Database
from app.repositories import RepositoryBundle
from app.services.decision_bundle import get_or_build_decision_state
from app.services.evidence_pack import _rows_to_csv
from app.services.historical_replay_shadow_pack import (
    DEFAULT_REPLAY_LOOKBACK_DAYS,
    DEFAULT_REPLAY_OFFSETS,
    get_or_build_historical_replay_shadow_zip,
)
from app.services.replay_bottleneck_pack import (
    TRADEABLE_SHARE_SUPPORT_THRESHOLD,
    _read_json_bytes,
    _read_replay_pack,
    _to_float,
    _to_int,
)
from app.services.replay_checkpoint_decay_pack import build_replay_checkpoint_decay_pack
from app.services.replay_checkpoint_compatibility_pack import (
    SCAN_TIME_METRIC_SPECS,
    _best_gate,
    _enrich_focus_rows,
    _metric_gate_candidates,
    _partition_rows,
)
from app.services.weaker_checkpoint_gate_shadow_pack import build_weaker_checkpoint_gate_shadow_pack, _gate_pass
from app.version import VERSION

UTC = timezone.utc


def _csv_rows(raw: bytes) -> list[dict[str, Any]]:
    if not raw:
        return []
    text = raw.decode('utf-8').strip()
    if not text:
        return []
    return list(csv.DictReader(io.StringIO(text)))


def _current_primary_gate(decision_state: dict[str, Any]) -> dict[str, Any]:
    compatibility = dict(decision_state.get('historical_replay_checkpoint_compatibility') or {})
    return dict(compatibility.get('best_gate_weaker_offset_only') or {}) or dict(compatibility.get('best_gate_weaker_offset_total') or {})


def _apply_primary_gate(rows: list[dict[str, Any]], metric_name: str, comparator: str, threshold: Any) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in rows:
        value = row.get(metric_name)
        if value is None:
            value = row.get('gate_metric_value') if str(row.get('gate_metric_name') or '') == metric_name else None
        if _gate_pass(value, comparator, threshold):
            output.append(dict(row))
    return output


def _secondary_candidates(rows: list[dict[str, Any]], scope_name: str, primary_metric_name: str) -> list[dict[str, Any]]:
    candidates = _metric_gate_candidates(rows, scope_name)
    filtered: list[dict[str, Any]] = []
    for row in candidates:
        if str(row.get('metric_name') or '') == str(primary_metric_name or ''):
            continue
        filtered.append(dict(row))
    return filtered


def _current_overlay_rows(rows: list[dict[str, Any]], secondary_gate: dict[str, Any] | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not secondary_gate:
        return [], list(rows)
    metric_name = str(secondary_gate.get('metric_name') or '')
    comparator = str(secondary_gate.get('comparator') or '>=')
    threshold = secondary_gate.get('threshold_value')
    passed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for row in rows:
        value = row.get(metric_name)
        ok = _gate_pass(value, comparator, threshold)
        (passed if ok else failed).append(dict(row))
    return passed, failed


def build_weaker_checkpoint_secondary_gate_pack(
    settings: Settings,
    db: Database | RepositoryBundle,
    alpaca,
    *,
    lookback_days: int = DEFAULT_REPLAY_LOOKBACK_DAYS,
    offsets: list[int] | None = None,
) -> dict[str, bytes]:
    requested_offsets = list(offsets or DEFAULT_REPLAY_OFFSETS)
    decision_state = get_or_build_decision_state(settings, db, alpaca, days=60, offsets=requested_offsets)
    primary_gate = _current_primary_gate(decision_state)
    primary_metric_name = str(primary_gate.get('metric_name') or '')
    primary_comparator = str(primary_gate.get('comparator') or '>=')
    primary_threshold = primary_gate.get('threshold_value')

    raw_replay_zip = get_or_build_historical_replay_shadow_zip(
        settings,
        db,
        alpaca,
        lookback_days=lookback_days,
        offsets=requested_offsets,
    )
    replay_summary, replay_rows = _read_replay_pack(raw_replay_zip)
    decay_pack = build_replay_checkpoint_decay_pack(settings, db, alpaca, lookback_days=lookback_days, offsets=requested_offsets)
    decay_summary = _read_json_bytes(decay_pack.get('replay_checkpoint_decay_summary.json', b''))

    focus_profile_name = str(
        (decision_state.get('historical_replay_checkpoint_compatibility') or {}).get('focus_profile_name')
        or ((decision_state.get('historical_replay_shadow') or {}).get('recommended_profile') or {}).get('profile_name')
        or (replay_summary.get('recommended_profile') or {}).get('profile_name')
        or ''
    )
    supported_offset = _to_int((decision_state.get('historical_replay_checkpoint_compatibility') or {}).get('supported_offset_minutes') or decay_summary.get('supported_offset_minutes'))
    weaker_offset = _to_int((decision_state.get('historical_replay_checkpoint_compatibility') or {}).get('weaker_offset_minutes') or decay_summary.get('weaker_offset_minutes'))

    focus_rows = _enrich_focus_rows(replay_rows, focus_profile_name)
    _, weaker_all, _, weaker_only = _partition_rows(focus_rows, supported_offset, weaker_offset)
    weaker_all_primary = _apply_primary_gate(weaker_all, primary_metric_name, primary_comparator, primary_threshold)
    weaker_only_primary = _apply_primary_gate(weaker_only, primary_metric_name, primary_comparator, primary_threshold)

    secondary_total_candidates = _secondary_candidates(weaker_all_primary, 'weaker_offset_total_primary_gate_pass', primary_metric_name)
    secondary_weaker_only_candidates = _secondary_candidates(weaker_only_primary, 'weaker_offset_only_primary_gate_pass', primary_metric_name)
    best_secondary_total = _best_gate(secondary_total_candidates)
    best_secondary_weaker_only = _best_gate(secondary_weaker_only_candidates)

    current_shadow_pack = build_weaker_checkpoint_gate_shadow_pack(settings, db, alpaca, offsets=requested_offsets)
    current_shadow_summary = _read_json_bytes(current_shadow_pack.get('weaker_checkpoint_gate_shadow_summary.json', b''))
    current_primary_rows = _csv_rows(current_shadow_pack.get('weaker_checkpoint_gate_pass_candidates.csv', b''))
    current_best_secondary = dict(best_secondary_weaker_only or best_secondary_total or {})
    current_secondary_pass, current_secondary_fail = _current_overlay_rows(current_primary_rows, current_best_secondary or None)
    current_secondary_regressed = [row for row in current_secondary_pass if str(row.get('regressed_from_supported_checkpoint') or '').lower() == 'true']
    current_secondary_advanced = [row for row in current_secondary_pass if str(row.get('advanced_to_stage2') or '').lower() == 'true']

    summary = {
        'generated_at_utc': datetime.now(UTC).isoformat(),
        'app_version': VERSION,
        'bundle_type': 'weaker_checkpoint_secondary_gate_pack',
        'decision_recommendation_code': decision_state.get('decision_recommendation_code'),
        'source_replay_generated_at_utc': replay_summary.get('generated_at_utc'),
        'source_replay_app_version': replay_summary.get('app_version'),
        'source_decay_generated_at_utc': decay_summary.get('generated_at_utc'),
        'source_decay_app_version': decay_summary.get('app_version'),
        'source_current_shadow_generated_at_utc': current_shadow_summary.get('generated_at_utc'),
        'source_current_shadow_app_version': current_shadow_summary.get('app_version'),
        'lookback_days_requested': lookback_days,
        'lookback_days_effective': replay_summary.get('lookback_days_effective'),
        'focus_profile_name': focus_profile_name,
        'supported_offset_minutes': supported_offset,
        'weaker_offset_minutes': weaker_offset,
        'primary_gate_metric_name': primary_metric_name,
        'primary_gate_comparator': primary_comparator,
        'primary_gate_threshold_value': primary_threshold,
        'primary_gate_tradeable_share_from_replay': primary_gate.get('tradeable_share'),
        'tradeable_share_support_threshold': TRADEABLE_SHARE_SUPPORT_THRESHOLD,
        'replay_weaker_offset_primary_gate_pass_count': len(weaker_all_primary),
        'replay_weaker_only_primary_gate_pass_count': len(weaker_only_primary),
        'replay_secondary_gate_metric_count_evaluated': len([metric for metric, _ in SCAN_TIME_METRIC_SPECS if metric != primary_metric_name]) + 2,
        'best_secondary_gate_weaker_offset_total_primary_pass': best_secondary_total,
        'best_secondary_gate_weaker_offset_only_primary_pass': best_secondary_weaker_only,
        'current_primary_gate_pass_count': len(current_primary_rows),
        'current_best_secondary_gate': current_best_secondary,
        'current_secondary_gate_pass_count': len(current_secondary_pass),
        'current_secondary_gate_fail_count': len(current_secondary_fail),
        'current_secondary_gate_pass_advanced_to_stage2_count': len(current_secondary_advanced),
        'current_secondary_gate_pass_regressed_from_supported_count': len(current_secondary_regressed),
        'currently_valid_now_count': current_shadow_summary.get('currently_valid_now_count'),
        'surface_message': (
            f"Primary weaker-checkpoint gate {primary_metric_name} {primary_comparator} {primary_threshold} still leaves all current candidates rejected. "
            f"Use the best secondary scan-time gate in shadow before touching live behavior."
        ),
    }

    report_lines = [
        '# Weaker checkpoint secondary gate pack',
        '',
        'Purpose: scan for a narrower secondary scan-time gate inside the current weaker-checkpoint primary-gate pass subset, without changing live behavior.',
        '',
        f"Generated at: {summary['generated_at_utc']}",
        f"App version: {VERSION}",
        f"Decision recommendation: {summary.get('decision_recommendation_code')}",
        f"Focus profile: {focus_profile_name or 'none'}",
        f"Supported offset: {supported_offset}",
        f"Weaker offset: {weaker_offset}",
        f"Primary gate: {primary_metric_name} {primary_comparator} {primary_threshold} (replay tradeable share {primary_gate.get('tradeable_share')})",
        '',
        '## Current live-shaped shadow overlay',
        f"- Current primary-gate pass candidates: {len(current_primary_rows)}",
        f"- Current best-secondary pass candidates: {len(current_secondary_pass)}",
        f"- Current best-secondary pass advanced to stage 2: {len(current_secondary_advanced)}",
        f"- Current best-secondary regressed-from-supported retained: {len(current_secondary_regressed)}",
        '',
        '## Interpretation',
        '- This pack answers the next V2 question automatically: is there a narrower scan-time-only subset inside the weaker-checkpoint gate-pass population worth shadow testing next?',
        '- Do not change live thresholds from this pack alone.',
        '- A good secondary gate trims current weaker-checkpoint clutter materially while still retaining any supported-regressed names worth watching and improving replay tradeable share.',
    ]

    manifest = {
        'bundle_type': 'weaker_checkpoint_secondary_gate_pack',
        'generated_at_utc': summary['generated_at_utc'],
        'app_version': VERSION,
        'files': [
            'MANIFEST.json',
            'replay_secondary_gate_candidates_total_primary_pass.csv',
            'replay_secondary_gate_candidates_weaker_only_primary_pass.csv',
            'current_primary_gate_pass_candidates.csv',
            'current_secondary_gate_pass_candidates.csv',
            'current_secondary_gate_fail_candidates.csv',
            'current_secondary_gate_regressed_from_supported.csv',
            'weaker_checkpoint_secondary_gate_summary.json',
            'report.md',
        ],
    }

    return {
        'MANIFEST.json': json.dumps(manifest, indent=2).encode('utf-8'),
        'replay_secondary_gate_candidates_total_primary_pass.csv': _rows_to_csv(secondary_total_candidates),
        'replay_secondary_gate_candidates_weaker_only_primary_pass.csv': _rows_to_csv(secondary_weaker_only_candidates),
        'current_primary_gate_pass_candidates.csv': _rows_to_csv(current_primary_rows),
        'current_secondary_gate_pass_candidates.csv': _rows_to_csv(current_secondary_pass),
        'current_secondary_gate_fail_candidates.csv': _rows_to_csv(current_secondary_fail),
        'current_secondary_gate_regressed_from_supported.csv': _rows_to_csv(current_secondary_regressed),
        'weaker_checkpoint_secondary_gate_summary.json': json.dumps(summary, indent=2).encode('utf-8'),
        'report.md': ('\n'.join(report_lines).strip() + '\n').encode('utf-8'),
    }
