"""Synthetic NSE-hours candles for demos and tests. Not real market data."""
from __future__ import annotations

import numpy as np
import pandas as pd

IST = "Asia/Kolkata"


def synthetic_intraday(days: int = 60, start_price: float = 2500.0, interval_min: int = 5,
                       daily_vol: float = 0.015, drift: float = 0.0, seed: int = 7,
                       start: str = "2026-01-05", trend_days: float = 0.3,
                       trend_strength: float = 0.08) -> pd.DataFrame:
    """Random-walk bars from 09:15 to 15:30 IST on weekdays.

    A fraction `trend_days` of days get a mild intraday drift (about 0.7x the
    daily volatility at the default strength). Set trend_days=0 for a pure
    random walk, where no strategy should make money before costs.
    """
    rng = np.random.default_rng(seed)
    bars_per_day = int(375 / interval_min)
    sigma = daily_vol / np.sqrt(bars_per_day)
    dates = pd.bdate_range(start, periods=days)
    rows, price = [], start_price
    for d in dates:
        price *= np.exp(rng.normal(0, daily_vol / 2))       # overnight gap
        regime = rng.normal(0, sigma * trend_strength) if rng.random() < trend_days else 0.0
        t0 = pd.Timestamp(d).tz_localize(IST) + pd.Timedelta(hours=9, minutes=15)
        for b in range(bars_per_day):
            ret = rng.normal(drift / bars_per_day + regime, sigma)
            o = price
            c = o * np.exp(ret)
            wick = abs(rng.normal(0, sigma / 2)) * o
            h, l = max(o, c) + wick, min(o, c) - wick
            rows.append((t0 + pd.Timedelta(minutes=b * interval_min), o, h, l, c,
                         int(rng.integers(5_000, 50_000))))
            price = c
    df = pd.DataFrame(rows, columns=["datetime", "open", "high", "low", "close", "volume"])
    return df.set_index("datetime").round(2)


def synthetic_daily(days: int = 300, start_price: float = 1000.0, daily_vol: float = 0.018,
                    seed: int = 11, start: str = "2025-01-01") -> pd.DataFrame:
    """Random-walk daily candles (one bar per weekday)."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, periods=days, tz=IST)
    r = rng.normal(0, daily_vol, days)
    close = start_price * np.exp(np.cumsum(r))
    open_ = np.r_[start_price, close[:-1]] * np.exp(rng.normal(0, daily_vol / 3, days))
    wick = np.abs(rng.normal(0, daily_vol / 2, days)) * close
    df = pd.DataFrame({
        "open": open_, "high": np.maximum(open_, close) + wick, "low": np.minimum(open_, close) - wick,
        "close": close, "volume": rng.integers(500_000, 3_000_000, days).astype(float)}, index=idx)
    df.index.name = "datetime"
    return df.round(2)
