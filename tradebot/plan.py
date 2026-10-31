"""Trade plan calculator: size, risk, margin and charges for one idea.

Answers "if I take this trade, how many shares/lots, what do I risk, what do I
make at target after all charges, and how far must price move just to break even?"
Uses the same risk engine and charge model as the backtester.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .costs import ChargeModel
from .models import Action, Instrument, Product, Segment, Side, Signal
from .risk import RiskConfig, RiskManager


def make_plan(symbol: str, side: str, entry: float, stop: float, target: float | None = None,
              reward_risk: float = 2.0, segment: str = "EQ", product: str = "INTRADAY",
              lot_size: int = 1, capital: float = 500_000, risk_pct: float = 1.0,
              max_position_pct: float = 25.0, charges: ChargeModel | None = None) -> dict:
    s = Side(side.upper())
    inst = Instrument(symbol.upper(), segment=Segment(segment.upper()), product=Product(product.upper()),
                      lot_size=lot_size)
    per_unit = abs(entry - stop)
    if target is None:
        target = entry + s.sign * reward_risk * per_unit
    ts = datetime.now(ZoneInfo("Asia/Kolkata")).replace(hour=10, minute=0)   # inside entry window
    rm = RiskManager(RiskConfig(capital=capital, risk_per_trade_pct=risk_pct, max_position_pct=max_position_pct))
    d = rm.check_entry(Signal(inst, Action.ENTER, s, entry, ts, "plan", stop_loss=stop, target=target),
                       capital, 0, 0)
    plan = {"symbol": inst.symbol, "side": s.value, "entry": entry, "stop": stop, "target": round(target, 2),
            "approved": d.approved, "note": d.reason}
    if not d.approved:
        return plan
    q = d.qty
    cm = charges or ChargeModel()
    exit_side = s.opposite()

    def rt_charges(exit_px):
        return cm.order_charges(inst.segment, inst.product, s, entry, q) + \
               cm.order_charges(inst.segment, inst.product, exit_side, exit_px, q)

    gross_win = abs(target - entry) * q
    gross_loss = per_unit * q
    ch_win, ch_loss = rt_charges(target), rt_charges(stop)
    breakeven_move = rt_charges(entry) / q
    mfrac = rm.margin_fraction(inst, s)
    plan.update({
        "qty": q, "lots": q // lot_size,
        "position_value": round(q * entry, 2), "margin_needed": round(q * entry * mfrac, 2),
        "risk_at_stop": round(gross_loss + ch_loss, 2),
        "profit_at_target": round(gross_win - ch_win, 2),
        "net_reward_risk": round((gross_win - ch_win) / (gross_loss + ch_loss), 2),
        "round_trip_charges": round(ch_win, 2),
        "breakeven_move": round(breakeven_move, 2),
        "breakeven_pct": round(breakeven_move / entry * 100, 3),
        "win_rate_needed_pct": round(100 * (gross_loss + ch_loss) / (gross_win - ch_win + gross_loss + ch_loss), 1),
    })
    return plan
