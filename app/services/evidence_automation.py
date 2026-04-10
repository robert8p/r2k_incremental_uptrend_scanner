from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from app.config import Settings
from app.db import Database
from app.repositories import RepositoryBundle, ensure_repository_bundle
from app.services.diagnostics import build_contract_health
from app.services.evidence_pack import pack_to_zip_bytes
from app.version import VERSION

UTC = timezone.utc
DEFAULT_EVIDENCE_DAYS = 60
DEFAULT_REVIEW_DAYS = 10
DEFAULT_LOOKBACK_DAYS = 90
DEFAULT_OFFSETS = [120, 150]
_CACHE_DIR_NAME = 'evidence_automation'
_CACHE_ZIP_NAME = 'evidence_automation_latest.zip'
_CACHE_SUMMARY_NAME = 'evidence_automation_latest.json'
_DELTA_SUMMARY_NAME = 'evidence_delta_latest.json'
_SMOKE_SUMMARY_NAME = 'evidence_smoke_latest.json'
_FIDELITY_SUMMARY_NAME = 'replay_live_fidelity_latest.json'
_FIVE_SESSION_ZIP_NAME = 'five_session_evidence_review_latest.zip'
_FIVE_SESSION_SUMMARY_NAME = 'five_session_evidence_review_latest.json'
_ARTIFACTS_DIR_NAME = 'artifacts'
_HISTORY_DIR_NAME = 'history'
_HISTORY_KEEP_COUNT = 10


REQUIRED_DIAGNOSTIC_ROUTES = [
    '/diagnostics/historical-replay-shadow-pack.zip',
    '/diagnostics/replay-bottleneck-pack.zip',
    '/diagnostics/replay-checkpoint-compatibility-pack.zip',
    '/diagnostics/decision-bundle.zip',
    '/diagnostics/surfaced-checkpoint-visual-review-pack.zip',
    '/diagnostics/surfaced-multisession-visual-review-pack.zip',
    '/diagnostics/evidence-automation-pack.zip',
    '/diagnostics/five-session-evidence-review-pack.zip',
]


ARTIFACT_FILENAMES = {
    'historical_replay_shadow_pack': 'historical_replay_shadow_pack_latest.zip',
    'replay_bottleneck_pack': 'replay_bottleneck_pack_latest.zip',
    'replay_checkpoint_compatibility_pack': 'replay_checkpoint_compatibility_pack_latest.zip',
    'decision_bundle': 'decision_bundle_latest.zip',
    'surfaced_checkpoint_visual_review_pack': 'surfaced_checkpoint_visual_review_pack_latest.zip',
    'surfaced_multisession_visual_review_pack': 'surfaced_multisession_visual_review_pack_latest.zip',
}

WEEKLY_REVIEW_PACK_NAME = 'five_session_evidence_review_latest.zip'


KEY_DELTA_FIELDS = (
    'decision_recommendation_code',
    'clean_day_count',
    'currently_valid_now_count',
    'replay_supported_profile_name',
    'replay_live_fidelity_verdict',
    'smoke_overall_status',
)



def _to_int(value: Any) -> int:
    try:
        if value in (None, '', 'None'):
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



def _read_json_bytes(raw: bytes) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        return dict(json.loads(raw.decode('utf-8')))
    except Exception:
        return {}



def _cache_dir(settings: Settings) -> Path:
    path = Path(settings.data_dir) / _CACHE_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path



def _cache_zip_path(settings: Settings) -> Path:
    return _cache_dir(settings) / _CACHE_ZIP_NAME



def _cache_summary_path(settings: Settings) -> Path:
    return _cache_dir(settings) / _CACHE_SUMMARY_NAME



def _delta_summary_path(settings: Settings) -> Path:
    return _cache_dir(settings) / _DELTA_SUMMARY_NAME



def _smoke_summary_path(settings: Settings) -> Path:
    return _cache_dir(settings) / _SMOKE_SUMMARY_NAME



def _fidelity_summary_path(settings: Settings) -> Path:
    return _cache_dir(settings) / _FIDELITY_SUMMARY_NAME


def _five_session_zip_path(settings: Settings) -> Path:
    return _cache_dir(settings) / _FIVE_SESSION_ZIP_NAME


def _five_session_summary_path(settings: Settings) -> Path:
    return _cache_dir(settings) / _FIVE_SESSION_SUMMARY_NAME


def _history_dir(settings: Settings) -> Path:
    path = _cache_dir(settings) / _HISTORY_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _history_session_dir(settings: Settings, session_key: str) -> Path:
    key = str(session_key or '').strip() or 'unknown_session'
    path = _history_dir(settings) / key
    path.mkdir(parents=True, exist_ok=True)
    return path


def _history_session_summary_path(settings: Settings, session_key: str) -> Path:
    return _history_session_dir(settings, session_key) / 'session_summary.json'


def _list_history_session_keys(settings: Settings) -> list[str]:
    root = _history_dir(settings)
    keys = [path.name for path in root.iterdir() if path.is_dir()]
    return sorted(keys)


def read_cached_five_session_evidence_review_summary(settings: Settings) -> dict[str, Any] | None:
    return _read_json_path(_five_session_summary_path(settings))


