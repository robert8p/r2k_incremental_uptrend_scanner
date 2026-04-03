from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pandas as pd
from pydantic import BaseModel

from app.config import Settings
from app.db import Database
from app.services.alpaca_client import AlpacaClient
from app.services.telemetry import emit_event


class ScanRequest(BaseModel):
    trading_day: str
    offset_minutes: int


def _get_session_impl(trading_day: str, offset_minutes: int):
    from app.services.market_time import get_session_for_day

    return get_session_for_day(trading_day, offset_minutes)


def _load_universe_impl(settings: Settings, db: Database):
    from app.services.universe import load_universe

    return load_universe(settings, db, force_refresh=False)


def _build_stage1_impl(stage1_records: List[Dict[str, Any]], settings: Settings, avg_daily_dollar_volume_lookup: Dict[str, float] | None = None):
    from app.services.shared_logic import build_stage1_target_group_with_alignment

    return build_stage1_target_group_with_alignment(
        stage1_records,
        settings=settings,
        avg_daily_dollar_volume_lookup=avg_daily_dollar_volume_lookup,
    )


def _spread_bps_impl(intraday_bars):
    from app.services.shared_logic import spread_bps

    return spread_bps(intraday_bars)


def _quality_filter_reason_impl(row, settings: Settings, avg_daily_dollar_volume: float, spread_bps_value: float):
    from app.services.shared_logic import quality_filter_reason

    return quality_filter_reason(row, settings, avg_daily_dollar_volume, spread_bps_value)


def _score_candidate_impl(*, row, top_stage1, intraday_bars, daily_bars, spread_bps, minutes_remaining, settings: Settings):
    from app.services.scoring import build_candidate_score

    return build_candidate_score(
        row=row,
        top_stage1=top_stage1,
        intraday_bars=intraday_bars,
        daily_bars=daily_bars,
        spread_bps=spread_bps,
        minutes_remaining=minutes_remaining,
        settings=settings,
    )


def load_scan_inputs(settings: Settings, db: Database, alpaca: AlpacaClient, request: ScanRequest) -> Dict[str, Any]:
    if not alpaca.has_credentials():
        raise RuntimeError('Alpaca credentials are required for scans.')

    universe = _load_universe_impl(settings, db)
    universe_rows = universe['symbols']
    symbols = [row['symbol'] for row in universe_rows if row.get('tradable', True) or row.get('asset_status') == 'unknown']
    company_lookup = {row['symbol']: row.get('company_name') for row in universe_rows}
    tradable_lookup = {row['symbol']: bool(row.get('tradable', True)) for row in universe_rows}

    session = _get_session_impl(request.trading_day, request.offset_minutes)
    bars_map = alpaca.fetch_bars(symbols, '1Min', session.market_open.isoformat(), session.checkpoint.isoformat())
    return {
        'request': request,
        'session': session,
        'universe': universe,
        'universe_rows': universe_rows,
        'symbols': symbols,
        'company_lookup': company_lookup,
        'tradable_lookup': tradable_lookup,
        'bars_map': bars_map,
    }


def build_stage1_snapshot(settings: Settings, scan_inputs: Dict[str, Any], alpaca: AlpacaClient | None = None) -> Dict[str, Any]:
    stage1_records: List[Dict[str, Any]] = []
    for symbol in scan_inputs['symbols']:
        bars = scan_inputs['bars_map'].get(symbol)
        if bars is None or bars.empty:
            continue
        open_price = float(bars['open'].iloc[0])
        current_price = float(bars['close'].iloc[-1])
        if open_price <= 0:
            continue
        stage1_records.append({
            'symbol': symbol,
            'company_name': scan_inputs['company_lookup'].get(symbol),
            'tradable': scan_inputs['tradable_lookup'].get(symbol, True),
            'session_open': open_price,
            'current_price': current_price,
            'intraday_pct_gain': (current_price / open_price - 1.0) * 100.0,
            'cum_volume': float(scan_inputs['bars_map'][symbol]['volume'].sum()),
        })

    avg_daily_dollar_volume_lookup: Dict[str, float] = {}
    stage1_daily_bars_map: Dict[str, pd.DataFrame] = {}
    if settings.stage1_alignment_enabled and alpaca is not None and stage1_records:
        from app.services.shared_logic import average_daily_dollar_volume, build_stage1_target_group

        preview = build_stage1_target_group(stage1_records, max(
            int(settings.top_mover_count) * max(int(settings.stage1_alignment_pool_multiplier), 1),
            max(int(settings.stage1_alignment_min_pool_size), int(settings.top_mover_count)),
        ))
        preview_symbols = preview['symbol'].tolist() if not preview.empty else []
        if preview_symbols:
            session = scan_inputs['session']
            stage1_daily_bars_map = alpaca.fetch_daily_bars(
                preview_symbols,
                (session.market_open - timedelta(days=60)).isoformat(),
                session.market_open.isoformat(),
            )
            avg_daily_dollar_volume_lookup = {
                symbol: average_daily_dollar_volume(frame)
                for symbol, frame in stage1_daily_bars_map.items()
            }

    stage1_selection = _build_stage1_impl(stage1_records, settings, avg_daily_dollar_volume_lookup)
    stage1 = stage1_selection['stage1']
    if stage1.empty:
        raise RuntimeError('No positive intraday movers available at the scan checkpoint.')
    return {
        'stage1_records': stage1_records,
        'stage1': stage1,
        'positive_movers': stage1_selection.get('positive_movers'),
        'alignment_pool': stage1_selection.get('alignment_pool'),
        'daily_bars_map': stage1_daily_bars_map,
        'diagnostics': stage1_selection.get('diagnostics', {}),
    }


