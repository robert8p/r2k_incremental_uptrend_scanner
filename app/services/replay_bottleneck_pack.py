from __future__ import annotations

import csv
import io
import json
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from app.config import Settings
from app.db import Database
from app.repositories import RepositoryBundle
from app.services.historical_replay_shadow_pack import (
    DEFAULT_REPLAY_LOOKBACK_DAYS,
    DEFAULT_REPLAY_OFFSETS,
    get_or_build_historical_replay_shadow_zip,
)
from app.version import VERSION

UTC = timezone.utc
TRADEABLE_SHARE_SUPPORT_THRESHOLD = 0.50


def _read_json_bytes(raw: bytes) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        return dict(json.loads(raw.decode('utf-8')))
    except Exception:
        return {}



def _read_csv_bytes(raw: bytes) -> list[dict[str, Any]]:
    if not raw:
        return []
    try:
        reader = csv.DictReader(io.StringIO(raw.decode('utf-8')))
        return [dict(row) for row in reader]
    except Exception:
        return []



def _csv_bytes(rows: list[dict[str, Any]]) -> bytes:
    if not rows:
        return b''
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode('utf-8')



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



def _read_replay_pack(raw_zip: bytes) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    with zipfile.ZipFile(io.BytesIO(raw_zip), 'r') as zf:
        summary = _read_json_bytes(zf.read('historical_replay_shadow_summary.json'))
        rows = _read_csv_bytes(zf.read('historical_replay_shadow_profile_rows.csv'))
    return summary, rows



def _failure_path(row: dict[str, Any]) -> str:
    tradeable = str(row.get('shadow_tradeable_if_admitted') or '').lower() == 'true'
    entry_touched = str(row.get('entry_touched') or '').lower() == 'true'
    if tradeable:
        return 'tradeable'
    if not entry_touched:
        return 'entry_never_touched'
    return 'entry_touched_no_target'



def _rank_bucket(mover_rank: Any) -> str:
    rank = _to_int(mover_rank)
    if rank <= 0:
        return 'unknown'
    if rank <= 10:
        return '1-10'
    if rank <= 20:
        return '11-20'
    if rank <= 30:
        return '21-30'
    if rank <= 50:
        return '31-50'
    return '51+'



def _metric_summary(rows: list[dict[str, Any]], field: str) -> dict[str, float | None]:
    values = sorted(v for v in (_to_float(row.get(field)) for row in rows) if v is not None)
    if not values:
        return {'count': 0, 'mean': None, 'p25': None, 'p50': None, 'p75': None}
    count = len(values)

    def _q(p: float) -> float:
        idx = int(round((count - 1) * p))
        return float(values[idx])

    return {
        'count': count,
        'mean': round(sum(values) / count, 4),
        'p25': round(_q(0.25), 4),
        'p50': round(_q(0.50), 4),
        'p75': round(_q(0.75), 4),
    }



def _offset_rollup(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[_to_int(row.get('scan_offset_minutes'))].append(row)

    output: list[dict[str, Any]] = []
    for offset in sorted(grouped):
        bucket = grouped[offset]
        total = len(bucket)
        tradeable = sum(1 for row in bucket if row['failure_path'] == 'tradeable')
        entry_never = sum(1 for row in bucket if row['failure_path'] == 'entry_never_touched')
        entry_no_target = sum(1 for row in bucket if row['failure_path'] == 'entry_touched_no_target')
        share = round(tradeable / max(total, 1), 4) if total else None
        output.append(
            {
                'scan_offset_minutes': offset,
                'flagged_total': total,
                'tradeable_count': tradeable,
                'entry_never_touched_count': entry_never,
                'entry_touched_no_target_count': entry_no_target,
                'tradeable_share': share,
                'tradeable_share_gap_to_support': round((share or 0.0) - TRADEABLE_SHARE_SUPPORT_THRESHOLD, 4) if share is not None else None,
            }
        )
    return output



def _failure_path_rollup(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    total = len(rows)
    for path in ['tradeable', 'entry_never_touched', 'entry_touched_no_target']:
        count = sum(1 for row in rows if row['failure_path'] == path)
        output.append(
            {
                'failure_path': path,
                'count': count,
                'share_of_flagged_total': round(count / max(total, 1), 4) if total else None,
            }
        )
    return output



def _rank_bucket_rollup(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get('rank_bucket') or 'unknown')].append(row)

    order = ['1-10', '11-20', '21-30', '31-50', '51+', 'unknown']
    output: list[dict[str, Any]] = []
    for bucket in order:
        members = grouped.get(bucket, [])
        if not members:
            continue
        total = len(members)
        tradeable = sum(1 for row in members if row['failure_path'] == 'tradeable')
        output.append(
            {
                'rank_bucket': bucket,
                'flagged_total': total,
                'tradeable_count': tradeable,
                'tradeable_share': round(tradeable / max(total, 1), 4) if total else None,
            }
        )
    return output



def _daily_offset_rollup(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row.get('trading_day') or ''), _to_int(row.get('scan_offset_minutes')))].append(row)
    output: list[dict[str, Any]] = []
    for (day, offset) in sorted(grouped):
        members = grouped[(day, offset)]
        total = len(members)
        tradeable = sum(1 for row in members if row['failure_path'] == 'tradeable')
        output.append(
            {
                'trading_day': day,
                'scan_offset_minutes': offset,
                'flagged_total': total,
                'tradeable_count': tradeable,
                'tradeable_share': round(tradeable / max(total, 1), 4) if total else None,
            }
        )
    return output



