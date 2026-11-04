"""Live paper trading: real prices, simulated orders. No real orders are sent.

Data: Upstox candles (primary). If Upstox fails 3 times in a row for a symbol
(expired token, outage), that symbol switches to Kotak Neo quotes, turned into
candles locally, and Upstox is retried every few minutes. Warm-up history falls
back to Yahoo if Upstox is unavailable at start-up.

Each poll pushes every newly *completed* candle through the same TradingDesk
the backtester uses.

Kill switch: create a file named STOP in the run folder. All positions are
closed and no new trades are taken.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .config import build_desk
from .data.barbuilder import BarBuilder, empty_bars
from .data.upstox import UpstoxData

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger(__name__)

FAILS_BEFORE_BACKUP = 3
RETRY_PRIMARY_EVERY = 15      # polls


def market_open(now: datetime, holidays: set[str]) -> bool:
    if now.weekday() >= 5 or now.strftime("%Y-%m-%d") in holidays:
        return False
    return datetime.strptime("09:15", "%H:%M").time() <= now.time() <= datetime.strptime("15:30", "%H:%M").time()


def merge_bars(*frames: pd.DataFrame, keep: str = "last") -> pd.DataFrame:
    """Join candle tables, skipping empty ones, and always return a sorted time index."""
    parts = [f for f in frames if f is not None and len(f)]
    if not parts:
        return empty_bars()
    df = pd.concat(parts) if len(parts) > 1 else parts[0].copy()
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.DatetimeIndex(pd.to_datetime(df.index, utc=True)).tz_convert(IST)
    return df[~df.index.duplicated(keep=keep)].sort_index()


def completed(df: pd.DataFrame, interval_min: int, now: datetime) -> pd.DataFrame:
    """Drop the still-forming last candle."""
    return df[df.index + pd.Timedelta(minutes=interval_min) <= now]


class LiveFeed:
    """Today's completed candles per symbol, with Upstox -> Kotak Neo failover."""

    def __init__(self, specs, interval: int, upstox=None, kotak=None, notify=None):
        self.specs = {s.instrument.symbol: s for s in specs}
        self.interval = interval
        self.upstox = upstox
        self.kotak = kotak
        self.notify = notify or (lambda msg: None)
        self.fails = {sym: 0 for sym in self.specs}
        self.on_backup: set[str] = set()
        self.builders = {sym: BarBuilder(interval) for sym in self.specs}
        self.last_primary: dict[str, pd.DataFrame] = {}
        if upstox is None:   # Kotak Neo is the main source: every symbol with a Kotak token starts there
            self.on_backup = {sym for sym in self.specs if self._has_backup(sym)}
        self._polls = 0

    def last_price(self, sym: str, latest: pd.DataFrame | None = None) -> float | None:
        """Most recent price we know: Kotak's forming candle, else the last finished candle."""
        b = self.builders.get(sym)
        if sym in self.on_backup and b is not None and b.current:
            return float(b.current["close"])
        if latest is not None and len(latest):
            return float(latest["close"].iloc[-1])
        return None

    def _has_backup(self, sym) -> bool:
        b = self.specs[sym].backup or {}
        return self.kotak is not None and bool(b.get("kotak_token"))

    def poll(self, now: datetime) -> dict[str, pd.DataFrame]:
        self._polls += 1
        out = {}
        for sym, spec in self.specs.items():
            if sym in self.on_backup and (self.upstox is None or self._polls % RETRY_PRIMARY_EVERY):
                continue                                  # stay on Kotak (until the next Upstox retry)
            if self.upstox is None:
                continue                                  # no Upstox and no Kotak token for this symbol
            try:
                df = self.upstox.intraday(spec.data_key, "minutes", self.interval)
                self.last_primary[sym] = completed(df, self.interval, now)
                self.fails[sym] = 0
                if sym in self.on_backup:
                    self.on_backup.discard(sym)
                    self.notify(f"[PAPER] {sym}: Upstox data is back, switched from Kotak Neo")
                out[sym] = self.last_primary[sym]
            except Exception as e:
                self.fails[sym] += 1
                log.warning("%s Upstox error (%d in a row): %s", sym, self.fails[sym], e)
                if self.fails[sym] >= FAILS_BEFORE_BACKUP and sym not in self.on_backup and self._has_backup(sym):
                    self.on_backup.add(sym)
                    self.notify(f"[PAPER] {sym}: Upstox failing ({e}); switched to Kotak Neo quotes")
        if self.on_backup:
            self._poll_backup(now)
            for sym in self.on_backup:
                built = self.builders[sym].frame()
                out[sym] = merge_bars(self.last_primary.get(sym), built, keep="first")   # prefer real candles
        return out

    def _poll_backup(self, now: datetime) -> None:
        toks = {}
        for sym in self.on_backup:
            b = self.specs[sym].backup
            toks[str(b["kotak_token"])] = (sym, b.get("kotak_segment", "nse_cm"))
        try:
            prices = self.kotak.ltp([(t, seg) for t, (_, seg) in toks.items()])
        except Exception as e:
            log.warning("Kotak Neo quotes failed: %s", e)
            prices = {}
        for tok, (sym, _) in toks.items():
            if tok in prices:
                self.builders[sym].update(now, prices[tok])
            self.builders[sym].close_due(now)


