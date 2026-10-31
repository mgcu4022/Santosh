"""Paper broker: simulated fills with slippage and Indian charges.

Fill rules (kept conservative on purpose):
- Market orders fill at the NEXT bar's open, plus slippage against you.
- Stops fill at the stop price, or at the open if the bar gaps through it.
- Targets fill at the target price, or at the open if it gaps through (no slippage).
- If a bar touches both stop and target, the stop is assumed to hit first.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from ..costs import ChargeModel
from ..models import Instrument, Order, OrderStatus, Position, Side, Trade
from .base import Broker


class PaperBroker(Broker):
    def __init__(self, capital: float, charges: ChargeModel | None = None,
                 slippage_bps: float = 2.0, margin_fn=None):
        self.cash = capital            # realized cash (capital + realized pnl - charges)
        self.charges = charges or ChargeModel()
        self.slippage = slippage_bps / 10_000
        self.margin_fn = margin_fn or (lambda inst, side: 1.0)
        self.pending: list[Order] = []
        self.positions: dict[str, Position] = {}
        self.trades: list[Trade] = []
        self.orders: list[Order] = []
        self.last_price: dict[str, float] = {}

    # --- Broker interface ----------------------------------------------------
    def place_order(self, order: Order) -> Order:
        self.pending.append(order)
        self.orders.append(order)
        return order

    def cancel_all(self, symbol: Optional[str] = None) -> None:
        for o in list(self.pending):
            if symbol is None or o.instrument.symbol == symbol:
                o.status = OrderStatus.CANCELLED
                self.pending.remove(o)

    def get_position(self, symbol: str) -> Optional[Position]:
        p = self.positions.get(symbol)
        return p if p and p.is_open else None

    def open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if p.is_open]

    def has_pending(self, symbol: str) -> bool:
        return any(o.instrument.symbol == symbol for o in self.pending)

    # --- accounting -----------------------------------------------------------
    def equity(self) -> float:
        unreal = sum(p.unrealized(self.last_price.get(s, p.avg_price))
                     for s, p in self.positions.items() if p.is_open)
        return self.cash + unreal

    def used_margin(self) -> float:
        return sum(abs(p.qty) * p.avg_price * self.margin_fn(p.instrument, p.side)
                   for p in self.open_positions())

    # --- simulation -------------------------------------------------------------
    def on_bar(self, inst: Instrument, ts: datetime, o: float, h: float, l: float, c: float) -> None:
        """Process one new bar for an instrument: fill pending orders, then stops/targets."""
        sym = inst.symbol
        for order in [x for x in self.pending if x.instrument.symbol == sym]:
            self.pending.remove(order)
            px = o * (1 + self.slippage * order.side.sign)
            self._fill(order, inst.round_price(px), ts, reason=order.tag or "signal")
        self._check_exits(inst, ts, o, h, l)
        self.last_price[sym] = c

    def close_now(self, inst: Instrument, price: float, ts: datetime, reason: str) -> None:
        pos = self.get_position(inst.symbol)
        if not pos:
            return
        side = pos.side.opposite()
        px = inst.round_price(price * (1 + self.slippage * side.sign))
        order = Order(instrument=inst, side=side, qty=abs(pos.qty), timestamp=ts,
                      product=inst.product, tag=reason)
        self.orders.append(order)
        self._fill(order, px, ts, reason)

    def _check_exits(self, inst, ts, o, h, l):
        pos = self.get_position(inst.symbol)
        if not pos:
            return
        stop, tgt = pos.stop_loss, pos.target
        if pos.qty > 0:
            if stop is not None and l <= stop:
                px = o if o <= stop else stop
                return self.close_now(inst, px, ts, "stop-loss")
            if tgt is not None and h >= tgt:
                px = o if o >= tgt else tgt
                return self._close_at(inst, pos, px, ts, "target")
        else:
            if stop is not None and h >= stop:
                px = o if o >= stop else stop
                return self.close_now(inst, px, ts, "stop-loss")
            if tgt is not None and l <= tgt:
                px = o if o <= tgt else tgt
                return self._close_at(inst, pos, px, ts, "target")

    def _close_at(self, inst, pos, px, ts, reason):
        order = Order(instrument=inst, side=pos.side.opposite(), qty=abs(pos.qty),
                      timestamp=ts, product=inst.product, order_type="LIMIT", tag=reason)
        self.orders.append(order)
        self._fill(order, inst.round_price(px), ts, reason)

    def _fill(self, order: Order, px: float, ts: datetime, reason: str) -> None:
        inst = order.instrument
        charges = self.charges.order_charges(inst.segment, inst.product, order.side, px, order.qty)
        order.status, order.fill_price, order.fill_time, order.charges = OrderStatus.FILLED, px, ts, charges
        self.cash -= charges
        pos = self.positions.get(inst.symbol)
        signed = order.qty * order.side.sign

        if pos is None or not pos.is_open:
            self.positions[inst.symbol] = Position(
                instrument=inst, qty=signed, avg_price=px, stop_loss=order.stop_loss,
                target=order.target, entry_time=ts, strategy=order.tag, entry_charges=charges)
            return

        if (pos.qty > 0) == (signed > 0):
            # adding to a position (not used by the default desk, kept for completeness)
            new_qty = pos.qty + signed
            pos.avg_price = (pos.avg_price * pos.qty + px * signed) / new_qty
            pos.qty = new_qty
            pos.entry_charges += charges
            return

        # reducing / closing
        close_qty = min(abs(signed), abs(pos.qty))
        gross = (px - pos.avg_price) * close_qty * (1 if pos.qty > 0 else -1)
        self.cash += gross
        frac = close_qty / abs(pos.qty)
        entry_ch = pos.entry_charges * frac
        self.trades.append(Trade(
            symbol=inst.symbol, strategy=pos.strategy, side="LONG" if pos.qty > 0 else "SHORT",
            qty=close_qty, entry_time=pos.entry_time, entry_price=pos.avg_price,
            exit_time=ts, exit_price=px, gross_pnl=round(gross, 2),
            charges=round(entry_ch + charges, 2), exit_reason=reason))
        pos.entry_charges -= entry_ch
        pos.qty += close_qty * (1 if pos.qty < 0 else -1)
        if pos.qty == 0:
            pos.stop_loss = pos.target = None
