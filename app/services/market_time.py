from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pandas_market_calendars as mcal


NY_TZ = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


@dataclass
class SessionWindow:
    trading_day: str
    market_open: datetime
    market_close: datetime
    checkpoint: datetime
    now_et: datetime
    market_is_open_now: bool
    minutes_since_open_now: int
    minutes_until_close_now: int
    minutes_since_open_checkpoint: int
    minutes_until_close_checkpoint: int


def get_nyse_calendar():
    return mcal.get_calendar("NYSE")


def get_session_schedule(start_date: str, end_date: str) -> pd.DataFrame:
    cal = get_nyse_calendar()
    return cal.schedule(start_date=start_date, end_date=end_date)


def list_trading_days(start_date: str, end_date: str) -> list[str]:
    schedule = get_session_schedule(start_date, end_date)
    return [idx.strftime("%Y-%m-%d") for idx in schedule.index]


def get_session_for_day(trading_day: str, offset_minutes: int) -> SessionWindow:
    schedule = get_session_schedule(trading_day, trading_day)
    if schedule.empty:
        raise ValueError(f"{trading_day} is not a NYSE trading day.")
    row = schedule.iloc[0]
    market_open = row["market_open"].to_pydatetime().astimezone(NY_TZ)
    market_close = row["market_close"].to_pydatetime().astimezone(NY_TZ)
    checkpoint = market_open + timedelta(minutes=offset_minutes)
    now_et = datetime.now(tz=NY_TZ)
    market_is_open_now = market_open <= now_et <= market_close
    return SessionWindow(
        trading_day=trading_day,
        market_open=market_open,
        market_close=market_close,
        checkpoint=checkpoint,
        now_et=now_et,
        market_is_open_now=market_is_open_now,
        minutes_since_open_now=max(int((now_et - market_open).total_seconds() // 60), 0),
        minutes_until_close_now=max(int((market_close - now_et).total_seconds() // 60), 0),
        minutes_since_open_checkpoint=max(int((checkpoint - market_open).total_seconds() // 60), 0),
        minutes_until_close_checkpoint=max(int((market_close - checkpoint).total_seconds() // 60), 0),
    )


def latest_or_previous_trading_day(reference: datetime | None = None) -> str:
    now_et = (reference or datetime.now(tz=NY_TZ)).astimezone(NY_TZ)
    candidate = now_et.date()
    for _ in range(7):
        day = candidate.strftime("%Y-%m-%d")
        if day in list_trading_days(day, day):
            return day
        candidate -= timedelta(days=1)
    raise RuntimeError("Could not identify a recent NYSE trading day.")


def iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")
