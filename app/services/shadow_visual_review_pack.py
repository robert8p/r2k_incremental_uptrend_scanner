from __future__ import annotations

import csv
import json
import math
from datetime import datetime, timezone
from io import StringIO
from typing import Any

import pandas as pd

from app.config import Settings
from app.db import Database
from app.repositories import RepositoryBundle, ensure_repository_bundle
from app.services.classifier_audit_pack import _session_bounds_for_regular_day
from app.services.diagnostics import build_contract_health
from app.services.evidence_pack import _json_bytes, _rows_to_csv
from app.services.outcome_adjudication_pack import _trade_deadline_utc
from app.services.shadow_promotion_pack import build_shadow_promotion_pack
from app.services.stage2_regression_pack import _candidate_maps, _select_recent_scans
from app.version import VERSION

UTC = timezone.utc
DEFAULT_REVIEW_LIMIT = 10
PRECHECK_LOOKBACK_MINUTES = 60
SVG_WIDTH = 920
SVG_HEIGHT = 340


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


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {'1', 'true', 'yes', 'y'}


def _candidate_lookup_from_recent_scans(
    repos: RepositoryBundle,
    *,
    days: int,
    offsets: list[int],
) -> dict[tuple[str, int, str], dict[str, Any]]:
    _, chosen_scans = _select_recent_scans(repos, days=days, offsets=offsets)
    lookup: dict[tuple[str, int, str], dict[str, Any]] = {}
    for (trading_day, offset), scan in chosen_scans.items():
        scan_id = int(scan.get('id') or 0)
        if scan_id <= 0:
            continue
        for symbol, candidate in _candidate_maps(repos, scan_id).items():
            if symbol:
                lookup[(str(trading_day), int(offset), str(symbol))] = candidate
    return lookup


def _bars_by_symbol_day(rows: list[dict[str, Any]]) -> dict[tuple[str, str], pd.DataFrame]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        trading_day = str(row.get('trading_day') or '')
        symbol = str(row.get('symbol') or '')
        if not trading_day or not symbol:
            continue
        grouped.setdefault((trading_day, symbol), []).append(row)

    output: dict[tuple[str, str], pd.DataFrame] = {}
    for key, values in grouped.items():
        frame = pd.DataFrame(values)
        if frame.empty:
            continue
        frame['timestamp_utc'] = pd.to_datetime(frame['timestamp_utc'], utc=True, errors='coerce')
        frame = frame[frame['timestamp_utc'].notna()].copy()
        if frame.empty:
            continue
        for column in ['open', 'high', 'low', 'close', 'volume']:
            frame[column] = pd.to_numeric(frame[column], errors='coerce')
        output[key] = frame.sort_values('timestamp_utc').reset_index(drop=True)
    return output


def _selected_review_rows(
    promotion_summary: dict[str, Any],
    shadow_rows: list[dict[str, Any]],
    profile_rows: list[dict[str, Any]],
    *,
    limit: int,
) -> tuple[str, list[dict[str, Any]]]:
    recommended_profile = str(((promotion_summary.get('recommended_profile') or {}).get('profile_name')) or '')
    if not recommended_profile and profile_rows:
        recommended_profile = str(profile_rows[0].get('profile_name') or '')

    flagged_keys = {
        (
            str(row.get('trading_day') or ''),
            _to_int(row.get('scan_offset_minutes')),
            str(row.get('symbol') or ''),
            str(row.get('verdict_bucket') or ''),
        )
        for row in profile_rows
        if str(row.get('profile_name') or '') == recommended_profile
        and _truthy(row.get('would_pass_shadow_profile_excluding_classifier_veto'))
    }
    verdict_priority = {
        'advanced_and_tradeable': 5,
        'possible_classifier_overstrict': 4,
        'advanced_but_entry_never_touched': 3,
        'advanced_but_not_tradeable': 2,
        'classifier_correct_reject': 1,
    }
    selected = [
        row
        for row in shadow_rows
        if (
            str(row.get('trading_day') or ''),
            _to_int(row.get('scan_offset_minutes')),
            str(row.get('symbol') or ''),
            str(row.get('verdict_bucket') or ''),
        )
        in flagged_keys
    ]
    selected.sort(
        key=lambda row: (
            verdict_priority.get(str(row.get('verdict_bucket') or ''), 0),
            _to_int(row.get('scan_offset_minutes')) * -1,
            _to_float(row.get('total_score')) or -999.0,
            str(row.get('symbol') or ''),
        ),
        reverse=True,
    )
    return recommended_profile, selected[: max(int(limit), 0)]


