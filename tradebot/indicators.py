"""Technical indicators on OHLCV DataFrames (columns: open, high, low, close, volume)."""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100.0).where(gain.notna())


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def vwap(df: pd.DataFrame) -> pd.Series:
    """Intraday VWAP, reset each trading day."""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    day = df.index.date
    pv = (typical * df["volume"]).groupby(day).cumsum()
    vol = df["volume"].groupby(day).cumsum().replace(0, np.nan)
    return pv / vol


def supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> pd.DataFrame:
    """Returns columns `supertrend` (line) and `st_dir` (+1 up-trend, -1 down-trend)."""
    a = atr(df, period)
    hl2 = (df["high"] + df["low"]) / 2
    upper = (hl2 + multiplier * a).to_numpy()
    lower = (hl2 - multiplier * a).to_numpy()
    close = df["close"].to_numpy()
    n = len(df)
    final_up = np.full(n, np.nan)
    final_lo = np.full(n, np.nan)
    direction = np.zeros(n)
    line = np.full(n, np.nan)
    for i in range(n):
        if np.isnan(upper[i]):
            continue
        if i == 0 or np.isnan(final_up[i - 1]):
            final_up[i], final_lo[i], direction[i] = upper[i], lower[i], 1
        else:
            final_up[i] = upper[i] if (upper[i] < final_up[i - 1] or close[i - 1] > final_up[i - 1]) else final_up[i - 1]
            final_lo[i] = lower[i] if (lower[i] > final_lo[i - 1] or close[i - 1] < final_lo[i - 1]) else final_lo[i - 1]
            if direction[i - 1] == 1:
                direction[i] = -1 if close[i] < final_lo[i] else 1
            else:
                direction[i] = 1 if close[i] > final_up[i] else -1
        line[i] = final_lo[i] if direction[i] == 1 else final_up[i]
    return pd.DataFrame({"supertrend": line, "st_dir": direction}, index=df.index)
