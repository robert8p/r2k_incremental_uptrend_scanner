from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List

import requests

from app.config import Settings
from app.db import Database

logger = logging.getLogger(__name__)

CACHE_KEY = 'russell2000_proxy'


@dataclass
class UniverseStatus:
    source: str
    loaded_at: str
    cache_age_hours: float
    raw_count: int
    active_count: int
    tradable_count: int
    note: str


def _is_valid_symbol(symbol: str) -> bool:
    symbol = (symbol or '').strip().upper()
    if not symbol or len(symbol) > 10:
        return False
    if not re.fullmatch(r"[A-Z][A-Z0-9]*(?:[.-][A-Z0-9]+)*", symbol):
        return False
    base = re.sub(r"[.-]", "", symbol)
    digit_count = sum(ch.isdigit() for ch in base)
    if digit_count > 1:
        return False
    if '.' not in symbol and '-' not in symbol and len(base) > 5:
        return False
    return True


def _normalize_symbol(symbol: str) -> str:
    return symbol.replace(' ', '').upper().strip()


def _parse_holdings_csv(content: str) -> List[Dict[str, str]]:
    lines = content.splitlines()
    start_idx = None
    for idx, line in enumerate(lines):
        if 'Ticker' in line and 'Name' in line:
            start_idx = idx
            break
    if start_idx is None:
        raise ValueError('Could not find holdings table header in iShares CSV.')
    reader = csv.DictReader(lines[start_idx:])
    rows = []
    for row in reader:
        symbol = _normalize_symbol((row.get('Ticker') or '').strip())
        name = (row.get('Name') or '').strip()
        if not symbol or not _is_valid_symbol(symbol):
            continue
        rows.append({'symbol': symbol, 'company_name': name})
    deduped: Dict[str, Dict[str, str]] = {}
    for row in rows:
        deduped[row['symbol']] = row
    return list(sorted(deduped.values(), key=lambda x: x['symbol']))


def _fetch_alpaca_assets(settings: Settings) -> Dict[str, Dict[str, object]]:
    if not settings.alpaca_api_key or not settings.alpaca_secret_key:
        return {}
    url = f"{settings.alpaca_trading_base_url.rstrip('/')}/v2/assets"
    headers = {
        'APCA-API-KEY-ID': settings.alpaca_api_key,
        'APCA-API-SECRET-KEY': settings.alpaca_secret_key,
    }
    params = {'status': 'active', 'asset_class': 'us_equity'}
    resp = requests.get(url, headers=headers, params=params, timeout=settings.alpaca_request_timeout_seconds)
    resp.raise_for_status()
    payload = resp.json()
    assets = {}
    for item in payload:
        symbol = (item.get('symbol') or '').upper()
        assets[symbol] = {
            'status': item.get('status'),
            'tradable': item.get('tradable', False),
            'fractionable': item.get('fractionable', False),
            'shortable': item.get('shortable', False),
            'easy_to_borrow': item.get('easy_to_borrow', False),
            'exchange': item.get('exchange'),
        }
    return assets


def load_universe(settings: Settings, db: Database, force_refresh: bool = False) -> Dict[str, object]:
    cache = db.get_universe_cache(CACHE_KEY)
    now = datetime.now(timezone.utc)
    ttl = timedelta(hours=settings.universe_cache_ttl_hours)

    if cache and not force_refresh:
        loaded_at = datetime.fromisoformat(cache['loaded_at'])
        if now - loaded_at <= ttl:
            payload = cache['payload']
            payload['status'] = compute_status(payload)
            return payload

    logger.info('Refreshing Russell 2000 proxy universe from iShares.')
    try:
        response = requests.get(settings.universe_holdings_url, timeout=settings.alpaca_request_timeout_seconds)
        response.raise_for_status()
        holdings = _parse_holdings_csv(response.text)
    except Exception as exc:
        logger.warning('Universe refresh failed: %s', exc)
        if cache:
            payload = cache['payload']
            payload['note'] = str(payload.get('note', '')) + ' Refresh failed, so cached universe was reused.'
            payload['status'] = compute_status(payload)
            return payload
        payload = {
            'source': 'iShares IWM holdings proxy',
            'note': (
                'Universe refresh failed and no cache was available yet. The app stays online, but scans cannot run until '
                'the holdings file is reachable and the cache is populated.'
            ),
            'asset_note': 'Alpaca asset status not applied.',
            'loaded_at': now.isoformat(),
            'symbols': [],
        }
        payload['status'] = compute_status(payload)
        return payload

    asset_map = {}
    asset_note = 'Alpaca asset status not applied.'
    try:
        asset_map = _fetch_alpaca_assets(settings)
        asset_note = 'Alpaca active/tradable flags applied where available.'
    except Exception as exc:
        logger.warning('Could not refresh Alpaca asset metadata: %s', exc)

    enriched = []
    for item in holdings:
        symbol = item['symbol']
        asset = asset_map.get(symbol, {})
        if asset_map and not asset:
            asset_status = 'missing_from_alpaca_assets'
            tradable = False
        else:
            asset_status = asset.get('status', 'unknown')
            tradable = bool(asset.get('tradable', True if not asset_map else False))
        enriched.append(
            {
                'symbol': symbol,
                'company_name': item['company_name'],
                'asset_status': asset_status,
                'tradable': tradable,
                'fractionable': bool(asset.get('fractionable', False)),
                'exchange': asset.get('exchange'),
            }
        )

    payload = {
        'source': 'iShares IWM holdings proxy',
        'note': (
            'Constituent universe is approximated from the iShares Russell 2000 ETF holdings file and then filtered '
            'through Alpaca active/tradable metadata when credentials allow. It is a pragmatic operational proxy, not '
            'a licensed official Russell membership feed.'
        ),
        'asset_note': asset_note,
        'loaded_at': now.isoformat(),
        'symbols': enriched,
    }
    db.upsert_universe_cache(CACHE_KEY, payload['loaded_at'], payload)
    payload['status'] = compute_status(payload)
    return payload


def compute_status(payload: Dict[str, object]) -> UniverseStatus:
    loaded_at = datetime.fromisoformat(payload['loaded_at'])
    symbols = payload['symbols']
    tradable_count = sum(1 for row in symbols if row.get('tradable'))
    active_count = sum(1 for row in symbols if row.get('asset_status') == 'active')
    cache_age = (datetime.now(timezone.utc) - loaded_at).total_seconds() / 3600.0
    return UniverseStatus(
        source=str(payload['source']),
        loaded_at=payload['loaded_at'],
        cache_age_hours=round(cache_age, 2),
        raw_count=len(symbols),
        active_count=active_count,
        tradable_count=tradable_count,
        note=str(payload['note']),
    )