def _window_bars(frame: pd.DataFrame, *, trading_day: str, offset_minutes: int, settings: Settings) -> tuple[pd.DataFrame, pd.Timestamp, pd.Timestamp, pd.Timestamp]:
    _, _, checkpoint_dt = _session_bounds_for_regular_day(trading_day, int(offset_minutes))
    checkpoint_ts = pd.Timestamp(checkpoint_dt)
    deadline_ts = pd.Timestamp(_trade_deadline_utc(trading_day, int(offset_minutes), settings))
    window_start = checkpoint_ts - pd.Timedelta(minutes=PRECHECK_LOOKBACK_MINUTES)
    bars = frame[(frame['timestamp_utc'] >= window_start) & (frame['timestamp_utc'] <= deadline_ts)].copy()
    return bars.reset_index(drop=True), checkpoint_ts, deadline_ts, window_start


def _count_direction_changes(closes: pd.Series) -> int:
    diffs = closes.diff().fillna(0.0)
    signs = diffs.apply(lambda value: 1 if value > 0 else (-1 if value < 0 else 0))
    non_zero = [int(value) for value in signs.tolist() if int(value) != 0]
    if len(non_zero) < 2:
        return 0
    return sum(1 for left, right in zip(non_zero, non_zero[1:]) if left != right)


def _line_efficiency(closes: pd.Series) -> float:
    if closes.empty:
        return 1.0
    total_path = float(closes.diff().abs().fillna(0.0).sum())
    net_move = abs(float(closes.iloc[-1]) - float(closes.iloc[0]))
    if total_path <= 1e-9:
        return 1.0
    return round(net_move / total_path, 4)


def _overlap_count(frame: pd.DataFrame, low: float | None, high: float | None) -> int:
    if low is None or high is None or frame.empty:
        return 0
    zone_low = min(float(low), float(high))
    zone_high = max(float(low), float(high))
    overlap = (frame['low'] <= zone_high) & (frame['high'] >= zone_low)
    return int(overlap.sum())


def _band_containment_share(frame: pd.DataFrame, band_low: float | None, band_high: float | None) -> float | None:
    if band_low is None or band_high is None or frame.empty:
        return None
    low = min(float(band_low), float(band_high))
    high = max(float(band_low), float(band_high))
    inside = frame['close'].between(low, high, inclusive='both')
    return round(float(inside.mean()), 4)


def _band_breakout_share(frame: pd.DataFrame, band_low: float | None, band_high: float | None) -> float | None:
    if band_low is None or band_high is None or frame.empty:
        return None
    low = min(float(band_low), float(band_high))
    high = max(float(band_low), float(band_high))
    outside = (frame['close'] < low) | (frame['close'] > high)
    return round(float(outside.mean()), 4)


