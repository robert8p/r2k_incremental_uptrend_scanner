from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict

from app.config import Settings
from app.db import Database
from app.repositories import RepositoryBundle, ensure_repository_bundle
from app.services.diagnostics import build_contract_health
from app.version import VERSION
from app.services.telemetry import emit_event
from app.view_models import build_research_view, build_validation_view

EVIDENCE_PACK_VERSION = '1.0'


def _rows_to_csv(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ''
    flat_rows = []
    for row in rows:
        base = {k: v for k, v in row.items() if k not in {'component_scores', 'metrics'}}
        for k, v in (row.get('component_scores') or {}).items():
            base[f'component_{k}'] = v
        metrics = row.get('metrics') or {}
        for k, v in metrics.items():
            if isinstance(v, (dict, list)):
                continue
            base[f'metric_{k}'] = v
        flat_rows.append(base)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=sorted({key for row in flat_rows for key in row.keys()}))
    writer.writeheader()
    writer.writerows(flat_rows)
    return buffer.getvalue()


def _json_bytes(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, indent=2, sort_keys=True, default=str).encode('utf-8')


def _base_manifest(*, bundle_type: str, subject_id: int, settings: Settings, row_count: int | None = None) -> Dict[str, Any]:
    manifest = {
        'bundle_type': bundle_type,
        'bundle_contract_version': EVIDENCE_PACK_VERSION,
        'app_version': VERSION,
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'subject_id': int(subject_id),
        'settings_snapshot': settings.public_snapshot(),
    }
    if row_count is not None:
        manifest['row_count'] = int(row_count)
    return manifest


def build_validation_evidence_pack(settings: Settings, db: Database | RepositoryBundle, validation_id: int) -> Dict[str, bytes]:
    repos = ensure_repository_bundle(db)
    validation = build_validation_view(repos.validation.get(validation_id))
    if not validation:
        raise ValueError('Validation run not found.')

    rows = list(validation.get('rows') or [])
    manifest = _base_manifest(
        bundle_type='validation_evidence_pack',
        subject_id=validation_id,
        settings=settings,
        row_count=len(rows),
    )
    manifest.update({
        'scan_offset_minutes': int(validation.get('scan_offset_minutes') or 0),
        'start_date': validation.get('start_date'),
        'end_date': validation.get('end_date'),
        'status': validation.get('status'),
    })

    record = {k: v for k, v in validation.items() if k != 'rows'}
    pack = {
        'MANIFEST.json': _json_bytes(manifest),
        'settings_snapshot.json': _json_bytes(settings.public_snapshot()),
        'contract_health.json': _json_bytes(build_contract_health(repos.db)),
        'validation_record.json': _json_bytes(record),
        'validation_summary.json': _json_bytes(validation.get('summary') or {}),
        'validation_rows.csv': _rows_to_csv(rows).encode('utf-8'),
    }
    emit_event('evidence_pack.built', pack_type='validation', subject_id=int(validation_id), file_count=len(pack), row_count=len(rows))
    return pack


def build_research_evidence_pack(settings: Settings, db: Database | RepositoryBundle, run_id: int) -> Dict[str, bytes]:
    repos = ensure_repository_bundle(db)
    research = build_research_view(repos.research.get(run_id))
    if not research:
        raise ValueError('Research run not found.')

    result = dict(research.get('result') or {})
    linked_validation_id = result.get('best_validation_id') or result.get('validation_id')
    manifest = _base_manifest(
        bundle_type='research_evidence_pack',
        subject_id=run_id,
        settings=settings,
    )
    manifest.update({
        'status': research.get('status'),
        'research_mode': (research.get('params') or {}).get('mode') or (research.get('params') or {}).get('run_mode'),
        'linked_validation_id': linked_validation_id,
    })

    pack = {
        'MANIFEST.json': _json_bytes(manifest),
        'settings_snapshot.json': _json_bytes(settings.public_snapshot()),
        'contract_health.json': _json_bytes(build_contract_health(repos.db)),
        'research_record.json': _json_bytes({k: v for k, v in research.items() if k != 'result'}),
        'research_result.json': _json_bytes(result),
    }

    if linked_validation_id:
        validation = build_validation_view(repos.validation.get(int(linked_validation_id)))
        if validation:
            pack['linked_validation_summary.json'] = _json_bytes(validation.get('summary') or {})
            pack['linked_validation_rows.csv'] = _rows_to_csv(validation.get('rows') or []).encode('utf-8')
    emit_event('evidence_pack.built', pack_type='research', subject_id=int(run_id), file_count=len(pack), linked_validation_id=linked_validation_id)
    return pack


def pack_to_zip_bytes(files: Dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode='w', compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in sorted(files.items()):
            zf.writestr(name, data)
    return buffer.getvalue()
