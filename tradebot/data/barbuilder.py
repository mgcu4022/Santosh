"""Build OHLC candles from polled last-traded prices.

Used when the live candle API is down and only quotes are available. Candles
built from polls every few seconds miss ticks between polls, so highs/lows are
approximate and volume is 0. Good enough to keep paper trading running.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

SESSION_START = (9, 15)
COLUMNS = ["open", "high", "low", "close", "volume"]


def empty_bars() -> pd.DataFrame:
    """An empty candle table that still has a time index (so joins and .time keep working)."""
    from zoneinfo import ZoneInfo
    return pd.DataFrame({c: pd.Series(dtype=float) for c in COLUMNS},
                        index=pd.DatetimeIndex([], tz=ZoneInfo("Asia/Kolkata"), name="datetime"))


def bucket_start(ts: datetime, interval_min: int) -> datetime:
    start = ts.replace(hour=SESSION_START[0], minute=SESSION_START[1], second=0, microsecond=0)
    n = int((ts - start).total_seconds() // (interval_min * 60))
    return start + timedelta(minutes=n * interval_min)


class BarBuilder:
    def __init__(self, interval_min: int = 5):
        self.interval = interval_min
        self.current: dict | None = None
        self.done: list[dict] = []

    def update(self, ts: datetime, price: float) -> None:
        if (ts.hour, ts.minute) < SESSION_START:
            return                                   # before the session
        b = bucket_start(ts, self.interval)
        if self.current is None or b > self.current["datetime"]:
            if self.current is not None:
                self.done.append(self.current)
            self.current = {"datetime": b, "open": price, "high": price, "low": price, "close": price, "volume": 0.0}
        elif b == self.current["datetime"]:
            c = self.current
            c["high"], c["low"], c["close"] = max(c["high"], price), min(c["low"], price), price

    def close_due(self, now: datetime) -> None:
        """Finish the current candle once its time window has passed (even with no new quote)."""
        if self.current and now >= self.current["datetime"] + timedelta(minutes=self.interval):
            self.done.append(self.current)
            self.current = None

    def frame(self) -> pd.DataFrame:
        if not self.done:
            return empty_bars()
        return pd.DataFrame(self.done).set_index("datetime")[COLUMNS]