def _visual_verdict(
    review_row: dict[str, Any],
    *,
    postscan_bars: pd.DataFrame,
    candidate: dict[str, Any],
) -> tuple[str, str, dict[str, Any]]:
    metrics = dict(candidate.get('metrics') or {})
    chart_context = dict(candidate.get('chart_context') or {})
    entry_low = _to_float(candidate.get('entry_low'))
    entry_high = _to_float(candidate.get('entry_high'))
    band_low = _to_float(metrics.get('range_band_low'))
    band_high = _to_float(metrics.get('range_band_high'))
    if band_low is None:
        band_low = _to_float(chart_context.get('band_low'))
    if band_high is None:
        band_high = _to_float(chart_context.get('band_high'))

    direction_changes = _count_direction_changes(postscan_bars['close']) if not postscan_bars.empty else 0
    line_efficiency = _line_efficiency(postscan_bars['close']) if not postscan_bars.empty else 1.0
    entry_overlap_bars = _overlap_count(postscan_bars, entry_low, entry_high)
    containment_share = _band_containment_share(postscan_bars, band_low, band_high)
    breakout_share = _band_breakout_share(postscan_bars, band_low, band_high)

    entry_touched = _truthy(review_row.get('entry_touched'))
    hit_target = _truthy(review_row.get('hit_target')) or _truthy(review_row.get('intrabar_target_reached'))
    distance_to_entry_pct = _to_float(review_row.get('distance_to_entry_pct'))

    features = {
        'postscan_direction_changes': direction_changes,
        'postscan_line_efficiency': line_efficiency,
        'postscan_entry_overlap_bars': entry_overlap_bars,
        'postscan_band_containment_share': containment_share,
        'postscan_band_breakout_share': breakout_share,
        'entry_touched': entry_touched,
        'hit_target_or_intrabar': hit_target,
        'distance_to_entry_pct': distance_to_entry_pct,
    }

    if entry_touched and hit_target and (breakout_share is None or breakout_share <= 0.35) and line_efficiency <= 0.72 and direction_changes >= 2:
        return (
            'visually_supportive_range',
            'Entry was revisited, target was achieved, and the post-scan path stayed sufficiently two-sided rather than one-way.',
            features,
        )
    if entry_touched and hit_target:
        return (
            'tradeable_but_trend_biased',
            'Entry and target worked, but the post-scan path looked more directional than a clean oscillating range.',
            features,
        )
    if not entry_touched and distance_to_entry_pct is not None and abs(distance_to_entry_pct) >= 2.0:
        return (
            'not_actionable_from_entry_zone',
            'The candidate never reloaded into the preferred entry zone after the checkpoint, so it was not actionable from the intended range entry.',
            features,
        )
    if (breakout_share is not None and breakout_share >= 0.55) or line_efficiency >= 0.88 or direction_changes <= 1:
        return (
            'visually_trend_dominated',
            'The post-scan path looked trend-dominated or breakout-heavy rather than like a repeatable range oscillation.',
            features,
        )
    return (
        'visually_borderline',
        'The post-scan path showed some back-and-forth behavior, but the visual evidence was mixed rather than clearly range-like.',
        features,
    )


def _format_num(value: float | None, digits: int = 2) -> str:
    if value is None:
        return '—'
    return f'{float(value):.{digits}f}'


