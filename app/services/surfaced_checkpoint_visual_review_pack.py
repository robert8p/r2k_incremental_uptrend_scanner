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
from app.services.outcome_adjudication_pack import (
    _adjudication_bucket,
    _find_entry_touch_local,
    _future_after_timestamp,
    _intrabar_target_reached,
    _post_entry_outcome_local,
    _trade_deadline_utc,
)
from app.services.shadow_visual_review_pack import _bars_by_symbol_day, _review_row
from app.services.stage2_regression_pack import _candidate_maps, _select_recent_scans
from app.version import VERSION

UTC = timezone.utc
DEFAULT_REVIEW_LIMIT = 6
DEFAULT_OFFSETS = [120, 150]


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


def _empty_bars_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=['timestamp_utc', 'open', 'high', 'low', 'close', 'volume'])


def _candidate_rows_for_best_checkpoint(
    surface: dict[str, Any],
    *,
    review_limit: int,
) -> tuple[str, int, list[dict[str, Any]]]:
    summary = dict(surface.get('summary') or {})
    selected_day = str(summary.get('selected_day') or '')
    best_offset = _to_int(summary.get('best_checkpoint_offset_minutes'))
    rows = [
        dict(row)
        for row in (surface.get('best_candidates') or [])
        if _to_int(row.get('best_advanced_offset_minutes')) == best_offset
    ]
    rows.sort(
        key=lambda row: (
            -(_to_float(row.get('best_total_score')) or 0.0),
            str(row.get('symbol') or ''),
        )
    )
    return selected_day, best_offset, rows[: max(int(review_limit), 0)]


def _candidate_lookup_for_day_offset(
    repos: RepositoryBundle,
    *,
    trading_day: str,
    offset_minutes: int,
    lookback_days: int,
) -> tuple[dict[str, dict[str, Any]], int]:
    _, chosen_scans = _select_recent_scans(repos, days=max(int(lookback_days), 1), offsets=[offset_minutes])
    scan = chosen_scans.get((str(trading_day), int(offset_minutes))) or {}
    scan_id = _to_int(scan.get('id'))
    if scan_id <= 0:
        return {}, 0
    return _candidate_maps(repos, scan_id), scan_id


def _outcome_snapshot(
    *,
    full_day_bars: pd.DataFrame,
    trading_day: str,
    offset_minutes: int,
    candidate: dict[str, Any],
    settings: Settings,
) -> dict[str, Any]:
    empty = {
        'entry_touched': False,
        'hit_target': False,
        'intrabar_target_reached': False,
        'minutes_to_entry': None,
        'minutes_to_target': None,
        'verdict_bucket': 'advanced_but_entry_never_touched',
        'verdict_reason': 'Advanced to stage 2, but the preferred entry zone was never touched after the checkpoint.',
    }
    if full_day_bars.empty:
        return empty
    entry_low = _to_float(candidate.get('entry_low'))
    entry_high = _to_float(candidate.get('entry_high'))
    if entry_low is None or entry_high is None:
        return empty
    _, _, checkpoint_dt = _session_bounds_for_regular_day(trading_day, int(offset_minutes))
    checkpoint_ts = pd.Timestamp(checkpoint_dt)
    deadline_ts = pd.Timestamp(_trade_deadline_utc(trading_day, int(offset_minutes), settings))
    entry = _find_entry_touch_local(full_day_bars, checkpoint_ts, entry_low, entry_high, deadline_ts, settings)
    if entry.get('entry_touched') and entry.get('entry_timestamp') is not None and entry.get('entry_price') is not None:
        outcome = _post_entry_outcome_local(
            full_day_bars,
            pd.Timestamp(entry['entry_timestamp']),
            float(entry['entry_price']),
            float(settings.target_pct),
            deadline_ts,
            settings,
            0.0,
        )
        future_after_entry = _future_after_timestamp(full_day_bars, pd.Timestamp(entry['entry_timestamp']), deadline_ts)
        intrabar = _intrabar_target_reached(future_after_entry, float(entry['entry_price']), float(settings.target_pct))
    else:
        outcome = {
            'hit_target': False,
            'minutes_to_target': None,
        }
        intrabar = False
    verdict_bucket, verdict_reason = _adjudication_bucket(
        advanced=True,
        classification_code=str(((candidate.get('metrics') or {}) or {}).get('range_classification_code') or ''),
        entry_touched=bool(entry.get('entry_touched')),
        hit_target=bool(outcome.get('hit_target')),
        intrabar_target_reached=bool(intrabar),
    )
    return {
        'entry_touched': bool(entry.get('entry_touched')),
        'hit_target': bool(outcome.get('hit_target')),
        'intrabar_target_reached': bool(intrabar),
        'minutes_to_entry': entry.get('minutes_to_entry'),
        'minutes_to_target': outcome.get('minutes_to_target'),
        'verdict_bucket': verdict_bucket,
        'verdict_reason': verdict_reason,
    }


