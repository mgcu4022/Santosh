"""Multi-day paper-trading report: is the system actually working?

Combines every runs/paper_*/ folder and answers, with numbers:
  - how many trades so far (target: 100+ over 2-3 months)
  - P&L after all charges, win rate, profit factor, expectancy per trade
  - worst drawdown and longest losing streak
  - what works and what doesn't: by strategy, stock, exit reason, entry hour
  - whether the result is likely real or could be luck (t-statistic)
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

MIN_TRADES = 100
MIN_DAYS = 40          # ~2 months of trading days


@dataclass
class RunData:
    trades: pd.DataFrame
    journal: pd.DataFrame
    days: list[str]
    capital: float
    folders: int


def load_runs(runs_dir: str = "runs", start: str | None = None, end: str | None = None) -> RunData:
    trades, journals, days, capital, folders = [], [], set(), None, 0
    for d in sorted(Path(runs_dir).glob("paper_*")):
        if not d.is_dir():
            continue
        day = d.name.split("_")[1][:8]
        day = f"{day[:4]}-{day[4:6]}-{day[6:8]}"
        if (start and day < start) or (end and day > end):
            continue
        folders += 1
        days.add(day)
        meta = d / "meta.json"
        if meta.exists() and capital is None:
            capital = float(json.loads(meta.read_text()).get("capital", 0)) or None
        for name, bucket in (("trades.csv", trades), ("journal.csv", journals)):
            f = d / name
            if f.exists() and f.stat().st_size > 1:
                try:
                    df = pd.read_csv(f)
                except pd.errors.EmptyDataError:
                    continue
                df["run"] = d.name
                bucket.append(df)
    t = pd.concat(trades, ignore_index=True) if trades else pd.DataFrame()
    j = pd.concat(journals, ignore_index=True) if journals else pd.DataFrame()
    if len(t):
        t["entry_time"] = pd.to_datetime(t["entry_time"], utc=True).dt.tz_convert("Asia/Kolkata")
        t["exit_time"] = pd.to_datetime(t["exit_time"], utc=True).dt.tz_convert("Asia/Kolkata")
        t = t.sort_values("exit_time").reset_index(drop=True)
    return RunData(t, j, sorted(days), capital or 500_000.0, folders)


def _longest_losing_streak(pnl: pd.Series) -> int:
    best = cur = 0
    for x in pnl:
        cur = cur + 1 if x <= 0 else 0
        best = max(best, cur)
    return best


def _group(t: pd.DataFrame, key, by_pnl: bool = True) -> pd.DataFrame:
    g = t.groupby(key)["net_pnl"]
    out = pd.DataFrame({"trades": g.size(), "net_pnl": g.sum(), "win_rate_%": g.apply(lambda s: (s > 0).mean() * 100),
                        "avg_pnl": g.mean()})
    out = out.round(1)
    return out.sort_values("net_pnl", ascending=False) if by_pnl else out.sort_index()


def compute(rd: RunData) -> dict:
    t, j = rd.trades, rd.journal
    r: dict = {"days_run": len(rd.days), "first_day": rd.days[0] if rd.days else None,
               "last_day": rd.days[-1] if rd.days else None, "restarts": rd.folders - len(rd.days),
               "capital": rd.capital, "trades": len(t)}
    if j is not None and len(j) and "event" in j:
        ev = j["event"].value_counts()
        r["blocked"] = {k: int(ev.get(k, 0)) for k in ("RISK_BLOCK", "EVENT_BLOCK", "AI_VETO", "HALT")}
        entries = int(ev.get("ENTRY_SIG", 0))
        r["unclosed_entries"] = max(entries - len(t), 0)   # lost on crash/restart, or still open
        if "why" in j:
            rb = j[j["event"] == "RISK_BLOCK"]["why"].astype(str).str.replace(r"\(.*\)", "", regex=True)
            r["top_risk_blocks"] = rb.value_counts().head(5).to_dict()
    if not len(t):
        return r

    pnl = t["net_pnl"]
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    daily = t.groupby(t["exit_time"].dt.date)["net_pnl"].sum()
    cum = pnl.cumsum()
    dd = cum - cum.cummax()
    sd = pnl.std(ddof=1) if len(pnl) > 1 else 0.0
    r.update({
        "net_pnl": round(pnl.sum(), 2), "gross_pnl": round(t["gross_pnl"].sum(), 2),
        "charges": round(t["charges"].sum(), 2),
        "charges_pct_of_gross": round(t["charges"].sum() / abs(t["gross_pnl"].sum()) * 100, 1)
        if t["gross_pnl"].sum() else None,
        "return_on_capital_%": round(pnl.sum() / rd.capital * 100, 2),
        "win_rate_%": round(len(wins) / len(pnl) * 100, 1),
        "avg_win": round(wins.mean(), 2) if len(wins) else 0.0,
        "avg_loss": round(losses.mean(), 2) if len(losses) else 0.0,
        "profit_factor": round(wins.sum() / -losses.sum(), 2) if losses.sum() < 0 else math.inf,
        "expectancy_per_trade": round(pnl.mean(), 2),
        "avg_per_trading_day": round(pnl.sum() / max(len(rd.days), 1), 2),
        "best_day": round(daily.max(), 2), "worst_day": round(daily.min(), 2),
        "green_days_%": round((daily > 0).mean() * 100, 1),
        "max_drawdown": round(dd.min(), 2),
        "max_drawdown_%_capital": round(dd.min() / rd.capital * 100, 2),
        "longest_losing_streak": _longest_losing_streak(pnl),
        "t_stat": round(pnl.mean() / (sd / math.sqrt(len(pnl))), 2) if sd > 0 else 0.0,
    })
    r["by_strategy"] = _group(t, "strategy")
    r["by_symbol"] = _group(t, "symbol")
    r["by_exit"] = _group(t, "exit_reason")
    r["by_entry_hour"] = _group(t, t["entry_time"].dt.strftime("%H:00").rename("entry_hour"), by_pnl=False)
    r["by_side"] = _group(t, "side")
    r["verdict"] = verdict(r)
    return r


def verdict(r: dict) -> list[str]:
    """Plain-language conclusion. Deliberately strict: real money is at stake."""
    out = []
    n, days = r.get("trades", 0), r.get("days_run", 0)
    if n < MIN_TRADES or days < MIN_DAYS:
        out.append(f"NOT ENOUGH DATA YET: {n}/{MIN_TRADES} trades over {days}/{MIN_DAYS} trading days. "
                   "Keep paper trading; early results swing a lot.")
    pf, t = r.get("profit_factor", 0), r.get("t_stat", 0)
    if r.get("net_pnl", 0) <= 0:
        out.append("LOSING after charges. Do not trade real money with this setup.")
        if r.get("gross_pnl", 0) > 0:
            out.append("It is profitable BEFORE charges: trade less often or aim for bigger moves per trade.")
    elif pf < 1.2:
        out.append(f"Barely profitable (profit factor {pf}). Too thin an edge to survive real-world slippage.")
    elif t < 2:
        out.append(f"Profitable, but the t-statistic is {t} (below 2): the result could still be luck.")
    if n >= MIN_TRADES and days >= MIN_DAYS and r.get("net_pnl", 0) > 0 and pf >= 1.2 and t >= 2:
        out.append("PASSES the paper test. Next step: small real-money test (Rs 50k-1L) for 1-2 months, "
                   "same rules, and compare results with paper.")
    if r.get("unclosed_entries", 0):
        out.append(f"Data check: {r['unclosed_entries']} entries have no recorded exit (bot stopped mid-trade). "
                   "Keep the PC awake and avoid restarts during market hours.")
    return out


def _md_table(df: pd.DataFrame) -> str:
    cols = [df.index.name or ""] + list(df.columns)
    rows = ["| " + " | ".join(map(str, cols)) + " |", "|" + "---|" * len(cols)]
    for idx, row in df.iterrows():
        cells = [str(int(v)) if isinstance(v, float) and v.is_integer() and c == "trades" else str(v)
                 for c, v in zip(df.columns, row.tolist())]
        rows.append("| " + " | ".join([str(idx)] + cells) + " |")
    return "\n".join(rows)


def render(r: dict) -> str:
    L = [f"# Paper-trading report ({r.get('first_day')} to {r.get('last_day')})", ""]
    L.append(f"Trading days run: **{r['days_run']}**  |  Trades: **{r['trades']} / {MIN_TRADES}**  |  "
             f"Capital: Rs {r['capital']:,.0f}" + (f"  |  Restarts: {r['restarts']}" if r.get("restarts") else ""))
    if "verdict" in r:
        L += ["", "## Verdict"] + [f"- {v}" for v in r["verdict"]]
    if r.get("trades"):
        L += ["", "## Results (after all charges)", "",
              "| Metric | Value |", "|---|---|"]
        for k in ("net_pnl", "gross_pnl", "charges", "charges_pct_of_gross", "return_on_capital_%",
                  "avg_per_trading_day", "win_rate_%", "avg_win", "avg_loss", "profit_factor",
                  "expectancy_per_trade", "green_days_%", "best_day", "worst_day", "max_drawdown",
                  "max_drawdown_%_capital", "longest_losing_streak", "t_stat"):
            L.append(f"| {k.replace('_', ' ')} | {r.get(k)} |")
        for title, key in (("By strategy", "by_strategy"), ("By stock", "by_symbol"), ("By exit reason", "by_exit"),
                           ("By entry hour", "by_entry_hour"), ("Long vs short", "by_side")):
            L += ["", f"## {title}", "", _md_table(r[key])]
    if r.get("blocked"):
        L += ["", "## Trades stopped by the safety gates", "",
              ", ".join(f"{k}: {v}" for k, v in r["blocked"].items())]
        if r.get("top_risk_blocks"):
            L += ["", "Top risk-block reasons: " + "; ".join(f"{k.strip()} ({v})" for k, v in r["top_risk_blocks"].items())]
    L += ["", "_t-stat: how many standard errors the average trade is above zero. Below 2 = could be luck._",
          "_Paper results are usually better than real trading (no missed fills, no emotions)._"]
    return "\n".join(L) + "\n"


def short_summary(r: dict) -> str:
    """A few lines for Telegram."""
    lines = [f"📊 Paper report: {r['days_run']} days, {r['trades']}/{MIN_TRADES} trades"]
    if r.get("trades"):
        lines.append(f"Net P&L Rs{r['net_pnl']:,.0f} (charges Rs{r['charges']:,.0f}) | "
                     f"win {r['win_rate_%']}% | PF {r['profit_factor']} | max DD Rs{r['max_drawdown']:,.0f}")
    lines += [f"• {v}" for v in r.get("verdict", [])[:2]]
    return "\n".join(lines)
