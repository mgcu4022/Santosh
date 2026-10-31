"""Google Finance data via a Google Sheet (free, no API key).

Google has no public Finance API, but Google Sheets has GOOGLEFINANCE(), which
returns daily NSE/BSE candles. You put the formulas in a sheet, share it as
CSV, and the bot reads that CSV.

Setup (once):
  1. python -m tradebot google-sheet --universe config/universe_nifty50.txt > sheet.tsv
  2. New Google Sheet -> click cell A1 -> paste the contents of sheet.tsv.
     Row 1 holds symbols; row 2 holds one GOOGLEFINANCE formula per symbol.
  3. File -> Share -> Publish to web -> this sheet -> "Comma-separated values (.csv)"
     -> Publish, and copy the link.
  4. Use the link as  source: {type: google, url: <link>}  or  scan --google-url <link>.

Limits: daily candles only (no intraday history), quotes delayed up to ~20 min,
and Google may return #N/A for some symbols. Good for scanning and daily
backtests, not for intraday trading.
"""
from __future__ import annotations

import io
import re
from datetime import date

import pandas as pd
import requests

from .csv_feed import IST

BLOCK_WIDTH = 7          # 6 GOOGLEFINANCE columns + 1 blank spacer
FIELDS = ["date", "open", "high", "low", "close", "volume"]


class GoogleSheetError(RuntimeError):
    pass


def google_ticker(symbol: str, exchange: str = "NSE") -> str:
    """RELIANCE -> NSE:RELIANCE. Indices: NIFTY -> INDEXNSE:NIFTY_50."""
    if ":" in symbol:
        return symbol
    indices = {"NIFTY": "INDEXNSE:NIFTY_50", "NIFTY50": "INDEXNSE:NIFTY_50",
               "BANKNIFTY": "INDEXNSE:NIFTY_BANK", "SENSEX": "INDEXBOM:SENSEX"}
    if symbol.upper() in indices:
        return indices[symbol.upper()]
    return f"{'BOM' if exchange.upper() == 'BSE' else 'NSE'}:{symbol.upper()}"


def sheet_layout(symbols: list[str], start: date, exchange: str = "NSE") -> str:
    """Two tab-separated rows to paste at A1: symbols, then GOOGLEFINANCE formulas."""
    row1, row2 = [], []
    d = f"DATE({start.year},{start.month},{start.day})"
    for s in symbols:
        row1 += [s] + [""] * (BLOCK_WIDTH - 1)
        row2 += [f'=GOOGLEFINANCE("{google_ticker(s, exchange)}","all",{d},TODAY(),"DAILY")'] + [""] * (BLOCK_WIDTH - 1)
    return "\t".join(row1).rstrip("\t") + "\n" + "\t".join(row2).rstrip("\t") + "\n"


def _detect_dayfirst(values: pd.Series, default: bool = True) -> bool:
    """Sheets writes dates in the sheet's locale: 31/10/2026 (India) or 10/31/2026 (US)."""
    firsts, seconds = [], []
    for v in values.dropna().astype(str).head(400):
        m = re.match(r"\s*(\d{1,2})[/.-](\d{1,2})[/.-]\d{2,4}", v)
        if m:
            firsts.append(int(m.group(1)))
            seconds.append(int(m.group(2)))
    if any(x > 12 for x in firsts):
        return True
    if any(x > 12 for x in seconds):
        return False
    return default


def _clean_block(block: pd.DataFrame, dayfirst: bool | None) -> pd.DataFrame:
    block = block.iloc[:, :6].copy()
    block.columns = FIELDS
    block = block[block["date"].notna() & (block["date"].astype(str).str.strip() != "")]
    block = block[~block["date"].astype(str).str.strip().str.lower().isin(["date", "#n/a", "#error!"])]
    if block.empty:
        return pd.DataFrame(columns=FIELDS[1:])
    raw = block["date"].astype(str).str.strip()
    iso = raw.str.match(r"^\d{4}-\d{2}-\d{2}")
    df_first = _detect_dayfirst(raw) if dayfirst is None else dayfirst
    dates = pd.to_datetime(raw, format="ISO8601" if iso.all() else None,
                           dayfirst=False if iso.all() else df_first, errors="coerce")
    out = block[FIELDS[1:]].apply(lambda c: pd.to_numeric(c.astype(str).str.replace(",", ""), errors="coerce"))
    out.index = pd.DatetimeIndex(dates).normalize().tz_localize(IST)
    out.index.name = "datetime"
    out = out[out.index.notna()].dropna(subset=["open", "high", "low", "close"])
    out["volume"] = out["volume"].fillna(0)
    return out[~out.index.duplicated(keep="last")].sort_index()


def parse_sheet_csv(text: str, dayfirst: bool | None = None) -> dict[str, pd.DataFrame]:
    """Parse a published sheet.

    Two layouts are accepted:
      single: header row Date,Open,High,Low,Close,Volume (one stock; key "_single")
      multi:  row 1 = symbols every 7 columns, row 2 = headers, then data (from sheet_layout)
    """
    raw = pd.read_csv(io.StringIO(text), header=None, dtype=str, keep_default_na=False)
    if raw.empty:
        return {}
    first = [str(x).strip().lower() for x in raw.iloc[0]]
    if first[:1] == ["date"]:
        return {"_single": _clean_block(raw.iloc[1:], dayfirst)}
    out = {}
    for col in range(0, raw.shape[1], BLOCK_WIDTH):
        sym = str(raw.iat[0, col]).strip()
        if not sym:
            continue
        block = raw.iloc[2:, col:col + 6]
        if block.shape[1] < 6:
            continue
        df = _clean_block(block, dayfirst)
        if len(df):
            out[sym.upper()] = df
    return out


class GoogleSheetData:
    def __init__(self, url: str, timeout: float = 30.0, dayfirst: bool | None = None):
        self.url = to_csv_url(url)
        self.timeout = timeout
        self.dayfirst = dayfirst
        self._cache: dict[str, pd.DataFrame] | None = None

    def all(self) -> dict[str, pd.DataFrame]:
        if self._cache is None:
            r = requests.get(self.url, timeout=self.timeout)
            if r.status_code != 200 or r.text.lstrip().startswith("<"):
                raise GoogleSheetError(
                    f"Could not read sheet ({r.status_code}). Publish it to the web as CSV "
                    "(File -> Share -> Publish to web) or share it as 'Anyone with the link'.")
            self._cache = parse_sheet_csv(r.text, self.dayfirst)
        return self._cache

    def history(self, symbol: str) -> pd.DataFrame:
        data = self.all()
        if "_single" in data:
            return data["_single"]
        key = symbol.upper()
        if key not in data:
            raise GoogleSheetError(f"{symbol} not in sheet (has: {', '.join(list(data)[:10])} ...)")
        return data[key]


def to_csv_url(url: str) -> str:
    """Accept a normal sheet link too: .../spreadsheets/d/<id>/edit#gid=0 -> CSV export link."""
    if "output=csv" in url or "format=csv" in url:
        return url
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    if m and "/d/e/" not in url:
        gid = re.search(r"gid=(\d+)", url)
        return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv&gid={gid.group(1) if gid else 0}"
    if "/pub" in url:
        return url + ("&" if "?" in url else "?") + "output=csv"
    return url