def _svg_chart(
    bars: pd.DataFrame,
    *,
    trading_day: str,
    symbol: str,
    offset_minutes: int,
    checkpoint_ts: pd.Timestamp,
    deadline_ts: pd.Timestamp,
    review_row: dict[str, Any],
    candidate: dict[str, Any],
    visual_verdict: str,
) -> str:
    if bars.empty:
        return '<svg xmlns="http://www.w3.org/2000/svg" width="920" height="120"><text x="20" y="60">No chart bars available.</text></svg>'

    metrics = dict(candidate.get('metrics') or {})
    chart_context = dict(candidate.get('chart_context') or {})
    entry_low = _to_float(candidate.get('entry_low'))
    entry_high = _to_float(candidate.get('entry_high'))
    target_price = _to_float(candidate.get('target_price'))
    stop_price = _to_float(candidate.get('stop_price'))
    band_low = _to_float(metrics.get('range_band_low'))
    band_high = _to_float(metrics.get('range_band_high'))
    if band_low is None:
        band_low = _to_float(chart_context.get('band_low'))
    if band_high is None:
        band_high = _to_float(chart_context.get('band_high'))

    width = SVG_WIDTH
    height = SVG_HEIGHT
    left = 58
    right = 18
    top = 24
    bottom = 46
    plot_width = width - left - right
    plot_height = height - top - bottom

    min_price = float(bars['low'].min())
    max_price = float(bars['high'].max())
    if band_low is not None:
        min_price = min(min_price, float(band_low))
    if band_high is not None:
        max_price = max(max_price, float(band_high))
    if stop_price is not None:
        min_price = min(min_price, float(stop_price))
    if target_price is not None:
        max_price = max(max_price, float(target_price))
    pad = max((max_price - min_price) * 0.08, max_price * 0.003, 0.02)
    y_min = min_price - pad
    y_max = max_price + pad

    x0 = float(bars['timestamp_utc'].iloc[0].timestamp())
    x1 = float(bars['timestamp_utc'].iloc[-1].timestamp())
    if x1 <= x0:
        x1 = x0 + 60.0

    def x_scale(ts: pd.Timestamp) -> float:
        return left + ((float(ts.timestamp()) - x0) / (x1 - x0)) * plot_width

    def y_scale(price: float) -> float:
        return top + (1.0 - ((float(price) - y_min) / (y_max - y_min))) * plot_height

    points = ' '.join(f"{x_scale(ts):.2f},{y_scale(price):.2f}" for ts, price in zip(bars['timestamp_utc'], bars['close']))
    checkpoint_x = x_scale(checkpoint_ts)
    deadline_x = x_scale(min(deadline_ts, bars['timestamp_utc'].iloc[-1]))

    lines: list[str] = []
    if band_low is not None and band_high is not None:
        y1 = y_scale(band_high)
        y2 = y_scale(band_low)
        lines.append(f'<rect x="{left:.2f}" y="{min(y1,y2):.2f}" width="{plot_width:.2f}" height="{abs(y2-y1):.2f}" fill="#6baed6" opacity="0.08" />')
    if entry_low is not None and entry_high is not None:
        y1 = y_scale(entry_high)
        y2 = y_scale(entry_low)
        lines.append(f'<rect x="{left:.2f}" y="{min(y1,y2):.2f}" width="{plot_width:.2f}" height="{abs(y2-y1):.2f}" fill="#74c476" opacity="0.10" />')
    if target_price is not None:
        y = y_scale(target_price)
        lines.append(f'<line x1="{left}" y1="{y:.2f}" x2="{width-right}" y2="{y:.2f}" stroke="#2ca25f" stroke-dasharray="6 4" stroke-width="1.4" />')
    if stop_price is not None:
        y = y_scale(stop_price)
        lines.append(f'<line x1="{left}" y1="{y:.2f}" x2="{width-right}" y2="{y:.2f}" stroke="#de2d26" stroke-dasharray="6 4" stroke-width="1.4" />')

    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<style>.title{font:600 16px sans-serif}.meta{font:12px sans-serif;fill:#444}.axis{font:11px sans-serif;fill:#555}.small{font:10px sans-serif;fill:#666}</style>',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="white" />',
        f'<text class="title" x="{left}" y="18">{symbol} · {trading_day} · {offset_minutes}m · {visual_verdict}</text>',
        f'<text class="meta" x="{left}" y="{height-12}">checkpoint={checkpoint_ts.isoformat()} · deadline={deadline_ts.isoformat()} · verdict={review_row.get("verdict_bucket")}</text>',
        f'<rect x="{checkpoint_x:.2f}" y="{top}" width="{max(deadline_x-checkpoint_x,1):.2f}" height="{plot_height:.2f}" fill="#fdd0a2" opacity="0.16" />',
        f'<rect x="{left}" y="{top}" width="{plot_width}" height="{plot_height}" fill="none" stroke="#c7c7c7" />',
        *lines,
        f'<polyline fill="none" stroke="#1f77b4" stroke-width="1.8" points="{points}" />',
        f'<line x1="{checkpoint_x:.2f}" y1="{top}" x2="{checkpoint_x:.2f}" y2="{top+plot_height}" stroke="#ff7f0e" stroke-width="1.5" />',
        f'<line x1="{deadline_x:.2f}" y1="{top}" x2="{deadline_x:.2f}" y2="{top+plot_height}" stroke="#636363" stroke-width="1.0" stroke-dasharray="4 3" />',
    ]

    for idx, price in enumerate([y_max, (y_max + y_min) / 2.0, y_min]):
        y = y_scale(price)
        svg.append(f'<line x1="{left}" y1="{y:.2f}" x2="{width-right}" y2="{y:.2f}" stroke="#efefef" />')
        svg.append(f'<text class="axis" x="6" y="{y+4:.2f}">{price:.2f}</text>')
    svg.append(f'<text class="small" x="{checkpoint_x + 4:.2f}" y="{top + 12:.2f}">scan</text>')
    svg.append(f'<text class="small" x="{deadline_x + 4:.2f}" y="{top + 12:.2f}">deadline</text>')
    svg.append('</svg>')
    return ''.join(svg)