def _excluded_candidate(row, reason: str) -> Dict[str, Any]:
    return {
        'symbol': row['symbol'],
        'company_name': row.get('company_name'),
        'mover_rank': int(row['mover_rank']),
        'intraday_pct_gain': round(float(row['intraday_pct_gain']), 3),
        'advanced_to_stage2': False,
        'exclusion_reason': reason,
        'current_price': round(float(row['current_price']), 4),
        'current_cum_volume': round(float(row['cum_volume']), 2),
        'relative_volume': None,
        'total_score': None,
        'component_scores': {},
        'metrics': {},
        'rationale': reason,
        'entry_low': None,
        'entry_high': None,
        'target_price': None,
        'stretch_target_price': None,
        'stop_price': None,
        'chart_context': {},
    }


def score_stage2_candidates(settings: Settings, scan_inputs: Dict[str, Any], stage1_snapshot: Dict[str, Any], alpaca: AlpacaClient) -> Dict[str, Any]:
    stage1 = stage1_snapshot['stage1']
    top_symbols = stage1['symbol'].tolist()
    intraday_map = {symbol: scan_inputs['bars_map'][symbol] for symbol in top_symbols if symbol in scan_inputs['bars_map']}
    session = scan_inputs['session']
    daily_map = {
        symbol: frame
        for symbol, frame in (stage1_snapshot.get('daily_bars_map') or {}).items()
        if symbol in top_symbols
    }
    missing_daily_symbols = [symbol for symbol in top_symbols if symbol not in daily_map]
    if missing_daily_symbols:
        fetched_daily_map = alpaca.fetch_daily_bars(
            missing_daily_symbols,
            (session.market_open - timedelta(days=60)).isoformat(),
            session.market_open.isoformat(),
        )
        daily_map.update(fetched_daily_map)

    candidates: List[Dict[str, Any]] = []
    for _, row in stage1.iterrows():
        symbol = row['symbol']
        intraday_bars = intraday_map.get(symbol)
        if intraday_bars is None or intraday_bars.empty:
            candidates.append(_excluded_candidate(row, 'Missing intraday bar history for candidate.'))
            continue

        daily_bars = daily_map.get(symbol, pd.DataFrame())
        spread_bps_value = _spread_bps_impl(intraday_bars)
        avg_daily_dollar_volume = (
            float(daily_bars['close'].tail(min(20, len(daily_bars))).mean() * daily_bars['volume'].tail(min(20, len(daily_bars))).mean())
            if not daily_bars.empty else 0.0
        )
        exclusion_reason = _quality_filter_reason_impl(row, settings, avg_daily_dollar_volume, spread_bps_value)
        if exclusion_reason:
            candidates.append(_excluded_candidate(row, exclusion_reason))
            continue

        candidates.append(_score_candidate_impl(
            row=row,
            top_stage1=stage1,
            intraday_bars=intraday_bars,
            daily_bars=daily_bars,
            spread_bps=spread_bps_value,
            minutes_remaining=session.minutes_until_close_checkpoint,
            settings=settings,
        ))

    candidates = sorted(
        candidates,
        key=lambda x: (
            0 if x.get('advanced_to_stage2') else 1,
            -float(x.get('total_score') or -1),
            int(x.get('mover_rank') or 999999),
        ),
    )
    advanced_count = int(sum(1 for candidate in candidates if candidate.get('advanced_to_stage2')))
    return {
        'candidates': candidates,
        'advanced_count': advanced_count,
    }


