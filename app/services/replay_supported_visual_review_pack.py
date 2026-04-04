from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.config import Settings
from app.db import Database
from app.repositories import RepositoryBundle, ensure_repository_bundle
from app.services.diagnostics import build_contract_health
from app.services.evidence_pack import _json_bytes, _rows_to_csv
from app.services.historical_replay_shadow_pack import read_cached_historical_replay_summary
from app.services.replay_bottleneck_pack import build_replay_bottleneck_pack
from app.services.shadow_promotion_pack import build_shadow_promotion_pack
from app.services.shadow_visual_review_pack import (
    _bars_by_symbol_day,
    _candidate_lookup_from_recent_scans,
    _read_csv_bytes,
    _read_json_bytes,
    _review_row,
)
from app.version import VERSION

UTC = timezone.utc
DEFAULT_REVIEW_LIMIT = 6


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


def _resolve_focus(replay_summary: dict[str, Any], bottleneck_summary: dict[str, Any], requested_offsets: list[int]) -> tuple[str, int, float | None, float | None]:
    profile = str(((replay_summary.get('recommended_profile') or {}) or {}).get('profile_name') or '')
    support_threshold = _to_float(bottleneck_summary.get('tradeable_share_support_threshold'))
    best_offset = dict(bottleneck_summary.get('best_offset_by_tradeable_share') or {})
    offset_minutes = _to_int(best_offset.get('scan_offset_minutes'))
    offset_share = _to_float(best_offset.get('tradeable_share'))
    if offset_minutes <= 0 and requested_offsets:
        offset_minutes = int(requested_offsets[0])
    return profile, offset_minutes, offset_share, support_threshold


def _select_review_rows(
    shadow_rows: list[dict[str, Any]],
    profile_rows: list[dict[str, Any]],
    *,
    profile_name: str,
    offset_minutes: int,
    limit: int,
) -> list[dict[str, Any]]:
    flagged_keys = {
        (
            str(row.get('trading_day') or ''),
            _to_int(row.get('scan_offset_minutes')),
            str(row.get('symbol') or ''),
            str(row.get('verdict_bucket') or ''),
        )
        for row in profile_rows
        if str(row.get('profile_name') or '') == profile_name
        and _to_int(row.get('scan_offset_minutes')) == offset_minutes
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
        dict(row)
        for row in shadow_rows
        if (
            str(row.get('trading_day') or ''),
            _to_int(row.get('scan_offset_minutes')),
            str(row.get('symbol') or ''),
            str(row.get('verdict_bucket') or ''),
        ) in flagged_keys
    ]
    selected.sort(
        key=lambda row: (
            verdict_priority.get(str(row.get('verdict_bucket') or ''), 0),
            _to_float(row.get('total_score')) or -999.0,
            str(row.get('symbol') or ''),
        ),
        reverse=True,
    )
    return selected[: max(int(limit), 0)]


