"""Official NSE data: Bhavcopy (end-of-day prices), F&O ban list, events.

Free, from NSE's own website. NSE blocks obvious bots, so requests use a
browser-like session and pause between calls. If NSE changes a URL or format,
only the small parsers below need updating.

  Bhavcopy (UDiFF format, since July 2024), one zip per trading day:
    https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip
  F&O ban list for the next trading day:
    https://nsearchives.nseindia.com/content/fo/fo_secban.csv
  Results / board meetings, corporate actions, announcements (JSON, needs site cookies):
    https://www.nseindia.com/api/event-calendar?index=equities&symbol=SYMBOL
"""
from __future__ import annotations

import io
import logging
import re
import time
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

from .csv_feed import IST

log = logging.getLogger(__name__)

ARCHIVE = "https://nsearchives.nseindia.com"
SITE = "https://www.nseindia.com"
BHAV_URL = ARCHIVE + "/content/cm/BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
BAN_URL = ARCHIVE + "/content/fo/fo_secban.csv"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36",
    "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9", "Referer": SITE + "/",
}


class NseError(RuntimeError):
    pass


# --- parsers (pure functions, unit-tested) ------------------------------------------
def parse_bhavcopy(raw: bytes) -> pd.DataFrame:
    """UDiFF bhavcopy (zip or csv bytes) -> equity rows: symbol, open, high, low, close, volume, name."""
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            raw = z.read(z.namelist()[0])
    df = pd.read_csv(io.BytesIO(raw))
    df.columns = [c.strip() for c in df.columns]
    if "TckrSymb" not in df.columns:
        raise NseError("Unexpected bhavcopy format (no TckrSymb column)")
    if "SctySrs" in df.columns:
        df = df[df["SctySrs"].astype(str).str.strip().isin(["EQ", "BE"])]   # regular equity series
    out = pd.DataFrame({
        "date": pd.to_datetime(df["TradDt"]).dt.date,
        "symbol": df["TckrSymb"].astype(str).str.strip(),
        "open": df["OpnPric"], "high": df["HghPric"], "low": df["LwPric"], "close": df["ClsPric"],
        "volume": df["TtlTradgVol"],
        "name": df.get("FinInstrmNm", pd.Series([""] * len(df), index=df.index)),
    })
    return out.reset_index(drop=True)


def bhav_to_daily(frames: list[pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Stack many days of bhavcopy into one OHLCV DataFrame per symbol."""
    if not frames:
        return {}
    allf = pd.concat(frames, ignore_index=True)
    out = {}
    for sym, g in allf.groupby("symbol"):
        g = g.sort_values("date").drop_duplicates("date", keep="last")
        idx = pd.DatetimeIndex(pd.to_datetime(g["date"])).tz_localize(IST)
        idx.name = "datetime"
        out[sym] = pd.DataFrame(g[["open", "high", "low", "close", "volume"]].astype(float).values,
                                index=idx, columns=["open", "high", "low", "close", "volume"])
    return out


def parse_ban_list(text: str) -> set[str]:
    """fo_secban.csv: a title line, then rows like '1,SYMBOL'."""
    syms = set()
    for line in text.splitlines()[1:]:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2 and re.fullmatch(r"[A-Z0-9&\-]+", parts[1]):
            syms.add(parts[1])
    return syms


def _pick(d: dict, *keys, default=""):
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return default


def _parse_nse_date(s: str) -> date | None:
    s = str(s).strip()
    for fmt in ("%d-%b-%Y", "%d-%b-%Y %H:%M:%S", "%d-%m-%Y", "%Y-%m-%d", "%d %b %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_events(items: list[dict]) -> list[dict]:
    """Event-calendar / board-meeting JSON -> [{date, purpose, detail}]."""
    out = []
    for it in items or []:
        d = _parse_nse_date(_pick(it, "date", "bm_date", "meetingDate"))
        if d:
            out.append({"date": d, "purpose": str(_pick(it, "purpose", "bm_purpose")).strip(),
                        "detail": str(_pick(it, "bm_desc", "desc", "description"))[:300].strip()})
    return sorted(out, key=lambda e: e["date"])


def parse_corporate_actions(items: list[dict]) -> list[dict]:
    out = []
    for it in items or []:
        d = _parse_nse_date(_pick(it, "exDate", "ex_date"))
        if d:
            out.append({"date": d, "purpose": str(_pick(it, "subject", "purpose")).strip(), "detail": "ex-date"})
    return sorted(out, key=lambda e: e["date"])


def parse_announcements(items: list[dict], limit: int = 5) -> list[dict]:
    out = []
    for it in items or []:
        out.append({"time": str(_pick(it, "an_dt", "sort_date", "dt")),
                    "title": str(_pick(it, "desc", "subject")).strip(),
                    "detail": str(_pick(it, "attchmntText", "smIndustry"))[:300].strip()})
    return out[:limit]


# --- client ----------------------------------------------------------------------------
class NseClient:
    def __init__(self, cache_dir: str = "data/nse", pause: float = 0.6, timeout: float = 20.0):
        self.cache = Path(cache_dir)
        self.pause, self.timeout = pause, timeout
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self._warm = False

    def _get(self, url: str, json_api: bool = False):
        if json_api and not self._warm:
            self.s.get(SITE, timeout=self.timeout)   # sets cookies the JSON API needs
            self._warm = True
        r = self.s.get(url, timeout=self.timeout)
        time.sleep(self.pause)
        if r.status_code != 200:
            raise NseError(f"NSE {r.status_code} for {url}")
        return r.json() if json_api else r

    # Bhavcopy -------------------------------------------------------------
    def bhavcopy(self, d: date) -> pd.DataFrame | None:
        """One day's bhavcopy, cached on disk. None on holidays/weekends."""
        path = self.cache / "bhav" / f"{d:%Y%m%d}.csv"
        if path.exists():
            df = pd.read_csv(path)
            df["date"] = pd.to_datetime(df["date"]).dt.date
            return df
        if d.weekday() >= 5:
            return None
        try:
            df = parse_bhavcopy(self._get(BHAV_URL.format(d=d)).content)
        except NseError as e:
            log.info("No bhavcopy for %s (%s)", d, e)   # holiday or not yet published
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(path, index=False)
        return df

    def daily_history(self, days: int = 120, end: date | None = None) -> dict[str, pd.DataFrame]:
        """Daily candles for every NSE equity from the last `days` calendar days of bhavcopies."""
        end = end or date.today()
        frames = [f for f in (self.bhavcopy(end - timedelta(days=k)) for k in range(days)) if f is not None]
        return bhav_to_daily(frames)

    # Ban list & events -----------------------------------------------------
    def ban_list(self) -> set[str]:
        return parse_ban_list(self._get(BAN_URL).text)

    def events(self, symbol: str) -> list[dict]:
        q = requests.utils.quote(symbol)
        cal = self._get(f"{SITE}/api/event-calendar?index=equities&symbol={q}", json_api=True)
        acts = self._get(f"{SITE}/api/corporates-corporateActions?index=equities&symbol={q}", json_api=True)
        return sorted(parse_events(cal) + parse_corporate_actions(acts), key=lambda e: e["date"])

    def announcements(self, symbol: str, limit: int = 5) -> list[dict]:
        q = requests.utils.quote(symbol)
        data = self._get(f"{SITE}/api/corporate-announcements?index=equities&symbol={q}", json_api=True)
        return parse_announcements(data if isinstance(data, list) else data.get("data", []), limit)