def build_scan_summary(settings: Settings, scan_inputs: Dict[str, Any], stage1_snapshot: Dict[str, Any], stage2_snapshot: Dict[str, Any]) -> Dict[str, Any]:
    stage1 = stage1_snapshot['stage1']
    session = scan_inputs['session']
    request = scan_inputs['request']
    advanced_count = int(stage2_snapshot['advanced_count'])
    diagnostics = stage1_snapshot.get('diagnostics', {}) or {}
    return {
        'goal': 'Rank the top Russell 2000 movers by how suitable they are for repeatable 1% intraday range trades before the hard cutoff.',
        'target_group_size': int(len(stage1)),
        'stage1_target_group_count': int(len(stage1)),
        'advanced_count': advanced_count,
        'stage2_candidate_count': advanced_count,
        'leader_symbol': str(stage1.iloc[0]['symbol']) if not stage1.empty else None,
        'leader_gain_pct': round(float(stage1.iloc[0]['intraday_pct_gain']), 3) if not stage1.empty else None,
        'checkpoint_minutes_until_close': int(session.minutes_until_close_checkpoint),
        'scan_offset_minutes': int(request.offset_minutes),
        'data_contract': {
            'alpaca_data_feed': settings.alpaca_data_feed,
            'universe_source': 'IWM holdings proxy',
            'universe_count': int(scan_inputs['universe']['status'].tradable_count),
        },
        'shortlist_alignment': {
            'enabled': bool(settings.stage1_alignment_enabled),
            'selection_mode': diagnostics.get('selection_mode'),
            'raw_positive_mover_count': int(diagnostics.get('raw_positive_mover_count') or 0),
            'alignment_pool_size': int(diagnostics.get('alignment_pool_size') or 0),
            'alignment_prefilter_kept_count': int(diagnostics.get('alignment_prefilter_kept_count') or 0),
            'prefilter_rejection_counts': diagnostics.get('prefilter_rejection_counts', {}),
            'selected_stage1_count': int(diagnostics.get('selected_stage1_count') or len(stage1)),
        },
        'range_trade_cutoff_rule': f'Valid exits should be achievable before {settings.trade_window_end_buffer_minutes_before_close} minutes before the close.',
        'scan_focus': 'Broad, active, repeatable intraday ranges with multiple demonstrated low/high cycles rather than one-way continuation.',
    }


def execute_scan(settings: Settings, db: Database, alpaca: AlpacaClient, request: ScanRequest) -> Dict[str, Any]:
    emit_event('scan.started', trading_day=request.trading_day, offset_minutes=int(request.offset_minutes), trading_mode=settings.trading_mode)
    try:
        scan_inputs = load_scan_inputs(settings, db, alpaca, request)
        stage1_snapshot = build_stage1_snapshot(settings, scan_inputs, alpaca)
        stage2_snapshot = score_stage2_candidates(settings, scan_inputs, stage1_snapshot, alpaca)
        summary = build_scan_summary(settings, scan_inputs, stage1_snapshot, stage2_snapshot)
        session = scan_inputs['session']
        candidates = stage2_snapshot['candidates']
        scan_payload = {
            'created_at': datetime.now(timezone.utc).isoformat(),
            'trading_day': request.trading_day,
            'scan_offset_minutes': int(request.offset_minutes),
            'scan_timestamp': session.checkpoint.isoformat(),
            'status': 'ok',
            'mode': settings.trading_mode,
            'universe_count': int(scan_inputs['universe']['status'].tradable_count),
            'stage1_count': int(len(stage1_snapshot['stage1'])),
            'stage2_count': int(stage2_snapshot['advanced_count']),
            'summary': summary,
        }
        scan_id = db.insert_scan(scan_payload, candidates)
        scan_payload['id'] = scan_id
        scan_payload['candidates'] = candidates
        emit_event(
            'scan.completed',
            trading_day=request.trading_day,
            offset_minutes=int(request.offset_minutes),
            scan_id=int(scan_id),
            universe_count=int(scan_payload['universe_count']),
            stage1_count=int(scan_payload['stage1_count']),
            stage2_count=int(scan_payload['stage2_count']),
        )
        return scan_payload
    except Exception as exc:
        emit_event(
            'scan.failed',
            level='error',
            trading_day=request.trading_day,
            offset_minutes=int(request.offset_minutes),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        raise
