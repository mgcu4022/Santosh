"""Upstox market data (read-only): historical / intraday candles and LTP.

Docs: https://upstox.com/developer/api-documentation/
Instrument keys look like "NSE_EQ|INE002A01018" (RELIANCE) or "NSE_INDEX|Nifty 50".
Find keys in the instrument master:
https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz

The access token is created daily via the Upstox login (OAuth) flow and set
in the UPSTOX_ACCESS_TOKEN environment variable. It is never written to disk.
"""
from __future__ import annotations

import gzip
import io
import json
import os
import time
from datetime import date, timedelta
from urllib.parse import quote

import pandas as pd
import requests

from .csv_feed import IST

BASE = "https://api.upstox.com"
INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"


class UpstoxError(RuntimeError):
    pass


class UpstoxData:
    def __init__(self, access_token: str | None = None, timeout: float = 15.0):
        self.token = access_token or os.environ.get("UPSTOX_ACCESS_TOKEN", "")
        self.timeout = timeout
        self.session = requests.Session()

    def _get(self, path: str, params: dict | None = None) -> dict:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        for attempt in range(4):
            r = self.session.get(BASE + path, params=params, headers=headers, timeout=self.timeout)
            if r.status_code == 429:            # rate limited: back off
                time.sleep(2 ** attempt)
                continue
            break
        if r.status_code != 200:
            raise UpstoxError(f"Upstox {r.status_code}: {r.text[:300]}")
        body = r.json()
        if body.get("status") != "success":
            raise UpstoxError(f"Upstox error: {body}")
        return body["data"]

    @staticmethod
    def _candles_to_df(candles: list) -> pd.DataFrame:
        if not candles:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df = pd.DataFrame(candles).iloc[:, :6]
        df.columns = ["datetime", "open", "high", "low", "close", "volume"]
        df["datetime"] = pd.to_datetime(df["datetime"]).dt.tz_convert(IST)
        df = df.set_index("datetime").astype(float).sort_index()
        return df[~df.index.duplicated(keep="last")]

    def historical(self, instrument_key: str, unit: str = "minutes", interval: int = 5,
                   from_date: date | None = None, to_date: date | None = None) -> pd.DataFrame:
        """unit: minutes | hours | days | weeks | months.

        Upstox limits how much history one call returns for minute candles,
        so long ranges are fetched in 28-day chunks.
        """
        to_date = to_date or date.today()
        from_date = from_date or (to_date - timedelta(days=30))
        chunk = timedelta(days=28 if unit in ("minutes", "hours") else 3650)
        frames, end = [], to_date
        key = quote(instrument_key, safe="")
        while end >= from_date:
            start = max(from_date, end - chunk)
            path = f"/v3/historical-candle/{key}/{unit}/{interval}/{end:%Y-%m-%d}/{start:%Y-%m-%d}"
            frames.append(self._candles_to_df(self._get(path).get("candles", [])))
            end = start - timedelta(days=1)
        df = pd.concat(frames).sort_index() if frames else self._candles_to_df([])
        return df[~df.index.duplicated(keep="last")]

    def intraday(self, instrument_key: str, unit: str = "minutes", interval: int = 5) -> pd.DataFrame:
        """Today's candles so far."""
        key = quote(instrument_key, safe="")
        return self._candles_to_df(self._get(f"/v3/historical-candle/intraday/{key}/{unit}/{interval}").get("candles", []))

    def ltp(self, instrument_keys: list[str]) -> dict[str, float]:
        """Last traded price. Requires an access token."""
        data = self._get("/v2/market-quote/ltp", params={"instrument_key": ",".join(instrument_keys)})
        out = {}
        for v in data.values():
            k = v.get("instrument_token") or v.get("instrument_key")
            out[k] = float(v["last_price"])
        return out


def find_instrument_key(trading_symbol: str, segment: str = "NSE_EQ", path: str | None = None) -> list[dict]:
    """Search the Upstox instrument master by trading symbol (e.g. 'RELIANCE', 'NIFTY')."""
    if path:
        raw = open(path, "rb").read()
    else:
        raw = requests.get(INSTRUMENTS_URL, timeout=60).content
    items = json.load(io.BytesIO(gzip.decompress(raw)))
    sym = trading_symbol.upper()
    return [i for i in items
            if i.get("segment") == segment and sym in str(i.get("trading_symbol", "")).upper()][:25]
