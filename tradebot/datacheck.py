"""Compare live data sources side by side: Kotak Neo, Upstox and Yahoo.

For each source and round: did it answer, how long it took, and how far its
price is from the others (median of all sources = reference). Run it a few times
during the 10-day trial; results are appended to <runs_dir>/datacheck.csv so the
days can be compared.

  python -m tradebot datacheck -c config/trial_20k.yaml --minutes 3
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

import pandas as pd

IST = ZoneInfo("Asia/Kolkata")


def build_sources(specs) -> dict:
    """name -> callable() returning {symbol: price}. Unavailable sources are skipped with a reason."""
    sources, skipped = {}, {}
    kotak_syms = {s.instrument.symbol: str(s.backup["kotak_token"]) for s in specs if (s.backup or {}).get("kotak_token")}
    if kotak_syms:
        try:
            from .data.kotak import KotakNeoQuotes
            k = KotakNeoQuotes()
            k.client
            tok2sym = {t: s for s, t in kotak_syms.items()}

            def kotak():
                return {tok2sym[t]: p for t, p in k.ltp([(t, "nse_cm") for t in tok2sym]).items() if t in tok2sym}
            sources["kotak"] = kotak
        except Exception as e:
            skipped["kotak"] = str(e)[:120]
    up_keys = {s.instrument.symbol: s.data_key for s in specs if s.data_key}
    if up_keys:
        from .upstox_auth import saved_token
        if saved_token():
            from .data.upstox import UpstoxData
            u = UpstoxData()

            def upstox():
                got = u.ltp(list(up_keys.values()))
                out = {}
                for sym, key in up_keys.items():   # response keys may be NSE_EQ|ISIN or NSE_EQ:SYMBOL
                    for k2, p in got.items():
                        if k2 in (key, key.replace("|", ":")) or k2.endswith(":" + sym):
                            out[sym] = p
                return out
            sources["upstox"] = upstox
        else:
            skipped["upstox"] = "no UPSTOX_ACCESS_TOKEN (segments inactive / token not generated yet)"
    syms = [s.instrument.symbol for s in specs]
    from .data.yahoo import YahooData, to_yahoo_ticker
    y = YahooData(pause=0.1)
    exch = {s.instrument.symbol: s.instrument.exchange for s in specs}

    def yahoo():
        out = {}
        for sym in syms:
            df = y.history(to_yahoo_ticker(sym, exch[sym]), "1m", datetime.now(IST).date(), datetime.now(IST).date())
            if len(df):
                out[sym] = float(df["close"].iloc[-1])
        return out
    sources["yahoo"] = yahoo
    return {"sources": sources, "skipped": skipped}


def run_rounds(sources: dict, rounds: int, every_s: float, clock=time.monotonic, sleep=time.sleep,
               now=lambda: datetime.now(IST)) -> pd.DataFrame:
    rows = []
    for r in range(rounds):
        ts = now()
        for name, fn in sources.items():
            t0 = clock()
            try:
                prices, err = fn(), ""
            except Exception as e:
                prices, err = {}, str(e)[:120]
            ms = (clock() - t0) * 1000
            if not prices:
                rows.append({"time": ts, "round": r, "source": name, "symbol": None, "price": None, "ms": ms,
                             "ok": False, "error": err or "no prices"})
            for sym, p in prices.items():
                rows.append({"time": ts, "round": r, "source": name, "symbol": sym, "price": p, "ms": ms,
                             "ok": True, "error": ""})
        if r < rounds - 1:
            sleep(every_s)
    return pd.DataFrame(rows)


def summarize(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    ok = df[df["ok"]].copy()
    if len(ok):
        ref = ok.groupby(["round", "symbol"])["price"].transform(median)
        ok["diff_bps"] = (ok["price"] / ref - 1).abs() * 10_000
    out = []
    for name, g in df.groupby("source"):
        calls = g.groupby("round")["ok"].any()
        okg = ok[ok["source"] == name] if len(ok) else ok
        out.append({"source": name, "rounds_ok_%": round(calls.mean() * 100, 1),
                    "avg_ms": round(g.groupby("round")["ms"].first().mean(), 0),
                    "symbols_per_round": round(okg.groupby("round")["symbol"].nunique().mean(), 1) if len(okg) else 0,
                    "median_gap_bps": round(okg["diff_bps"].median(), 1) if len(okg) else None,
                    "max_gap_bps": round(okg["diff_bps"].max(), 1) if len(okg) else None,
                    "last_error": next((e for e in reversed(g["error"].tolist()) if e), "")})
    return pd.DataFrame(out).set_index("source").sort_values(["rounds_ok_%", "avg_ms"], ascending=[False, True])


def save(df: pd.DataFrame, runs_dir: str) -> Path:
    p = Path(runs_dir) / "datacheck.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(p, mode="a", header=not p.exists(), index=False)
    return p
