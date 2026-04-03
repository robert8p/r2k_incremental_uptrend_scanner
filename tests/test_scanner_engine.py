from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from app.config import Settings
from app.services.scanner_engine import (
    ScanRequest,
    build_scan_summary,
    build_stage1_snapshot,
    score_stage2_candidates,
)


@dataclass
class DummyStatus:
    tradable_count: int


class DummyAlpaca:
    def __init__(self, daily_map):
        self.daily_map = daily_map

    def fetch_daily_bars(self, symbols, start, end):
        return {symbol: self.daily_map.get(symbol, pd.DataFrame()) for symbol in symbols}


def _bars(open_price: float, close_price: float, volume: float) -> pd.DataFrame:
    return pd.DataFrame({
        'open': [open_price, open_price],
        'close': [close_price, close_price],
        'volume': [volume / 2.0, volume / 2.0],
    })


def _daily_bars(close_price: float, volume: float, rows: int = 20) -> pd.DataFrame:
    return pd.DataFrame({
        'close': [close_price] * rows,
        'volume': [volume] * rows,
    })


def test_build_stage1_snapshot_raises_when_no_positive_movers(monkeypatch):
    monkeypatch.setattr('app.services.scanner_engine._build_stage1_impl', lambda records, settings, avg_daily_dollar_volume_lookup=None: {'stage1': pd.DataFrame()})

    with pd.option_context('mode.copy_on_write', False):
        scan_inputs = {
            'symbols': ['AAA'],
            'bars_map': {'AAA': pd.DataFrame({'open': [0.0], 'close': [1.0], 'volume': [100]})},
            'company_lookup': {'AAA': 'AAA Co'},
            'tradable_lookup': {'AAA': True},
        }
        try:
            build_stage1_snapshot(Settings(), scan_inputs)
            assert False, 'Expected RuntimeError'
        except RuntimeError as exc:
            assert 'No positive intraday movers' in str(exc)


def test_build_stage1_snapshot_prefilters_top_movers_against_tradability():
    settings = Settings(
        top_mover_count=2,
        stage1_alignment_enabled=True,
        stage1_alignment_pool_multiplier=2,
        stage1_alignment_min_pool_size=4,
        min_price=2.0,
        low_price_hard_floor=1.0,
        min_avg_dollar_volume=1_000_000.0,
        min_intraday_dollar_volume=100_000.0,
    )
    scan_inputs = {
        'symbols': ['AAA', 'BBB', 'CCC', 'DDD'],
        'bars_map': {
            'AAA': _bars(10.0, 14.0, 50_000.0),
            'BBB': _bars(10.0, 13.0, 60_000.0),
            'CCC': _bars(10.0, 12.0, 80_000.0),
            'DDD': _bars(10.0, 11.0, 90_000.0),
        },
        'company_lookup': {symbol: f'{symbol} Co' for symbol in ['AAA', 'BBB', 'CCC', 'DDD']},
        'tradable_lookup': {symbol: True for symbol in ['AAA', 'BBB', 'CCC', 'DDD']},
        'session': type('S', (), {'market_open': pd.Timestamp('2026-01-01T14:30:00Z')})(),
    }
    alpaca = DummyAlpaca({
        'AAA': _daily_bars(14.0, 50_000.0),   # 700k, below threshold
        'BBB': _daily_bars(13.0, 60_000.0),   # 780k, below threshold
        'CCC': _daily_bars(12.0, 120_000.0),  # 1.44m, passes
        'DDD': _daily_bars(11.0, 130_000.0),  # 1.43m, passes
    })

    snapshot = build_stage1_snapshot(settings, scan_inputs, alpaca)

    assert snapshot['stage1']['symbol'].tolist() == ['CCC', 'DDD']
    assert snapshot['stage1']['mover_rank'].tolist() == [1, 2]
    assert snapshot['diagnostics']['selection_mode'] == 'aligned_prefilter_pool'
    assert snapshot['diagnostics']['prefilter_rejection_counts']['Average daily dollar volume below threshold.'] == 2


