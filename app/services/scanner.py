from __future__ import annotations

from typing import Dict

from app.config import Settings
from app.db import Database
from app.services.alpaca_client import AlpacaClient
from app.services.scanner_engine import ScanRequest, execute_scan


def run_scan(settings: Settings, db: Database, alpaca: AlpacaClient, *, trading_day: str, offset_minutes: int) -> Dict[str, object]:
    payload = execute_scan(
        settings,
        db,
        alpaca,
        ScanRequest(trading_day=trading_day, offset_minutes=offset_minutes),
    )
    try:
        from app.services.live_trust import evaluate_pending_live_outcomes

        evaluate_pending_live_outcomes(settings, db, alpaca)
    except Exception:
        pass
    return payload
