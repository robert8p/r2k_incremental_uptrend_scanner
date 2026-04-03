from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from app.config import Settings
from app.db import Database
from app.repositories import RepositoryBundle, ensure_repository_bundle
from app.services.diagnostics import build_contract_health
from app.services.evidence_pack import _json_bytes, _rows_to_csv
from app.version import VERSION
from app.view_models import build_candidate_list, build_scan_view


def _counter_rows(counter: Counter[str]) -> List[Dict[str, Any]]:
    return [{'key': key, 'count': count} for key, count in counter.most_common()]


def _scan_rollup_row(scan: Dict[str, Any], candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    summary = dict(scan.get('summary') or {})
    shortlist_alignment = dict(summary.get('shortlist_alignment') or {})
    advanced = [candidate for candidate in candidates if candidate.get('advanced_to_stage2')]
    excluded = [candidate for candidate in candidates if not candidate.get('advanced_to_stage2')]
    exclusion_reasons = Counter(str(candidate.get('exclusion_reason') or 'No explicit exclusion reason.') for candidate in excluded)
    classification_counts = Counter(str((candidate.get('metrics') or {}).get('range_classification') or 'Unknown') for candidate in excluded)
    tier_counts = Counter(str(candidate.get('recommendation_tier') or 'unknown') for candidate in advanced)
    book_counts = Counter(str(candidate.get('recommendation_book') or 'unknown') for candidate in advanced)
    lane_counts = Counter(str(candidate.get('execution_lane') or 'unknown') for candidate in advanced)
    return {
        'trading_day': str(scan.get('trading_day') or ''),
        'scan_id': int(scan.get('id') or 0),
        'scan_offset_minutes': int(scan.get('scan_offset_minutes') or 0),
        'stage1_count': int(scan.get('stage1_count') or 0),
        'stage2_count': int(scan.get('stage2_count') or 0),
        'alignment_prefilter_kept_count': int(shortlist_alignment.get('alignment_prefilter_kept_count') or 0),
        'selection_mode': shortlist_alignment.get('selection_mode') or 'unknown',
        'top_exclusion_reason': exclusion_reasons.most_common(1)[0][0] if exclusion_reasons else None,
        'top_exclusion_reason_count': exclusion_reasons.most_common(1)[0][1] if exclusion_reasons else 0,
        'top_range_classification': classification_counts.most_common(1)[0][0] if classification_counts else None,
        'top_range_classification_count': classification_counts.most_common(1)[0][1] if classification_counts else 0,
        'advanced_tier_counts': '; '.join(f"{key} ({count})" for key, count in tier_counts.items()) if tier_counts else '',
        'advanced_book_counts': '; '.join(f"{key} ({count})" for key, count in book_counts.items()) if book_counts else '',
        'advanced_lane_counts': '; '.join(f"{key} ({count})" for key, count in lane_counts.items()) if lane_counts else '',
    }


def _advanced_candidate_rows(scan: Dict[str, Any], candidates: List[Dict[str, Any]], outcomes_by_symbol: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for candidate in sorted([c for c in candidates if c.get('advanced_to_stage2')], key=lambda item: (-float(item.get('total_score') or 0.0), str(item.get('symbol') or ''))):
        metrics = dict(candidate.get('metrics') or {})
        outcome = outcomes_by_symbol.get(str(candidate.get('symbol') or '')) or {}
        rows.append({
            'trading_day': str(scan.get('trading_day') or ''),
            'scan_id': int(scan.get('id') or 0),
            'scan_offset_minutes': int(scan.get('scan_offset_minutes') or 0),
            'symbol': candidate.get('symbol'),
            'company_name': candidate.get('company_name'),
            'total_score': candidate.get('total_score'),
            'recommendation_tier': candidate.get('recommendation_tier'),
            'recommendation_book': candidate.get('recommendation_book'),
            'execution_lane': candidate.get('execution_lane'),
            'touch_window_band': candidate.get('touch_window_band'),
            'monitor_cadence_minutes': candidate.get('monitor_cadence_minutes'),
            'intraday_pct_gain': candidate.get('intraday_pct_gain'),
            'distance_to_entry_pct': metrics.get('distance_to_entry_pct'),
            'range_classification': metrics.get('range_classification'),
            'range_current_location': metrics.get('range_current_location'),
            'execution_readiness_score': metrics.get('execution_readiness_score'),
            'follow_through_confidence_score': metrics.get('follow_through_confidence_score'),
            'expected_actionability_score': metrics.get('expected_actionability_score'),
            'actionability_score': metrics.get('actionability_score'),
            'headline_rank_score': metrics.get('headline_rank_score'),
            'structural_score': metrics.get('structural_score'),
            'evaluation_status': outcome.get('evaluation_status'),
            'entry_touched': outcome.get('entry_touched'),
            'hit_target': outcome.get('hit_target'),
            'minutes_to_entry': outcome.get('minutes_to_entry'),
            'minutes_to_target': outcome.get('minutes_to_target'),
            'mfe_pct': outcome.get('mfe_pct'),
            'mae_pct': outcome.get('mae_pct'),
            'end_of_window_return_pct': outcome.get('end_of_window_return_pct'),
            'net_end_of_window_return_pct': outcome.get('net_end_of_window_return_pct'),
        })
    return rows


def _latest_day_progression(latest_day: str, scans_by_key: Dict[Tuple[str, int], Dict[str, Any]], candidates_by_scan_id: Dict[int, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    offsets = sorted({offset for day, offset in scans_by_key if day == latest_day})
    if len(offsets) < 2:
        return []
    early_offset = offsets[0]
    late_offset = offsets[-1]
    early_scan = scans_by_key[(latest_day, early_offset)]
    late_scan = scans_by_key[(latest_day, late_offset)]
    early_candidates = {str(candidate.get('symbol') or ''): candidate for candidate in candidates_by_scan_id[int(early_scan['id'])]}
    late_candidates = {str(candidate.get('symbol') or ''): candidate for candidate in candidates_by_scan_id[int(late_scan['id'])]}
    tracked_symbols = sorted({symbol for symbol, candidate in early_candidates.items() if candidate.get('advanced_to_stage2')} | {symbol for symbol, candidate in late_candidates.items() if candidate.get('advanced_to_stage2')})
    rows: List[Dict[str, Any]] = []
    for symbol in tracked_symbols:
        early = early_candidates.get(symbol) or {}
        late = late_candidates.get(symbol) or {}
        early_metrics = dict(early.get('metrics') or {})
        late_metrics = dict(late.get('metrics') or {})
        rows.append({
            'trading_day': latest_day,
            'symbol': symbol,
            'present_at_early_offset': bool(early),
            'present_at_late_offset': bool(late),
            f'advanced_at_{early_offset}': bool(early.get('advanced_to_stage2')),
            f'tier_at_{early_offset}': early.get('recommendation_tier'),
            f'book_at_{early_offset}': early.get('recommendation_book'),
            f'lane_at_{early_offset}': early.get('execution_lane'),
            f'total_score_at_{early_offset}': early.get('total_score'),
            f'distance_to_entry_pct_at_{early_offset}': early_metrics.get('distance_to_entry_pct'),
            f'advanced_at_{late_offset}': bool(late.get('advanced_to_stage2')),
            f'tier_at_{late_offset}': late.get('recommendation_tier'),
            f'book_at_{late_offset}': late.get('recommendation_book'),
            f'lane_at_{late_offset}': late.get('execution_lane'),
            f'total_score_at_{late_offset}': late.get('total_score'),
            f'exclusion_reason_at_{late_offset}': late.get('exclusion_reason'),
            f'range_classification_at_{late_offset}': late_metrics.get('range_classification'),
            f'score_cap_reason_at_{late_offset}': late_metrics.get('score_cap_reason'),
            f'distance_to_entry_pct_at_{late_offset}': late_metrics.get('distance_to_entry_pct'),
            'regressed_after_early_advance': bool(early.get('advanced_to_stage2')) and not bool(late.get('advanced_to_stage2')),
        })
    return rows


def build_stage2_sanity_pack(
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

    chosen_scans: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for day in selected_days:
        for offset in offsets:
            match = next((scan for scan in scans if str(scan['trading_day']) == day and int(scan['scan_offset_minutes']) == int(offset)), None)
            if match:
                chosen_scans[(day, int(offset))] = match

    candidates_by_scan_id: Dict[int, List[Dict[str, Any]]] = {}
    outcomes_by_scan_id: Dict[int, Dict[str, Dict[str, Any]]] = {}
    rollup_rows: List[Dict[str, Any]] = []
    advanced_rows: List[Dict[str, Any]] = []
    exclusion_reason_rows: List[Dict[str, Any]] = []
    classification_rows: List[Dict[str, Any]] = []
    pack: Dict[str, bytes] = {}

    for (day, offset), scan in sorted(chosen_scans.items()):
        scan_id = int(scan['id'])
        scan_view = build_scan_view(scan, alpaca_data_feed=settings.alpaca_data_feed)
        candidates = build_candidate_list(repos.scan.get_candidates(scan_id))
        candidates_by_scan_id[scan_id] = candidates
        outcomes = {str(row.get('symbol') or ''): row for row in repos.db.list_live_candidate_outcomes_for_scan(scan_id)}
        outcomes_by_scan_id[scan_id] = outcomes

        rollup_rows.append(_scan_rollup_row(scan_view or scan, candidates))
        advanced_rows.extend(_advanced_candidate_rows(scan_view or scan, candidates, outcomes))

        exclusion_counter = Counter(str(candidate.get('exclusion_reason') or 'No explicit exclusion reason.') for candidate in candidates if not candidate.get('advanced_to_stage2'))
        classification_counter = Counter(str((candidate.get('metrics') or {}).get('range_classification') or 'Unknown') for candidate in candidates if not candidate.get('advanced_to_stage2'))
        exclusion_reason_rows.extend([{
            'trading_day': day,
            'scan_offset_minutes': offset,
            'scan_id': scan_id,
            'exclusion_reason': key,
            'count': count,
        } for key, count in exclusion_counter.most_common()])
        classification_rows.extend([{
            'trading_day': day,
            'scan_offset_minutes': offset,
            'scan_id': scan_id,
            'range_classification': key,
            'count': count,
        } for key, count in classification_counter.most_common()])

        prefix = f'stage2_sanity/{day}/offset_{offset}_scan_{scan_id}'
        pack[f'{prefix}_summary.json'] = _json_bytes(scan_view or {})
        pack[f'{prefix}_advanced_candidates.csv'] = _rows_to_csv(_advanced_candidate_rows(scan_view or scan, candidates, outcomes)).encode('utf-8')
        pack[f'{prefix}_excluded_reason_breakdown.json'] = _json_bytes({'rows': _counter_rows(exclusion_counter)})
        pack[f'{prefix}_excluded_range_classification_breakdown.json'] = _json_bytes({'rows': _counter_rows(classification_counter)})

    latest_day = selected_days[0] if selected_days else ''
    progression_rows = _latest_day_progression(latest_day, chosen_scans, candidates_by_scan_id) if latest_day else []

    audit_summary = {
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'app_version': VERSION,
        'selected_days': selected_days,
        'offsets_requested': offsets,
        'latest_trading_day': latest_day,
        'pipeline_proof_exists': any(int(row.get('stage2_count') or 0) > 0 for row in rollup_rows),
        'latest_day_rollup': [row for row in rollup_rows if row.get('trading_day') == latest_day],
        'latest_day_progression_row_count': len(progression_rows),
        'freeze_recommendation': [
            'Do not add new scoring features before finishing the stage-2 sanity audit.',
            'Do not add new tier or queue logic before verifying whether current stage-2 candidates are already directionally useful.',
            'Do not revert to stage-1 population redesign while shortlist alignment is clearly retaining a large tradable pool.',
        ],
    }

    report_lines = [
        '# Stage-2 sanity audit',
        '',
        f"Generated at: {audit_summary['generated_at_utc']}",
        f"App version: {VERSION}",
        f"Latest trading day in bundle: {latest_day or 'None'}",
        '',
        '## Rollup',
    ]
    for row in [row for row in rollup_rows if row.get('trading_day') == latest_day]:
        report_lines.extend([
            f"- Offset {row['scan_offset_minutes']}: stage1={row['stage1_count']} stage2={row['stage2_count']} alignment_kept={row['alignment_prefilter_kept_count']} top_exclusion={row['top_exclusion_reason']} ({row['top_exclusion_reason_count']})",
        ])
    if progression_rows:
        regressed = [row for row in progression_rows if row.get('regressed_after_early_advance')]
        report_lines.extend(['', '## Latest day progression', f"- Advanced symbols tracked across early/late offsets: {len(progression_rows)}", f"- Symbols that advanced earlier but not later: {len(regressed)}"])
    else:
        report_lines.extend(['', '## Latest day progression', '- Not enough offsets were available to build a same-day progression view.'])
    report_lines.extend(['', '## Freeze guidance', *[f'- {line}' for line in audit_summary['freeze_recommendation']]])

    manifest = {
        'bundle_type': 'stage2_sanity_pack',
        'bundle_contract_version': '1.0',
        'app_version': VERSION,
        'generated_at_utc': audit_summary['generated_at_utc'],
        'days_requested': int(days),
        'offsets_requested': offsets,
        'selected_days': selected_days,
        'settings_snapshot': settings.public_snapshot(),
    }

    pack['MANIFEST.json'] = _json_bytes(manifest)
    pack['settings_snapshot.json'] = _json_bytes(settings.public_snapshot())
    pack['contract_health.json'] = _json_bytes(build_contract_health(repos.db))
    pack['audit_summary.json'] = _json_bytes(audit_summary)
    pack['scan_rollup.csv'] = _rows_to_csv(rollup_rows).encode('utf-8')
    pack['advanced_candidates.csv'] = _rows_to_csv(advanced_rows).encode('utf-8')
    pack['excluded_reason_breakdown.csv'] = _rows_to_csv(exclusion_reason_rows).encode('utf-8')
    pack['excluded_range_classification_breakdown.csv'] = _rows_to_csv(classification_rows).encode('utf-8')
    pack['latest_day_progression.csv'] = _rows_to_csv(progression_rows).encode('utf-8')
    pack['report.md'] = '\n'.join(report_lines).encode('utf-8')
    pack['report.md'] = '\n'.join(report_lines).encode('utf-8')
    return pack
