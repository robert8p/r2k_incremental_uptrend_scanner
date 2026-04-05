from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from app.config import Settings
from app.db import Database
from app.repositories import RepositoryBundle, ensure_repository_bundle
from app.services.checkpoint_decision_surface import build_checkpoint_decision_surface
from app.services.classifier_audit_pack import _intraday_bar_rows, _iso_z, _normalise_timestamp_column, _session_bounds_for_regular_day
from app.services.diagnostics import build_contract_health
from app.services.evidence_pack import _json_bytes, _rows_to_csv
from app.services.surfaced_checkpoint_visual_review_pack import _outcome_snapshot, _to_float, _to_int
from app.services.shadow_visual_review_pack import _bars_by_symbol_day, _review_row
from app.services.stage2_regression_pack import _candidate_maps, _select_recent_scans
from app.version import VERSION

UTC = timezone.utc
DEFAULT_REVIEW_LIMIT = 10
DEFAULT_OFFSETS = [120, 150]


def build_surfaced_multisession_visual_review_pack(
    settings: Settings,
    db: Database | RepositoryBundle,
    alpaca,
    *,
    days: int = 10,
    offsets: list[int] | None = None,
    review_limit: int = DEFAULT_REVIEW_LIMIT,
) -> dict[str, bytes]:
    repos = ensure_repository_bundle(db)
    requested_offsets = sorted({int(value) for value in (offsets or DEFAULT_OFFSETS) if int(value) > 0}) or list(DEFAULT_OFFSETS)
    surface = build_checkpoint_decision_surface(settings, repos, offsets=requested_offsets)
    focus_offset_minutes = _to_int((surface.get('summary') or {}).get('best_checkpoint_offset_minutes'))
    if focus_offset_minutes <= 0:
        focus_offset_minutes = requested_offsets[0]

    selected_days, chosen_scans = _select_recent_scans(repos, days=max(int(days), 1), offsets=[focus_offset_minutes])
    raw_rows: list[dict[str, Any]] = []
    total_stage2_candidates = 0
    for trading_day in selected_days:
        scan = chosen_scans.get((str(trading_day), int(focus_offset_minutes))) or {}
        scan_id = _to_int(scan.get('id'))
        if scan_id <= 0:
            continue
        candidate_map = _candidate_maps(repos, scan_id)
        stage2_rows = [dict(candidate) for candidate in candidate_map.values() if bool(candidate.get('advanced_to_stage2'))]
        total_stage2_candidates += len(stage2_rows)
        stage2_rows.sort(key=lambda row: (-(_to_float(row.get('total_score')) or 0.0), str(row.get('symbol') or '')))
        for row in stage2_rows[:2]:
            row['trading_day'] = str(trading_day)
            row['scan_offset_minutes'] = int(focus_offset_minutes)
            row['focus_scan_id'] = scan_id
            raw_rows.append(row)
    raw_rows.sort(key=lambda row: (str(row.get('trading_day') or ''), -(_to_float(row.get('total_score')) or 0.0), str(row.get('symbol') or '')), reverse=True)
    selected_surface_rows = raw_rows[: max(int(review_limit), 0)]

    review_rows: list[dict[str, Any]] = []
    review_svgs: dict[str, bytes] = {}
    selected_intraday_rows: list[dict[str, Any]] = []

    if selected_surface_rows and getattr(alpaca, 'has_credentials', lambda: False)():
        rows_by_day: dict[str, list[dict[str, Any]]] = {}
        for row in selected_surface_rows:
            rows_by_day.setdefault(str(row.get('trading_day') or ''), []).append(row)
        for trading_day, day_rows in rows_by_day.items():
            symbols = sorted({str(row.get('symbol') or '') for row in day_rows if str(row.get('symbol') or '')})
            if not symbols:
                continue
            market_open, market_close, _ = _session_bounds_for_regular_day(trading_day, focus_offset_minutes)
            bars_map = alpaca.fetch_bars(symbols, '1Min', _iso_z(market_open), _iso_z(market_close))
            intraday_rows = _intraday_bar_rows(bars_map, trading_day=trading_day, offset_minutes=[focus_offset_minutes])
            bars_lookup = _bars_by_symbol_day(intraday_rows)
            full_bars_lookup = {str(symbol): _normalise_timestamp_column(frame) for symbol, frame in (bars_map or {}).items()}
            for surface_row in day_rows:
                symbol = str(surface_row.get('symbol') or '')
                full_bars = full_bars_lookup.get(symbol)
                if full_bars is None:
                    full_bars = pd.DataFrame(columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                outcome = _outcome_snapshot(
                    full_day_bars=full_bars,
                    trading_day=trading_day,
                    offset_minutes=focus_offset_minutes,
                    candidate=surface_row,
                    settings=settings,
                )
                review_input = {
                    'trading_day': trading_day,
                    'scan_offset_minutes': focus_offset_minutes,
                    'symbol': symbol,
                    'company_name': surface_row.get('company_name'),
                    'selected_profile_name': 'actual_surfaced_stage2_multisession',
                    'verdict_bucket': outcome.get('verdict_bucket'),
                    'verdict_reason': outcome.get('verdict_reason'),
                    'entry_touched': outcome.get('entry_touched'),
                    'hit_target': outcome.get('hit_target'),
                    'intrabar_target_reached': outcome.get('intrabar_target_reached'),
                    'minutes_to_entry': outcome.get('minutes_to_entry'),
                    'minutes_to_target': outcome.get('minutes_to_target'),
                    'distance_to_entry_pct': ((surface_row.get('metrics') or {}) or {}).get('distance_to_entry_pct'),
                    'total_score': surface_row.get('total_score'),
                }
                review, svg = _review_row(
                    review_input,
                    candidate=surface_row,
                    bars_frame=bars_lookup.get((trading_day, symbol), pd.DataFrame(columns=['timestamp_utc', 'open', 'high', 'low', 'close', 'volume'])),
                    settings=settings,
                )
                review['source_surface'] = 'actual_surfaced_best_checkpoint_multisession'
                review['focus_scan_id'] = surface_row.get('focus_scan_id')
                review_rows.append(review)
                review_svgs[review['chart_path']] = svg.encode('utf-8')
                selected_intraday_rows.extend(
                    row for row in intraday_rows if str(row.get('trading_day') or '') == trading_day and str(row.get('symbol') or '') == symbol
                )

    verdict_counts: dict[str, int] = {}
    for review in review_rows:
        verdict = str(review.get('visual_review_verdict') or 'unknown')
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1

    summary = {
        'generated_at_utc': datetime.now(UTC).isoformat(),
        'app_version': VERSION,
        'bundle_type': 'surfaced_multisession_visual_review_pack',
        'focus_offset_minutes': focus_offset_minutes or None,
        'days_reviewed': selected_days,
        'selected_review_count': len(review_rows),
        'stage2_candidates_considered_total': total_stage2_candidates,
        'visual_review_verdict_counts': verdict_counts,
        'decision_rule': (
            'This pack reality-checks the actual surfaced stage-2 names across recent sessions at the best live checkpoint so the app can judge whether the surfaced path itself is thesis-valid over more than one day. '
            'It is advisory evidence only and does not change live behavior.'
        ),
        'rows': review_rows,
    }
    report_lines = [
        '# Surfaced multisession visual review',
        '',
        f"Generated at: {summary['generated_at_utc']}",
        f'App version: {VERSION}',
        f"Focus checkpoint: {focus_offset_minutes or 'none'}m",
        f"Days reviewed: {', '.join(selected_days) if selected_days else 'none'}",
        f"Stage-2 candidates considered total: {total_stage2_candidates}",
        f"Selected review rows: {len(review_rows)}",
        '',
        '## Visual verdict counts',
    ]
    for verdict, count in sorted(verdict_counts.items()):
        report_lines.append(f'- {verdict}: {count}')
    report_lines.extend(['', '## Reviewed symbols'])
    for row in review_rows:
        report_lines.append(
            f"- {row['trading_day']} {row['scan_offset_minutes']}m {row['symbol']}: {row['visual_review_verdict']} · {row['visual_review_reason']} (chart={row['chart_path']})"
        )
    html_rows = []
    for row in review_rows:
        html_rows.append(
            f"<tr><td>{row['trading_day']}</td><td>{row['scan_offset_minutes']}</td><td>{row['symbol']}</td><td>{row['visual_review_verdict']}</td><td>{row['visual_review_reason']}</td><td><a href=\"{row['chart_path']}\">chart</a></td></tr>"
        )
    review_html = (
        '<html><head><meta charset="utf-8"><title>Surfaced multisession visual review</title></head><body>'
        f'<h1>Surfaced multisession visual review · {focus_offset_minutes or "—"}m</h1>'
        f'<p>Stage-2 candidates considered total: {total_stage2_candidates}</p>'
        '<table border="1" cellspacing="0" cellpadding="6">'
        '<thead><tr><th>Day</th><th>Offset</th><th>Symbol</th><th>Visual verdict</th><th>Reason</th><th>Chart</th></tr></thead>'
        f'<tbody>{"".join(html_rows)}</tbody></table></body></html>'
    )
    manifest = {
        'bundle_type': 'surfaced_multisession_visual_review_pack',
        'bundle_contract_version': '1.0',
        'app_version': VERSION,
        'generated_at_utc': summary['generated_at_utc'],
        'days_requested': int(days),
        'offsets_requested': list(requested_offsets),
        'focus_offset_minutes': focus_offset_minutes,
        'selected_review_count': len(review_rows),
    }
    pack = {
        'MANIFEST.json': json.dumps(manifest, indent=2).encode('utf-8'),
        'surfaced_multisession_visual_review_summary.json': json.dumps(summary, indent=2).encode('utf-8'),
        'surfaced_multisession_visual_review_rows.csv': _rows_to_csv(review_rows).encode('utf-8'),
        'surfaced_multisession_visual_review_intraday_bars.csv': _rows_to_csv(selected_intraday_rows).encode('utf-8'),
        'surfaced_multisession_visual_review.html': review_html.encode('utf-8'),
        'report.md': '\n'.join(report_lines).encode('utf-8'),
        'contract_health.json': _json_bytes(build_contract_health(repos.db)),
        'settings_snapshot.json': _json_bytes(settings.public_snapshot()),
    }
    pack.update(review_svgs)
    return pack