def _review_row(
    row: dict[str, Any],
    *,
    candidate: dict[str, Any],
    bars_frame: pd.DataFrame,
    settings: Settings,
) -> tuple[dict[str, Any], str]:
    trading_day = str(row.get('trading_day') or '')
    symbol = str(row.get('symbol') or '')
    offset_minutes = _to_int(row.get('scan_offset_minutes'))
    window_bars, checkpoint_ts, deadline_ts, window_start = _window_bars(
        bars_frame,
        trading_day=trading_day,
        offset_minutes=offset_minutes,
        settings=settings,
    )
    postscan = window_bars[window_bars['timestamp_utc'] > checkpoint_ts].copy()
    visual_verdict, visual_reason, features = _visual_verdict(row, postscan_bars=postscan, candidate=candidate)
    chart_name = f'charts/{trading_day}_{offset_minutes}m_{symbol}.svg'
    review = {
        'trading_day': trading_day,
        'scan_offset_minutes': offset_minutes,
        'symbol': symbol,
        'company_name': row.get('company_name'),
        'selected_profile_name': row.get('selected_profile_name'),
        'verdict_bucket': row.get('verdict_bucket'),
        'verdict_reason': row.get('verdict_reason'),
        'visual_review_verdict': visual_verdict,
        'visual_review_reason': visual_reason,
        'entry_touched': _truthy(row.get('entry_touched')),
        'hit_target': _truthy(row.get('hit_target')),
        'intrabar_target_reached': _truthy(row.get('intrabar_target_reached')),
        'minutes_to_entry': _to_float(row.get('minutes_to_entry')),
        'minutes_to_target': _to_float(row.get('minutes_to_target')),
        'distance_to_entry_pct': _to_float(row.get('distance_to_entry_pct')),
        'range_current_location': _to_float(row.get('range_current_location')),
        'total_score': _to_float(row.get('total_score')),
        'window_start_utc': window_start.isoformat(),
        'checkpoint_utc': checkpoint_ts.isoformat(),
        'deadline_utc': deadline_ts.isoformat(),
        'window_bar_count': int(len(window_bars)),
        'postscan_bar_count': int(len(postscan)),
        'chart_path': chart_name,
        **features,
    }
    svg = _svg_chart(
        window_bars,
        trading_day=trading_day,
        symbol=symbol,
        offset_minutes=offset_minutes,
        checkpoint_ts=checkpoint_ts,
        deadline_ts=deadline_ts,
        review_row=row,
        candidate=candidate,
        visual_verdict=visual_verdict,
    )
    return review, svg