def test_score_stage2_candidates_handles_missing_intraday_history(monkeypatch):
    stage1 = pd.DataFrame([
        {'symbol': 'AAA', 'company_name': 'AAA Co', 'mover_rank': 1, 'intraday_pct_gain': 3.2, 'current_price': 10.0, 'cum_volume': 1500.0},
    ])
    scan_inputs = {
        'bars_map': {},
        'session': type('S', (), {'market_open': pd.Timestamp('2026-01-01T14:30:00Z'), 'minutes_until_close_checkpoint': 120})(),
    }
    result = score_stage2_candidates(Settings(), scan_inputs, {'stage1': stage1}, DummyAlpaca({}))

    assert result['advanced_count'] == 0
    assert result['candidates'][0]['advanced_to_stage2'] is False
    assert result['candidates'][0]['exclusion_reason'] == 'Missing intraday bar history for candidate.'


def test_build_scan_summary_reflects_stage_counts_and_universe():
    settings = Settings()
    request = ScanRequest(trading_day='2026-01-05', offset_minutes=120)
    scan_inputs = {
        'request': request,
        'session': type('S', (), {'minutes_until_close_checkpoint': 120})(),
        'universe': {'status': DummyStatus(tradable_count=1550)},
    }
    stage1 = pd.DataFrame([
        {'symbol': 'AAA', 'intraday_pct_gain': 3.2},
        {'symbol': 'BBB', 'intraday_pct_gain': 2.1},
    ])
    summary = build_scan_summary(
        settings,
        scan_inputs,
        {
            'stage1': stage1,
            'diagnostics': {
                'selection_mode': 'aligned_prefilter_pool',
                'raw_positive_mover_count': 200,
                'alignment_pool_size': 150,
                'alignment_prefilter_kept_count': 34,
                'prefilter_rejection_counts': {'Average daily dollar volume below threshold.': 116},
                'selected_stage1_count': 2,
            },
        },
        {'advanced_count': 1},
    )

    assert summary['target_group_size'] == 2
    assert summary['advanced_count'] == 1
    assert summary['data_contract']['universe_count'] == 1550
    assert summary['scan_offset_minutes'] == 120
    assert summary['shortlist_alignment']['selection_mode'] == 'aligned_prefilter_pool'
    assert summary['shortlist_alignment']['prefilter_rejection_counts']['Average daily dollar volume below threshold.'] == 116



def test_score_stage2_candidates_reuses_stage1_daily_bars_map(monkeypatch):
    settings = Settings(min_avg_dollar_volume=1_000_000.0, min_intraday_dollar_volume=100_000.0)
    stage1 = pd.DataFrame([
        {'symbol': 'AAA', 'company_name': 'AAA Co', 'mover_rank': 1, 'intraday_pct_gain': 3.2, 'current_price': 10.0, 'cum_volume': 20_000.0},
    ])
    intraday = pd.DataFrame({
        'open': [10.0, 10.1, 10.2],
        'high': [10.2, 10.3, 10.4],
        'low': [9.9, 10.0, 10.1],
        'close': [10.1, 10.2, 10.3],
        'volume': [6_000.0, 7_000.0, 7_000.0],
    })
    daily = _daily_bars(10.0, 200_000.0)
    scan_inputs = {
        'bars_map': {'AAA': intraday},
        'session': type('S', (), {'market_open': pd.Timestamp('2026-01-01T14:30:00Z'), 'minutes_until_close_checkpoint': 120})(),
    }

    class CapturingAlpaca(DummyAlpaca):
        def __init__(self):
            super().__init__({'AAA': daily})
            self.calls = 0

        def fetch_daily_bars(self, symbols, start, end):
            self.calls += 1
            return super().fetch_daily_bars(symbols, start, end)

    alpaca = CapturingAlpaca()
    monkeypatch.setattr(
        'app.services.scanner_engine._score_candidate_impl',
        lambda **kwargs: {
            'symbol': 'AAA',
            'company_name': 'AAA Co',
            'mover_rank': 1,
            'intraday_pct_gain': 3.2,
            'advanced_to_stage2': True,
            'exclusion_reason': None,
            'current_price': 10.0,
            'current_cum_volume': 20_000.0,
            'relative_volume': 1.1,
            'total_score': 77.0,
            'component_scores': {},
            'metrics': {},
            'rationale': 'ok',
            'entry_low': 9.9,
            'entry_high': 10.0,
            'target_price': 10.1,
            'stretch_target_price': 10.2,
            'stop_price': 9.8,
            'chart_context': {},
        },
    )

    result = score_stage2_candidates(
        settings,
        scan_inputs,
        {'stage1': stage1, 'daily_bars_map': {'AAA': daily}},
        alpaca,
    )

    assert alpaca.calls == 0
    assert result['advanced_count'] == 1
    assert result['candidates'][0]['advanced_to_stage2'] is True
