from __future__ import annotations

from collections import defaultdict
import logging
import re
from typing import Any, Dict, Iterable, List

import pandas as pd
import requests

from app.config import Settings


logger = logging.getLogger(__name__)


def _is_valid_request_symbol(symbol: str) -> bool:
    symbol = (symbol or "").strip().upper()
    if not symbol or len(symbol) > 10:
        return False
    if not re.fullmatch(r"[A-Z][A-Z0-9]*(?:[.-][A-Z0-9]+)*", symbol):
        return False
    base = re.sub(r"[.-]", "", symbol)
    digit_count = sum(ch.isdigit() for ch in base)
    if digit_count > 1:
        return False
    if "." not in symbol and "-" not in symbol and len(base) > 5:
        return False
    return True


class AlpacaClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.data_base_url = settings.alpaca_data_base_url.rstrip("/")
        self.paper_base_url = settings.alpaca_paper_base_url.rstrip("/")
        self.live_base_url = settings.alpaca_live_base_url.rstrip("/")

    @property
    def data_headers(self) -> Dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.settings.alpaca_api_key,
            "APCA-API-SECRET-KEY": self.settings.alpaca_secret_key,
        }

    def _request(self, method: str, url: str, *, params: Dict[str, Any] | None = None, json_body: Dict[str, Any] | None = None) -> Any:
        resp = requests.request(
            method=method,
            url=url,
            headers=self.data_headers,
            params=params,
            json=json_body,
            timeout=self.settings.alpaca_request_timeout_seconds,
        )
        resp.raise_for_status()
        return resp.json()

    def _chunk(self, items: Iterable[str], size: int) -> Iterable[List[str]]:
        batch: List[str] = []
        for item in items:
            batch.append(item)
            if len(batch) >= size:
                yield batch
                batch = []
        if batch:
            yield batch

    def has_credentials(self) -> bool:
        return bool(self.settings.alpaca_api_key and self.settings.alpaca_secret_key)

    def _clean_symbols(self, symbols: List[str]) -> List[str]:
        cleaned: List[str] = []
        seen = set()
        dropped: List[str] = []
        for raw in symbols:
            symbol = (raw or '').strip().upper()
            if not _is_valid_request_symbol(symbol):
                if raw not in dropped:
                    dropped.append(str(raw))
                continue
            if symbol in seen:
                continue
            seen.add(symbol)
            cleaned.append(symbol)
        if dropped:
            sample = ', '.join(dropped[:10])
            suffix = ' ...' if len(dropped) > 10 else ''
            logger.warning('Dropped %s suspicious symbols before Alpaca request: %s%s', len(dropped), sample, suffix)
        return cleaned

    @staticmethod
    def _status_code(exc: Exception) -> int | None:
        response = getattr(exc, 'response', None)
        return getattr(response, 'status_code', None)

    @staticmethod
    def _response_excerpt(exc: Exception) -> str:
        response = getattr(exc, 'response', None)
        text = getattr(response, 'text', '') or ''
        return text[:300].replace('\n', ' ')

    def _request_symbol_batch_with_fallback(
        self,
        endpoint: str,
        batch: List[str],
        *,
        params: Dict[str, Any],
        extract_key: str,
    ) -> Dict[str, Any]:
        try:
            data = self._request('GET', endpoint, params=params)
            chunk = data.get(extract_key, data)
            return chunk or {}
        except requests.HTTPError as exc:
            if self._status_code(exc) == 400 and len(batch) > 1:
                midpoint = max(1, len(batch) // 2)
                logger.warning(
                    'Alpaca rejected a %s-symbol batch for %s; bisecting batch. First symbols: %s. Response: %s',
                    len(batch),
                    extract_key,
                    ', '.join(batch[:8]),
                    self._response_excerpt(exc),
                )
                left = self._request_symbol_batch_with_fallback(
                    endpoint,
                    batch[:midpoint],
                    params={**params, 'symbols': ','.join(batch[:midpoint])},
                    extract_key=extract_key,
                )
                right = self._request_symbol_batch_with_fallback(
                    endpoint,
                    batch[midpoint:],
                    params={**params, 'symbols': ','.join(batch[midpoint:])},
                    extract_key=extract_key,
                )
                merged = {}
                merged.update(left)
                merged.update(right)
                return merged
            if self._status_code(exc) == 400 and len(batch) == 1:
                logger.warning(
                    'Dropping Alpaca-rejected symbol %s for %s request. Response: %s',
                    batch[0],
                    extract_key,
                    self._response_excerpt(exc),
                )
                return {}
            raise

    def fetch_snapshots(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        results: Dict[str, Dict[str, Any]] = {}
        symbols = self._clean_symbols(symbols)
        if not symbols:
            return results
        endpoint = f"{self.data_base_url}/v2/stocks/snapshots"
        for batch in self._chunk(symbols, 200):
            params = {"symbols": ",".join(batch), "feed": self.settings.alpaca_data_feed}
            results.update(self._request_symbol_batch_with_fallback(endpoint, batch, params=params, extract_key='snapshots'))
        return results

    def fetch_latest_quotes(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        results: Dict[str, Dict[str, Any]] = {}
        symbols = self._clean_symbols(symbols)
        if not symbols:
            return results
        endpoint = f"{self.data_base_url}/v2/stocks/quotes/latest"
        for batch in self._chunk(symbols, 200):
            params = {"symbols": ",".join(batch), "feed": self.settings.alpaca_data_feed}
            results.update(self._request_symbol_batch_with_fallback(endpoint, batch, params=params, extract_key='quotes'))
        return results

    def _fetch_bars_for_batch(
        self,
        endpoint: str,
        batch: List[str],
        *,
        timeframe: str,
        start_iso: str,
        end_iso: str,
        adjustment: str,
        limit: int,
        frames: Dict[str, List[Dict[str, Any]]],
    ) -> None:
        try:
            next_token = None
            while True:
                params = {
                    'symbols': ','.join(batch),
                    'timeframe': timeframe,
                    'start': start_iso,
                    'end': end_iso,
                    'adjustment': adjustment,
                    'feed': self.settings.alpaca_data_feed,
                    'limit': limit,
                }
                if next_token:
                    params['page_token'] = next_token
                data = self._request('GET', endpoint, params=params)
                bars = data.get('bars', {})
                if isinstance(bars, dict):
                    for symbol, rows in bars.items():
                        for row in rows:
                            frames[symbol].append(self._normalize_bar_row(row))
                elif isinstance(bars, list):
                    for row in bars:
                        symbol = row.get('S') or row.get('symbol')
                        frames[symbol].append(self._normalize_bar_row(row))
                next_token = data.get('next_page_token')
                if not next_token:
                    break
        except requests.HTTPError as exc:
            if self._status_code(exc) == 400 and len(batch) > 1:
                midpoint = max(1, len(batch) // 2)
                logger.warning(
                    'Alpaca rejected a %s-symbol bars batch; bisecting. First symbols: %s. Response: %s',
                    len(batch),
                    ', '.join(batch[:8]),
                    self._response_excerpt(exc),
                )
                self._fetch_bars_for_batch(
                    endpoint,
                    batch[:midpoint],
                    timeframe=timeframe,
                    start_iso=start_iso,
                    end_iso=end_iso,
                    adjustment=adjustment,
                    limit=limit,
                    frames=frames,
                )
                self._fetch_bars_for_batch(
                    endpoint,
                    batch[midpoint:],
                    timeframe=timeframe,
                    start_iso=start_iso,
                    end_iso=end_iso,
                    adjustment=adjustment,
                    limit=limit,
                    frames=frames,
                )
                return
            if self._status_code(exc) == 400 and len(batch) == 1:
                logger.warning(
                    'Dropping Alpaca-rejected bars symbol %s. Response: %s',
                    batch[0],
                    self._response_excerpt(exc),
                )
                return
            raise

    def fetch_bars(
        self,
        symbols: List[str],
        timeframe: str,
        start_iso: str,
        end_iso: str,
        *,
        adjustment: str = 'raw',
        limit: int = 10000,
    ) -> Dict[str, pd.DataFrame]:
        symbols = self._clean_symbols(symbols)
        if not symbols:
            return {}
        endpoint = f"{self.data_base_url}/v2/stocks/bars"
        frames: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for batch in self._chunk(symbols, 100):
            self._fetch_bars_for_batch(
                endpoint,
                batch,
                timeframe=timeframe,
                start_iso=start_iso,
                end_iso=end_iso,
                adjustment=adjustment,
                limit=limit,
                frames=frames,
            )
        output: Dict[str, pd.DataFrame] = {}
        for symbol, rows in frames.items():
            df = pd.DataFrame(rows)
            if df.empty:
                continue
            df = df.sort_values('timestamp').reset_index(drop=True)
            output[symbol] = df
        return output

    def fetch_daily_bars(self, symbols: List[str], start_iso: str, end_iso: str) -> Dict[str, pd.DataFrame]:
        return self.fetch_bars(symbols, '1Day', start_iso, end_iso)

    def get_account(self, mode: str) -> Dict[str, Any]:
        base = self.paper_base_url if mode == 'paper' else self.live_base_url
        url = f"{base}/v2/account"
        return self._request('GET', url)

    def submit_limit_buy(self, symbol: str, notional_usd: float, limit_price: float, mode: str) -> Dict[str, Any]:
        base = self.paper_base_url if mode == 'paper' else self.live_base_url
        url = f"{base}/v2/orders"
        body = {
            'symbol': symbol,
            'notional': round(float(notional_usd), 2),
            'side': 'buy',
            'type': 'limit',
            'time_in_force': 'day',
            'limit_price': round(float(limit_price), 4),
        }
        return self._request('POST', url, json_body=body)

    def ping_data_api(self) -> Dict[str, Any]:
        if not self.has_credentials():
            return {'ok': False, 'message': 'Missing Alpaca credentials.'}
        endpoint = f"{self.data_base_url}/v2/stocks/trades/latest"
        try:
            data = self._request('GET', endpoint, params={'symbols': 'SPY', 'feed': self.settings.alpaca_data_feed})
            return {'ok': True, 'message': 'ok', 'sample': data}
        except Exception as exc:
            return {'ok': False, 'message': str(exc)}

    @staticmethod
    def _normalize_bar_row(row: Dict[str, Any]) -> Dict[str, Any]:
        timestamp = row.get('t') or row.get('timestamp')
        if timestamp is None:
            raise ValueError('Bar row missing timestamp.')
        return {
            'timestamp': pd.to_datetime(timestamp, utc=True),
            'open': float(row.get('o', row.get('open', 0.0))),
            'high': float(row.get('h', row.get('high', 0.0))),
            'low': float(row.get('l', row.get('low', 0.0))),
            'close': float(row.get('c', row.get('close', 0.0))),
            'volume': float(row.get('v', row.get('volume', 0.0))),
            'trade_count': float(row.get('n', row.get('trade_count', 0.0))),
            'vwap': float(row.get('vw', row.get('vwap', 0.0))) if row.get('vw', row.get('vwap')) is not None else None,
        }
