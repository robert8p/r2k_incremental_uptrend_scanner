from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pandas as pd

from app.config import Settings
from app.db import Database
from app.services.alpaca_client import AlpacaClient


UTC = timezone.utc


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _deadline_passed(scan: Dict[str, Any], settings: Settings, *, reference: Optional[datetime] = None) -> bool:
    from app.services.backtest import _trade_window_deadline
    from app.services.market_time import get_session_for_day

    session = get_session_for_day(str(scan['trading_day']), int(scan['scan_offset_minutes']))
    checkpoint_ts = pd.Timestamp(session.checkpoint)
    deadline_ts = _trade_window_deadline(
        checkpoint_ts,
        int(session.minutes_until_close_checkpoint),
        int(settings.trade_window_end_buffer_minutes_before_close),
    )
    ref = pd.Timestamp(reference or datetime.now(UTC))
    return bool(ref >= deadline_ts.tz_convert('UTC') if deadline_ts.tzinfo is not None else ref >= deadline_ts)


def evaluate_pending_live_outcomes(
    settings: Settings,
    db: Database,
    alpaca: AlpacaClient,
    *,
    max_candidates: int = 80,
) -> Dict[str, Any]:
    pending = db.list_live_candidate_outcomes(limit=max_candidates, status='pending')
    if not pending:
        return {
            'evaluated': 0,
            'skipped_not_due': 0,
            'errored': 0,
            'scan_ids_touched': [],
            'message': 'No pending live candidate outcomes.',
        }

    grouped: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for row in pending:
        grouped[int(row['scan_id'])].append(row)

    evaluated = 0
    skipped_not_due = 0
    errored = 0
    scan_ids_touched: List[int] = []

    for scan_id, rows in grouped.items():
        scan = db.get_scan(scan_id)
        if not scan:
            for row in rows:
                db.update_live_candidate_outcome(
                    scan_id,
                    row['symbol'],
                    evaluated_at=_utc_now_iso(),
                    evaluation_status='error',
                    error_message='Parent scan not found.',
                )
                errored += 1
            continue
        if not _deadline_passed(scan, settings):
            skipped_not_due += len(rows)
            continue

        from app.services.backtest import _find_entry_touch, _post_entry_outcome, _trade_window_deadline
        from app.services.market_time import get_session_for_day

        session = get_session_for_day(str(scan['trading_day']), int(scan['scan_offset_minutes']))
        checkpoint_ts = pd.Timestamp(session.checkpoint)
        deadline_ts = _trade_window_deadline(
            checkpoint_ts,
            int(session.minutes_until_close_checkpoint),
            int(settings.trade_window_end_buffer_minutes_before_close),
        )
        start_iso = session.market_open.isoformat()
        end_iso = session.market_close.isoformat()
        symbols = [str(row['symbol']) for row in rows]
        bars_map = alpaca.fetch_bars(symbols, '1Min', start_iso, end_iso)
        scan_ids_touched.append(scan_id)

        for row in rows:
            symbol = str(row['symbol'])
            try:
                bars = bars_map.get(symbol)
                if bars is None or bars.empty:
                    db.update_live_candidate_outcome(
                        scan_id,
                        symbol,
                        evaluated_at=_utc_now_iso(),
                        evaluation_status='error',
                        error_message='Missing full-day bars for live outcome evaluation.',
                    )
                    errored += 1
                    continue
                entry_low = row.get('entry_low')
                entry_high = row.get('entry_high')
                if entry_low is None or entry_high is None or row.get('target_price') is None:
                    db.update_live_candidate_outcome(
                        scan_id,
                        symbol,
                        evaluated_at=_utc_now_iso(),
                        evaluation_status='error',
                        error_message='Missing entry or target fields needed for live outcome evaluation.',
                    )
                    errored += 1
                    continue
                entry = _find_entry_touch(bars, checkpoint_ts, float(entry_low), float(entry_high), deadline_ts, settings)
                spread_bps_value = float((row.get('metrics') or {}).get('spread_bps') or 0.0)
                if entry.get('entry_touched'):
                    outcome = _post_entry_outcome(
                        bars,
                        pd.Timestamp(entry['entry_timestamp']),
                        float(entry['entry_price']),
                        float(settings.target_pct),
                        deadline_ts,
                        settings,
                        spread_bps_value,
                    )
                else:
                    outcome = {
                        'hit_target': False,
                        'minutes_to_target': None,
                        'mfe_pct': None,
                        'mae_pct': None,
                        'end_of_window_return_pct': None,
                        'net_end_of_window_return_pct': None,
                        'round_trip_cost_bps': round(float(spread_bps_value * float(settings.replay_spread_cost_multiplier) + float(settings.replay_slippage_bps_per_side) * 2.0), 3),
                        'target_timestamp': None,
                        'target_fill_method': str(settings.replay_target_hit_mode or 'close_confirmed'),
                    }
                db.update_live_candidate_outcome(
                    scan_id,
                    symbol,
                    evaluated_at=_utc_now_iso(),
                    evaluation_status='evaluated',
                    entry_touched=1 if entry.get('entry_touched') else 0,
                    hit_target=1 if outcome.get('hit_target') else 0,
                    minutes_to_entry=entry.get('minutes_to_entry'),
                    minutes_to_target=outcome.get('minutes_to_target'),
                    entry_fill_method=entry.get('entry_fill_method'),
                    target_fill_method=outcome.get('target_fill_method'),
                    mfe_pct=outcome.get('mfe_pct'),
                    mae_pct=outcome.get('mae_pct'),
                    end_of_window_return_pct=outcome.get('end_of_window_return_pct'),
                    net_end_of_window_return_pct=outcome.get('net_end_of_window_return_pct'),
                    round_trip_cost_bps=outcome.get('round_trip_cost_bps'),
                    target_timestamp=outcome.get('target_timestamp').isoformat() if outcome.get('target_timestamp') is not None else None,
                    error_message=None,
                )
                evaluated += 1
            except Exception as exc:  # pragma: no cover - defensive runtime path
                db.update_live_candidate_outcome(
                    scan_id,
                    symbol,
                    evaluated_at=_utc_now_iso(),
                    evaluation_status='error',
                    error_message=f'{type(exc).__name__}: {exc}',
                )
                errored += 1

    return {
        'evaluated': evaluated,
        'skipped_not_due': skipped_not_due,
        'errored': errored,
        'scan_ids_touched': scan_ids_touched,
        'message': 'Live candidate outcomes evaluation completed.' if evaluated else 'No live candidate outcomes were due for evaluation yet.',
    }


