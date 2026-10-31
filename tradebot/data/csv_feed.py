"""Load OHLCV candles from CSV.

Expected columns (case-insensitive): datetime/date/timestamp, open, high, low, close, volume.
Timestamps without a timezone are treated as IST (Asia/Kolkata).
"""
from __future__ import annotations

import pandas as pd

IST = "Asia/Kolkata"


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [c.strip().lower() for c in df.columns]
    tcol = next((c for c in ("datetime", "timestamp", "date", "time") if c in df.columns), None)
    if tcol is None:
        raise ValueError("CSV needs a datetime/date/timestamp column")
    idx = pd.to_datetime(df[tcol])
    idx = idx.dt.tz_localize(IST) if idx.dt.tz is None else idx.dt.tz_convert(IST)
    df.index = pd.DatetimeIndex(idx, name="datetime")
    if "volume" not in df.columns:
        df["volume"] = 0
    out = df[["open", "high", "low", "close", "volume"]].astype(float)
    return out[~out.index.duplicated(keep="last")].sort_index()


def load_csv(path: str) -> pd.DataFrame:
    return normalize(pd.read_csv(path))