def build_surfaced_checkpoint_visual_review_pack(
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
    selected_day, focus_offset_minutes, selected_surface_rows = _candidate_rows_for_best_checkpoint(surface, review_limit=review_limit)
    stage2_candidate_lookup, focus_scan_id = _candidate_lookup_for_day_offset(
        repos,
        trading_day=selected_day,
        offset_minutes=focus_offset_minutes,
        lookback_days=max(int(days), 1),
    ) if selected_day and focus_offset_minutes > 0 else ({}, 0)

    focus_symbols = [str(row.get('symbol') or '') for row in selected_surface_rows if str(row.get('symbol') or '')]
    intraday_rows: list[dict[str, Any]] = []
    bars_lookup: dict[tuple[str, str], pd.DataFrame] = {}
    full_bars_lookup: dict[str, pd.DataFrame] = {}
    if selected_day and focus_symbols and getattr(alpaca, 'has_credentials', lambda: False)():
        market_open, market_close, _ = _session_bounds_for_regular_day(selected_day, focus_offset_minutes)
        bars_map = alpaca.fetch_bars(sorted(set(focus_symbols)), '1Min', _iso_z(market_open), _iso_z(market_close))
        intraday_rows = _intraday_bar_rows(bars_map, trading_day=selected_day, offset_minutes=[focus_offset_minutes])
        bars_lookup = _bars_by_symbol_day(intraday_rows)
        full_bars_lookup = {str(symbol): _normalise_timestamp_column(frame) for symbol, frame in (bars_map or {}).items()}

    review_rows: list[dict[str, Any]] = []
    review_svgs: dict[str, bytes] = {}
    selected_intraday_rows: list[dict[str, Any]] = []

    for surface_row in selected_surface_rows:
        symbol = str(surface_row.get('symbol') or '')
        candidate = dict(stage2_candidate_lookup.get(symbol) or {})
        full_bars = full_bars_lookup.get(symbol)
        if full_bars is None:
            full_bars = pd.DataFrame(columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        outcome = _outcome_snapshot(
            full_day_bars=full_bars,
            trading_day=selected_day,
            offset_minutes=focus_offset_minutes,
            candidate=candidate,
            settings=settings,
        )
        review_input = {
            'trading_day': selected_day,
            'scan_offset_minutes': focus_offset_minutes,
            'symbol': symbol,
            'company_name': surface_row.get('company_name') or candidate.get('company_name'),
            'selected_profile_name': 'actual_surfaced_stage2',
            'verdict_bucket': outcome.get('verdict_bucket'),
            'verdict_reason': outcome.get('verdict_reason'),
            'entry_touched': outcome.get('entry_touched'),
            'hit_target': outcome.get('hit_target'),
            'intrabar_target_reached': outcome.get('intrabar_target_reached'),
            'minutes_to_entry': outcome.get('minutes_to_entry'),
            'minutes_to_target': outcome.get('minutes_to_target'),
            'distance_to_entry_pct': ((candidate.get('metrics') or {}) or {}).get('distance_to_entry_pct'),
        }
        review, svg = _review_row(
            review_input,
            candidate=candidate,
            bars_frame=bars_lookup.get((selected_day, symbol), _empty_bars_frame()),
            settings=settings,
        )
        review['source_surface'] = 'actual_surfaced_best_checkpoint'
        review['focus_scan_id'] = focus_scan_id or None
        review_rows.append(review)
        review_svgs[review['chart_path']] = svg.encode('utf-8')
        selected_intraday_rows.extend(
            row for row in intraday_rows if str(row.get('trading_day') or '') == selected_day and str(row.get('symbol') or '') == symbol
        )

    verdict_counts: dict[str, int] = {}
    for review in review_rows:
        verdict = str(review.get('visual_review_verdict') or 'unknown')
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1

    summary = {
        'generated_at_utc': datetime.now(UTC).isoformat(),
        'app_version': VERSION,
        'bundle_type': 'surfaced_checkpoint_visual_review_pack',
        'focus_trading_day': selected_day or None,
        'focus_offset_minutes': focus_offset_minutes or None,
        'focus_scan_id': focus_scan_id or None,
        'selected_review_count': len(review_rows),
        'stage2_candidates_at_focus_checkpoint': len(selected_surface_rows),
        'visual_review_verdict_counts': verdict_counts,
        'decision_rule': (
            'This pack reality-checks the actual surfaced stage-2 names at the best live checkpoint so the app can judge '
            'product-truth on the currently surfaced path rather than only on hypothetical rescue branches. '
            'It is advisory evidence only and does not change live behavior.'
        ),
        'rows': review_rows,
    }

    report_lines = [
        '# Surfaced checkpoint visual review',
        '',
        f"Generated at: {summary['generated_at_utc']}",
        f'App version: {VERSION}',
        f"Focus trading day: {selected_day or 'none'}",
        f"Focus checkpoint: {focus_offset_minutes or 'none'}m",
        f"Stage-2 candidates at focus checkpoint: {len(selected_surface_rows)}",
        f"Selected review rows: {len(review_rows)}",
        '',
        '## Visual verdict counts',
    ]
    for verdict, count in sorted(verdict_counts.items()):
        report_lines.append(f'- {verdict}: {count}')
    report_lines.extend(['', '## Reviewed symbols'])
    for row in review_rows:
        report_lines.append(
            f"- {row['trading_day']} {row['scan_offset_minutes']}m {row['symbol']}: "
            f"{row['visual_review_verdict']} · {row['visual_review_reason']} "
            f"(verdict_bucket={row['verdict_bucket']}, chart={row['chart_path']})"
        )

    html_rows = []
    for row in review_rows:
        html_rows.append(
            f"<tr><td>{row['trading_day']}</td><td>{row['scan_offset_minutes']}</td><td>{row['symbol']}</td>"
            f"<td>{row['verdict_bucket']}</td><td>{row['visual_review_verdict']}</td>"
            f"<td>{row['visual_review_reason']}</td><td><a href=\"{row['chart_path']}\">chart</a></td></tr>"
        )
    review_html = (
        '<html><head><meta charset="utf-8"><title>Surfaced checkpoint visual review</title></head><body>'
        f'<h1>Surfaced checkpoint visual review · {selected_day or "none"} · {focus_offset_minutes or "—"}m</h1>'
        f'<p>Stage-2 candidates at focus checkpoint: {len(selected_surface_rows)}</p>'
        '<table border="1" cellspacing="0" cellpadding="6">'
        '<thead><tr><th>Day</th><th>Offset</th><th>Symbol</th><th>Outcome bucket</th><th>Visual verdict</th><th>Reason</th><th>Chart</th></tr></thead>'
        f'<tbody>{"".join(html_rows)}</tbody></table></body></html>'
    )

    manifest = {
        'bundle_type': 'surfaced_checkpoint_visual_review_pack',
        'bundle_contract_version': '1.0',
        'app_version': VERSION,
        'generated_at_utc': summary['generated_at_utc'],
        'days_requested': int(days),
        'offsets_requested': list(requested_offsets),
        'focus_trading_day': selected_day,
        'focus_offset_minutes': focus_offset_minutes,
        'selected_review_count': len(review_rows),
    }
    pack = {
        'MANIFEST.json': json.dumps(manifest, indent=2).encode('utf-8'),
        'surfaced_checkpoint_visual_review_summary.json': json.dumps(summary, indent=2).encode('utf-8'),
        'surfaced_checkpoint_visual_review_rows.csv': _rows_to_csv(review_rows).encode('utf-8'),
        'surfaced_checkpoint_visual_review_intraday_bars.csv': _rows_to_csv(selected_intraday_rows).encode('utf-8'),
        'surfaced_checkpoint_visual_review.html': review_html.encode('utf-8'),
        'checkpoint_decision_summary.json': json.dumps(dict(surface.get('summary') or {}), indent=2).encode('utf-8'),
        'checkpoint_decision_scan_rows.csv': _rows_to_csv(surface.get('scan_rows') or []).encode('utf-8'),
        'checkpoint_decision_best_candidates.csv': _rows_to_csv(surface.get('best_candidates') or []).encode('utf-8'),
        'report.md': '\n'.join(report_lines).encode('utf-8'),
        'contract_health.json': _json_bytes(build_contract_health(repos.db)),
        'settings_snapshot.json': _json_bytes(settings.public_snapshot()),
    }
    pack.update(review_svgs)
    return pack
