"""Stock scanner: rank a universe of NSE stocks for today's watchlist.

Works on daily candles (last row = most recent completed session). It does not
predict anything; it surfaces liquid stocks that are *moving* or *coiled*, which
is where intraday strategies like ORB have room to work.

Setups:
  breakout20   close above the prior 20-day high
  breakdown20  close below the prior 20-day low
  gap          open gapped >= gap_pct vs previous close
  vol_spike    volume >= vol_mult x 20-day average
  nr7          narrowest daily range of the last 7 days (volatility often expands next)
  near_52w_high  within 3% of the 52-week high
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .indicators import atr, ema


@dataclass
class ScanConfig:
    min_price: float = 50.0
    max_price: float | None = None    # e.g. 200 for a small-capital account
    min_turnover_cr: float = 10.0     # 20-day avg traded value, Rs crore/day
    min_atr_pct: float = 1.0          # too quiet = no room for intraday moves
    max_atr_pct: float = 6.0          # too wild = stops get hit by noise
    gap_pct: float = 1.0
    vol_mult: float = 2.0
    top: int = 10


def load_universe(path: str) -> list[str]:
    syms = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if line:
            syms.append(line.upper())
    return syms


def scan_one(df: pd.DataFrame, cfg: ScanConfig) -> dict | None:
    """Metrics and setup tags for one stock. Needs ~60+ daily bars."""
    if len(df) < 60:
        return None
    d = df.copy()
    d["atr"] = atr(d, 14)
    d["ema50"] = ema(d["close"], 50)
    last, prev = d.iloc[-1], d.iloc[-2]
    hist = d.iloc[:-1]                         # everything before the last session
    avg_vol = hist["volume"].tail(20).mean()
    turnover_cr = float((hist["close"] * hist["volume"]).tail(20).mean() / 1e7)
    rng = d["high"] - d["low"]
    hi20, lo20 = hist["high"].tail(20).max(), hist["low"].tail(20).min()
    hi52 = d["high"].tail(252).max()

    m = {
        "close": round(float(last["close"]), 2),
        "chg_pct": round(float((last["close"] / prev["close"] - 1) * 100), 2),
        "gap_pct": round(float((last["open"] / prev["close"] - 1) * 100), 2),
        "rel_vol": round(float(last["volume"] / avg_vol), 2) if avg_vol > 0 else np.nan,
        "atr_pct": round(float(last["atr"] / last["close"] * 100), 2),
        "turnover_cr": round(turnover_cr, 1),
        "trend": "up" if last["close"] > last["ema50"] else "down",
        "from_52w_high_pct": round(float((last["close"] / hi52 - 1) * 100), 2),
    }
    tags = []
    if last["close"] > hi20:
        tags.append("breakout20")
    if last["close"] < lo20:
        tags.append("breakdown20")
    if abs(m["gap_pct"]) >= cfg.gap_pct:
        tags.append("gap-up" if m["gap_pct"] > 0 else "gap-down")
    if m["rel_vol"] >= cfg.vol_mult:
        tags.append("vol_spike")
    if rng.iloc[-1] <= rng.tail(7).min():
        tags.append("nr7")
    if m["from_52w_high_pct"] >= -3:
        tags.append("near_52w_high")
    m["setups"] = ",".join(tags)

    # Liquidity / volatility filters
    reasons = []
    if m["close"] < cfg.min_price or (cfg.max_price is not None and m["close"] > cfg.max_price):
        reasons.append("price")
    if turnover_cr < cfg.min_turnover_cr:
        reasons.append("illiquid")
    if not (cfg.min_atr_pct <= m["atr_pct"] <= cfg.max_atr_pct):
        reasons.append("atr")
    m["filtered"] = ",".join(reasons)

    # Score: movement + participation, with a bonus when the setup agrees with the trend
    score = 0.0
    score += min(m["rel_vol"], 5) * 2
    score += min(abs(m["gap_pct"]), 5)
    score += 3 * ("breakout20" in tags or "breakdown20" in tags)
    score += 1.5 * ("nr7" in tags)
    score += 1 * ("near_52w_high" in tags)
    aligned = ("breakout20" in tags and m["trend"] == "up") or ("breakdown20" in tags and m["trend"] == "down")
    score += 2 * aligned
    m["score"] = round(score, 2)
    m["bias"] = ("long" if (m["trend"] == "up" and m["chg_pct"] >= 0) else
                 "short" if (m["trend"] == "down" and m["chg_pct"] <= 0) else "neutral")
    return m


def run_scan(data: dict[str, pd.DataFrame], cfg: ScanConfig | None = None) -> pd.DataFrame:
    cfg = cfg or ScanConfig()
    rows = []
    for sym, df in data.items():
        m = scan_one(df, cfg)
        if m:
            rows.append({"symbol": sym, **m})
    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows).set_index("symbol")
    passed = out[out["filtered"] == ""].sort_values("score", ascending=False)
    return passed.head(cfg.top) if cfg.top else passed


def watchlist_yaml(results: pd.DataFrame, strategy: str = "orb") -> str:
    """Instruments block to paste into a paper-trading config."""
    lines = ["instruments:"]
    for sym, r in results.iterrows():
        lines += [f"  - symbol: {sym}            # score {r['score']}, {r['setups'] or 'no setup'}, bias {r['bias']}",
                  "    segment: EQ",
                  "    product: INTRADAY",
                  "    instrument_key: \"\"         # fill with: python -m tradebot find-key " + sym,
                  f"    strategy: {{name: {strategy}}}",
                  "    source: {type: upstox}"]
    return "\n".join(lines)
