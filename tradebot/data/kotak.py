"""Kotak Neo live quotes (backup data source for paper trading).

Kotak Neo's API is free for Kotak Securities clients but has no historical
candle endpoint, so it is used only as a live backup: quotes are polled and
turned into candles by BarBuilder.

Install the official SDK:
  pip install "git+https://github.com/Kotak-Neo/kotak-neo-api.git#egg=neo_api_client" pyotp

Credentials come from environment variables only (never files):
  KOTAK_CONSUMER_KEY   from the Neo API section of the Kotak Neo app/web
  KOTAK_MOBILE         registered mobile, e.g. +919999999999
  KOTAK_UCC            your client code
  KOTAK_MPIN           6-digit MPIN
  KOTAK_TOTP_SECRET    optional: authenticator secret, to create the TOTP automatically
                       (otherwise you're asked to type the 6-digit TOTP at start-up)

Each instrument needs its Kotak token in the config:
  backup: {kotak_token: "2885", kotak_segment: nse_cm}     # nse_cm, bse_cm, nse_fo
Find tokens with:  python -m tradebot kotak-find RELIANCE

The SDK login and quote calls follow Kotak's published SDK (TOTP login flow).
They have not been tested against the live API from this project, so check
`python -m tradebot kotak-test` works before relying on it.
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
        try:
            from neo_api_client import NeoAPI
        except ImportError as e:
            raise KotakError("Kotak SDK not installed (see tradebot/data/kotak.py)") from e
        env = {k: os.environ.get(k, "") for k in ("KOTAK_CONSUMER_KEY", "KOTAK_MOBILE", "KOTAK_UCC", "KOTAK_MPIN")}
        missing = [k for k, v in env.items() if not v]
        if missing:
            raise KotakError(f"Set environment variables: {', '.join(missing)}")
        secret = os.environ.get("KOTAK_TOTP_SECRET")
        if secret:
            import pyotp
            totp = pyotp.TOTP(secret).now()
        else:
            totp = input("Kotak Neo TOTP (6 digits from your authenticator app): ").strip()
        client = NeoAPI(environment="prod", access_token=None, neo_fin_key=None,
                        consumer_key=env["KOTAK_CONSUMER_KEY"])
        client.totp_login(mobile_number=env["KOTAK_MOBILE"], ucc=env["KOTAK_UCC"], totp=totp)
        client.totp_validate(mpin=env["KOTAK_MPIN"])
        log.info("Kotak Neo login OK")
        return client

    def ltp(self, tokens: list[tuple[str, str]]) -> dict[str, float]:
        """tokens: [(token, segment)], e.g. [("2885", "nse_cm")] -> {"2885": 1401.5}"""
        req = [{"instrument_token": str(t), "exchange_segment": seg} for t, seg in tokens]
        return parse_quotes(self.client.quotes(instrument_tokens=req, quote_type="ltp"))

    def find(self, symbol: str, segment: str = "nse_cm") -> list[dict]:
        """Search Kotak's scrip master for a symbol's token."""
        import pandas as pd
        url = self.client.scrip_master(exchange_segment=segment)
        if isinstance(url, dict):
            url = url.get("filesPaths", [url.get("data")])[0] if url.get("filesPaths") else url.get("data")
        df = pd.read_csv(url, low_memory=False)
        cols = [c for c in df.columns if "sym" in c.lower() or "name" in c.lower()]
        mask = False
        for c in cols:
            mask = mask | df[c].astype(str).str.upper().str.contains(symbol.upper(), regex=False)
        keep = [c for c in df.columns if c.lower() in ("ptoken", "psymbol", "ptrdsymbol", "psymbolname",
                                                       "lotsize", "llotsize", "pexpirydate")] or cols
        return df.loc[mask, keep].head(20).to_dict("records")