def read_cached_five_session_evidence_review_zip(settings: Settings) -> bytes | None:
    path = _five_session_zip_path(settings)
    if not path.exists():
        return None
    try:
        return path.read_bytes()
    except Exception:
        return None


def _prune_history(settings: Settings, keep_count: int = _HISTORY_KEEP_COUNT) -> None:
    keys = _list_history_session_keys(settings)
    if len(keys) <= keep_count:
        return
    for key in keys[:-keep_count]:
        path = _history_dir(settings) / key
        for child in sorted(path.rglob('*'), reverse=True):
            if child.is_file():
                child.unlink(missing_ok=True)
            elif child.is_dir():
                child.rmdir()
        path.rmdir()



def _artifacts_dir(settings: Settings) -> Path:
    path = _cache_dir(settings) / _ARTIFACTS_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path



def _artifact_path(settings: Settings, artifact_key: str) -> Path:
    return _artifacts_dir(settings) / ARTIFACT_FILENAMES[artifact_key]



def _read_json_path(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return dict(json.loads(path.read_text(encoding='utf-8')))
    except Exception:
        return None



def read_cached_evidence_automation_summary(settings: Settings) -> dict[str, Any] | None:
    return _read_json_path(_cache_summary_path(settings))



def read_cached_evidence_delta_summary(settings: Settings) -> dict[str, Any] | None:
    return _read_json_path(_delta_summary_path(settings))



def read_cached_evidence_smoke_summary(settings: Settings) -> dict[str, Any] | None:
    return _read_json_path(_smoke_summary_path(settings))



def read_cached_replay_live_fidelity_summary(settings: Settings) -> dict[str, Any] | None:
    return _read_json_path(_fidelity_summary_path(settings))



def read_cached_evidence_automation_zip(settings: Settings) -> bytes | None:
    path = _cache_zip_path(settings)
    if not path.exists():
        return None
    try:
        return path.read_bytes()
    except Exception:
        return None



def _write_json(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    path.write_text(json.dumps(payload, indent=2), encoding='utf-8')
    return payload



def _safe_pack_to_zip(pack: dict[str, bytes]) -> bytes:
    return pack_to_zip_bytes(pack or {'MANIFEST.json': json.dumps({'app_version': VERSION, 'generated_at_utc': datetime.now(UTC).isoformat()}, indent=2).encode('utf-8')})



def _store_artifact(settings: Settings, artifact_key: str, pack: dict[str, bytes]) -> dict[str, Any]:
    zip_bytes = _safe_pack_to_zip(pack)
    path = _artifact_path(settings, artifact_key)
    path.write_bytes(zip_bytes)
    return {
        'artifact_key': artifact_key,
        'filename': ARTIFACT_FILENAMES[artifact_key],
        'path': str(path),
        'size_bytes': len(zip_bytes),
    }


def _build_history_session_summary(
    current_summary: dict[str, Any],
    delta_summary: dict[str, Any],
    smoke_summary: dict[str, Any],
    fidelity_summary: dict[str, Any],
    decision_state: dict[str, Any],
) -> dict[str, Any]:
    checkpoint = dict(decision_state.get('checkpoint_summary') or {})
    surfaced_summary = dict((decision_state.get('surfaced_multisession_visual_review') or {}).get('summary') or {})
    return {
        'generated_at_utc': datetime.now(UTC).isoformat(),
        'app_version': VERSION,
        'session_key': str(current_summary.get('latest_selected_day') or ''),
        'latest_selected_day': current_summary.get('latest_selected_day'),
        'decision_recommendation_code': current_summary.get('decision_recommendation_code'),
        'clean_day_count': current_summary.get('clean_day_count'),
        'currently_valid_now_count': current_summary.get('currently_valid_now_count'),
        'max_stage2_count_any_checkpoint': current_summary.get('max_stage2_count_any_checkpoint'),
        'best_checkpoint_offset_minutes': checkpoint.get('best_checkpoint_offset_minutes'),
        'replay_supported_profile_name': current_summary.get('replay_supported_profile_name'),
        'replay_live_fidelity_verdict': fidelity_summary.get('verdict'),
        'smoke_overall_status': smoke_summary.get('overall_status'),
        'material_change_count': delta_summary.get('material_change_count'),
        'surfaced_multisession_selected_review_count': surfaced_summary.get('selected_review_count'),
        'surfaced_multisession_visual_review_verdict_counts': dict(surfaced_summary.get('visual_review_verdict_counts') or {}),
    }


def _store_history_snapshot(
    settings: Settings,
    *,
    current_summary: dict[str, Any],
    delta_summary: dict[str, Any],
    smoke_summary: dict[str, Any],
    fidelity_summary: dict[str, Any],
    decision_state: dict[str, Any],
    evidence_pack_zip: bytes,
    decision_bundle_zip: bytes | None,
) -> dict[str, Any] | None:
    session_key = str(current_summary.get('latest_selected_day') or '').strip()
    if not session_key:
        return None
    session_dir = _history_session_dir(settings, session_key)
    session_summary = _build_history_session_summary(current_summary, delta_summary, smoke_summary, fidelity_summary, decision_state)
    _write_json(session_dir / 'session_summary.json', session_summary)
    _write_json(session_dir / 'evidence_delta_summary.json', delta_summary)
    _write_json(session_dir / 'evidence_smoke_summary.json', smoke_summary)
    _write_json(session_dir / 'replay_live_fidelity_audit_summary.json', fidelity_summary)
    (session_dir / 'evidence_automation_pack_latest.zip').write_bytes(evidence_pack_zip)
    if decision_bundle_zip:
        (session_dir / 'decision_bundle_latest.zip').write_bytes(decision_bundle_zip)
    _prune_history(settings)
    return session_summary


def _read_history_session_summary(settings: Settings, session_key: str) -> dict[str, Any] | None:
    return _read_json_path(_history_session_summary_path(settings, session_key))


def build_five_session_evidence_review_pack(
    settings: Settings,
    *,
    session_count: int = 5,
) -> dict[str, bytes]:
    keys = _list_history_session_keys(settings)[-max(int(session_count), 1):]
    keys = list(reversed(keys))
    session_summaries: list[dict[str, Any]] = []
    report_lines = [
        '# Five-session evidence review pack',
        '',
        f'Generated at: {datetime.now(UTC).isoformat()}',
        f'App version: {VERSION}',
        f'Sessions included: {len(keys)}',
        '',
        '## Session rollup',
    ]
    pack: dict[str, bytes] = {}
    for key in keys:
        session_dir = _history_session_dir(settings, key)
        summary = _read_history_session_summary(settings, key) or {}
        session_summaries.append(summary)
        report_lines.append(
            f"- {key}: recommendation={summary.get('decision_recommendation_code')}, clean_day_count={summary.get('clean_day_count')}, currently_valid_now_count={summary.get('currently_valid_now_count')}, best_checkpoint_offset_minutes={summary.get('best_checkpoint_offset_minutes')}, fidelity={summary.get('replay_live_fidelity_verdict')}"
        )
        for filename in ('session_summary.json', 'evidence_delta_summary.json', 'evidence_smoke_summary.json', 'replay_live_fidelity_audit_summary.json', 'evidence_automation_pack_latest.zip', 'decision_bundle_latest.zip'):
            path = session_dir / filename
            if path.exists():
                pack[f'sessions/{key}/{filename}'] = path.read_bytes()

    aggregate = {
        'generated_at_utc': datetime.now(UTC).isoformat(),
        'app_version': VERSION,
        'bundle_type': 'five_session_evidence_review_pack',
        'session_count_requested': int(session_count),
        'session_count_included': len(keys),
        'session_keys': keys,
        'latest_session_key': keys[0] if keys else None,
        'earliest_session_key': keys[-1] if keys else None,
        'sessions': session_summaries,
    }
    changes = [s for s in session_summaries if _to_int(s.get('material_change_count')) > 0]
    aggregate['sessions_with_material_changes'] = len(changes)
    report_lines.extend(['', '## Material-change sessions'])
    if changes:
        report_lines.extend([f"- {s.get('session_key')}: {s.get('material_change_count')} material changes" for s in changes])
    else:
        report_lines.append('- None in the included window.')
    pack.update({
        'MANIFEST.json': json.dumps({
            'bundle_type': 'five_session_evidence_review_pack',
            'bundle_contract_version': '1.0',
            'app_version': VERSION,
            'generated_at_utc': aggregate['generated_at_utc'],
            'session_count_included': len(keys),
            'session_keys': keys,
        }, indent=2).encode('utf-8'),
        'five_session_evidence_review_summary.json': json.dumps(aggregate, indent=2).encode('utf-8'),
        'report.md': '\n'.join(report_lines).encode('utf-8'),
    })
    return pack


def get_or_build_five_session_evidence_review_zip(
    settings: Settings,
    *,
    session_count: int = 5,
    prefer_cache: bool = False,
) -> bytes:
    cached = read_cached_five_session_evidence_review_zip(settings) if prefer_cache else None
    if cached:
        return cached
    pack = build_five_session_evidence_review_pack(settings, session_count=session_count)
    zip_bytes = _safe_pack_to_zip(pack)
    _five_session_zip_path(settings).write_bytes(zip_bytes)
    summary = _read_json_bytes(pack.get('five_session_evidence_review_summary.json', b''))
    if summary:
        _write_json(_five_session_summary_path(settings), summary)
    return zip_bytes



def _safe_generate(label: str, builder, *args, **kwargs) -> tuple[dict[str, bytes], dict[str, Any]]:
    try:
        pack = dict(builder(*args, **kwargs) or {})
        return pack, {'ok': True, 'label': label, 'error': None, 'file_count': len(pack)}
    except Exception as exc:  # pragma: no cover - defensive
        return {}, {'ok': False, 'label': label, 'error': f'{type(exc).__name__}: {exc}', 'file_count': 0}



def _route_availability(route_paths: Iterable[str] | None) -> dict[str, bool]:
    paths = set(route_paths or [])
    return {path: (path in paths if paths else None) for path in REQUIRED_DIAGNOSTIC_ROUTES}



def build_evidence_smoke_summary(
    settings: Settings,
    db: Database | RepositoryBundle,
    alpaca,
    *,
    route_paths: Iterable[str] | None = None,
    scheduler_status: dict[str, Any] | None = None,
    generated_artifacts: dict[str, dict[str, Any]] | None = None,
    reason: str = 'on_demand',
) -> dict[str, Any]:
    from app.services.decision_bundle import read_cached_decision_bundle_zip, read_cached_decision_state

    repos = ensure_repository_bundle(db)
    cached_state = read_cached_decision_state(settings) or {}
    cached_zip = read_cached_decision_bundle_zip(settings)
    cache_version = str(cached_state.get('app_version') or '') if cached_state else None
    cache_fresh = cache_version == VERSION and bool(cached_zip)
    route_status = _route_availability(route_paths)
    contract_health = build_contract_health(repos.db)
    data_api_status: dict[str, Any] | None = None
    if hasattr(alpaca, 'ping_data_api'):
        try:
            data_api_status = alpaca.ping_data_api()
        except Exception as exc:  # pragma: no cover - defensive
            data_api_status = {'ok': False, 'message': f'{type(exc).__name__}: {exc}'}
    artifact_status = dict(generated_artifacts or {})
    failed_artifacts = [key for key, payload in artifact_status.items() if not bool(payload.get('ok'))]
    missing_routes = [path for path, available in route_status.items() if available is False]
    issues: list[str] = []
    if not cache_fresh:
        issues.append('Decision-bundle cache is missing, stale, or failed freshness checks for the current app version.')
    if failed_artifacts:
        issues.append(f"Artifact generation failed for: {', '.join(sorted(failed_artifacts))}.")
    if missing_routes:
        issues.append(f"Missing required diagnostics routes: {', '.join(sorted(missing_routes))}.")
    if contract_health.get('ok') is False:
        issues.append('Contract health reported runtime/data issues.')
    if data_api_status is not None and str(data_api_status.get('message') or '').lower() not in {'ok', 'healthy', 'success'} and data_api_status.get('ok') is False:
        issues.append('Data API ping did not report a clean status.')
    if not getattr(alpaca, 'has_credentials', lambda: False)():
        issues.append('Market-data credentials are not present, so fresh evidence generation cannot be trusted on demand.')
    overall_status = 'ok' if not issues else ('warn' if cache_fresh and not failed_artifacts else 'fail')
    return {
        'generated_at_utc': datetime.now(UTC).isoformat(),
        'app_version': VERSION,
        'bundle_type': 'evidence_smoke_validation',
        'reason': reason,
        'overall_status': overall_status,
        'issues': issues,
        'auth_enabled': bool(settings.auth_token),
        'alpaca_credentials_present': bool(getattr(alpaca, 'has_credentials', lambda: False)()),
        'decision_bundle_cache_version': cache_version,
        'decision_bundle_cache_fresh': cache_fresh,
        'route_availability': route_status,
        'generated_artifacts': artifact_status,
        'scheduler_status': dict(scheduler_status or {}),
        'contract_health_ok': bool(contract_health.get('ok')),
        'contract_health': contract_health,
        'data_api_status': data_api_status,
    }



def refresh_evidence_smoke_validation_cache(
    settings: Settings,
    db: Database | RepositoryBundle,
    alpaca,
    *,
    route_paths: Iterable[str] | None = None,
    scheduler_status: dict[str, Any] | None = None,
    generated_artifacts: dict[str, dict[str, Any]] | None = None,
    reason: str = 'startup',
) -> dict[str, Any]:
    summary = build_evidence_smoke_summary(
        settings,
        db,
        alpaca,
        route_paths=route_paths,
        scheduler_status=scheduler_status,
        generated_artifacts=generated_artifacts,
        reason=reason,
    )
    return _write_json(_smoke_summary_path(settings), summary)



def build_replay_live_fidelity_audit(
    *,
    replay_summary: dict[str, Any],
    replay_bottleneck_summary: dict[str, Any],
    surfaced_checkpoint_summary: dict[str, Any],
    surfaced_multisession_summary: dict[str, Any],
    decision_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    replay_profile = str(((replay_summary.get('recommended_profile') or {}) or {}).get('profile_name') or '') or None
    replay_best_offset = _to_int(((replay_bottleneck_summary.get('best_offset_by_tradeable_share') or {}) or {}).get('scan_offset_minutes')) or None
    replay_best_share = _to_float(((replay_bottleneck_summary.get('best_offset_by_tradeable_share') or {}) or {}).get('tradeable_share'))
    surfaced_focus_offset = _to_int(surfaced_multisession_summary.get('focus_offset_minutes') or surfaced_checkpoint_summary.get('focus_offset_minutes') or 0) or None
    surfaced_selected = _to_int(surfaced_multisession_summary.get('selected_review_count'))
    surfaced_total_stage2 = _to_int(surfaced_multisession_summary.get('stage2_candidates_considered_total'))
    surfaced_verdicts = dict(surfaced_multisession_summary.get('visual_review_verdict_counts') or {})
    surfaced_supportive = _to_int(surfaced_verdicts.get('visually_supportive_range')) + _to_int(surfaced_verdicts.get('clean_range_cycler'))
    surfaced_non_supportive = surfaced_selected - surfaced_supportive
    decision_payload = dict(decision_state or {})
    current_valid_now_count = _to_int((decision_payload.get('checkpoint_summary') or {}).get('currently_valid_now_count') or decision_payload.get('currently_valid_now_count'))

    if replay_best_offset is None:
        verdict = 'replay_focus_unavailable'
        reason = 'No replay-supported checkpoint split was available to compare with the surfaced live-shaped path.'
    elif surfaced_focus_offset is not None and replay_best_offset != surfaced_focus_offset:
        verdict = 'replay_and_surfaced_focus_mismatch'
        reason = f'Replay focus offset {replay_best_offset} did not match the surfaced focus offset {surfaced_focus_offset}.'
    elif surfaced_selected <= 0 and surfaced_total_stage2 <= 0:
        verdict = 'insufficient_surfaced_examples'
        reason = 'There were no surfaced stage-2 examples to compare against replay on the actual surfaced path.'
    elif surfaced_selected > 0 and surfaced_supportive <= 0 and (replay_best_share or 0.0) >= 0.50:
        verdict = 'replay_materially_more_optimistic_than_surfaced_path'
        reason = 'Replay still clears the support bar at the best checkpoint, but the surfaced live-shaped path has not produced thesis-supportive reviewed names.'
    elif surfaced_supportive > 0:
        verdict = 'replay_and_surfaced_path_directionally_aligned'
        reason = 'Replay and the surfaced live-shaped path are both producing thesis-supportive names at the focus checkpoint.'
    else:
        verdict = 'mixed_or_thin_alignment'
        reason = 'Replay and surfaced evidence are not cleanly aligned yet, but surfaced examples remain too thin or mixed to support a stronger conclusion.'

    return {
        'generated_at_utc': datetime.now(UTC).isoformat(),
        'app_version': VERSION,
        'bundle_type': 'replay_live_fidelity_audit',
        'focus_profile_name': replay_profile,
        'focus_offset_minutes': replay_best_offset,
        'replay_focus_tradeable_share': replay_best_share,
        'surfaced_focus_offset_minutes': surfaced_focus_offset,
        'surfaced_selected_review_count': surfaced_selected,
        'surfaced_stage2_candidates_considered_total': surfaced_total_stage2,
        'surfaced_supportive_range_count': surfaced_supportive,
        'surfaced_non_supportive_count': surfaced_non_supportive,
        'currently_valid_now_count': current_valid_now_count,
        'verdict': verdict,
        'reason': reason,
    }



def _build_material_delta(previous: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
    previous = dict(previous or {})
    changes: list[str] = []
    field_changes: dict[str, dict[str, Any]] = {}
    for field in KEY_DELTA_FIELDS:
        old = previous.get(field)
        new = current.get(field)
        if old != new:
            field_changes[field] = {'previous': old, 'current': new}
            changes.append(f'{field} changed from {old!r} to {new!r}.')

    for field in ('surfaced_multisession_visual_review_verdict_counts', 'replay_supported_visual_review_verdict_counts'):
        old = dict(previous.get(field) or {})
        new = dict(current.get(field) or {})
        if old != new:
            field_changes[field] = {'previous': old, 'current': new}
            changes.append(f'{field} changed from {old} to {new}.')

    smoke_prev = str(previous.get('smoke_overall_status') or '')
    smoke_curr = str(current.get('smoke_overall_status') or '')
    if smoke_prev != smoke_curr and smoke_prev:
        if smoke_prev == 'ok' and smoke_curr != 'ok':
            changes.append(f'Smoke validation degraded from {smoke_prev} to {smoke_curr}.')
        elif smoke_prev != 'ok' and smoke_curr == 'ok':
            changes.append('Smoke validation recovered to ok.')

    if not changes:
        changes.append('No material evidence changes since the last automation run.')
    return {
        'generated_at_utc': datetime.now(UTC).isoformat(),
        'app_version': VERSION,
        'bundle_type': 'evidence_delta_summary',
        'material_change_count': 0 if changes == ['No material evidence changes since the last automation run.'] else len(changes),
        'changes': changes,
        'field_changes': field_changes,
    }



def _render_delta_text(delta: dict[str, Any]) -> str:
    lines = [
        'Evidence delta summary',
        f"Generated at UTC: {delta.get('generated_at_utc')}",
        f"App version: {delta.get('app_version')}",
        '',
    ]
    lines.extend([f'- {item}' for item in (delta.get('changes') or [])])
    lines.append('')
    return '\n'.join(lines)



def _build_current_summary(
    decision_state: dict[str, Any],
    smoke_summary: dict[str, Any],
    fidelity_summary: dict[str, Any],
) -> dict[str, Any]:
    checkpoint = dict(decision_state.get('checkpoint_summary') or {})
    replay_summary = dict(decision_state.get('historical_replay_shadow') or {})
    replay_supported_summary = dict((decision_state.get('replay_supported_visual_review') or {}).get('summary') or {})
    surfaced_multisession_summary = dict((decision_state.get('surfaced_multisession_visual_review') or {}).get('summary') or {})
    return {
        'generated_at_utc': datetime.now(UTC).isoformat(),
        'app_version': VERSION,
        'bundle_type': 'evidence_automation_summary',
        'latest_selected_day': decision_state.get('latest_selected_day'),
        'decision_recommendation_code': decision_state.get('decision_recommendation_code'),
        'clean_day_count': _to_int(decision_state.get('clean_day_count')),
        'currently_valid_now_count': _to_int(checkpoint.get('currently_valid_now_count') or decision_state.get('currently_valid_now_count')),
        'max_stage2_count_any_checkpoint': _to_int(decision_state.get('max_stage2_count_any_checkpoint')),
        'replay_supported_profile_name': str(((replay_summary.get('recommended_profile') or {}) or {}).get('profile_name') or '') or None,
        'replay_supported_visual_review_verdict_counts': dict(replay_supported_summary.get('visual_review_verdict_counts') or {}),
        'surfaced_multisession_visual_review_verdict_counts': dict(surfaced_multisession_summary.get('visual_review_verdict_counts') or {}),
        'replay_live_fidelity_verdict': fidelity_summary.get('verdict'),
        'smoke_overall_status': smoke_summary.get('overall_status'),
    }



def write_evidence_automation_cache(
    settings: Settings,
    pack: dict[str, bytes],
    summary: dict[str, Any],
    delta: dict[str, Any],
    smoke_summary: dict[str, Any],
    fidelity_summary: dict[str, Any],
) -> tuple[dict[str, Any], bytes]:
    zip_bytes = _safe_pack_to_zip(pack)
    _cache_zip_path(settings).write_bytes(zip_bytes)
    _write_json(_cache_summary_path(settings), summary)
    _write_json(_delta_summary_path(settings), delta)
    _write_json(_smoke_summary_path(settings), smoke_summary)
    _write_json(_fidelity_summary_path(settings), fidelity_summary)
    return summary, zip_bytes



def build_evidence_automation_pack(
    settings: Settings,
    db: Database | RepositoryBundle,
    alpaca,
    *,
    days: int = DEFAULT_EVIDENCE_DAYS,
    offsets: list[int] | None = None,
    review_days: int = DEFAULT_REVIEW_DAYS,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    route_paths: Iterable[str] | None = None,
    scheduler_status: dict[str, Any] | None = None,
    reason: str = 'on_demand',
) -> dict[str, bytes]:
    from app.services.decision_bundle import refresh_decision_bundle_cache, read_cached_decision_bundle_zip
    from app.services.historical_replay_shadow_pack import build_historical_replay_shadow_pack
    from app.services.replay_bottleneck_pack import build_replay_bottleneck_pack
    from app.services.replay_checkpoint_compatibility_pack import build_replay_checkpoint_compatibility_pack
    from app.services.surfaced_checkpoint_visual_review_pack import build_surfaced_checkpoint_visual_review_pack
    from app.services.surfaced_multisession_visual_review_pack import build_surfaced_multisession_visual_review_pack

    repos = ensure_repository_bundle(db)
    requested_offsets = sorted({int(value) for value in (offsets or DEFAULT_OFFSETS) if int(value) > 0}) or list(DEFAULT_OFFSETS)
    previous_summary = read_cached_evidence_automation_summary(settings)

    artifact_results: dict[str, dict[str, Any]] = {}
    packs: dict[str, dict[str, bytes]] = {}
    summaries: dict[str, dict[str, Any]] = {}

    packs['historical_replay_shadow_pack'], artifact_results['historical_replay_shadow_pack'] = _safe_generate(
        'historical_replay_shadow_pack',
        build_historical_replay_shadow_pack,
        settings,
        repos,
        alpaca,
        lookback_days=lookback_days,
        offsets=requested_offsets,
    )
    summaries['historical_replay_shadow_pack'] = _read_json_bytes(packs['historical_replay_shadow_pack'].get('historical_replay_shadow_summary.json', b''))

    packs['replay_bottleneck_pack'], artifact_results['replay_bottleneck_pack'] = _safe_generate(
        'replay_bottleneck_pack',
        build_replay_bottleneck_pack,
        settings,
        repos,
        alpaca,
        lookback_days=lookback_days,
        offsets=requested_offsets,
    )
    summaries['replay_bottleneck_pack'] = _read_json_bytes(packs['replay_bottleneck_pack'].get('replay_bottleneck_summary.json', b''))

    packs['replay_checkpoint_compatibility_pack'], artifact_results['replay_checkpoint_compatibility_pack'] = _safe_generate(
        'replay_checkpoint_compatibility_pack',
        build_replay_checkpoint_compatibility_pack,
        settings,
        repos,
        alpaca,
        lookback_days=lookback_days,
        offsets=requested_offsets,
    )
    summaries['replay_checkpoint_compatibility_pack'] = _read_json_bytes(packs['replay_checkpoint_compatibility_pack'].get('replay_checkpoint_compatibility_summary.json', b''))

    packs['surfaced_checkpoint_visual_review_pack'], artifact_results['surfaced_checkpoint_visual_review_pack'] = _safe_generate(
        'surfaced_checkpoint_visual_review_pack',
        build_surfaced_checkpoint_visual_review_pack,
        settings,
        repos,
        alpaca,
        days=review_days,
        offsets=requested_offsets,
    )
    summaries['surfaced_checkpoint_visual_review_pack'] = _read_json_bytes(packs['surfaced_checkpoint_visual_review_pack'].get('surfaced_checkpoint_visual_review_summary.json', b''))

    packs['surfaced_multisession_visual_review_pack'], artifact_results['surfaced_multisession_visual_review_pack'] = _safe_generate(
        'surfaced_multisession_visual_review_pack',
        build_surfaced_multisession_visual_review_pack,
        settings,
        repos,
        alpaca,
        days=review_days,
        offsets=requested_offsets,
    )
    summaries['surfaced_multisession_visual_review_pack'] = _read_json_bytes(packs['surfaced_multisession_visual_review_pack'].get('surfaced_multisession_visual_review_summary.json', b''))

    fidelity_summary = build_replay_live_fidelity_audit(
        replay_summary=summaries['historical_replay_shadow_pack'],
        replay_bottleneck_summary=summaries['replay_bottleneck_pack'],
        surfaced_checkpoint_summary=summaries['surfaced_checkpoint_visual_review_pack'],
        surfaced_multisession_summary=summaries['surfaced_multisession_visual_review_pack'],
        decision_state=None,
    )
    _write_json(_fidelity_summary_path(settings), fidelity_summary)

    # Decision bundle should ingest the latest cached fidelity/smoke summaries, so write a provisional smoke result first.
    refresh_evidence_smoke_validation_cache(
        settings,
        repos,
        alpaca,
        route_paths=route_paths,
        scheduler_status=scheduler_status,
        generated_artifacts=artifact_results,
        reason=reason,
    )

    decision_state: dict[str, Any] = {}
    cached_decision_bundle: bytes | None = None
    try:
        decision_state = refresh_decision_bundle_cache(settings, repos, alpaca, days=days, offsets=requested_offsets)
        cached_decision_bundle = read_cached_decision_bundle_zip(settings)
        artifact_results['decision_bundle'] = {
            'ok': bool(cached_decision_bundle),
            'label': 'decision_bundle',
            'error': None if cached_decision_bundle else 'Decision bundle cache was empty after refresh.',
            'file_count': 1 if cached_decision_bundle else 0,
        }
    except Exception as exc:  # pragma: no cover - defensive
        artifact_results['decision_bundle'] = {
            'ok': False,
            'label': 'decision_bundle',
            'error': f'{type(exc).__name__}: {exc}',
            'file_count': 0,
        }
    packs['decision_bundle'] = {'decision_bundle_latest.zip': cached_decision_bundle or b''}

    fidelity_summary = build_replay_live_fidelity_audit(
        replay_summary=summaries['historical_replay_shadow_pack'],
        replay_bottleneck_summary=summaries['replay_bottleneck_pack'],
        surfaced_checkpoint_summary=summaries['surfaced_checkpoint_visual_review_pack'],
        surfaced_multisession_summary=summaries['surfaced_multisession_visual_review_pack'],
        decision_state=decision_state,
    )
    smoke_summary = refresh_evidence_smoke_validation_cache(
        settings,
        repos,
        alpaca,
        route_paths=route_paths,
        scheduler_status=scheduler_status,
        generated_artifacts=artifact_results,
        reason=reason,
    )
    current_summary = _build_current_summary(decision_state, smoke_summary, fidelity_summary)
    delta_summary = _build_material_delta(previous_summary, current_summary)

    stored_artifacts: dict[str, dict[str, Any]] = {}
    for artifact_key, pack in packs.items():
        if artifact_key == 'decision_bundle':
            raw = cached_decision_bundle or b''
            if raw:
                path = _artifact_path(settings, artifact_key)
                path.write_bytes(raw)
                stored_artifacts[artifact_key] = {
                    'artifact_key': artifact_key,
                    'filename': ARTIFACT_FILENAMES[artifact_key],
                    'path': str(path),
                    'size_bytes': len(raw),
                }
            continue
        if pack:
            stored_artifacts[artifact_key] = _store_artifact(settings, artifact_key, pack)

    manifest = {
        'bundle_type': 'evidence_automation_pack',
        'bundle_contract_version': '1.0',
        'app_version': VERSION,
        'generated_at_utc': datetime.now(UTC).isoformat(),
        'days_requested': int(days),
        'review_days_requested': int(review_days),
        'lookback_days_requested': int(lookback_days),
        'offsets_requested': list(requested_offsets),
        'reason': reason,
    }

    report_lines = [
        '# Evidence automation pack',
        '',
        f"Generated at: {manifest['generated_at_utc']}",
        f'App version: {VERSION}',
        f"Reason: {reason}",
        f"Decision recommendation: {current_summary.get('decision_recommendation_code')}",
        f"Smoke status: {smoke_summary.get('overall_status')}",
        f"Replay-vs-live fidelity verdict: {fidelity_summary.get('verdict')}",
        '',
        '## Material changes only',
    ]
    report_lines.extend([f'- {item}' for item in (delta_summary.get('changes') or [])])
    report_lines.extend([
        '',
        '## Stored artifacts',
    ])
    for key, meta in sorted(stored_artifacts.items()):
        report_lines.append(f"- {key}: {meta.get('filename')} ({meta.get('size_bytes')} bytes)")

    pack: dict[str, bytes] = {
        'MANIFEST.json': json.dumps(manifest, indent=2).encode('utf-8'),
        'evidence_automation_summary.json': json.dumps(current_summary, indent=2).encode('utf-8'),
        'evidence_smoke_summary.json': json.dumps(smoke_summary, indent=2).encode('utf-8'),
        'replay_live_fidelity_audit_summary.json': json.dumps(fidelity_summary, indent=2).encode('utf-8'),
        'evidence_delta_summary.json': json.dumps(delta_summary, indent=2).encode('utf-8'),
        'evidence_delta.txt': _render_delta_text(delta_summary).encode('utf-8'),
        'report.md': '\n'.join(report_lines).encode('utf-8'),
    }
    for artifact_key, meta in stored_artifacts.items():
        artifact_path = Path(meta['path'])
        if artifact_path.exists():
            pack[f'artifacts/{meta["filename"]}'] = artifact_path.read_bytes()
    pack.update({
        'summaries/historical_replay_shadow_summary.json': json.dumps(summaries['historical_replay_shadow_pack'], indent=2).encode('utf-8'),
        'summaries/replay_bottleneck_summary.json': json.dumps(summaries['replay_bottleneck_pack'], indent=2).encode('utf-8'),
        'summaries/replay_checkpoint_compatibility_summary.json': json.dumps(summaries['replay_checkpoint_compatibility_pack'], indent=2).encode('utf-8'),
        'summaries/surfaced_checkpoint_visual_review_summary.json': json.dumps(summaries['surfaced_checkpoint_visual_review_pack'], indent=2).encode('utf-8'),
        'summaries/surfaced_multisession_visual_review_summary.json': json.dumps(summaries['surfaced_multisession_visual_review_pack'], indent=2).encode('utf-8'),
    })
    _, evidence_zip = write_evidence_automation_cache(settings, pack, current_summary, delta_summary, smoke_summary, fidelity_summary)
    _store_history_snapshot(
        settings,
        current_summary=current_summary,
        delta_summary=delta_summary,
        smoke_summary=smoke_summary,
        fidelity_summary=fidelity_summary,
        decision_state=decision_state,
        evidence_pack_zip=evidence_zip,
        decision_bundle_zip=cached_decision_bundle,
    )
    get_or_build_five_session_evidence_review_zip(settings, session_count=5, prefer_cache=False)
    return pack



def get_or_build_evidence_automation_zip(
    settings: Settings,
    db: Database | RepositoryBundle,
    alpaca,
    *,
    days: int = DEFAULT_EVIDENCE_DAYS,
    offsets: list[int] | None = None,
    review_days: int = DEFAULT_REVIEW_DAYS,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    route_paths: Iterable[str] | None = None,
    scheduler_status: dict[str, Any] | None = None,
    reason: str = 'on_demand',
    prefer_cache: bool = True,
) -> bytes:
    cached = read_cached_evidence_automation_zip(settings) if prefer_cache else None
    if cached:
        return cached
    pack = build_evidence_automation_pack(
        settings,
        db,
        alpaca,
        days=days,
        offsets=offsets,
        review_days=review_days,
        lookback_days=lookback_days,
        route_paths=route_paths,
        scheduler_status=scheduler_status,
        reason=reason,
    )
    return _safe_pack_to_zip(pack)



def maybe_refresh_evidence_automation_after_close(
    settings: Settings,
    db: Database | RepositoryBundle,
    alpaca,
    *,
    days: int = DEFAULT_EVIDENCE_DAYS,
    offsets: list[int] | None = None,
    review_days: int = DEFAULT_REVIEW_DAYS,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    route_paths: Iterable[str] | None = None,
    scheduler_status: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    from app.services.market_time import get_session_for_day, latest_or_previous_trading_day

    requested_offsets = list(offsets or DEFAULT_OFFSETS)
    trading_day = latest_or_previous_trading_day()
    session = get_session_for_day(trading_day, max(requested_offsets))
    if session.now_et < session.market_close:
        return None
    repos = ensure_repository_bundle(db)
    scans_for_day = [scan for scan in repos.scan.list_recent(limit=50) if str(scan.get('trading_day') or '') == trading_day]
    if not scans_for_day:
        return None
    cached = read_cached_evidence_automation_summary(settings) or {}
    if str(cached.get('latest_selected_day') or '') == trading_day and str(cached.get('app_version') or '') == VERSION:
        return cached
    build_evidence_automation_pack(
        settings,
        repos,
        alpaca,
        days=days,
        offsets=requested_offsets,
        review_days=review_days,
        lookback_days=lookback_days,
        route_paths=route_paths,
        scheduler_status=scheduler_status,
        reason='post_close',
    )
    return read_cached_evidence_automation_summary(settings)