def _metric_rollup(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    metrics = [
        'width_retention_ratio',
        'cycle_persistence_ratio',
        'bounce_quality_score',
        'cycle_durability_score',
        'distance_to_entry_pct',
        'minutes_to_entry',
        'minutes_to_target',
        'intraday_pct_gain',
        'mover_rank',
    ]
    output: list[dict[str, Any]] = []
    by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_path[str(row.get('failure_path') or 'unknown')].append(row)
    for metric in metrics:
        for path in ['tradeable', 'entry_never_touched', 'entry_touched_no_target']:
            summary = _metric_summary(by_path.get(path, []), metric)
            output.append(
                {
                    'metric_name': metric,
                    'failure_path': path,
                    **summary,
                }
            )
    return output



def build_replay_bottleneck_pack(
    settings: Settings,
    db: Database | RepositoryBundle,
    alpaca,
    *,
    lookback_days: int = DEFAULT_REPLAY_LOOKBACK_DAYS,
    offsets: list[int] | None = None,
) -> dict[str, bytes]:
    requested_offsets = list(offsets or DEFAULT_REPLAY_OFFSETS)
    raw_zip = get_or_build_historical_replay_shadow_zip(
        settings,
        db,
        alpaca,
        lookback_days=lookback_days,
        offsets=requested_offsets,
        prefer_cache=True,
    )
    replay_summary, replay_rows = _read_replay_pack(raw_zip)
    recommended_profile = dict(replay_summary.get('recommended_profile') or {})
    profile_name = str(recommended_profile.get('profile_name') or '')
    focus_rows: list[dict[str, Any]] = []
    for row in replay_rows:
        if str(row.get('profile_name') or '') != profile_name:
            continue
        if str(row.get('would_pass_shadow_profile') or '').lower() != 'true':
            continue
        enriched = dict(row)
        enriched['failure_path'] = _failure_path(row)
        enriched['rank_bucket'] = _rank_bucket(row.get('mover_rank'))
        focus_rows.append(enriched)

    offset_rows = _offset_rollup(focus_rows)
    failure_rows = _failure_path_rollup(focus_rows)
    rank_rows = _rank_bucket_rollup(focus_rows)
    daily_rows = _daily_offset_rollup(focus_rows)
    metric_rows = _metric_rollup(focus_rows)

    best_offset_row = max(offset_rows, key=lambda row: (_to_float(row.get('tradeable_share')) or -1.0, _to_int(row.get('tradeable_count'))), default=None)
    worst_offset_row = min(offset_rows, key=lambda row: (_to_float(row.get('tradeable_share')) or 999.0, -_to_int(row.get('tradeable_count'))), default=None)
    failure_only_rows = [row for row in failure_rows if str(row.get('failure_path') or '') != 'tradeable']
    dominant_failure = max(failure_only_rows, key=lambda row: _to_int(row.get('count')), default=None)

    summary = {
        'generated_at_utc': datetime.now(UTC).isoformat(),
        'app_version': VERSION,
        'bundle_type': 'replay_bottleneck_pack',
        'source_replay_generated_at_utc': replay_summary.get('generated_at_utc'),
        'source_replay_app_version': replay_summary.get('app_version'),
        'source_replay_verdict': replay_summary.get('overall_verdict'),
        'source_replay_reason': replay_summary.get('overall_reason'),
        'lookback_days_requested': _to_int(replay_summary.get('lookback_days_requested')),
        'lookback_days_effective': _to_int(replay_summary.get('lookback_days_effective')),
        'offsets_requested': list(replay_summary.get('offsets_requested') or requested_offsets),
        'focus_profile_name': profile_name,
        'focus_profile_flagged_total': len(focus_rows),
        'focus_profile_tradeable_count': _to_int(recommended_profile.get('flagged_tradeable')),
        'focus_profile_tradeable_share': _to_float(recommended_profile.get('tradeable_share')),
        'tradeable_share_support_threshold': TRADEABLE_SHARE_SUPPORT_THRESHOLD,
        'tradeable_share_gap_to_support': round((_to_float(recommended_profile.get('tradeable_share')) or 0.0) - TRADEABLE_SHARE_SUPPORT_THRESHOLD, 4) if recommended_profile else None,
        'best_offset_by_tradeable_share': best_offset_row,
        'worst_offset_by_tradeable_share': worst_offset_row,
        'dominant_failure_path': dominant_failure,
        'offset_split_signal': (
            'later_checkpoint_weaker_than_earlier' if best_offset_row and worst_offset_row and _to_int(best_offset_row.get('scan_offset_minutes')) < _to_int(worst_offset_row.get('scan_offset_minutes')) else 'mixed_or_unclear'
        ),
    }

    report_lines = [
        '# Replay bottleneck pack',
        '',
        'Purpose: explain why the replay-recommended shadow profile still misses the support bar before any threshold change is considered.',
        '',
        f"Generated at: {summary['generated_at_utc']}",
        f"App version: {VERSION}",
        f"Source replay version: {summary['source_replay_app_version']}",
        f"Focus profile: {profile_name}",
        f"Replay verdict: {summary['source_replay_verdict']}",
        f"Focus profile tradeable share: {summary['focus_profile_tradeable_share']}",
        f"Support threshold: {TRADEABLE_SHARE_SUPPORT_THRESHOLD}",
        f"Gap to support: {summary['tradeable_share_gap_to_support']}",
        '',
        '## Best / worst offset split',
        json.dumps(best_offset_row or {}, indent=2),
        json.dumps(worst_offset_row or {}, indent=2),
        '',
        '## Dominant failure path',
        json.dumps(dominant_failure or {}, indent=2),
        '',
        '## Interpretation',
        '- Use this pack to decide whether the next tranche should isolate checkpoint-specific decay or entry/target path failure inside the recommended replay profile.',
        '- Do not use it to justify a live threshold change by itself.',
    ]

    manifest = {
        'bundle_type': 'replay_bottleneck_pack',
        'bundle_contract_version': '1.0',
        'app_version': VERSION,
        'generated_at_utc': summary['generated_at_utc'],
        'focus_profile_name': profile_name,
        'source_replay_generated_at_utc': replay_summary.get('generated_at_utc'),
        'lookback_days_effective': _to_int(replay_summary.get('lookback_days_effective')),
        'offsets_requested': list(replay_summary.get('offsets_requested') or requested_offsets),
    }

    return {
        'MANIFEST.json': json.dumps(manifest, indent=2).encode('utf-8'),
        'replay_bottleneck_summary.json': json.dumps(summary, indent=2).encode('utf-8'),
        'recommended_profile_offset_rollup.csv': _csv_bytes(offset_rows),
        'recommended_profile_failure_path_rollup.csv': _csv_bytes(failure_rows),
        'recommended_profile_rank_bucket_rollup.csv': _csv_bytes(rank_rows),
        'recommended_profile_daily_offset_rollup.csv': _csv_bytes(daily_rows),
        'recommended_profile_metric_rollup.csv': _csv_bytes(metric_rows),
        'recommended_profile_rows.csv': _csv_bytes(focus_rows),
        'report.md': '\n'.join(report_lines).encode('utf-8'),
    }
