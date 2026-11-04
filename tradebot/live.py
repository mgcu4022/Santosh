"""Live snapshot for the user (and for Claude's /trader-live skill).

Reads, never writes:
  - the option terminal, if it is running (http://127.0.0.1:<port>/api/state)
  - today's strategy-bot run folder (runs/paper_YYYYMMDD*/state.json, saved every poll)

Warnings are computed here, in code, so the numbers are exact; Claude only explains them.
"""
from __future__ import annotations

import json
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
STALE_SECONDS = 180


def fetch_terminal(port: int = 8050, timeout: float = 3.0) -> dict | None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # localhost: never via a proxy
    try:
        with opener.open(f"http://127.0.0.1:{port}/api/state", timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None


def latest_paper_state(runs_dir: str = "runs", today: str | None = None) -> tuple[dict | None, Path | None]:
    today = today or datetime.now(IST).strftime("%Y%m%d")
    folders = sorted(Path(runs_dir).glob(f"**/paper_{today}*"), key=lambda p: p.stat().st_mtime)   # incl. runs/trial20k
    for d in reversed(folders):
        f = d / "state.json"
        if f.exists():
            try:
                return json.loads(f.read_text(encoding="utf-8")), d
            except json.JSONDecodeError:
                continue
    return None, None


def _pct(a, b):
    return a / b * 100 if b else 0.0


def terminal_warnings(s: dict) -> list[str]:
    w = []
    lim = (s.get("limits") or {}).get("daily_loss_limit") or 0
    loss = max(0.0, -(s.get("mtm") or 0))
    if s.get("halted"):
        w.append(f"STOPPED for today: {s.get('halt_reason')}")
    elif lim and loss >= 0.8 * lim:
        w.append(f"Daily loss Rs{loss:,.0f} is {_pct(loss, lim):.0f}% of the Rs{lim:,.0f} limit: one more bad trade stops trading")
    elif lim and loss >= 0.5 * lim:
        w.append(f"Daily loss Rs{loss:,.0f} has used {_pct(loss, lim):.0f}% of the Rs{lim:,.0f} limit")
    rules = s.get("rules") or {}
    if rules.get("mtm_stop_loss") and loss >= 0.8 * rules["mtm_stop_loss"] and not s.get("halted"):
        w.append(f"MTM Rs{-loss:,.0f} is close to your auto-exit at -Rs{rules['mtm_stop_loss']:,.0f}")
    if s.get("equity") and _pct(s.get("margin_used", 0), s["equity"]) >= 80:
        w.append(f"{_pct(s['margin_used'], s['equity']):.0f}% of capital is blocked as margin: little room left")
    spot, dte = s.get("spot"), s.get("days_to_expiry")
    a = s.get("analytics") or {}
    for p in s.get("positions", []):
        name = f"{'SOLD' if p['qty'] < 0 else 'BOUGHT'} {p['strike']:g} {p['opt']}"
        if p.get("stop") and p.get("ltp"):
            gap = abs(p["stop"] - p["ltp"]) / p["ltp"] * 100
            if gap <= 10:
                w.append(f"{name}: price Rs{p['ltp']:g} is within {gap:.0f}% of its stop-loss Rs{p['stop']:g}")
        if spot and p["qty"] < 0:
            k = p["strike"]
            tested = (p["opt"] == "CE" and spot >= k * 0.995) or (p["opt"] == "PE" and spot <= k * 1.005)
            if tested:
                w.append(f"{name}: {s.get('underlying')} at {spot:,.0f} is at/through the strike: losses can grow fast")
        if dte is not None and dte < 1 and p["qty"] > 0:
            w.append(f"{name}: expiry today, bought options lose value quickly (time decay)")
    if spot and a.get("resistance") and 0 <= a["resistance"] - spot <= spot * 0.003:
        w.append(f"{s.get('underlying')} {spot:,.0f} is right below resistance {a['resistance']:,.0f} (big call OI)")
    if spot and a.get("support") and 0 <= spot - a["support"] <= spot * 0.003:
        w.append(f"{s.get('underlying')} {spot:,.0f} is right above support {a['support']:,.0f} (big put OI)")
    return w


def paper_warnings(s: dict, now: datetime | None = None) -> list[str]:
    w = []
    now = now or datetime.now(IST)
    try:
        age = (now - datetime.fromisoformat(s["updated"])).total_seconds()
        if age > STALE_SECONDS:
            w.append(f"Strategy bot data is {age / 60:.0f} min old: is the bot still running?")
    except (KeyError, ValueError):
        pass
    lim, loss = s.get("daily_loss_limit") or 0, max(0.0, -(s.get("mtm") or 0))
    if s.get("halted"):
        w.append(f"Strategy bot STOPPED for today: {s.get('halt_reason')}")
    elif lim and loss >= 0.5 * lim:
        w.append(f"Strategy bot has used {_pct(loss, lim):.0f}% of its Rs{lim:,.0f} daily loss limit")
    for p in s.get("positions", []):
        if p.get("stop") and p.get("ltp") and p.get("avg") and abs(p["avg"] - p["stop"]):
            left = abs(p["ltp"] - p["stop"]) / abs(p["avg"] - p["stop"]) * 100
            if left <= 25:
                w.append(f"{p['symbol']} {p['side']}: only {left:.0f}% of the distance to its stop-loss is left")
    if s.get("max_trades") and s.get("trades_today", 0) >= s["max_trades"]:
        w.append("Strategy bot reached its maximum trades for today")
    return w


def render(term: dict | None, paper: dict | None, paper_dir: Path | None) -> str:
    L = [f"LIVE SNAPSHOT {datetime.now(IST):%d %b %Y %H:%M:%S} IST (paper trading)"]
    if term is None and paper is None:
        L += ["", "Nothing is running right now.",
              "Start the option terminal:  python -m tradebot terminal --demo   (or --login for live prices)",
              "Start the strategy bot:     python -m tradebot paper -c config/my.yaml --login -v"]
        return "\n".join(L)
    if term:
        a = term.get("analytics") or {}
        chg = (term["spot"] - term["prev_close"]) if term.get("spot") and term.get("prev_close") else None
        L += ["", f"== OPTION TERMINAL ({term.get('mode')} data) ==",
              f"{term['underlying']} {term.get('spot'):,.2f}" + (f" ({chg:+,.2f})" if chg is not None else "") +
              f" | expiry {term.get('expiry')} ({term.get('days_to_expiry')} days) | expected move +/-{term.get('straddle')}",
              f"Support {a.get('support')} | Resistance {a.get('resistance')} | PCR {a.get('pcr')} | Max pain {a.get('max_pain')} | ATM IV {a.get('atm_iv')}%",
              f"MTM Rs{term['mtm']:,.0f} (booked Rs{term['realized']:,.0f}, open Rs{term['unrealized']:,.0f}) | "
              f"margin Rs{term['margin_used']:,.0f} of Rs{term['equity']:,.0f} | trades {term['trades_today']}/{term['max_trades']} | "
              f"daily loss limit Rs{(term.get('limits') or {}).get('daily_loss_limit', 0):,.0f}"]
        if term.get("positions"):
            L.append("Positions:")
            for p in term["positions"]:
                L.append(f"  {'SOLD' if p['qty'] < 0 else 'BOUGHT'} {p['lots']} lot {p['strike']:g} {p['opt']} @ {p['avg']} -> {p['ltp']} | "
                         f"P&L Rs{p['pnl']:,.0f} ({p['pnl_pct']:+}%) | SL {p['stop']} | breakeven {p['breakeven']:,.0f} | {p['desc']}")
        else:
            L.append("Positions: none open")
        for t in (term.get("closed") or [])[:5]:
            L.append(f"  closed {t['time']} {t['symbol']} Rs{t['net']:,.0f} ({t['why']})")
        L += ["Warnings:"] + [f"  ! {x}" for x in terminal_warnings(term)] if terminal_warnings(term) else ["Warnings: none"]
    if paper:
        L += ["", f"== STRATEGY BOT ({paper_dir.name if paper_dir else ''}, updated {paper.get('updated', '')[11:19]}) ==",
              f"MTM Rs{paper['mtm']:,.0f} | booked Rs{paper['realized']:,.0f} | equity Rs{paper['equity']:,.0f} | "
              f"trades {paper['trades_today']}/{paper['max_trades']} | daily loss limit Rs{paper['daily_loss_limit']:,.0f}"]
        if paper.get("positions"):
            L.append("Positions:")
            for p in paper["positions"]:
                L.append(f"  {p['side']} {abs(p['qty'])} {p['symbol']} @ {p['avg']} -> {p['ltp']} | P&L Rs{p['pnl']:,.0f} | "
                         f"SL {p['stop']} | target {p['target']} | {p['strategy']}")
        else:
            L.append("Positions: none open")
        for t in (paper.get("closed") or [])[-5:]:
            L.append(f"  closed {str(t['exit_time'])[11:19]} {t['symbol']} {t['side']} Rs{t['net_pnl']:,.0f} ({t['why']})")
        blocks = [e for e in paper.get("recent_events", []) if e.get("event") in ("RISK_BLOCK", "EVENT_BLOCK", "AI_VETO")]
        for e in blocks[-3:]:
            L.append(f"  blocked: {e.get('symbol')} {e.get('event')}: {e.get('why')}")
        pw = paper_warnings(paper)
        L += ["Warnings:"] + [f"  ! {x}" for x in pw] if pw else ["Warnings: none"]
    elif term:
        L += ["", "Strategy bot: not running today (no runs/paper_<today>/state.json)."]
    return "\n".join(L)
