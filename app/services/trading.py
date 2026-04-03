from __future__ import annotations

from typing import Dict

from app.config import Settings
from app.services.alpaca_client import AlpacaClient


class TradingSafetyError(RuntimeError):
    pass


def validate_trade_request(settings: Settings, mode: str) -> None:
    if mode not in {"paper", "live"}:
        raise TradingSafetyError("Trading endpoint only supports paper or live modes.")
    if mode == "live" and not settings.enable_live_trading:
        raise TradingSafetyError("Live trading is disabled. Set ENABLE_LIVE_TRADING=true explicitly to allow it.")
    if settings.trading_mode != mode:
        raise TradingSafetyError(f"Application is currently in {settings.trading_mode} mode, not {mode} mode.")


def submit_candidate_limit_buy(settings: Settings, alpaca: AlpacaClient, symbol: str, notional_usd: float, entry_limit_price: float, mode: str) -> Dict[str, object]:
    validate_trade_request(settings, mode)
    return alpaca.submit_limit_buy(symbol=symbol, notional_usd=notional_usd, limit_price=entry_limit_price, mode=mode)