def build_shadow_visual_review_pack(
    settings: Settings,
    db: Database | RepositoryBundle,
    alpaca,
    *,
    days: int = 10,
    offsets: list[int] | None = None,
    review_limit: int = DEFAULT_REVIEW_LIMIT,
) -> dict[str, bytes]:
    repos = ensure_repository_bundle(db)
    requested_offsets = sorted({int(value) for value in (offsets or [120, 150]) if int(value) > 0}) or [120, 150]
    base_pack = build_shadow_promotion_pack(settings, repos, alpaca, days=days, offsets=requested_offsets)

    promotion_summary = _read_json_bytes(base_pack.get('shadow_promotion_summary.json', b''))
    shadow_rows = _read_csv_bytes(base_pack.get('overstrictness_shadow_rows.csv', b''))
    profile_rows = _read_csv_bytes(base_pack.get('shadow_threshold_profile_rows.csv', b''))
    intraday_rows = _read_csv_bytes(base_pack.get('overstrictness_intraday_bars.csv', b''))

    selected_profile, selected_rows = _selected_review_rows(promotion_summary, shadow_rows, profile_rows, limit=review_limit)
    candidate_lookup = _candidate_lookup_from_recent_scans(repos, days=days, offsets=requested_offsets)
    bars_lookup = _bars_by_symbol_day(intraday_rows)

    review_rows: list[dict[str, Any]] = []
    review_svgs: dict[str, bytes] = {}
    selected_intraday_rows: list[dict[str, Any]] = []

    for row in selected_rows:
        row = dict(row)
        row['selected_profile_name'] = selected_profile
        key = (str(row.get('trading_day') or ''), _to_int(row.get('scan_offset_minutes')), str(row.get('symbol') or ''))
        candidate = candidate_lookup.get(key, {})
        bars_frame = bars_lookup.get((str(row.get('trading_day') or ''), str(row.get('symbol') or '')), pd.DataFrame())
        review, svg = _review_row(row, candidate=candidate, bars_frame=bars_frame, settings=settings)
        review_rows.append(review)
        review_svgs[review['chart_path']] = svg.encode('utf-8')
        matching_bars = [
            bar for bar in intraday_rows
            if str(bar.get('trading_day') or '') == str(row.get('trading_day') or '')
            and str(bar.get('symbol') or '') == str(row.get('symbol') or '')
        ]
        selected_intraday_rows.extend(matching_bars)

    verdict_counts: dict[str, int] = {}
    for review in review_rows:
        verdict = str(review.get('visual_review_verdict') or 'unknown')
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1

    summary = {
        'generated_at_utc': datetime.now(UTC).isoformat(),
        'app_version': VERSION,
        'best_profile_name': selected_profile,
        'shadow_promotion_readiness': promotion_summary.get('overall_promotion_readiness'),
        'selected_review_count': len(review_rows),
        'visual_review_verdict_counts': verdict_counts,
        'decision_rule': (
            'This pack automates the pre-promotion visual sanity check by rendering the chart window and computing '
            'a lightweight path-shape verdict for names the current best shadow profile would admit. '
            'It is advisory evidence only and does not change live behavior.'
        ),
        'rows': review_rows,
    }

    report_lines = [
        '# Shadow visual review',
        '',
        f"Generated at: {summary['generated_at_utc']}",
        f"App version: {VERSION}",
        f"Best profile: {selected_profile or 'none'}",
        f"Shadow promotion readiness: {promotion_summary.get('overall_promotion_readiness')}",
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
        '<html><head><meta charset="utf-8"><title>Shadow visual review</title></head><body>'
        f'<h1>Shadow visual review · {selected_profile}</h1>'
        f'<p>Promotion readiness: {promotion_summary.get("overall_promotion_readiness")}</p>'
        '<table border="1" cellspacing="0" cellpadding="6">'
        '<thead><tr><th>Day</th><th>Offset</th><th>Symbol</th><th>Outcome bucket</th><th>Visual verdict</th><th>Reason</th><th>Chart</th></tr></thead>'
        f'<tbody>{"".join(html_rows)}</tbody></table></body></html>'
    )

    manifest = {
        'bundle_type': 'shadow_visual_review_pack',
        'bundle_contract_version': '1.0',
        'app_version': VERSION,
        'generated_at_utc': summary['generated_at_utc'],
        'days_requested': int(days),
        'offsets_requested': list(requested_offsets),
        'best_profile_name': selected_profile,
        'selected_review_count': len(review_rows),
    }

    pack = dict(base_pack)
    pack.update(
        {
            'MANIFEST.json': json.dumps(manifest, indent=2).encode('utf-8'),
            'shadow_visual_review_summary.json': json.dumps(summary, indent=2).encode('utf-8'),
            'shadow_visual_review_rows.csv': _rows_to_csv(review_rows).encode('utf-8'),
            'shadow_visual_review_intraday_bars.csv': _rows_to_csv(selected_intraday_rows).encode('utf-8'),
            'shadow_visual_review.html': review_html.encode('utf-8'),
            'report.md': '\n'.join(report_lines).encode('utf-8'),
            'contract_health.json': _json_bytes(build_contract_health(repos.db)),
            'settings_snapshot.json': _json_bytes(settings.public_snapshot()),
        }
    )
    pack.update(review_svgs)
    return pack
