from __future__ import annotations

import csv
import io
import json
from collections import Counter, defaultdict
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
from app.services.replay_bottleneck_pack import (
    TRADEABLE_SHARE_SUPPORT_THRESHOLD,
    _failure_path,
    _rank_bucket,
    _read_replay_pack,
    _read_json_bytes,
    _to_float,
    _to_int,
    build_replay_bottleneck_pack,
)
from app.version import VERSION

UTC = timezone.utc


def _csv_bytes(rows: list[dict[str, Any]]) -> bytes:
    if not rows:
        return b''
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode('utf-8')



def _enrich_focus_rows(replay_rows: list[dict[str, Any]], profile_name: str) -> list[dict[str, Any]]:
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
    return focus_rows



def _build_pair_rows(
    rows: list[dict[str, Any]],
    supported_offset: int,
    weaker_offset: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], Counter[str]]:
    grouped: dict[tuple[str, str], dict[int, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        day = str(row.get('trading_day') or '')
        symbol = str(row.get('symbol') or '')
        offset = _to_int(row.get('scan_offset_minutes'))
        grouped[(day, symbol)][offset] = row

    pair_rows: list[dict[str, Any]] = []
    supported_only_rows: list[dict[str, Any]] = []
    weaker_only_rows: list[dict[str, Any]] = []
    transition_counter: Counter[str] = Counter()

    for (day, symbol), by_offset in sorted(grouped.items()):
        supported = by_offset.get(supported_offset)
        weaker = by_offset.get(weaker_offset)
        if supported and weaker:
            transition = f"{supported['failure_path']}__to__{weaker['failure_path']}"
            transition_counter[transition] += 1
            pair_rows.append(
                {
                    'trading_day': day,
                    'symbol': symbol,
                    'supported_offset_minutes': supported_offset,
                    'weaker_offset_minutes': weaker_offset,
                    'supported_failure_path': supported['failure_path'],
                    'weaker_failure_path': weaker['failure_path'],
                    'transition_type': transition,
                    'supported_rank_bucket': supported['rank_bucket'],
                    'weaker_rank_bucket': weaker['rank_bucket'],
                    'supported_mover_rank': _to_int(supported.get('mover_rank')),
                    'weaker_mover_rank': _to_int(weaker.get('mover_rank')),
                    'supported_distance_to_entry_pct': _to_float(supported.get('distance_to_entry_pct')),
                    'weaker_distance_to_entry_pct': _to_float(weaker.get('distance_to_entry_pct')),
                    'supported_bounce_quality_score': _to_float(supported.get('bounce_quality_score')),
                    'weaker_bounce_quality_score': _to_float(weaker.get('bounce_quality_score')),
                    'supported_cycle_durability_score': _to_float(supported.get('cycle_durability_score')),
                    'weaker_cycle_durability_score': _to_float(weaker.get('cycle_durability_score')),
                    'supported_minutes_to_entry': _to_float(supported.get('minutes_to_entry')),
                    'weaker_minutes_to_entry': _to_float(weaker.get('minutes_to_entry')),
                    'supported_intraday_pct_gain': _to_float(supported.get('intraday_pct_gain')),
                    'weaker_intraday_pct_gain': _to_float(weaker.get('intraday_pct_gain')),
                }
            )
        elif supported:
            supported_only_rows.append(
                {
                    'trading_day': day,
                    'symbol': symbol,
                    'scan_offset_minutes': supported_offset,
                    'failure_path': supported['failure_path'],
                    'rank_bucket': supported['rank_bucket'],
                    'mover_rank': _to_int(supported.get('mover_rank')),
                    'distance_to_entry_pct': _to_float(supported.get('distance_to_entry_pct')),
                    'bounce_quality_score': _to_float(supported.get('bounce_quality_score')),
                    'cycle_durability_score': _to_float(supported.get('cycle_durability_score')),
                    'minutes_to_entry': _to_float(supported.get('minutes_to_entry')),
                    'intraday_pct_gain': _to_float(supported.get('intraday_pct_gain')),
                }
            )
        elif weaker:
            weaker_only_rows.append(
                {
                    'trading_day': day,
                    'symbol': symbol,
                    'scan_offset_minutes': weaker_offset,
                    'failure_path': weaker['failure_path'],
                    'rank_bucket': weaker['rank_bucket'],
                    'mover_rank': _to_int(weaker.get('mover_rank')),
                    'distance_to_entry_pct': _to_float(weaker.get('distance_to_entry_pct')),
                    'bounce_quality_score': _to_float(weaker.get('bounce_quality_score')),
                    'cycle_durability_score': _to_float(weaker.get('cycle_durability_score')),
                    'minutes_to_entry': _to_float(weaker.get('minutes_to_entry')),
                    'intraday_pct_gain': _to_float(weaker.get('intraday_pct_gain')),
                }
            )

    return pair_rows, supported_only_rows, weaker_only_rows, transition_counter



def _transition_rollup(pair_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counter = Counter(str(row.get('transition_type') or '') for row in pair_rows)
    total = len(pair_rows)
    rows: list[dict[str, Any]] = []
    for transition, count in counter.most_common():
        rows.append(
            {
                'transition_type': transition,
                'count': count,
                'share_of_paired_rows': round(count / max(total, 1), 4) if total else None,
            }
        )
    return rows



def _offset_rank_bucket_comparison(rows: list[dict[str, Any]], supported_offset: int, weaker_offset: int) -> list[dict[str, Any]]:
    grouped: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(_to_int(row.get('scan_offset_minutes')), str(row.get('rank_bucket') or 'unknown'))].append(row)

    order = ['1-10', '11-20', '21-30', '31-50', '51+', 'unknown']
    output: list[dict[str, Any]] = []
    for bucket in order:
        supported_rows = grouped.get((supported_offset, bucket), [])
        weaker_rows = grouped.get((weaker_offset, bucket), [])
        supported_total = len(supported_rows)
        weaker_total = len(weaker_rows)
        supported_tradeable = sum(1 for row in supported_rows if str(row.get('failure_path') or '') == 'tradeable')
        weaker_tradeable = sum(1 for row in weaker_rows if str(row.get('failure_path') or '') == 'tradeable')
        supported_share = round(supported_tradeable / supported_total, 4) if supported_total else None
        weaker_share = round(weaker_tradeable / weaker_total, 4) if weaker_total else None
        if not supported_total and not weaker_total:
            continue
        output.append(
            {
                'rank_bucket': bucket,
                'supported_offset_minutes': supported_offset,
                'supported_flagged_total': supported_total,
                'supported_tradeable_count': supported_tradeable,
                'supported_tradeable_share': supported_share,
                'weaker_offset_minutes': weaker_offset,
                'weaker_flagged_total': weaker_total,
                'weaker_tradeable_count': weaker_tradeable,
                'weaker_tradeable_share': weaker_share,
                'tradeable_share_delta_weaker_minus_supported': round((weaker_share or 0.0) - (supported_share or 0.0), 4) if supported_share is not None and weaker_share is not None else None,
            }
        )
    return output



def _offset_failure_path_comparison(rows: list[dict[str, Any]], supported_offset: int, weaker_offset: int) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for offset in [supported_offset, weaker_offset]:
        subset = [row for row in rows if _to_int(row.get('scan_offset_minutes')) == offset]
        total = len(subset)
        for failure_path in ['tradeable', 'entry_never_touched', 'entry_touched_no_target']:
            count = sum(1 for row in subset if str(row.get('failure_path') or '') == failure_path)
            output.append(
                {
                    'scan_offset_minutes': offset,
                    'failure_path': failure_path,
                    'count': count,
                    'share_of_offset_flagged_total': round(count / max(total, 1), 4) if total else None,
                }
            )
    return output



def build_replay_checkpoint_decay_pack(
    settings: Settings,
    db: Database | RepositoryBundle,
    alpaca,
    *,
    lookback_days: int = DEFAULT_REPLAY_LOOKBACK_DAYS,
    offsets: list[int] | None = None,
) -> dict[str, bytes]:
    requested_offsets = list(offsets or DEFAULT_REPLAY_OFFSETS)
    raw_replay_zip = get_or_build_historical_replay_shadow_zip(
        settings,
        db,
        alpaca,
        lookback_days=lookback_days,
        offsets=requested_offsets,
        prefer_cache=True,
    )
    replay_summary, replay_rows = _read_replay_pack(raw_replay_zip)
    bottleneck_pack = build_replay_bottleneck_pack(
        settings,
        db,
        alpaca,
        lookback_days=lookback_days,
        offsets=requested_offsets,
    )
    bottleneck_summary = _read_json_bytes(bottleneck_pack.get('replay_bottleneck_summary.json', b''))
    profile_name = str(bottleneck_summary.get('focus_profile_name') or (replay_summary.get('recommended_profile') or {}).get('profile_name') or '')
    focus_rows = _enrich_focus_rows(replay_rows, profile_name)

    supported_offset = _to_int(((bottleneck_summary.get('best_offset_by_tradeable_share') or {}) or {}).get('scan_offset_minutes'))
    weaker_offset = _to_int(((bottleneck_summary.get('worst_offset_by_tradeable_share') or {}) or {}).get('scan_offset_minutes'))
    pair_rows, supported_only_rows, weaker_only_rows, transition_counter = _build_pair_rows(focus_rows, supported_offset, weaker_offset)
    transition_rows = _transition_rollup(pair_rows)
    rank_bucket_rows = _offset_rank_bucket_comparison(focus_rows, supported_offset, weaker_offset)
    failure_path_rows = _offset_failure_path_comparison(focus_rows, supported_offset, weaker_offset)

    shared_tradeable_to_failure = sum(
        count for transition, count in transition_counter.items()
        if transition.startswith('tradeable__to__') and transition != 'tradeable__to__tradeable'
    )
    shared_failure_to_tradeable = sum(
        count for transition, count in transition_counter.items()
        if transition.endswith('__to__tradeable') and transition != 'tradeable__to__tradeable'
    )
    dominant_decay_transition = None
    decay_rows = [row for row in transition_rows if str(row.get('transition_type') or '').startswith('tradeable__to__') and str(row.get('transition_type') or '') != 'tradeable__to__tradeable']
    if decay_rows:
        dominant_decay_transition = max(decay_rows, key=lambda row: _to_int(row.get('count')))

    summary = {
        'generated_at_utc': datetime.now(UTC).isoformat(),
        'app_version': VERSION,
        'bundle_type': 'replay_checkpoint_decay_pack',
        'source_replay_generated_at_utc': replay_summary.get('generated_at_utc'),
        'source_replay_app_version': replay_summary.get('app_version'),
        'source_replay_verdict': replay_summary.get('overall_verdict'),
        'source_bottleneck_generated_at_utc': bottleneck_summary.get('generated_at_utc'),
        'source_bottleneck_app_version': bottleneck_summary.get('app_version'),
        'lookback_days_requested': _to_int(replay_summary.get('lookback_days_requested')),
        'lookback_days_effective': _to_int(replay_summary.get('lookback_days_effective')),
        'offsets_requested': list(replay_summary.get('offsets_requested') or requested_offsets),
        'focus_profile_name': profile_name,
        'tradeable_share_support_threshold': TRADEABLE_SHARE_SUPPORT_THRESHOLD,
        'supported_offset_minutes': supported_offset,
        'supported_offset_tradeable_share': _to_float(((bottleneck_summary.get('best_offset_by_tradeable_share') or {}) or {}).get('tradeable_share')),
        'weaker_offset_minutes': weaker_offset,
        'weaker_offset_tradeable_share': _to_float(((bottleneck_summary.get('worst_offset_by_tradeable_share') or {}) or {}).get('tradeable_share')),
        'paired_symbol_day_count': len(pair_rows),
        'supported_only_symbol_day_count': len(supported_only_rows),
        'weaker_only_symbol_day_count': len(weaker_only_rows),
        'shared_tradeable_to_tradeable_count': transition_counter.get('tradeable__to__tradeable', 0),
        'shared_tradeable_to_failure_count': shared_tradeable_to_failure,
        'shared_failure_to_tradeable_count': shared_failure_to_tradeable,
        'dominant_shared_decay_transition': dominant_decay_transition,
        'dominant_shared_decay_count': _to_int((dominant_decay_transition or {}).get('count')) if dominant_decay_transition else 0,
        'composition_shift_larger_than_shared_decay': len(weaker_only_rows) > shared_tradeable_to_failure,
    }

    report_lines = [
        '# Replay checkpoint decay pack',
        '',
        'Purpose: isolate whether later-checkpoint weakness is mostly shared-row decay, population drift, or both before any live threshold change is considered.',
        '',
        f"Generated at: {summary['generated_at_utc']}",
        f"App version: {VERSION}",
        f"Source replay version: {summary['source_replay_app_version']}",
        f"Source bottleneck version: {summary['source_bottleneck_app_version']}",
        f"Focus profile: {profile_name}",
        f"Supported offset: {supported_offset} ({summary['supported_offset_tradeable_share']})",
        f"Weaker offset: {weaker_offset} ({summary['weaker_offset_tradeable_share']})",
        f"Paired symbol-day count: {len(pair_rows)}",
        f"Supported-only symbol-day count: {len(supported_only_rows)}",
        f"Weaker-only symbol-day count: {len(weaker_only_rows)}",
        f"Shared tradeable→failure count: {shared_tradeable_to_failure}",
        f"Shared failure→tradeable count: {shared_failure_to_tradeable}",
        '',
        '## Dominant shared decay transition',
        json.dumps(dominant_decay_transition or {}, indent=2),
        '',
        '## Interpretation',
        '- Use this pack to decide whether the next tranche should target later-checkpoint population drift, later-checkpoint target-path decay, or a narrower compatibility gate.',
        '- Do not use it to justify a live threshold change by itself.',
    ]

    manifest = {
        'bundle_type': 'replay_checkpoint_decay_pack',
        'bundle_contract_version': '1.0',
        'app_version': VERSION,
        'generated_at_utc': summary['generated_at_utc'],
        'focus_profile_name': profile_name,
        'source_replay_generated_at_utc': replay_summary.get('generated_at_utc'),
        'source_bottleneck_generated_at_utc': bottleneck_summary.get('generated_at_utc'),
        'lookback_days_effective': _to_int(replay_summary.get('lookback_days_effective')),
        'supported_offset_minutes': supported_offset,
        'weaker_offset_minutes': weaker_offset,
    }

    return {
        'MANIFEST.json': json.dumps(manifest, indent=2).encode('utf-8'),
        'replay_checkpoint_decay_summary.json': json.dumps(summary, indent=2).encode('utf-8'),
        'offset_rank_bucket_comparison.csv': _csv_bytes(rank_bucket_rows),
        'offset_failure_path_comparison.csv': _csv_bytes(failure_path_rows),
        'paired_symbol_day_transitions.csv': _csv_bytes(pair_rows),
        'paired_transition_rollup.csv': _csv_bytes(transition_rows),
        'supported_offset_only_rows.csv': _csv_bytes(supported_only_rows),
        'weaker_offset_only_rows.csv': _csv_bytes(weaker_only_rows),
        'report.md': '\n'.join(report_lines).encode('utf-8'),
    }