def warmup_history(spec, interval: int, days: int, upstox, include_today: bool = False,
                   now: datetime | None = None) -> pd.DataFrame:
    """Previous days' candles so indicators are ready. Upstox first, then Yahoo.

    include_today (Kotak mode): also take today's finished candles from Yahoo, so a strategy
    started mid-session still knows today's opening range. Kotak quotes take over from there.
    """
    end = date.today() - timedelta(days=1)
    if upstox is not None:
        try:
            return upstox.historical(spec.data_key, "minutes", interval, date.today() - timedelta(days=days), end)
        except Exception as e:
            log.warning("%s: Upstox history failed (%s); trying Yahoo", spec.instrument.symbol, e)
    try:
        from .data.yahoo import YahooData, to_yahoo_ticker
        df = YahooData().history(to_yahoo_ticker(spec.instrument.symbol, spec.instrument.exchange),
                                 f"{interval}m", date.today() - timedelta(days=min(days, 59)),
                                 date.today() if include_today else end)
        t = df.index.time
        df = df[(t >= datetime.strptime("09:15", "%H:%M").time()) & (t < datetime.strptime("15:30", "%H:%M").time())]
        if include_today:
            df = completed(df, interval, now or datetime.now(IST))     # drop the candle still forming
        return df
    except Exception as e:
        log.warning("%s: Yahoo history failed too (%s); indicators will warm up live", spec.instrument.symbol, e)
        return empty_bars()


def is_trading_day(d: date, holidays: set[str]) -> bool:
    return d.weekday() < 5 and d.isoformat() not in holidays


def run_dir_for_today(out_dir: str, now: datetime) -> Path:
    """runs/paper_YYYYMMDD, or _2, _3... if the bot is restarted the same day."""
    base = Path(out_dir) / f"paper_{now:%Y%m%d}"
    run, n = base, 1
    while run.exists():
        n += 1
        run = base.with_name(f"{base.name}_{n}")
    return run


