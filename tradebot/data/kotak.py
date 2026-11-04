"""Kotak Neo live quotes (data source for paper trading). Read-only.

Uses Kotak's official Python SDK `kotakneoapi` (module `neo_api_client`, Python 3.10+):
  pip install kotakneoapi

Reading quotes and the scrip master needs ONLY the consumer key (no TOTP, no MPIN),
so nothing else is stored. Get it in the Kotak Neo app/web: More -> Trade API ->
create/generate application -> copy the token. Put it in .env:
  KOTAK_CONSUMER_KEY=...

Kotak has no historical candle endpoint, so quotes are polled and turned into
5-minute candles by BarBuilder; earlier days come from Yahoo.

Each instrument needs its Kotak token in the config:
  backup: {kotak_token: "2885", kotak_segment: nse_cm}     # nse_cm, bse_cm, nse_fo, bse_fo
Find tokens with:  python -m tradebot kotak-find RELIANCE
Check prices with: python -m tradebot kotak-test
Limits (Kotak docs): 50 instruments per quotes call, 25 requests per second.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


class KotakError(RuntimeError):
    pass


def parse_quotes(resp) -> dict[str, float]:
    """Pull {token: last price} out of a quotes response, tolerating format changes."""
    if isinstance(resp, dict):
        if resp.get("error") or resp.get("Error"):
            raise KotakError(str(resp.get("error") or resp.get("Error")))
        items = resp.get("message") or resp.get("data") or []
    else:
        items = resp or []
    if isinstance(items, dict):
        items = [items]
    out = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        tok = it.get("exchange_token") or it.get("instrument_token") or it.get("tk")
        px = it.get("ltp") or it.get("last_traded_price") or it.get("ltP")
        try:
            if tok is not None and px not in (None, ""):
                out[str(tok)] = float(px)
        except (TypeError, ValueError):
            continue
    return out


def match_tokens(df, symbols: list[str]) -> dict[str, str]:
    """Find each symbol's token in Kotak's scrip master (column names vary a little between versions)."""
    cols = {c.lower(): c for c in df.columns}
    pick = lambda *names: next((cols[n] for n in names if n in cols), None)
    tok_c = pick("psymbol", "ptoken", "token", "instrument_token", "exchange_token")
    trd_c = pick("ptrdsymbol", "trading_symbol", "tradingsymbol")
    name_c = pick("psymbolname", "symbol_name", "symbol")
    grp_c = pick("pgroup", "series", "pseries")
    if tok_c is None or (trd_c is None and name_c is None):
        raise KotakError(f"Unrecognised scrip master columns: {list(df.columns)[:15]}")
    out: dict[str, str] = {}
    trd = df[trd_c].astype(str).str.upper().str.strip() if trd_c else None
    name = df[name_c].astype(str).str.upper().str.strip() if name_c else None
    grp = df[grp_c].astype(str).str.upper().str.strip() if grp_c else None
    for sym in symbols:
        s = sym.upper()
        hit = df[trd == f"{s}-EQ"] if trd is not None else df.iloc[0:0]
        if hit.empty and name is not None:
            mask = name == s
            if grp is not None:
                mask &= grp.isin(["EQ", "NAN", ""])
            hit = df[mask]
        if not hit.empty:
            out[sym] = str(hit.iloc[0][tok_c]).split(".")[0]
    return out


def scrip_url(resp, segment: str) -> str:
    """scrip_master() returns either the file URL or {"filesPaths": [...]}: pick this segment's CSV."""
    if isinstance(resp, str):
        return resp
    if isinstance(resp, dict):
        paths = resp.get("filesPaths") or ([resp["data"]] if isinstance(resp.get("data"), str) else [])
        for p in paths:
            if f"/{segment}" in p.lower() or p.lower().endswith(f"{segment}.csv"):
                return p
        if paths:
            return paths[0]
    raise KotakError(f"Unexpected scrip master response: {str(resp)[:200]}")


class KotakNeoQuotes:
    def __init__(self, client=None):
        self._client = client

    @property
    def client(self):
        if self._client is None:
            self._client = self._login()
        return self._client

    @staticmethod
    def _login():
        """Create the SDK client. Quotes need only the consumer key: no TOTP/MPIN login."""
        try:
            from neo_api_client import NeoAPI
        except ImportError as e:
            raise KotakError("Kotak SDK not installed: pip install kotakneoapi") from e
        key = os.environ.get("KOTAK_CONSUMER_KEY", "")
        if not key:
            raise KotakError("Set KOTAK_CONSUMER_KEY in .env (Kotak Neo: More -> Trade API -> copy token)")
        client = NeoAPI(consumer_key=key, environment="prod", access_token=None, neo_fin_key=None)
        log.info("Kotak Neo client ready (quotes only)")
        return client

    def ltp(self, tokens: list[tuple[str, str]]) -> dict[str, float]:
        """tokens: [(token, segment)], e.g. [("2885", "nse_cm")] -> {"2885": 1401.5}"""
        out: dict[str, float] = {}
        for i in range(0, len(tokens), 50):           # Kotak limit: 50 instruments per call
            req = [{"instrument_token": str(t), "exchange_segment": seg} for t, seg in tokens[i:i + 50]]
            out.update(parse_quotes(self.client.quotes(instrument_tokens=req, quote_type="ltp")))
        return out

    def scrip_master_df(self, segment: str = "nse_cm"):
        import pandas as pd
        return pd.read_csv(scrip_url(self.client.scrip_master(exchange_segment=segment), segment), low_memory=False)

    def tokens_for(self, symbols: list[str], segment: str = "nse_cm") -> dict[str, str]:
        """NSE symbol -> Kotak instrument token (cash segment, EQ series)."""
        return match_tokens(self.scrip_master_df(segment), symbols)

    def find(self, symbol: str, segment: str = "nse_cm") -> list[dict]:
        """Search Kotak's scrip master for a symbol's token."""
        import pandas as pd
        url = scrip_url(self.client.scrip_master(exchange_segment=segment), segment)
        df = pd.read_csv(url, low_memory=False)
        cols = [c for c in df.columns if "sym" in c.lower() or "name" in c.lower()]
        mask = False
        for c in cols:
            mask = mask | df[c].astype(str).str.upper().str.contains(symbol.upper(), regex=False)
        keep = [c for c in df.columns if c.lower() in ("ptoken", "psymbol", "ptrdsymbol", "psymbolname",
                                                       "lotsize", "llotsize", "pexpirydate")] or cols
        return df.loc[mask, keep].head(20).to_dict("records")
