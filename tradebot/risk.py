"""Risk agent: sizes every trade and can block or halt trading.

Every entry must pass these checks before an order is created. Exits are
always allowed (closing risk is never blocked).
"""
from __future__ import annotations

import time as _time
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Optional

from .models import Action, Instrument, Product, Segment, Side, Signal


@dataclass
class RiskConfig:
    capital: float = 500_000.0
    risk_per_trade_pct: float = 1.0          # % of equity lost if the stop is hit
    max_position_pct: float = 20.0           # max margin per position, % of equity
    max_open_positions: int = 5
    max_daily_loss_pct: float = 3.0          # kill switch: stop for the day
    max_trades_per_day: int = 20
    no_entry_before: str = "09:20"
    no_entry_after: str = "15:00"
    square_off_time: str = "15:15"           # intraday positions closed at this time
    allow_option_selling: bool = False       # option writing has unlimited risk
    max_orders_per_second: float = 5.0       # stay under SEBI's retail-algo threshold (10/s)
    # Approximate margin as fraction of notional (check your broker's SPAN/exposure margin)
    margin: dict = field(default_factory=lambda: {
        "eq_intraday": 0.20, "eq_delivery": 1.0, "fut": 0.15, "opt_buy": 1.0,
    })

    @classmethod
    def from_dict(cls, d: dict | None) -> "RiskConfig":
        d = dict(d or {})
        margin = {**cls().margin, **d.pop("margin", {})}
        return cls(margin=margin, **d)


@dataclass
class Decision:
    approved: bool
    qty: int = 0
    reason: str = ""


class RiskManager:
    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg
        self._day: Optional[date] = None
        self.day_start_equity = cfg.capital
        self.trades_today = 0
        self.halted = False
        self.halt_reason = ""
        self._order_times: deque[float] = deque()

    # --- daily bookkeeping -------------------------------------------------
    def new_bar(self, ts: datetime, equity: float) -> None:
        if self._day != ts.date():
            self._day = ts.date()
            self.day_start_equity = equity
            self.trades_today = 0
            self.halted = False
            self.halt_reason = ""
        loss_pct = (self.day_start_equity - equity) / self.day_start_equity * 100
        if not self.halted and loss_pct >= self.cfg.max_daily_loss_pct:
            self.halted = True
            self.halt_reason = f"Daily loss {loss_pct:.2f}% hit limit {self.cfg.max_daily_loss_pct}%"

    def kill(self, reason: str = "manual kill switch") -> None:
        self.halted = True
        self.halt_reason = reason

    def is_square_off_time(self, ts: datetime) -> bool:
        return ts.time() >= time.fromisoformat(self.cfg.square_off_time)

    # --- margin ------------------------------------------------------------
    def margin_fraction(self, inst: Instrument, side: Side) -> float:
        m = self.cfg.margin
        if inst.segment is Segment.FUT:
            return m["fut"]
        if inst.segment is Segment.OPT:
            return m["opt_buy"]
        return m["eq_delivery"] if inst.product is Product.DELIVERY else m["eq_intraday"]

    # --- main check ---------------------------------------------------------
    def check_entry(self, sig: Signal, equity: float, open_positions: int,
                    used_margin: float, live_clock: bool = False,
                    margin_per_unit: float | None = None) -> Decision:
        """margin_per_unit overrides the notional x margin-fraction estimate (e.g. option selling,
        where margin depends on the index level, not the premium)."""
        inst = sig.instrument
        if sig.action is not Action.ENTER:
            return Decision(True, reason="exit")
        if self.halted:
            return Decision(False, reason=f"Trading halted: {self.halt_reason}")
        t = sig.timestamp.time()
        if t < time.fromisoformat(self.cfg.no_entry_before) or t > time.fromisoformat(self.cfg.no_entry_after):
            return Decision(False, reason=f"Outside entry window ({t})")
        if self.trades_today >= self.cfg.max_trades_per_day:
            return Decision(False, reason="Max trades per day reached")
        if open_positions >= self.cfg.max_open_positions:
            return Decision(False, reason="Max open positions reached")
        if sig.side is Side.SELL:
            if inst.segment is Segment.EQ and inst.product is Product.DELIVERY:
                return Decision(False, reason="Cannot short equity in delivery")
            if inst.segment is Segment.OPT and not self.cfg.allow_option_selling:
                return Decision(False, reason="Option selling disabled")
        if sig.stop_loss is None:
            return Decision(False, reason="Signal has no stop-loss")
        per_unit_risk = abs(sig.price - sig.stop_loss)
        if per_unit_risk <= 0:
            return Decision(False, reason="Stop-loss equals entry price")
        if (sig.side is Side.BUY and sig.stop_loss >= sig.price) or \
           (sig.side is Side.SELL and sig.stop_loss <= sig.price):
            return Decision(False, reason="Stop-loss on wrong side of entry")

        risk_budget = equity * self.cfg.risk_per_trade_pct / 100
        qty_by_risk = risk_budget / per_unit_risk
        unit_margin = margin_per_unit if margin_per_unit else sig.price * self.margin_fraction(inst, sig.side)
        max_margin = equity * self.cfg.max_position_pct / 100
        free_margin = max(equity - used_margin, 0)
        qty_by_margin = min(max_margin, free_margin) / unit_margin
        qty = inst.round_qty(min(qty_by_risk, qty_by_margin))
        if qty <= 0:
            return Decision(False, reason=f"Size below 1 lot (lot={inst.lot_size}); risk or margin too small")

        if live_clock and not self._rate_ok():
            return Decision(False, reason="Order rate limit")
        return Decision(True, qty=qty, reason=f"risk Rs{qty * per_unit_risk:,.0f}, margin Rs{qty * unit_margin:,.0f}")

    def record_entry(self) -> None:
        self.trades_today += 1

    def _rate_ok(self) -> bool:
        now = _time.monotonic()
        while self._order_times and now - self._order_times[0] > 1.0:
            self._order_times.popleft()
        if len(self._order_times) >= self.cfg.max_orders_per_second:
            return False
        self._order_times.append(now)
        return True