def run_paper(cfg: dict, out_dir: str = "runs", poll_seconds: int = 20, warmup_days: int = 10,
              login: bool = False, login_timeout_min: float = 10) -> None:
    out_dir = cfg.get("runs_dir", out_dir)           # e.g. runs/trial20k keeps a trial's results separate
    holidays = {str(h) for h in cfg.get("holidays", [])}
    if not is_trading_day(datetime.now(IST).date(), holidays):
        log.warning("Not a trading day (weekend or holiday in config). Exiting.")
        print("Not a trading day (weekend or holiday). Nothing to do.")
        return
    from .alerts import build_notifier
    from .context import ContextConfig, MarketContext

    notifier = build_notifier(cfg.get("alerts"))
    ctx_cfg = ContextConfig.from_dict(cfg.get("context"))
    symbols = [i["symbol"] for i in cfg["instruments"]]
    context = MarketContext.build(ctx_cfg, symbols) if ctx_cfg.enabled else None
    desk, specs = build_desk(cfg, live_clock=True, notifier=notifier)
    desk.context = context
    rc = desk.risk.cfg
    desk.settings = {"capital": rc.capital, "live_data": str(cfg.get("live_data", "upstox")),
                     "broker": cfg.get("broker") or "default", "entry_from": rc.no_entry_before,
                     "entry_until": rc.no_entry_after, "square_off": rc.square_off_time,
                     "risk_per_trade_pct": rc.risk_per_trade_pct, "max_open_positions": rc.max_open_positions,
                     "max_daily_loss_pct": rc.max_daily_loss_pct,
                     "strategies": {sp.instrument.symbol: sp.strategy_cfg.get("name") for sp in specs}}
    tf = cfg.get("timeframe", {"unit": "minutes", "interval": 5})
    if tf["unit"] != "minutes":
        raise ValueError("Paper trading supports minute candles only")

    live_data = str(cfg.get("live_data", "upstox")).lower()
    kotak = None
    if live_data == "kotak":
        from .data.kotak import KotakNeoQuotes
        missing = [s.instrument.symbol for s in specs if not (s.backup or {}).get("kotak_token")]
        if missing:
            raise ValueError(f"live_data: kotak needs backup: {{kotak_token: ...}} for {', '.join(missing)}")
        kotak = KotakNeoQuotes()
        kotak.client                     # log in now (TOTP prompt at start, not mid-session)
        upstox = None
        poll_seconds = min(poll_seconds, 5)   # quote polling builds the candles: poll often
        log.info("Live data: Kotak Neo quotes -> 5-min candles (history from Yahoo)")
    else:
        from .upstox_auth import get_token
        token = get_token(login, login_timeout_min, notify=notifier.send if notifier else None)
        if login and token is None:
            msg = "[PAPER] No Upstox login; continuing on backup data (Kotak Neo) where configured."
            log.warning(msg)
            if notifier:
                notifier.send(msg)
        upstox = UpstoxData(access_token=token)
        if any(s.backup for s in specs):
            from .data.kotak import KotakNeoQuotes
            kotak = KotakNeoQuotes()   # logs in lazily, only if a backup is needed
    feed = LiveFeed(specs, tf["interval"], upstox, kotak,
                    notify=notifier.send if notifier else (lambda m: log.warning(m)))

    run = run_dir_for_today(out_dir, datetime.now(IST))
    run.mkdir(parents=True, exist_ok=True)
    (run / "meta.json").write_text(json.dumps({
        "capital": cfg.get("capital", 500_000), "started": datetime.now(IST).isoformat(),
        "symbols": symbols, "strategies": {i["symbol"]: i["strategy"]["name"] for i in cfg["instruments"]},
    }, indent=2), encoding="utf-8")
    stop_file = run / "STOP"
    log.info("Paper trading started. Kill switch: touch %s", stop_file)
    if context is not None and context.banned:
        log.info("F&O ban list today: %s", ", ".join(sorted(context.banned)))

    history = {s.instrument.symbol: warmup_history(s, tf["interval"], warmup_days, upstox,
                                                   include_today=(live_data == "kotak")) for s in specs}
    last_done = {sym: (df.index[-1] if len(df) else None) for sym, df in history.items()}
    first_pass = set(history)   # on start-up, skip candles that closed before we started
    errors: dict[str, int] = {}
    crashed = False

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
            polled = feed.poll(now)
            desk.live_px = {sym: feed.last_price(sym, df) for sym, df in polled.items()}
            for sym, today in polled.items():
                try:
                    process_symbol(desk, sym, today, history, last_done, first_pass)
                    errors[sym] = 0
                except Exception as e:      # one bad symbol/candle must not stop the whole session
                    errors[sym] = errors.get(sym, 0) + 1
                    log.exception("%s: skipped this update after an error: %s", sym, e)
                    if errors[sym] == 3 and notifier is not None:
                        notifier.send(f"[PAPER] {sym}: repeated errors ({str(e)[:100]}); still running")
            _save(desk, run)
            time.sleep(poll_seconds)
    except KeyboardInterrupt:
        log.info("Stopped by user.")
    except Exception:
        crashed = True
        raise
    finally:
        _save(desk, run)
        if notifier is not None:
            what = "STOPPED BY AN ERROR" if crashed else "Day end"
            notifier.send(f"[PAPER] {what}: equity Rs{desk.broker.equity():,.0f}, trades {len(desk.broker.trades)}")
            notifier.close()
        log.info("Equity Rs%.2f | trades %d | saved to %s", desk.broker.equity(), len(desk.broker.trades), run)


def process_symbol(desk, sym: str, today: pd.DataFrame, history: dict, last_done: dict, first_pass: set) -> None:
    """Push every newly completed candle of one symbol through the trading desk."""
    df = merge_bars(history.get(sym), today)
    if sym in first_pass:          # on start-up, candles that closed before we started are history only
        first_pass.discard(sym)
        if len(df):
            last_done[sym] = df.index[-1]
        return
    if df.empty:
        return
    prepared = desk.books[sym].strategy.prepare(df)
    new_idx = [i for i, ts in enumerate(prepared.index) if last_done.get(sym) is None or ts > last_done[sym]]
    for i in new_idx:
        desk.on_bar(sym, prepared, i)
    if new_idx:
        last_done[sym] = prepared.index[new_idx[-1]]
    if not hasattr(desk, "watch"):
        desk.watch = {}
    last = prepared.iloc[-1]
    desk.watch[sym] = {"candle_time": prepared.index[-1].strftime("%H:%M"), "close": float(last["close"]),
                       **{k: (None if pd.isna(last[k]) else float(last[k])) for k in
                          ("or_high", "or_low", "or_breaks_before", "ema_fast", "ema_slow", "supertrend", "st_dir")
                          if k in prepared.columns}}