def build_replay_supported_visual_review_pack(
    settings: Settings,
    db: Database | RepositoryBundle,
    alpaca,
    *,
    days: int = 10,
    offsets: list[int] | None = None,
    lookback_days: int = 90,
    review_limit: int = DEFAULT_REVIEW_LIMIT,
) -> dict[str, bytes]:
    repos = ensure_repository_bundle(db)
    requested_offsets = sorted({int(value) for value in (offsets or [120, 150]) if int(value) > 0}) or [120, 150]

    replay_summary = dict(read_cached_historical_replay_summary(settings) or {})
    bottleneck_pack = build_replay_bottleneck_pack(
        settings,
        repos,
        alpaca,
        lookback_days=lookback_days,
        offsets=requested_offsets,
    ) if replay_summary else {}
    bottleneck_summary = _read_json_bytes(bottleneck_pack.get('replay_bottleneck_summary.json', b''))
    focus_profile_name, focus_offset_minutes, focus_offset_tradeable_share, support_threshold = _resolve_focus(
        replay_summary,
        bottleneck_summary,
        requested_offsets,
    )

    shadow_pack = build_shadow_promotion_pack(settings, repos, alpaca, days=days, offsets=requested_offsets)
    shadow_rows = _read_csv_bytes(shadow_pack.get('overstrictness_shadow_rows.csv', b''))
    profile_rows = _read_csv_bytes(shadow_pack.get('shadow_threshold_profile_rows.csv', b''))
    intraday_rows = _read_csv_bytes(shadow_pack.get('overstrictness_intraday_bars.csv', b''))

    selected_rows = _select_review_rows(
        shadow_rows,
        profile_rows,
        profile_name=focus_profile_name,
        offset_minutes=focus_offset_minutes,
        limit=review_limit,
    ) if focus_profile_name and focus_offset_minutes > 0 else []

    candidate_lookup = _candidate_lookup_from_recent_scans(repos, days=days, offsets=requested_offsets) if selected_rows else {}
    bars_lookup = _bars_by_symbol_day(intraday_rows) if selected_rows else {}

    review_rows: list[dict[str, Any]] = []
    review_svgs: dict[str, bytes] = {}
    selected_intraday_rows: list[dict[str, Any]] = []

    for row in selected_rows:
        row = dict(row)
        row['selected_profile_name'] = focus_profile_name
        key = (str(row.get('trading_day') or ''), _to_int(row.get('scan_offset_minutes')), str(row.get('symbol') or ''))
        candidate = candidate_lookup.get(key, {})
        bars_frame = bars_lookup.get((str(row.get('trading_day') or ''), str(row.get('symbol') or '')))
        review, svg = _review_row(row, candidate=candidate, bars_frame=bars_frame, settings=settings)
        review['replay_supported_profile_name'] = focus_profile_name
        review['replay_supported_offset_minutes'] = focus_offset_minutes
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
        'bundle_type': 'replay_supported_visual_review_pack',
        'source_replay_generated_at_utc': replay_summary.get('generated_at_utc'),
        'source_replay_app_version': replay_summary.get('app_version'),
        'source_bottleneck_generated_at_utc': bottleneck_summary.get('generated_at_utc'),
        'source_bottleneck_app_version': bottleneck_summary.get('app_version'),
        'focus_profile_name': focus_profile_name or None,
        'focus_offset_minutes': focus_offset_minutes or None,
        'focus_offset_tradeable_share': focus_offset_tradeable_share,
        'tradeable_share_support_threshold': support_threshold,
        'selected_review_count': len(review_rows),
        'visual_review_verdict_counts': verdict_counts,
        'decision_rule': (
            'This pack automates the thesis-fidelity check for the surviving replay-supported path by rendering recent '
            'live-shaped rows that would be admitted by the replay-supported profile at its supported checkpoint. '
            'It is advisory evidence only and does not change live behavior.'
        ),
        'rows': review_rows,
    }

    report_lines = [
        '# Replay-supported visual review',
        '',
        f"Generated at: {summary['generated_at_utc']}",
        f"App version: {VERSION}",
        f"Replay-supported profile: {focus_profile_name or 'none'}",
        f"Replay-supported checkpoint: {focus_offset_minutes or 'none'}",
        f"Replay-supported tradeable share: {focus_offset_tradeable_share}",
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
        '<html><head><meta charset="utf-8"><title>Replay-supported visual review</title></head><body>'
        f'<h1>Replay-supported visual review · {focus_profile_name or "none"} · {focus_offset_minutes or "—"}m</h1>'
        f'<p>Replay-supported tradeable share: {focus_offset_tradeable_share}</p>'
        '<table border="1" cellspacing="0" cellpadding="6">'
        '<thead><tr><th>Day</th><th>Offset</th><th>Symbol</th><th>Outcome bucket</th><th>Visual verdict</th><th>Reason</th><th>Chart</th></tr></thead>'
        f'<tbody>{"".join(html_rows)}</tbody></table></body></html>'
    )

    manifest = {
        'bundle_type': 'replay_supported_visual_review_pack',
        'bundle_contract_version': '1.0',
        'app_version': VERSION,
        'generated_at_utc': summary['generated_at_utc'],
        'days_requested': int(days),
        'lookback_days_requested': int(lookback_days),
        'offsets_requested': list(requested_offsets),
        'focus_profile_name': focus_profile_name,
        'focus_offset_minutes': focus_offset_minutes,
        'selected_review_count': len(review_rows),
    }

    pack = dict(shadow_pack)
    pack.update(dict(bottleneck_pack))
    pack.update(
        {
            'MANIFEST.json': json.dumps(manifest, indent=2).encode('utf-8'),
            'replay_supported_visual_review_summary.json': json.dumps(summary, indent=2).encode('utf-8'),
            'replay_supported_visual_review_rows.csv': _rows_to_csv(review_rows).encode('utf-8'),
            'replay_supported_visual_review_intraday_bars.csv': _rows_to_csv(selected_intraday_rows).encode('utf-8'),
            'replay_supported_visual_review.html': review_html.encode('utf-8'),
            'report.md': '\n'.join(report_lines).encode('utf-8'),
            'report.md': '\n'.join(report_lines).encode('utf-8'),
            'contract_health.json': _json_bytes(build_contract_health(repos.db)),
            'settings_snapshot.json': _json_bytes(settings.public_snapshot()),
        }
    )
    pack.update(review_svgs)
    return pack
