"""NSE bulk and block deals: who bought or sold large quantities.

This is the Indian counterpart of "insider/politician trade trackers". Use it
as CONTEXT for research (who is accumulating or exiting), not as a buy/sell
signal: deals are disclosed after the fact, and copying them has no proven
intraday edge.

Bulk deal: a client trades > 0.5% of a company's shares in one day.
Block deal: a single trade of at least Rs 10 crore in the special block window.
NSE publishes the latest day's lists as CSV files.
"""
from __future__ import annotations

import io

import pandas as pd
import requests

URLS = {
    "bulk": "https://archives.nseindia.com/content/equities/bulk.csv",
    "block": "https://archives.nseindia.com/content/equities/block.csv",
}
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "text/csv,*/*", "Referer": "https://www.nseindia.com/"}

# NSE column names vary slightly between files and over time; map loosely.
_COLS = {
    "date": ("date",),
    "symbol": ("symbol",),
    "name": ("security name", "name"),
    "client": ("client name", "client"),
    "side": ("buy/sell", "buy / sell", "buysell"),
    "qty": ("quantity traded", "quantity"),
    "price": ("trade price / wght. avg. price", "trade price", "wght. avg. price", "price"),
}


def parse_deals(text: str, kind: str) -> pd.DataFrame:
    df = pd.read_csv(io.StringIO(text))
    norm = {c: c.strip().lower() for c in df.columns}
    df = df.rename(columns=norm)
    out = pd.DataFrame()
    for key, options in _COLS.items():
        col = next((c for c in df.columns if c in options or any(c.startswith(o) for o in options)), None)
        if col is not None:
            out[key] = df[col]
    if out.empty or "symbol" not in out:
        return pd.DataFrame(columns=list(_COLS) + ["kind", "value_cr"])
    out["symbol"] = out["symbol"].astype(str).str.strip().str.upper()
    out["side"] = out["side"].astype(str).str.strip().str.upper().str[0].map({"B": "BUY", "S": "SELL"})
    out["qty"] = pd.to_numeric(out["qty"].astype(str).str.replace(",", ""), errors="coerce")
    out["price"] = pd.to_numeric(out["price"].astype(str).str.replace(",", ""), errors="coerce")
    out["value_cr"] = (out["qty"] * out["price"] / 1e7).round(2)
    out["kind"] = kind
    return out.dropna(subset=["qty", "price"])


def fetch_deals(kinds=("bulk", "block"), timeout: float = 20.0) -> pd.DataFrame:
    frames = []
    s = requests.Session()
    for k in kinds:
        r = s.get(URLS[k], headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        frames.append(parse_deals(r.text, k))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def summarize(deals: pd.DataFrame, symbol: str | None = None) -> pd.DataFrame:
    """Net buy/sell value per symbol (Rs crore), largest first."""
    d = deals if symbol is None else deals[deals["symbol"] == symbol.upper()]
    if d.empty:
        return pd.DataFrame()
    d = d.assign(buy_cr=d["value_cr"].where(d["side"] == "BUY", 0.0),
                 sell_cr=d["value_cr"].where(d["side"] == "SELL", 0.0))
    g = d.groupby("symbol").agg(deals=("qty", "size"), buy_cr=("buy_cr", "sum"), sell_cr=("sell_cr", "sum"))
    g["net_cr"] = g["buy_cr"] - g["sell_cr"]
    return g.round(2).sort_values("net_cr", key=abs, ascending=False)