def _save(desk, run: Path) -> None:
    log_ = desk.__dict__.setdefault("mtm_log", [])
    stamp = datetime.now(IST).strftime("%H:%M")
    mtm = round(desk.broker.equity() - desk.risk.day_start_equity, 2)
    if not log_ or log_[-1][0] != stamp:
        log_.append([stamp, mtm])
        del log_[:-800]
    else:
        log_[-1][1] = mtm
    pd.DataFrame(desk.journal).to_csv(run / "journal.csv", index=False)
    pd.DataFrame([{**t.__dict__, "net_pnl": t.net_pnl} for t in desk.broker.trades]).to_csv(run / "trades.csv", index=False)
    (run / "state.json").write_text(json.dumps(snapshot(desk), indent=2, default=str), encoding="utf-8")


def snapshot(desk) -> dict:
    """Live state for `python -m tradebot live` (and Claude's /trader-live)."""
    b, r = desk.broker, desk.risk
    positions = []
    for p in b.open_positions():
        px = b.last_price.get(p.instrument.symbol, p.avg_price)
        positions.append({"symbol": p.instrument.symbol, "segment": p.instrument.segment.value,
                          "side": "LONG" if p.qty > 0 else "SHORT", "qty": p.qty, "avg": round(p.avg_price, 2),
                          "ltp": px, "stop": p.stop_loss, "target": p.target, "strategy": p.strategy,
                          "pnl": round(p.unrealized(px), 2), "entry_time": p.entry_time})
    realized = round(sum(t.net_pnl for t in b.trades), 2)
    return {"updated": datetime.now(IST).isoformat(timespec="seconds"), "equity": round(b.equity(), 2),
            "capital": r.cfg.capital, "mtm": round(b.equity() - r.day_start_equity, 2), "realized": realized,
            "daily_loss_limit": round(r.day_start_equity * r.cfg.max_daily_loss_pct / 100, 0),
            "halted": r.halted, "halt_reason": r.halt_reason, "trades_today": r.trades_today,
            "max_trades": r.cfg.max_trades_per_day, "positions": positions,
            "closed": [{"symbol": t.symbol, "side": t.side, "net_pnl": round(t.net_pnl, 2), "why": t.exit_reason,
                        "exit_time": t.exit_time} for t in b.trades[-10:]],
            "recent_events": [{k: v for k, v in e.items()} for e in desk.journal[-12:]],
            "events": [{k: v for k, v in e.items()} for e in desk.journal[-60:]],
            "watch": watchlist(desk), "settings": getattr(desk, "settings", {}),
            "mtm_log": getattr(desk, "mtm_log", [])}


def watchlist(desk) -> list[dict]:
    """Per stock: live price and where it stands versus the strategy's trigger, in plain words."""
    rows = []
    strategies = getattr(desk, "settings", {}).get("strategies", {})
    for sym, book in desk.books.items():
        w = getattr(desk, "watch", {}).get(sym, {})
        px = (getattr(desk, "live_px", {}) or {}).get(sym) or w.get("close")
        pos = desk.broker.get_position(sym)
        row = {"symbol": sym, "price": px, "candle_time": w.get("candle_time"),
               "strategy": strategies.get(sym) or book.strategy.name, "in_trade": bool(pos)}
        hi, lo = w.get("or_high"), w.get("or_low")
        if "or_high" in w or row["strategy"] == "orb":
            row.update(range_high=hi, range_low=lo)
            if pos:
                row["status"] = "In a trade"
            elif hi is None or lo is None:
                row["status"] = "Waiting for opening range (forms 09:15-09:30)"
            elif (w.get("or_breaks_before") or 0) > 0:
                row["status"] = "Range already broken today (one try per day)"
            elif px:
                up, dn = (hi - px) / px * 100, (px - lo) / px * 100
                row.update(to_high_pct=round(up, 2), to_low_pct=round(dn, 2))
                if px > hi:
                    row["status"] = "Above range: buy signal if the 5-min candle closes here"
                elif px < lo:
                    row["status"] = "Below range: sell signal if the 5-min candle closes here"
                else:
                    near = "high" if up < dn else "low"
                    row["status"] = f"Inside range, {min(up, dn):.2f}% from the {near}"
        elif "ema_fast" in w and w.get("ema_fast") and w.get("ema_slow"):
            gap = (w["ema_fast"] / w["ema_slow"] - 1) * 100
            row["status"] = "In a trade" if pos else f"Fast EMA {'above' if gap > 0 else 'below'} slow by {abs(gap):.2f}%"
        elif "st_dir" in w and w.get("st_dir"):
            row["status"] = "In a trade" if pos else f"Supertrend {'up' if w['st_dir'] > 0 else 'down'}, line {w.get('supertrend', 0):.2f}"
        else:
            row["status"] = "Warming up"
        rows.append(row)
    return rows
