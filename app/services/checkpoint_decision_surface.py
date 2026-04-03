from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable

from app.config import Settings
from app.db import Database
from app.repositories import RepositoryBundle, ensure_repository_bundle
from app.services.diagnostics import build_contract_health
from app.services.evidence_pack import _json_bytes, _rows_to_csv
from app.version import VERSION
from app.view_models import build_candidate_list, build_scan_view

DEFAULT_OFFSETS = [120, 150]
ACTIONABLE_TIERS = {"headline_shortlist", "ready_now", "near_ready"}


def _requested_offsets(offsets: Iterable[int] | None) -> list[int]:
    values = sorted({int(value) for value in (offsets or DEFAULT_OFFSETS) if int(value) > 0})
    return values or list(DEFAULT_OFFSETS)


def _recent_scans(repos: RepositoryBundle) -> list[dict[str, Any]]:
    return repos.scan.list_recent(limit=500)


def _resolve_trading_day(repos: RepositoryBundle, trading_day: str | None) -> str:
    if trading_day:
        return str(trading_day)
    scans = _recent_scans(repos)
    for scan in scans:
        day = str(scan.get('trading_day') or '')
        if day:
            return day
    return ''


def _select_scans_for_day(
    repos: RepositoryBundle,
    *,
    trading_day: str,
    offsets: Iterable[int] | None,
) -> tuple[list[int], dict[int, dict[str, Any]]]:
    requested = _requested_offsets(offsets)
    scans = _recent_scans(repos)
    chosen: dict[int, dict[str, Any]] = {}
    for offset in requested:
        match = next(
            (
                scan
                for scan in scans
                if str(scan.get('trading_day') or '') == str(trading_day)
                and int(scan.get('scan_offset_minutes') or 0) == int(offset)
            ),
            None,
        )
        if match:
            chosen[int(offset)] = match
    return requested, chosen


def _candidate_map(repos: RepositoryBundle, scan_id: int) -> dict[str, dict[str, Any]]:
    return {
        str(candidate.get('symbol') or ''): candidate
        for candidate in build_candidate_list(repos.scan.get_candidates(scan_id))
        if candidate is not None
    }


