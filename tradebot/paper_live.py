"""Live paper trading: real Upstox candles, simulated orders. No real orders are sent.

Each poll fetches recent candles, and every newly *completed* bar is pushed
through the same TradingDesk the backtester uses.

Kill switch: create a file named STOP in the run folder. All positions are
closed and no new trades are taken.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .config import build_desk
from .data.upstox import UpstoxData, UpstoxError

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger(__name__)


def market_open(now: datetime, holidays: set[str]) -> bool:
    if now.weekday() >= 5 or now.strftime("%Y-%m-%d") in holidays:
        return False
    return datetime.strptime("09:15", "%H:%M").time() <= now.time() <= datetime.strptime("15:30", "%H:%M").time()


def completed(df: pd.DataFrame, interval_min: int, now: datetime) -> pd.DataFrame:
    """Drop the still-forming last candle."""
    return df[df.index + pd.Timedelta(minutes=interval_min) <= now]


def run_paper(cfg: dict, out_dir: str = "runs", poll_seconds: int = 20, warmup_days: int = 10) -> None:
    from .alerts import build_notifier
    notifier = build_notifier(cfg.get("alerts"))
    desk, specs = build_desk(cfg, live_clock=True, notifier=notifier)
    tf = cfg.get("timeframe", {"unit": "minutes", "interval": 5})
    if tf["unit"] != "minutes":
        raise ValueError("Paper trading supports minute candles only")
    holidays = {str(h) for h in cfg.get("holidays", [])}
    upstox = UpstoxData()
    run = Path(out_dir) / f"paper_{datetime.now(IST):%Y%m%d_%H%M}"
    run.mkdir(parents=True, exist_ok=True)
    stop_file = run / "STOP"
    log.info("Paper trading started. Kill switch: touch %s", stop_file)

    # Warm-up history so indicators are valid from the first live bar.
    history: dict[str, pd.DataFrame] = {}
    for s in specs:
        h = upstox.historical(s.data_key, "minutes", tf["interval"],
                              date.today() - timedelta(days=warmup_days), date.today() - timedelta(days=1))
        history[s.instrument.symbol] = h
    last_done = {sym: (df.index[-1] if len(df) else None) for sym, df in history.items()}
    first_pass = set(history)   # on start-up, skip bars that closed before we started

    try:
        while True:
            now = datetime.now(IST)
            if stop_file.exists() and not desk.risk.halted:
                desk.risk.kill("STOP file found")
                desk.flatten_all(now, "kill switch")
            if not market_open(now, holidays):
                if now.time() > datetime.strptime("15:30", "%H:%M").time():
                    log.info("Market closed for the day.")
                    break
                time.sleep(poll_seconds)
                continue
            for s in specs:
                sym = s.instrument.symbol
                try:
                    today = upstox.intraday(s.data_key, "minutes", tf["interval"])
                except UpstoxError as e:
                    log.warning("%s data error: %s", sym, e)
                    continue
                today = completed(today, tf["interval"], now)
                df = pd.concat([history[sym], today])
                df = df[~df.index.duplicated(keep="last")].sort_index()
                if sym in first_pass:
                    first_pass.discard(sym)
                    if len(df):
                        last_done[sym] = df.index[-1]
                    continue
                prepared = desk.books[sym].strategy.prepare(df)
                new_idx = [i for i, ts in enumerate(prepared.index)
                           if last_done[sym] is None or ts > last_done[sym]]
                for i in new_idx:
                    desk.on_bar(sym, prepared, i)
                if new_idx:
                    last_done[sym] = prepared.index[new_idx[-1]]
            _save(desk, run)
            time.sleep(poll_seconds)
    except KeyboardInterrupt:
        log.info("Stopped by user.")
    finally:
        _save(desk, run)
        if notifier is not None:
            notifier.send(f"[PAPER] Day end: equity Rs{desk.broker.equity():,.0f}, trades {len(desk.broker.trades)}")
            notifier.close()
        log.info("Equity Rs%.2f | trades %d | saved to %s", desk.broker.equity(), len(desk.broker.trades), run)


def _save(desk, run: Path) -> None:
    pd.DataFrame(desk.journal).to_csv(run / "journal.csv", index=False)
    pd.DataFrame([{**t.__dict__, "net_pnl": t.net_pnl} for t in desk.broker.trades]).to_csv(run / "trades.csv", index=False)
