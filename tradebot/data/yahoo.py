"""Free Yahoo Finance candles for NSE/BSE (no account, no API key).

Tickers: RELIANCE.NS (NSE), RELIANCE.BO (BSE), ^NSEI (NIFTY 50), ^NSEBANK (BANK NIFTY).
Data is delayed and occasionally has gaps. Fine for scanning and backtests;
use Upstox for live paper trading. Intraday (5m) history only goes back ~60 days.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta

import pandas as pd
import requests

from .csv_feed import IST

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
HEADERS = {"User-Agent": "Mozilla/5.0 (tradebot research)"}


class YahooError(RuntimeError):
    pass


def to_yahoo_ticker(symbol: str, exchange: str = "NSE") -> str:
    if symbol.startswith("^") or "." in symbol:
        return symbol
    return f"{symbol}.{'BO' if exchange.upper() == 'BSE' else 'NS'}"


def parse_chart(payload: dict) -> pd.DataFrame:
    """Turn Yahoo's chart JSON into an OHLCV DataFrame indexed in IST."""
    chart = payload.get("chart", {})
    if chart.get("error"):
        raise YahooError(str(chart["error"]))
    res = (chart.get("result") or [None])[0]
    if not res or not res.get("timestamp"):
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    q = res["indicators"]["quote"][0]
    idx = pd.to_datetime(res["timestamp"], unit="s", utc=True).tz_convert(IST)
    df = pd.DataFrame({k: q.get(k) for k in ("open", "high", "low", "close", "volume")}, index=idx)
    df.index.name = "datetime"
    df = df.dropna(subset=["open", "high", "low", "close"]).astype(float)
    return df[~df.index.duplicated(keep="last")].sort_index()


class YahooData:
    def __init__(self, timeout: float = 15.0, pause: float = 0.3):
        self.timeout, self.pause = timeout, pause
        self.session = requests.Session()

    def history(self, ticker: str, interval: str = "1d", start: date | None = None,
                end: date | None = None) -> pd.DataFrame:
        end = end or date.today()
        start = start or (end - timedelta(days=365))
        params = {"interval": interval, "includePrePost": "false",
                  "period1": int(datetime.combine(start, datetime.min.time()).timestamp()),
                  "period2": int(datetime.combine(end + timedelta(days=1), datetime.min.time()).timestamp())}
        r = self.session.get(CHART_URL.format(ticker=ticker), params=params, headers=HEADERS, timeout=self.timeout)
        time.sleep(self.pause)   # be polite; Yahoo rate-limits bursts
        if r.status_code != 200:
            raise YahooError(f"Yahoo {r.status_code} for {ticker}: {r.text[:200]}")
        df = parse_chart(r.json())
        if interval.endswith("d") or interval.endswith("wk"):
            df.index = df.index.normalize()   # one bar per day
        return df
