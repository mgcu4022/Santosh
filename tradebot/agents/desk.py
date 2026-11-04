"""Trading desk: the orchestrator that chains the agents for each new bar.

    market data -> strategy agent -> risk agent -> AI analyst -> execution (broker)

The same desk runs in backtests and live paper trading, so both behave the same.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from ..brokers.paper import PaperBroker
from ..models import Action, Instrument, Order, Product, Signal
from ..risk import RiskManager
from ..strategies.base import Strategy
from .analyst import AIAnalyst

log = logging.getLogger(__name__)


@dataclass
class Book:
    """One instrument traded by one strategy."""
    instrument: Instrument
    strategy: Strategy


class TradingDesk:
    def __init__(self, books: list[Book], risk: RiskManager, broker: PaperBroker,
                 analyst: Optional[AIAnalyst] = None, live_clock: bool = False, notifier=None,
                 context=None):
        self.books = {b.instrument.symbol: b for b in books}
        self.risk = risk
        self.broker = broker
        self.analyst = analyst
        self.live_clock = live_clock
        self.journal: list[dict] = []
        self.notifier = notifier
        self.context = context          # MarketContext: ban list, events, news (paper mode)
        self._halt_announced = None

    def log(self, ts, symbol, event, **details):
        row = {"time": ts, "symbol": symbol, "event": event, **details}
        self.journal.append(row)
        log.info("%s %-12s %-10s %s", ts, symbol, event, details)
        if self.notifier is not None and self.notifier.wants(event):
            from ..alerts import format_event
            try:
                self.notifier.send(format_event(event, symbol, details))
            except Exception as e:   # alerts must never stop trading
                log.warning("alert failed: %s", e)

    # ------------------------------------------------------------------
    def on_bar(self, symbol: str, df: pd.DataFrame, i: int) -> None:
        """Handle bar i (just closed) of a prepared DataFrame for `symbol`."""
        book = self.books[symbol]
        inst, strat = book.instrument, book.strategy
        ts = df.index[i].to_pydatetime()
        row = df.iloc[i]
        o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])

        n_trades = len(self.broker.trades)
        self.broker.on_bar(inst, ts, o, h, l, c)      # fills + stop/target exits
        for t in self.broker.trades[n_trades:]:
            self.log(ts, symbol, "CLOSED", side=t.side, qty=t.qty, exit=t.exit_price,
                     net_pnl=round(t.net_pnl, 2), why=t.exit_reason)

        self.risk.new_bar(ts, self.broker.equity())
        pos = self.broker.get_position(symbol)

        # Kill switch: flatten and stop.
        if self.risk.halted:
            if self._halt_announced != ts.date():
                self._halt_announced = ts.date()
                self.log(ts, "ALL", "HALT", why=self.risk.halt_reason)
            if pos:
                self.broker.cancel_all(symbol)
                self._flatten(inst, c, ts, "daily-loss halt" if "Daily loss" in self.risk.halt_reason else "kill switch")
            return

        # Intraday square-off.
        if self.risk.is_square_off_time(ts) and inst.product is Product.INTRADAY:
            self.broker.cancel_all(symbol)
            if pos:
                self._flatten(inst, c, ts, "intraday square-off")
            return

        if self.broker.has_pending(symbol):
            return
        sig = strat.on_bar(inst, df, i, pos)
        if sig is None:
            return
        if sig.action is Action.EXIT and pos:
            self.broker.place_order(Order(instrument=inst, side=sig.side, qty=abs(pos.qty),
                                          timestamp=ts, product=inst.product, tag=sig.reason))
            self.log(ts, symbol, "EXIT_SIG", why=sig.reason)
        elif sig.action is Action.ENTER and not pos:
            self._try_enter(sig, df.iloc[: i + 1])

    def _try_enter(self, sig: Signal, history: pd.DataFrame) -> None:
        inst, ts = sig.instrument, sig.timestamp
        if self.context is not None:
            ok, why = self.context.guard(sig)
            if not ok:
                self.log(ts, inst.symbol, "EVENT_BLOCK", side=sig.side.value, why=why)
                return
        n_open = len(self.broker.open_positions()) + len(self.broker.pending_entries())   # waiting entries count too
        d = self.risk.check_entry(sig, self.broker.equity(), n_open,
                                  self.broker.used_margin(), live_clock=self.live_clock)
        if not d.approved:
            self.log(ts, inst.symbol, "RISK_BLOCK", side=sig.side.value, why=d.reason)
            return
        if self.analyst is not None and self.analyst.cfg.enabled:
            extra = self.context.brief(inst.symbol) if self.context is not None else ""
            v = self.analyst.review(sig, history, extra)
            if not v.approve:
                self.log(ts, inst.symbol, "AI_VETO", side=sig.side.value, conf=v.confidence, why=v.reason)
                return
            sig.notes.append(f"AI({v.source}) {v.confidence:.2f}: {v.reason}")
        self.broker.place_order(Order(instrument=inst, side=sig.side, qty=d.qty, timestamp=ts,
                                      product=inst.product, tag=sig.strategy,
                                      stop_loss=sig.stop_loss, target=sig.target))
        self.risk.record_entry()
        self.log(ts, inst.symbol, "ENTRY_SIG", side=sig.side.value, qty=d.qty, ref=round(sig.price, 2),
                 stop=round(sig.stop_loss, 2), target=None if sig.target is None else round(sig.target, 2),
                 why=sig.reason, risk=d.reason, notes="; ".join(sig.notes))

    def _flatten(self, inst: Instrument, price: float, ts, reason: str) -> None:
        n = len(self.broker.trades)
        self.broker.close_now(inst, price, ts, reason)
        for t in self.broker.trades[n:]:
            self.log(ts, inst.symbol, "CLOSED", side=t.side, qty=t.qty, exit=t.exit_price,
                     net_pnl=round(t.net_pnl, 2), why=t.exit_reason)

    def flatten_all(self, ts, reason: str) -> None:
        for p in self.broker.open_positions():
            self._flatten(p.instrument, self.broker.last_price.get(p.instrument.symbol, p.avg_price), ts, reason)