def build_live_trust_snapshot(settings: Settings, db: Database, *, lookback_candidates: int = 300) -> Dict[str, Any]:
    outcomes = db.list_live_candidate_outcomes(limit=lookback_candidates)
    completed_research = [run for run in db.list_research_runs(limit=20) if run.get('status') == 'completed' and run.get('result')]
    latest_research = completed_research[0] if completed_research else None
    expected_by_offset: Dict[int, Dict[str, Any]] = {}
    recommended_schedule = None
    if latest_research and (latest_research.get('result') or {}).get('offset_ladder_summary'):
        for row in latest_research['result']['offset_ladder_summary']:
            expected_by_offset[int(row['scan_offset_minutes'])] = row
        recommended_schedule = (latest_research.get('result') or {}).get('recommended_live_schedule')

    by_offset: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    status_counts: Dict[str, int] = defaultdict(int)
    for row in outcomes:
        status_counts[str(row.get('evaluation_status') or 'unknown')] += 1
        by_offset[int(row['scan_offset_minutes'])].append(row)

    offset_rows: List[Dict[str, Any]] = []
    for offset, rows in sorted(by_offset.items()):
        evaluated = [r for r in rows if r.get('evaluation_status') == 'evaluated']
        evaluated_scans = sorted({int(r['scan_id']) for r in evaluated})
        touched = [r for r in evaluated if r.get('entry_touched')]
        hits = [r for r in evaluated if r.get('hit_target')]
        conditional_hits = [r for r in touched if r.get('hit_target')]
        live_overall = round(len(hits) / len(evaluated), 4) if evaluated else None
        live_entry_touch = round(len(touched) / len(evaluated), 4) if evaluated else None
        live_conditional = round(len(conditional_hits) / len(touched), 4) if touched else None
        expected = expected_by_offset.get(offset) or {}
        expected_overall = expected.get('overall_hit_rate')
        expected_entry_touch = expected.get('entry_touch_rate_stage2')
        expected_conditional = expected.get('conditional_precision_at_10_entry_touched')
        delta_overall = round(live_overall - float(expected_overall), 4) if live_overall is not None and expected_overall is not None else None
        delta_touch = round(live_entry_touch - float(expected_entry_touch), 4) if live_entry_touch is not None and expected_entry_touch is not None else None
        delta_conditional = round(live_conditional - float(expected_conditional), 4) if live_conditional is not None and expected_conditional is not None else None
        if len(evaluated) < 8 or len(evaluated_scans) < 5:
            drift_status = 'insufficient_sample'
        elif delta_overall is not None and abs(delta_overall) > 0.20:
            drift_status = 'alert'
        elif delta_overall is not None and abs(delta_overall) > 0.10:
            drift_status = 'review'
        else:
            drift_status = 'ok'
        offset_rows.append(
            {
                'scan_offset_minutes': offset,
                'evaluated_candidates': len(evaluated),
                'evaluated_scans': len(evaluated_scans),
                'pending_candidates': sum(1 for r in rows if r.get('evaluation_status') == 'pending'),
                'live_overall_hit_rate': live_overall,
                'live_entry_touch_rate': live_entry_touch,
                'live_conditional_hit_rate_entry_touched': live_conditional,
                'expected_overall_hit_rate': expected_overall,
                'expected_entry_touch_rate': expected_entry_touch,
                'expected_conditional_hit_rate_entry_touched': expected_conditional,
                'delta_overall_hit_rate': delta_overall,
                'delta_entry_touch_rate': delta_touch,
                'delta_conditional_hit_rate_entry_touched': delta_conditional,
                'drift_status': drift_status,
                'most_recent_trading_day': max((str(r['trading_day']) for r in evaluated), default=None),
                'recommended': bool(expected.get('recommended')),
            }
        )

    current_offsets = sorted(int(v) for v in settings.scheduled_offsets)
    recommended_offsets = []
    if recommended_schedule and recommended_schedule.get('suggested_scheduled_scan_offsets'):
        recommended_offsets = sorted(
            int(part.strip())
            for part in str(recommended_schedule['suggested_scheduled_scan_offsets']).split(',')
            if part.strip()
        )
    alignment_ok = bool(not recommended_offsets or current_offsets == recommended_offsets)

    return {
        'has_latest_research_reference': latest_research is not None,
        'latest_research_run_id': latest_research.get('id') if latest_research else None,
        'recommended_schedule': recommended_schedule,
        'current_scheduled_offsets': current_offsets,
        'schedule_alignment_ok': alignment_ok,
        'schedule_alignment_message': (
            f'Current scheduled offsets {current_offsets} differ from latest validated recommendation {recommended_offsets}.'
            if recommended_offsets and not alignment_ok
            else 'Current scheduled offsets align with the latest validated recommendation.' if recommended_offsets else 'No completed offset-ladder research reference available yet.'
        ),
        'status_counts': dict(status_counts),
        'offset_rows': offset_rows,
    }
