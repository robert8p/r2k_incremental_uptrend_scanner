from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Tuple

from app.config import Settings
from app.db import Database
from app.repositories import RepositoryBundle, ensure_repository_bundle
from app.services.diagnostics import build_contract_health, build_diagnostics_snapshot
from app.services.evidence_pack import _json_bytes, _rows_to_csv
from app.services.job_governance import build_job_status_snapshot
from app.services.live_trust import build_live_trust_snapshot
from app.services.universe import load_universe
from app.version import VERSION
from app.view_models import build_candidate_list, build_scan_view


def _top_exclusion_reasons(candidates: List[Dict[str, object]], *, limit: int = 5) -> List[Dict[str, object]]:
    counter: Counter[str] = Counter()
    for candidate in candidates:
        if candidate.get('advanced_to_stage2'):
            continue
        reason = str(candidate.get('exclusion_reason') or candidate.get('rationale') or 'No explicit reason recorded.')
        counter[reason] += 1
    return [{'reason': reason, 'count': count} for reason, count in counter.most_common(limit)]


def build_live_confirmation_pack(
    settings: Settings,
    db: Database | RepositoryBundle,
    alpaca,
    *,
    days: int = 5,
    offsets: List[int] | None = None,
    scheduler_status: Dict[str, object] | None = None,
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

    chosen_scans: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for day in selected_days:
        for offset in offsets:
            match = next(
                (scan for scan in scans if str(scan['trading_day']) == day and int(scan['scan_offset_minutes']) == int(offset)),
                None,
            )
            if match:
                chosen_scans[(day, int(offset))] = match

    diagnostics_snapshot = build_diagnostics_snapshot(settings, repos.db, alpaca, scheduler_status=scheduler_status)
    live_trust = build_live_trust_snapshot(settings, repos.db)
    jobs_snapshot = build_job_status_snapshot(repos.db)
    universe_status = load_universe(settings, repos.db, force_refresh=False)['status'].__dict__

    rollup_rows: List[Dict[str, object]] = []
    pack: Dict[str, bytes] = {}
    for (day, offset), scan in sorted(chosen_scans.items()):
        view = build_scan_view(scan, alpaca_data_feed=settings.alpaca_data_feed)
        candidates = build_candidate_list(repos.scan.get_candidates(int(scan['id'])))
        stage2_count = int(sum(1 for candidate in candidates if candidate.get('advanced_to_stage2')))
        rejection_reasons = _top_exclusion_reasons(candidates)
        rollup_rows.append({
            'trading_day': day,
            'scan_offset_minutes': offset,
            'scan_id': int(scan['id']),
            'stage1_count': int(scan.get('stage1_count') or 0),
            'stage2_count': stage2_count,
            'top_rejection_reasons': '; '.join(f"{item['reason']} ({item['count']})" for item in rejection_reasons),
        })
        prefix = f'live_confirmation/{day}/offset_{offset}_scan_{int(scan["id"])}'
        pack[f'{prefix}_summary.json'] = _json_bytes(view or {})
        pack[f'{prefix}_candidates.csv'] = _rows_to_csv(candidates).encode('utf-8')
        pack[f'{prefix}_rejection_reasons.json'] = _json_bytes({'trading_day': day, 'scan_offset_minutes': offset, 'scan_id': int(scan['id']), 'top_rejection_reasons': rejection_reasons})

    manifest = {
        'bundle_type': 'live_confirmation_pack',
        'bundle_contract_version': '1.0',
        'app_version': VERSION,
        'days_requested': int(days),
        'offsets_requested': offsets,
        'trading_days_included': selected_days,
        'included_scan_pairs': [
            {'trading_day': day, 'scan_offset_minutes': offset, 'scan_id': int(scan['id'])}
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

    pack['MANIFEST.json'] = _json_bytes(manifest)
    pack['settings_snapshot.json'] = _json_bytes(settings.public_snapshot())
    pack['diagnostics_snapshot.json'] = _json_bytes(diagnostics_snapshot)
    pack['live_trust.json'] = _json_bytes(live_trust)
    pack['jobs_snapshot.json'] = _json_bytes(jobs_snapshot)
    pack['universe_status.json'] = _json_bytes(universe_status)
    pack['contract_health.json'] = _json_bytes(build_contract_health(repos.db))
    pack['scan_rollup.csv'] = _rows_to_csv(rollup_rows).encode('utf-8')
    return pack