def _surface_breakdown(scan: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    tier_counts = Counter(str(candidate.get('recommendation_tier') or 'unknown') for candidate in candidates)
    book_counts = Counter(str(candidate.get('recommendation_book') or 'unknown') for candidate in candidates)
    lane_counts = Counter(str(candidate.get('execution_lane') or 'unknown') for candidate in candidates)
    summary = dict((scan or {}).get('summary') or {})
    return {
        'trading_day': str(scan.get('trading_day') or ''),
        'scan_id': int(scan.get('id') or 0),
        'scan_offset_minutes': int(scan.get('scan_offset_minutes') or 0),
        'stage1_count': int(scan.get('stage1_count') or 0),
        'stage2_count': int(scan.get('stage2_count') or 0),
        'leader_symbol': summary.get('leader_symbol'),
        'leader_gain_pct': summary.get('leader_gain_pct'),
        'actionable_tier_count': sum(1 for candidate in candidates if str(candidate.get('recommendation_tier') or '') in ACTIONABLE_TIERS),
        'watchlist_tier_count': tier_counts.get('watchlist', 0),
        'rejected_tier_count': tier_counts.get('rejected', 0),
        'headline_shortlist_count': tier_counts.get('headline_shortlist', 0),
        'ready_now_count': tier_counts.get('ready_now', 0),
        'near_ready_count': tier_counts.get('near_ready', 0),
        'touch_soon_queue_count': book_counts.get('touch_soon_queue', 0),
        'touch_later_queue_count': book_counts.get('touch_later_queue', 0),
        'structural_watchlist_count': book_counts.get('structural_watchlist', 0),
        'rejected_book_count': book_counts.get('rejected', 0),
        'monitor_5m_lane_count': lane_counts.get('monitor_5m', 0),
        'passive_watchlist_lane_count': lane_counts.get('passive_watchlist', 0),
    }


def _candidate_snapshot(offset: int, scan_id: int, candidate: dict[str, Any]) -> dict[str, Any]:
    metrics = dict(candidate.get('metrics') or {})
    return {
        'scan_offset_minutes': int(offset),
        'scan_id': int(scan_id),
        'symbol': candidate.get('symbol'),
        'company_name': candidate.get('company_name'),
        'advanced_to_stage2': bool(candidate.get('advanced_to_stage2')),
        'recommendation_tier': candidate.get('recommendation_tier'),
        'recommendation_book': candidate.get('recommendation_book'),
        'execution_lane': candidate.get('execution_lane'),
        'touch_window_band': candidate.get('touch_window_band'),
        'monitor_cadence_minutes': candidate.get('monitor_cadence_minutes'),
        'mover_rank': candidate.get('mover_rank'),
        'intraday_pct_gain': candidate.get('intraday_pct_gain'),
        'total_score': candidate.get('total_score'),
        'current_price': candidate.get('current_price'),
        'relative_volume': candidate.get('relative_volume'),
        'range_classification': metrics.get('range_classification'),
        'range_classification_code': metrics.get('range_classification_code'),
        'exclusion_reason': candidate.get('exclusion_reason'),
        'score_cap_reason': metrics.get('score_cap_reason'),
        'distance_to_entry_pct': metrics.get('distance_to_entry_pct'),
        'actionability_score': metrics.get('actionability_score'),
        'expected_actionability_score': metrics.get('expected_actionability_score'),
        'follow_through_confidence_score': metrics.get('follow_through_confidence_score'),
        'execution_readiness_score': metrics.get('execution_readiness_score'),
    }


def build_checkpoint_decision_surface(
    settings: Settings,
    db: Database | RepositoryBundle,
    *,
    trading_day: str | None = None,
    offsets: list[int] | None = None,
) -> dict[str, Any]:
    repos = ensure_repository_bundle(db)
    selected_day = _resolve_trading_day(repos, trading_day)
    requested_offsets, chosen_scans = _select_scans_for_day(repos, trading_day=selected_day, offsets=offsets)
    available_offsets = sorted(chosen_scans.keys())
    latest_offset = available_offsets[-1] if available_offsets else None
    earliest_offset = available_offsets[0] if available_offsets else None

    scan_rows: list[dict[str, Any]] = []
    candidate_maps: dict[int, dict[str, dict[str, Any]]] = {}
    for offset in available_offsets:
        scan = build_scan_view(chosen_scans[offset], alpaca_data_feed=settings.alpaca_data_feed)
        candidates = list(_candidate_map(repos, int(chosen_scans[offset].get('id') or 0)).values())
        candidate_maps[offset] = {str(candidate.get('symbol') or ''): candidate for candidate in candidates}
        scan_rows.append(_surface_breakdown(scan or chosen_scans[offset], candidates))

    latest_candidates = candidate_maps.get(latest_offset or -1, {}) if latest_offset is not None else {}
    current_valid_now = [
        _candidate_snapshot(latest_offset, int(chosen_scans[latest_offset].get('id') or 0), candidate)
        for candidate in latest_candidates.values()
        if candidate.get('advanced_to_stage2')
    ] if latest_offset is not None else []
    current_valid_now.sort(key=lambda row: (-(float(row.get('total_score') or 0.0)), str(row.get('symbol') or '')))

    symbols_advanced_any_checkpoint: set[str] = set()
    symbols_current_valid = {str(row.get('symbol') or '') for row in current_valid_now}
    best_snapshots: dict[str, dict[str, Any]] = {}
    first_advanced_offset: dict[str, int] = {}

    for offset in available_offsets:
        scan_id = int(chosen_scans[offset].get('id') or 0)
        for symbol, candidate in candidate_maps[offset].items():
            if not candidate.get('advanced_to_stage2'):
                continue
            symbols_advanced_any_checkpoint.add(symbol)
            first_advanced_offset.setdefault(symbol, int(offset))
            snapshot = _candidate_snapshot(offset, scan_id, candidate)
            current_best = best_snapshots.get(symbol)
            if current_best is None or float(snapshot.get('total_score') or 0.0) > float(current_best.get('total_score') or 0.0):
                best_snapshots[symbol] = snapshot

    best_candidates: list[dict[str, Any]] = []
    regressed_candidates: list[dict[str, Any]] = []
    for symbol in sorted(symbols_advanced_any_checkpoint):
        best_snapshot = dict(best_snapshots.get(symbol) or {})
        latest_candidate = dict(latest_candidates.get(symbol) or {}) if latest_offset is not None else {}
        latest_snapshot = _candidate_snapshot(latest_offset, int(chosen_scans[latest_offset].get('id') or 0), latest_candidate) if latest_candidate and latest_offset is not None else None
        row = {
            'symbol': symbol,
            'company_name': best_snapshot.get('company_name') or (latest_snapshot or {}).get('company_name'),
            'first_advanced_offset_minutes': first_advanced_offset.get(symbol),
            'best_advanced_offset_minutes': best_snapshot.get('scan_offset_minutes'),
            'best_advanced_scan_id': best_snapshot.get('scan_id'),
            'best_total_score': best_snapshot.get('total_score'),
            'best_recommendation_tier': best_snapshot.get('recommendation_tier'),
            'best_recommendation_book': best_snapshot.get('recommendation_book'),
            'best_execution_lane': best_snapshot.get('execution_lane'),
            'best_touch_window_band': best_snapshot.get('touch_window_band'),
            'latest_offset_minutes': latest_offset,
            'latest_scan_id': int(chosen_scans[latest_offset].get('id') or 0) if latest_offset is not None else None,
            'currently_valid_now': symbol in symbols_current_valid,
            'regressed_after_earlier_validity': symbol not in symbols_current_valid and latest_snapshot is not None,
            'latest_total_score': (latest_snapshot or {}).get('total_score'),
            'latest_recommendation_tier': (latest_snapshot or {}).get('recommendation_tier'),
            'latest_recommendation_book': (latest_snapshot or {}).get('recommendation_book'),
            'latest_execution_lane': (latest_snapshot or {}).get('execution_lane'),
            'latest_touch_window_band': (latest_snapshot or {}).get('touch_window_band'),
            'latest_range_classification': (latest_snapshot or {}).get('range_classification'),
            'latest_exclusion_reason': (latest_snapshot or {}).get('exclusion_reason'),
            'latest_score_cap_reason': (latest_snapshot or {}).get('score_cap_reason'),
        }
        best_candidates.append(row)
        if row['regressed_after_earlier_validity']:
            regressed_candidates.append(row)

    best_candidates.sort(key=lambda row: (-(float(row.get('best_total_score') or 0.0)), str(row.get('symbol') or '')))
    regressed_candidates.sort(key=lambda row: (-(float(row.get('best_total_score') or 0.0)), str(row.get('symbol') or '')))

    if scan_rows:
        best_checkpoint_row = max(
            scan_rows,
            key=lambda row: (
                int(row.get('stage2_count') or 0),
                int(row.get('actionable_tier_count') or 0),
                -int(row.get('scan_offset_minutes') or 0),
            ),
        )
    else:
        best_checkpoint_row = None

    summary = {
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'app_version': VERSION,
        'selected_day': selected_day,
        'requested_offsets': requested_offsets,
        'available_offsets': available_offsets,
        'latest_offset_minutes': latest_offset,
        'earliest_offset_minutes': earliest_offset,
        'max_stage2_count_any_checkpoint': max((int(row.get('stage2_count') or 0) for row in scan_rows), default=0),
        'best_checkpoint_offset_minutes': (best_checkpoint_row or {}).get('scan_offset_minutes'),
        'unique_symbols_advanced_any_checkpoint': len(symbols_advanced_any_checkpoint),
        'currently_valid_now_count': len(current_valid_now),
        'regressed_after_earlier_validity_count': len(regressed_candidates),
        'surface_message': (
            'No currently valid names at the latest checkpoint, but earlier-valid names still deserve attention.'
            if not current_valid_now and regressed_candidates
            else 'Current surface includes names that are still valid now at the latest checkpoint.'
            if current_valid_now
            else 'No checkpoint-aware candidates were advanced on the selected day.'
        ),
    }

    return {
        'summary': summary,
        'scan_rows': scan_rows,
        'current_valid_now': current_valid_now,
        'regressed_candidates': regressed_candidates,
        'best_candidates': best_candidates,
    }


def build_checkpoint_decision_pack(
    settings: Settings,
    db: Database | RepositoryBundle,
    *,
    trading_day: str | None = None,
    offsets: list[int] | None = None,
) -> dict[str, bytes]:
    repos = ensure_repository_bundle(db)
    surface = build_checkpoint_decision_surface(settings, repos, trading_day=trading_day, offsets=offsets)
    summary = dict(surface.get('summary') or {})
    selected_day = str(summary.get('selected_day') or '')
    report_lines = [
        '# Checkpoint-aware decision surface',
        '',
        f"Generated at: {summary.get('generated_at_utc')}",
        f"App version: {VERSION}",
        f"Selected trading day: {selected_day or 'None'}",
        f"Available offsets: {', '.join(str(v) for v in summary.get('available_offsets') or []) or 'None'}",
        '',
        '## Summary',
        f"- Unique symbols advanced at any checkpoint: {summary.get('unique_symbols_advanced_any_checkpoint', 0)}",
        f"- Currently valid now: {summary.get('currently_valid_now_count', 0)}",
        f"- Regressed after earlier validity: {summary.get('regressed_after_earlier_validity_count', 0)}",
        f"- Best checkpoint by stage-2 count: {summary.get('best_checkpoint_offset_minutes')}",
        '',
        '## Operating note',
        f"- {summary.get('surface_message')}",
    ]
    manifest = {
        'bundle_type': 'checkpoint_decision_pack',
        'bundle_contract_version': '1.0',
        'app_version': VERSION,
        'generated_at_utc': summary.get('generated_at_utc'),
        'selected_day': selected_day,
        'requested_offsets': summary.get('requested_offsets') or [],
        'available_offsets': summary.get('available_offsets') or [],
        'settings_snapshot': settings.public_snapshot(),
    }
    return {
        'MANIFEST.json': _json_bytes(manifest),
        'settings_snapshot.json': _json_bytes(settings.public_snapshot()),
        'contract_health.json': _json_bytes(build_contract_health(repos.db)),
        'checkpoint_decision_summary.json': _json_bytes(summary),
        'checkpoint_scan_summary.csv': _rows_to_csv(surface.get('scan_rows') or []).encode('utf-8'),
        'checkpoint_current_valid_now.csv': _rows_to_csv(surface.get('current_valid_now') or []).encode('utf-8'),
        'checkpoint_regressed_candidates.csv': _rows_to_csv(surface.get('regressed_candidates') or []).encode('utf-8'),
        'checkpoint_best_candidates.csv': _rows_to_csv(surface.get('best_candidates') or []).encode('utf-8'),
        'report.md': '\n'.join(report_lines).encode('utf-8'),
    }
