from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from app.config import Settings
from app.db import Database
from app.repositories import RepositoryBundle, ensure_repository_bundle
from app.services.classifier_audit_pack import (
    DEFAULT_REJECT_SAMPLE_PER_OFFSET,
    _intraday_bar_rows,
    _iso_z,
    _normalise_timestamp_column,
    _sample_rejected_classification_c,
    _session_bounds_for_regular_day,
)
from app.services.diagnostics import build_contract_health
from app.services.evidence_pack import _json_bytes, _rows_to_csv
from app.services.stage2_regression_pack import (
    LATEST_DEFAULT_OFFSETS,
    _candidate_maps,
    _select_recent_scans,
)
from app.version import VERSION

UTC = timezone.utc


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if number != number or number in {float('inf'), float('-inf')}:
        return float(default)
    return float(number)


def _find_entry_touch_local(full_day_bars: pd.DataFrame, checkpoint_ts: pd.Timestamp, entry_low: float, entry_high: float, deadline_ts: pd.Timestamp, settings: Settings) -> dict[str, object]:
    after = full_day_bars[(full_day_bars['timestamp'] > checkpoint_ts) & (full_day_bars['timestamp'] <= deadline_ts)].copy()
    if after.empty:
        return {
            'entry_touched': False,
            'entry_timestamp': None,
            'entry_price': None,
            'minutes_to_entry': None,
            'entry_fill_method': 'no_post_scan_bars_in_trade_window',
        }

    zone_low = float(min(entry_low, entry_high))
    zone_high = float(max(entry_low, entry_high))
    zone_mid = (zone_low + zone_high) / 2.0
    fill_mode = str(settings.replay_entry_fill_mode or 'zone_mid').strip().lower()

    for _, row in after.iterrows():
        bar_low = float(row['low'])
        bar_high = float(row['high'])
        if bar_low > zone_high or bar_high < zone_low:
            continue

        if fill_mode == 'bar_typical_clipped':
            typical = (float(row['open']) + float(row['high']) + float(row['low']) + float(row['close'])) / 4.0
            fill_price = min(max(typical, zone_low), zone_high)
            fill_method = 'bar_typical_clipped_to_zone'
        else:
            fill_price = zone_mid
            fill_method = 'zone_mid_conservative_fill'

        entry_ts = pd.Timestamp(row['timestamp'])
        return {
            'entry_touched': True,
            'entry_timestamp': entry_ts,
            'entry_price': round(float(fill_price), 4),
            'minutes_to_entry': int((entry_ts - checkpoint_ts).total_seconds() // 60),
            'entry_fill_method': fill_method,
        }
    return {
        'entry_touched': False,
        'entry_timestamp': None,
        'entry_price': None,
        'minutes_to_entry': None,
        'entry_fill_method': 'zone_not_touched_within_trade_window',
    }


def _post_entry_outcome_local(full_day_bars: pd.DataFrame, entry_timestamp: pd.Timestamp, entry_price: float, target_pct: float, deadline_ts: pd.Timestamp, settings: Settings, spread_bps_value: float) -> dict[str, object]:
    future = full_day_bars[(full_day_bars['timestamp'] > entry_timestamp) & (full_day_bars['timestamp'] <= deadline_ts)].copy()
    round_trip_cost_bps = float(spread_bps_value * float(settings.replay_spread_cost_multiplier) + float(settings.replay_slippage_bps_per_side) * 2.0)
    if future.empty:
        return {
            'hit_target': False,
            'minutes_to_target': None,
            'mfe_pct': None,
            'mae_pct': None,
            'end_of_window_return_pct': None,
            'net_end_of_window_return_pct': None,
            'round_trip_cost_bps': round(round_trip_cost_bps, 3),
            'target_timestamp': None,
            'target_fill_method': str(settings.replay_target_hit_mode or 'close_confirmed'),
        }
    highs = future['high'] / max(entry_price, 1e-9) - 1.0
    lows = future['low'] / max(entry_price, 1e-9) - 1.0
    closes = future['close'] / max(entry_price, 1e-9) - 1.0
    target_ratio = target_pct / 100.0
    close_buffer = float(settings.replay_target_close_buffer_bps) / 10000.0
    hit_mode = str(settings.replay_target_hit_mode or 'close_confirmed').strip().lower()
    if hit_mode == 'close_with_buffer':
        qualifying = closes[closes >= max(target_ratio - close_buffer, 0.0)]
        target_fill_method = 'close_with_buffer'
    else:
        qualifying = closes[closes >= target_ratio]
        target_fill_method = 'close_confirmed'
    hit = not qualifying.empty
    target_ts = None
    minutes_to_target = None
    if hit:
        target_ts = pd.Timestamp(future.loc[qualifying.index[0], 'timestamp'])
        minutes_to_target = int((target_ts - entry_timestamp).total_seconds() // 60)
    end_of_window_return_pct = float((future['close'].iloc[-1] / max(entry_price, 1e-9) - 1.0) * 100.0)
    return {
        'hit_target': hit,
        'minutes_to_target': minutes_to_target,
        'mfe_pct': round(float(highs.max() * 100.0), 3),
        'mae_pct': round(float(lows.min() * 100.0), 3),
        'end_of_window_return_pct': round(end_of_window_return_pct, 3),
        'net_end_of_window_return_pct': round(end_of_window_return_pct - round_trip_cost_bps / 100.0, 3),
        'round_trip_cost_bps': round(round_trip_cost_bps, 3),
        'target_timestamp': target_ts.isoformat() if target_ts is not None else None,
        'target_fill_method': target_fill_method,
    }


def _trade_deadline_utc(trading_day: str, offset_minutes: int, settings: Settings) -> datetime:
    _, market_close, _ = _session_bounds_for_regular_day(trading_day, int(offset_minutes))
    return market_close - pd.Timedelta(minutes=int(settings.trade_window_end_buffer_minutes_before_close))


def _future_after_timestamp(full_day_bars: pd.DataFrame, after_ts: pd.Timestamp, deadline_ts: pd.Timestamp) -> pd.DataFrame:
    return full_day_bars[(full_day_bars['timestamp'] > after_ts) & (full_day_bars['timestamp'] <= deadline_ts)].copy()


def _intrabar_target_reached(future_bars: pd.DataFrame, entry_price: float, target_pct: float) -> bool:
    if future_bars.empty:
        return False
    target_ratio = float(target_pct) / 100.0
    threshold = float(entry_price) * (1.0 + target_ratio)
    return bool((future_bars['high'] >= threshold).any())


def _end_of_window_close(full_day_bars: pd.DataFrame, checkpoint_ts: pd.Timestamp, deadline_ts: pd.Timestamp) -> float | None:
    future = _future_after_timestamp(full_day_bars, checkpoint_ts, deadline_ts)
    if future.empty:
        return None
    return round(float(future['close'].iloc[-1]), 4)


def _adjudication_bucket(*, advanced: bool, classification_code: str, entry_touched: bool, hit_target: bool, intrabar_target_reached: bool) -> tuple[str, str]:
    if advanced:
        if hit_target:
            return 'advanced_and_tradeable', 'Advanced to stage 2 and hit the configured target after entry before the cutoff.'
        if entry_touched and intrabar_target_reached:
            return 'advanced_intrabar_only', 'Advanced to stage 2 and reached the target intrabar, but not under the configured close-confirmed target rule.'
        if entry_touched:
            return 'advanced_but_not_tradeable', 'Advanced to stage 2, touched the entry zone, but did not achieve the configured target before the cutoff.'
        return 'advanced_but_entry_never_touched', 'Advanced to stage 2, but the preferred entry zone was never touched after the checkpoint.'

    if str(classification_code or '') == 'C':
        if hit_target or intrabar_target_reached:
            return 'possible_classifier_overstrict', 'Rejected as classification C, but post-scan price action still looked tradeable from the preferred entry zone.'
        return 'classifier_correct_reject', 'Rejected as classification C and the post-scan price action did not deliver a tradeable entry-to-target path.'

    if hit_target or intrabar_target_reached:
        return 'non_c_reject_but_tradeable', 'Did not advance, but post-scan price action still looked tradeable from the preferred entry zone.'
    return 'non_c_reject_not_tradeable', 'Did not advance and post-scan price action did not deliver a tradeable entry-to-target path.'


def _candidate_checkpoint_row(
    *,
    settings: Settings,
    trading_day: str,
    offset_minutes: int,
    audit_reason: str,
    candidate: dict[str, Any],
    bars: pd.DataFrame,
) -> dict[str, Any]:
    metrics = dict(candidate.get('metrics') or {})
    checkpoint = _session_bounds_for_regular_day(trading_day, int(offset_minutes))[2]
    checkpoint_ts = pd.Timestamp(checkpoint)
    deadline_ts = pd.Timestamp(_trade_deadline_utc(trading_day, int(offset_minutes), settings))
    evaluation_status = 'evaluated'
    error_message = None

    entry_low = candidate.get('entry_low')
    entry_high = candidate.get('entry_high')
    target_pct = float(settings.target_pct)
    spread_bps_value = _safe_float(metrics.get('spread_bps'), 0.0)

    default_outcome = {
        'entry_touched': False,
        'entry_timestamp': None,
        'entry_price': None,
        'minutes_to_entry': None,
        'entry_fill_method': None,
        'hit_target': False,
        'minutes_to_target': None,
        'mfe_pct': None,
        'mae_pct': None,
        'end_of_window_return_pct': None,
        'net_end_of_window_return_pct': None,
        'round_trip_cost_bps': None,
        'target_timestamp': None,
        'target_fill_method': None,
        'intrabar_target_reached': False,
        'end_of_window_close': _end_of_window_close(bars, checkpoint_ts, deadline_ts),
    }

    outcome = dict(default_outcome)
    if entry_low is None or entry_high is None:
        evaluation_status = 'error'
        error_message = 'Missing entry bounds for outcome adjudication.'
    else:
        try:
            entry = _find_entry_touch_local(bars, checkpoint_ts, float(entry_low), float(entry_high), deadline_ts, settings)
            future_after_entry = pd.DataFrame()
            if entry.get('entry_touched') and entry.get('entry_timestamp') is not None and entry.get('entry_price') is not None:
                future_after_entry = _future_after_timestamp(bars, pd.Timestamp(entry['entry_timestamp']), deadline_ts)
                base_outcome = _post_entry_outcome_local(
                    bars,
                    pd.Timestamp(entry['entry_timestamp']),
                    float(entry['entry_price']),
                    target_pct,
                    deadline_ts,
                    settings,
                    spread_bps_value,
                )
                outcome.update(base_outcome)
                outcome['intrabar_target_reached'] = _intrabar_target_reached(future_after_entry, float(entry['entry_price']), target_pct)
            else:
                round_trip_cost_bps = float(spread_bps_value * float(settings.replay_spread_cost_multiplier) + float(settings.replay_slippage_bps_per_side) * 2.0)
                outcome.update(
                    {
                        'hit_target': False,
                        'minutes_to_target': None,
                        'mfe_pct': None,
                        'mae_pct': None,
                        'end_of_window_return_pct': None,
                        'net_end_of_window_return_pct': None,
                        'round_trip_cost_bps': round(round_trip_cost_bps, 3),
                        'target_timestamp': None,
                        'target_fill_method': str(settings.replay_target_hit_mode or 'close_confirmed'),
                        'intrabar_target_reached': False,
                    }
                )
            outcome.update(
                {
                    'entry_touched': bool(entry.get('entry_touched')),
                    'entry_timestamp': entry.get('entry_timestamp').isoformat() if entry.get('entry_timestamp') is not None else None,
                    'entry_price': entry.get('entry_price'),
                    'minutes_to_entry': entry.get('minutes_to_entry'),
                    'entry_fill_method': entry.get('entry_fill_method'),
                }
            )
        except Exception as exc:  # pragma: no cover - defensive runtime path
            evaluation_status = 'error'
            error_message = f'{type(exc).__name__}: {exc}'

    bucket, verdict_reason = _adjudication_bucket(
        advanced=bool(candidate.get('advanced_to_stage2')),
        classification_code=str(metrics.get('range_classification_code') or ''),
        entry_touched=bool(outcome.get('entry_touched')),
        hit_target=bool(outcome.get('hit_target')),
        intrabar_target_reached=bool(outcome.get('intrabar_target_reached')),
    )

    return {
        'trading_day': trading_day,
        'scan_offset_minutes': int(offset_minutes),
        'audit_reason': audit_reason,
        'symbol': candidate.get('symbol'),
        'company_name': candidate.get('company_name'),
        'advanced_to_stage2': bool(candidate.get('advanced_to_stage2')),
        'recommendation_tier': candidate.get('recommendation_tier'),
        'recommendation_book': candidate.get('recommendation_book'),
        'execution_lane': candidate.get('execution_lane'),
        'touch_window_band': candidate.get('touch_window_band'),
        'exclusion_reason': candidate.get('exclusion_reason'),
        'range_classification': metrics.get('range_classification'),
        'range_classification_code': metrics.get('range_classification_code'),
        'total_score': candidate.get('total_score'),
        'distance_to_entry_pct': metrics.get('distance_to_entry_pct'),
        'range_current_location': metrics.get('range_current_location'),
        'effective_headroom_pct': metrics.get('effective_headroom_pct'),
        'within_range_target_possible': metrics.get('within_range_target_possible'),
        'evaluation_status': evaluation_status,
        'error_message': error_message,
        'entry_touched': outcome.get('entry_touched'),
        'entry_timestamp': outcome.get('entry_timestamp'),
        'entry_price': outcome.get('entry_price'),
        'minutes_to_entry': outcome.get('minutes_to_entry'),
        'entry_fill_method': outcome.get('entry_fill_method'),
        'hit_target': outcome.get('hit_target'),
        'intrabar_target_reached': outcome.get('intrabar_target_reached'),
        'minutes_to_target': outcome.get('minutes_to_target'),
        'target_timestamp': outcome.get('target_timestamp'),
        'target_fill_method': outcome.get('target_fill_method'),
        'mfe_pct': outcome.get('mfe_pct'),
        'mae_pct': outcome.get('mae_pct'),
        'end_of_window_return_pct': outcome.get('end_of_window_return_pct'),
        'net_end_of_window_return_pct': outcome.get('net_end_of_window_return_pct'),
        'round_trip_cost_bps': outcome.get('round_trip_cost_bps'),
        'end_of_window_close': outcome.get('end_of_window_close'),
        'target_pct': target_pct,
        'verdict_bucket': bucket,
        'verdict_reason': verdict_reason,
    }


def _progression_rows(rows: list[dict[str, Any]], *, early_offset: int, late_offset: int) -> list[dict[str, Any]]:
    by_symbol: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        symbol = str(row.get('symbol') or '')
        offset = int(row.get('scan_offset_minutes') or 0)
        if symbol:
            by_symbol[symbol][offset] = row

    output: list[dict[str, Any]] = []
    for symbol, row_map in sorted(by_symbol.items()):
        early = row_map.get(int(early_offset))
        late = row_map.get(int(late_offset))
        if not early or not late:
            continue
        early_tradeable = bool(early.get('entry_touched')) and (bool(early.get('hit_target')) or bool(early.get('intrabar_target_reached')))
        late_tradeable = bool(late.get('entry_touched')) and (bool(late.get('hit_target')) or bool(late.get('intrabar_target_reached')))
        if bool(early.get('advanced_to_stage2')) and not bool(late.get('advanced_to_stage2')):
            if early_tradeable and not late_tradeable:
                pair_bucket = 'late_regression_was_correct'
            elif early_tradeable and late_tradeable:
                pair_bucket = 'possible_late_classifier_overstrict'
            elif not early_tradeable:
                pair_bucket = 'advanced_but_not_tradeable'
            else:
                pair_bucket = 'mixed_progression'
        elif early_tradeable and late_tradeable:
            pair_bucket = 'tradeable_across_checkpoints'
        elif not early_tradeable and not late_tradeable:
            pair_bucket = 'not_tradeable_across_checkpoints'
        else:
            pair_bucket = 'tradeability_changed_without_advancement_flip'
        output.append(
            {
                'trading_day': early.get('trading_day') or late.get('trading_day'),
                'symbol': symbol,
                'audit_reason_early': early.get('audit_reason'),
                'audit_reason_late': late.get('audit_reason'),
                'early_offset_minutes': early_offset,
                'late_offset_minutes': late_offset,
                'advanced_at_early_offset': early.get('advanced_to_stage2'),
                'advanced_at_late_offset': late.get('advanced_to_stage2'),
                'verdict_bucket_at_early_offset': early.get('verdict_bucket'),
                'verdict_bucket_at_late_offset': late.get('verdict_bucket'),
                'entry_touched_at_early_offset': early.get('entry_touched'),
                'entry_touched_at_late_offset': late.get('entry_touched'),
                'tradeable_at_early_offset': early_tradeable,
                'tradeable_at_late_offset': late_tradeable,
                'classification_code_at_early_offset': early.get('range_classification_code'),
                'classification_code_at_late_offset': late.get('range_classification_code'),
                'pair_verdict_bucket': pair_bucket,
            }
        )
    return output


def _rollup_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_checkpoint: dict[tuple[str, int], Counter] = defaultdict(Counter)
    for row in rows:
        key = (str(row.get('trading_day') or ''), int(row.get('scan_offset_minutes') or 0))
        by_checkpoint[key][str(row.get('verdict_bucket') or 'unknown')] += 1
    output: list[dict[str, Any]] = []
    for (trading_day, offset), counter in sorted(by_checkpoint.items()):
        total = sum(counter.values())
        for verdict, count in sorted(counter.items()):
            output.append(
                {
                    'trading_day': trading_day,
                    'scan_offset_minutes': offset,
                    'verdict_bucket': verdict,
                    'count': count,
                    'share_of_audited_symbols': round(count / max(total, 1), 4),
                }
            )
    return output


def build_outcome_adjudication_pack(
    settings: Settings,
    db: Database | RepositoryBundle,
    alpaca,
    *,
    days: int = 5,
    offsets: list[int] | None = None,
    rejected_sample_per_offset: int = DEFAULT_REJECT_SAMPLE_PER_OFFSET,
) -> dict[str, bytes]:
    repos = ensure_repository_bundle(db)
    requested_offsets = sorted({int(value) for value in (offsets or LATEST_DEFAULT_OFFSETS) if int(value) > 0}) or list(LATEST_DEFAULT_OFFSETS)
    selected_days, chosen_scans = _select_recent_scans(repos, days=days, offsets=requested_offsets)
    latest_day = selected_days[0] if selected_days else ''
    latest_offsets = sorted({offset for day, offset in chosen_scans if day == latest_day})
    if len(latest_offsets) < 2:
        latest_offsets = requested_offsets
    early_offset = latest_offsets[0] if latest_offsets else requested_offsets[0]
    late_offset = latest_offsets[-1] if latest_offsets else requested_offsets[-1]

    audited_symbols: set[str] = set()
    audited_rows: list[dict[str, Any]] = []
    progression_rows: list[dict[str, Any]] = []
    classification_c_samples_by_offset: dict[int, list[str]] = {}
    bars_rows: list[dict[str, Any]] = []
    evaluation_status_counts: Counter[str] = Counter()

    if latest_day and (latest_day, early_offset) in chosen_scans and (latest_day, late_offset) in chosen_scans and getattr(alpaca, 'has_credentials', lambda: False)():
        early_candidates = _candidate_maps(repos, int(chosen_scans[(latest_day, early_offset)].get('id') or 0))
        late_candidates = _candidate_maps(repos, int(chosen_scans[(latest_day, late_offset)].get('id') or 0))

        selected_by_offset: dict[int, list[tuple[str, str, dict[str, Any]]]] = {early_offset: [], late_offset: []}
        advanced_symbols = sorted(
            symbol
            for symbol, candidate in early_candidates.items()
            if bool(candidate.get('advanced_to_stage2')) and symbol in late_candidates
        )
        for symbol in advanced_symbols:
            selected_by_offset[early_offset].append(('advanced_at_early_checkpoint', symbol, early_candidates[symbol]))
            selected_by_offset[late_offset].append(('advanced_then_late_snapshot', symbol, late_candidates[symbol]))
            audited_symbols.add(symbol)

        for offset, candidates in ((early_offset, early_candidates), (late_offset, late_candidates)):
            sample = _sample_rejected_classification_c(candidates, limit=rejected_sample_per_offset, exclude_symbols=audited_symbols)
            classification_c_samples_by_offset[offset] = [str(candidate.get('symbol') or '') for candidate in sample]
            for candidate in sample:
                symbol = str(candidate.get('symbol') or '')
                if not symbol:
                    continue
                selected_by_offset[offset].append((f'rejected_classification_c_sample_{offset}m', symbol, candidate))
                audited_symbols.add(symbol)

        market_open, market_close, _ = _session_bounds_for_regular_day(latest_day, late_offset)
        bars_map = {
            symbol: _normalise_timestamp_column(frame)
            for symbol, frame in (alpaca.fetch_bars(sorted(audited_symbols), '1Min', _iso_z(market_open), _iso_z(market_close)) or {}).items()
        }
        bars_rows = _intraday_bar_rows(bars_map, trading_day=latest_day, offset_minutes=[early_offset, late_offset])

        for offset, triples in selected_by_offset.items():
            for audit_reason, symbol, candidate in triples:
                bars = bars_map.get(symbol)
                if bars is None or bars.empty:
                    row = {
                        'trading_day': latest_day,
                        'scan_offset_minutes': int(offset),
                        'audit_reason': audit_reason,
                        'symbol': symbol,
                        'company_name': candidate.get('company_name'),
                        'advanced_to_stage2': bool(candidate.get('advanced_to_stage2')),
                        'recommendation_tier': candidate.get('recommendation_tier'),
                        'recommendation_book': candidate.get('recommendation_book'),
                        'execution_lane': candidate.get('execution_lane'),
                        'touch_window_band': candidate.get('touch_window_band'),
                        'exclusion_reason': candidate.get('exclusion_reason'),
                        'range_classification': (candidate.get('metrics') or {}).get('range_classification'),
                        'range_classification_code': (candidate.get('metrics') or {}).get('range_classification_code'),
                        'total_score': candidate.get('total_score'),
                        'distance_to_entry_pct': (candidate.get('metrics') or {}).get('distance_to_entry_pct'),
                        'range_current_location': (candidate.get('metrics') or {}).get('range_current_location'),
                        'effective_headroom_pct': (candidate.get('metrics') or {}).get('effective_headroom_pct'),
                        'within_range_target_possible': (candidate.get('metrics') or {}).get('within_range_target_possible'),
                        'evaluation_status': 'error',
                        'error_message': 'Missing intraday bars for automated outcome adjudication.',
                        'entry_touched': False,
                        'entry_timestamp': None,
                        'entry_price': None,
                        'minutes_to_entry': None,
                        'entry_fill_method': None,
                        'hit_target': False,
                        'intrabar_target_reached': False,
                        'minutes_to_target': None,
                        'target_timestamp': None,
                        'target_fill_method': None,
                        'mfe_pct': None,
                        'mae_pct': None,
                        'end_of_window_return_pct': None,
                        'net_end_of_window_return_pct': None,
                        'round_trip_cost_bps': None,
                        'end_of_window_close': None,
                        'target_pct': float(settings.target_pct),
                        'verdict_bucket': 'evaluation_error',
                        'verdict_reason': 'Could not fetch intraday bars for automated outcome adjudication.',
                    }
                else:
                    row = _candidate_checkpoint_row(
                        settings=settings,
                        trading_day=latest_day,
                        offset_minutes=int(offset),
                        audit_reason=audit_reason,
                        candidate=candidate,
                        bars=bars,
                    )
                evaluation_status_counts[str(row.get('evaluation_status') or 'unknown')] += 1
                audited_rows.append(row)

        progression_rows = _progression_rows(audited_rows, early_offset=early_offset, late_offset=late_offset)

    rollup_rows = _rollup_rows(audited_rows)
    summary = {
        'generated_at_utc': datetime.now(UTC).isoformat(),
        'app_version': VERSION,
        'selected_days': selected_days,
        'latest_trading_day': latest_day,
        'early_offset_minutes': early_offset,
        'late_offset_minutes': late_offset,
        'audited_symbol_count': len(audited_symbols),
        'audited_row_count': len(audited_rows),
        'progression_row_count': len(progression_rows),
        'classification_c_sample_symbols_by_offset': classification_c_samples_by_offset,
        'evaluation_status_counts': dict(evaluation_status_counts),
        'verdict_counts': dict(Counter(str(row.get('verdict_bucket') or 'unknown') for row in audited_rows)),
        'pair_verdict_counts': dict(Counter(str(row.get('pair_verdict_bucket') or 'unknown') for row in progression_rows)),
        'decision_rule': 'Use this pack to decide whether classification-C domination is market-correct or potentially overstrict before any threshold change or new presentation work.',
        'freeze_recommendation': [
            'Do not retune thresholds before automated outcome adjudication shows recurring overstrict rejects.',
            'Do not build new presentation surfaces before the automated outcome buckets show real tradeable value.',
            'Do not redesign stage 1 while shortlist alignment is already retaining a large liquid pool.',
        ],
    }

    report_lines = [
        '# Automated outcome adjudication pack',
        '',
        f"Generated at: {summary['generated_at_utc']}",
        f'App version: {VERSION}',
        f"Latest trading day audited: {latest_day or 'None'}",
        f'Early/late offsets compared: {early_offset} -> {late_offset}',
        '',
        '## Why this pack exists',
        '- Replace manual chart review with automated entry-touch / target-outcome adjudication for audited symbols.',
        '- Test whether classification-C domination is market-correct or potentially overstrict.',
        '',
        '## Verdict counts',
    ]
    for verdict, count in sorted(summary['verdict_counts'].items()):
        report_lines.append(f'- {verdict}: {count}')
    report_lines.extend(['', '## Pair verdict counts'])
    for verdict, count in sorted(summary['pair_verdict_counts'].items()):
        report_lines.append(f'- {verdict}: {count}')
    report_lines.extend(['', '## Freeze guidance'])
    report_lines.extend(f'- {line}' for line in summary['freeze_recommendation'])

    manifest = {
        'bundle_type': 'outcome_adjudication_pack',
        'bundle_contract_version': '1.0',
        'app_version': VERSION,
        'generated_at_utc': summary['generated_at_utc'],
        'days_requested': int(days),
        'offsets_requested': requested_offsets,
        'latest_trading_day': latest_day,
        'settings_snapshot': settings.public_snapshot(),
    }

    return {
        'MANIFEST.json': _json_bytes(manifest),
        'settings_snapshot.json': _json_bytes(settings.public_snapshot()),
        'contract_health.json': _json_bytes(build_contract_health(repos.db)),
        'outcome_adjudication_summary.json': _json_bytes(summary),
        'outcome_adjudication_rows.csv': _rows_to_csv(audited_rows).encode('utf-8'),
        'outcome_adjudication_progression.csv': _rows_to_csv(progression_rows).encode('utf-8'),
        'outcome_adjudication_verdict_rollup.csv': _rows_to_csv(rollup_rows).encode('utf-8'),
        'outcome_adjudication_intraday_bars.csv': _rows_to_csv(bars_rows).encode('utf-8'),
        'report.md': '\n'.join(report_lines).encode('utf-8'),
    }
