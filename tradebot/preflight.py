"""Pre-market checklist: is everything ready for today's paper session?

Run on the trading PC before 09:00:  python -m tradebot preflight -c config/my.yaml
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
OK, WARN, FAIL = "OK  ", "WARN", "FAIL"


def next_trading_day(d: date, holidays: set[str]) -> date:
    while d.weekday() >= 5 or d.isoformat() in holidays:
        d += timedelta(days=1)
    return d


def run_checks(cfg: dict, online: bool = True, now: datetime | None = None) -> list[tuple[str, str, str]]:
    """Returns [(status, check, detail)]."""
    from .config import build_desk, parse_instruments
    now = now or datetime.now(IST)
    out: list[tuple[str, str, str]] = []
    add = lambda s, c, d="": out.append((s, c, d))

    # 1. Config
    try:
        desk, specs = build_desk(cfg)
        add(OK, "Config loads", f"{len(specs)} instruments, capital Rs{desk.risk.cfg.capital:,.0f}")
    except Exception as e:
        add(FAIL, "Config loads", str(e))
        return out
    missing = [s.instrument.symbol for s in specs if not s.data_key]
    add(FAIL if missing else OK, "Instrument keys", f"missing for {', '.join(missing)}" if missing else "all set")
    tf = cfg.get("timeframe", {}).get("unit", "minutes")
    add(OK if tf == "minutes" else FAIL, "Timeframe", "5-min candles" if tf == "minutes" else "paper needs minute candles")

    # 2. Calendar
    holidays = {str(h) for h in cfg.get("holidays", [])}
    today = now.date()
    nxt = next_trading_day(today, holidays)
    if nxt == today:
        add(OK, "Trading day", f"today {today:%a %d %b} is a trading day")
    else:
        add(WARN, "Trading day", f"market closed today; next session {nxt:%a %d %b}")
    future = [h for h in holidays if h >= today.isoformat()]
    add(OK if future else WARN, "Holiday list", f"{len(future)} upcoming" if future else "no upcoming holidays listed; update it")

    # 3. Keys
    has_login = os.environ.get("UPSTOX_API_KEY") and os.environ.get("UPSTOX_API_SECRET")
    has_token = os.environ.get("UPSTOX_ACCESS_TOKEN")
    add(OK if (has_login or has_token) else FAIL, "Upstox credentials",
        "API key/secret set (daily --login)" if has_login else ("access token set" if has_token else
        "set UPSTOX_API_KEY and UPSTOX_API_SECRET in .env"))
    if cfg.get("alerts", {}).get("telegram"):
        tg = os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")
        add(OK if tg else FAIL, "Telegram", "set" if tg else "telegram: true but TELEGRAM_* not set")
    if cfg.get("analyst", {}).get("enabled"):
        add(OK if os.environ.get("ANTHROPIC_API_KEY") else FAIL, "AI analyst key",
            "set" if os.environ.get("ANTHROPIC_API_KEY") else "analyst enabled but ANTHROPIC_API_KEY not set")
    if any(s.backup for s in specs):
        kot = all(os.environ.get(k) for k in ("KOTAK_CONSUMER_KEY", "KOTAK_MOBILE", "KOTAK_UCC", "KOTAK_MPIN"))
        try:
            import neo_api_client  # noqa: F401
            sdk = True
        except ImportError:
            sdk = False
        add(OK if kot and sdk else WARN, "Kotak Neo backup",
            "ready" if kot and sdk else "not set up: if Upstox fails, those stocks pause (optional)")

    # 4. Disk
    try:
        Path("runs").mkdir(exist_ok=True)
        probe = Path("runs") / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        add(OK, "Results folder", "runs/ is writable")
    except OSError as e:
        add(FAIL, "Results folder", str(e))

    # 5. Network (real calls)
    if online:
        from .data.upstox import UpstoxData
        s0 = specs[0]
        try:
            df = UpstoxData().historical(s0.data_key, "days", 1, today - timedelta(days=10), today)
            add(OK if len(df) else WARN, "Upstox data", f"{s0.instrument.symbol}: {len(df)} daily candles")
        except Exception as e:
            add(WARN if has_login and not has_token else FAIL, "Upstox data",
                f"{str(e)[:120]} (normal before today's login)" if has_login and not has_token else str(e)[:160])
        if cfg.get("context", {}).get("enabled"):
            from .data.nse import NseClient
            try:
                ban = NseClient().ban_list()
                add(OK, "NSE ban list", f"{len(ban)} stocks banned" if ban else "empty")
            except Exception as e:
                add(WARN, "NSE ban list", f"unreachable ({str(e)[:80]}); trading continues without it")
    return out


def print_checks(results) -> int:
    fails = 0
    for status, check, detail in results:
        mark = {"OK  ": "✅", "WARN": "⚠️ ", "FAIL": "❌"}[status]
        print(f"{mark} {check:<20} {detail}")
        fails += status == FAIL
    print("\nREADY for paper trading." if not fails else f"\nNOT READY: fix the {fails} ❌ item(s) above.")
    return 1 if fails else 0
