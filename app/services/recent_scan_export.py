from __future__ import annotations

from typing import Any, Dict, List

from app.config import Settings
from app.db import Database
from app.repositories import RepositoryBundle, ensure_repository_bundle
from app.services.diagnostics import build_contract_health
from app.services.evidence_pack import _json_bytes, _rows_to_csv
from app.version import VERSION
from app.view_models import build_candidate_list, build_scan_view


def build_recent_scan_export_pack(
    settings: Settings,
    db: Database | RepositoryBundle,
    *,
    days: int = 5,
    offsets: List[int] | None = None,
) -> Dict[str, bytes]:
    repos = ensure_repository_bundle(db)
    offsets = sorted(set(int(v) for v in (offsets or [120, 150]) if int(v) > 0)) or [120, 150]
    scans = repos.scan.list_recent(limit=500)
    unique_days: List[str] = []
    for scan in scans:
        day = str(scan['trading_day'])
        if day not in unique_days:
            unique_days.append(day)
        if len(unique_days) >= int(days):
            break
    selected_days = unique_days[: int(days)]

    chosen_scans: Dict[tuple[str, int], Dict[str, Any]] = {}
    for day in selected_days:
        for offset in offsets:
            match = next(
                (scan for scan in scans if str(scan['trading_day']) == day and int(scan['scan_offset_minutes']) == int(offset)),
                None,
            )
            if match:
                chosen_scans[(day, int(offset))] = match

    manifest = {
        'bundle_type': 'recent_scan_export',
        'bundle_contract_version': '1.0',
        'app_version': VERSION,
        'days_requested': int(days),
        'offsets_requested': offsets,
        'trading_days_included': selected_days,
        'included_scan_pairs': [
            {
                'trading_day': day,
                'scan_offset_minutes': offset,
                'scan_id': int(scan['id']),
            }
            for (day, offset), scan in sorted(chosen_scans.items())
        ],
        'missing_scan_pairs': [
            {'trading_day': day, 'scan_offset_minutes': offset}
            for day in selected_days
            for offset in offsets
            if (day, offset) not in chosen_scans
        ],
        'settings_snapshot': settings.public_snapshot(),
    }

    pack: Dict[str, bytes] = {
        'MANIFEST.json': _json_bytes(manifest),
        'settings_snapshot.json': _json_bytes(settings.public_snapshot()),
        'contract_health.json': _json_bytes(build_contract_health(repos.db)),
    }

    for (day, offset), scan in sorted(chosen_scans.items()):
        view = build_scan_view(scan, alpaca_data_feed=settings.alpaca_data_feed)
        candidates = build_candidate_list(repos.scan.get_candidates(int(scan['id'])))
        prefix = f'scans/{day}/offset_{offset}_scan_{int(scan["id"])}'
        pack[f'{prefix}_summary.json'] = _json_bytes(view or {})
        pack[f'{prefix}_candidates.csv'] = _rows_to_csv(candidates).encode('utf-8')

    return pack
