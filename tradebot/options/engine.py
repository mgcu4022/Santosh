"""Option terminal engine (paper): orders, positions, MTM rules, stop-losses.

Every new or added position goes through RiskManager.check_entry. Each position
gets an automatic premium stop-loss (buys: premium falls by buy_sl_pct; sells:
premium rises by sell_sl_pct). MTM rules exit everything and stop new trades for
the day when the day's P&L hits the loss limit or the profit target.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from ..brokers.paper import PaperBroker
from ..costs import ChargeModel
from ..models import Action, Instrument, Order, Product, Segment, Side, Signal
from ..risk import RiskConfig, RiskManager
from .chain import UNDERLYINGS, Chain

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger(__name__)


@dataclass
class TerminalConfig:
    capital: float = 500_000
    underlying: str = "NIFTY"
    strikes_each_side: int = 10
    slippage_bps: float = 10          # options: wider spreads than stocks
    buy_sl_pct: float = 30            # long option: exit if premium falls 30%
    sell_sl_pct: float = 100          # short option: exit if premium doubles
    sell_margin_pct: float = 12       # approx SPAN+exposure per unit, % of index level
    mtm_stop_loss: float = 10_000     # exit all at -Rs (0 = off)
    mtm_target: float = 0             # exit all at +Rs (0 = off)
    product: str = "INTRADAY"
    min_premium: float = 0.5          # no new trades in options priced below this (nothing to gain, no liquidity)
    out_dir: str = "runs"

    @classmethod
    def from_dict(cls, d: dict | None) -> "TerminalConfig":
        return cls(**{k: v for k, v in (d or {}).items() if k in cls.__dataclass_fields__})


class TerminalError(Exception):
    pass


class OptionTerminal:
    def __init__(self, provider, tcfg: TerminalConfig, risk_cfg: RiskConfig, clock=None):
        self.p = provider
        self.cfg = tcfg
        self.clock = clock or (lambda: datetime.now(IST))
        self.risk = RiskManager(risk_cfg)
        self.broker = PaperBroker(tcfg.capital, ChargeModel(), slippage_bps=tcfg.slippage_bps,
                                  margin_fn=lambda inst, side: 1.0)
        self.lock = threading.RLock()
        self.underlying = tcfg.underlying
        self.expiries = self.p.expiries(self.underlying)
        self.expiry = self.expiries[0] if self.expiries else ""
        self.chain: Chain | None = None
        self.chains: dict[tuple, Chain] = {}    # (underlying, expiry) -> latest chain
        self.journal: list[dict] = []
        self.messages: list[dict] = []
        self.meta: dict[str, dict] = {}         # symbol -> {strike, opt, expiry, underlying}
        self.halt_note = ""
        self.run_dir = None

    # --- helpers -----------------------------------------------------------------------
    def _note(self, level: str, text: str):
        self.messages.append({"time": self.clock().strftime("%H:%M:%S"), "level": level, "text": text})
        self.messages = self.messages[-30:]

    def _log(self, event: str, symbol: str, **kw):
        self.journal.append({"time": self.clock(), "symbol": symbol, "event": event, **kw})

    def _symbol(self, strike: float, opt: str, expiry: str | None = None, underlying: str | None = None) -> str:
        e = datetime.fromisoformat(expiry or self.expiry)
        return f"{underlying or self.underlying} {e:%d%b}".upper() + f" {strike:g} {opt}"

    def _instrument(self, strike, opt, lot) -> Instrument:
        return Instrument(self._symbol(strike, opt), exchange="BFO" if self.underlying == "SENSEX" else "NFO", segment=Segment.OPT, lot_size=lot,
                          tick_size=0.05, product=Product(self.cfg.product), option_type=opt,
                          strike=strike, expiry=self.expiry)

    def _price(self, sym: str) -> float | None:
        m = self.meta.get(sym)
        ch = self.chains.get((m["underlying"], m["expiry"])) if m else None
        return ch.ltp(m["strike"], m["opt"]) if ch else None

    def _spot(self) -> float:
        return self.chain.spot if self.chain else UNDERLYINGS[self.underlying][3]

    def _sell_margin_unit(self) -> float:
        return self._spot() * self.cfg.sell_margin_pct / 100

    def used_margin(self) -> float:
        total = 0.0
        for p in self.broker.open_positions():
            if p.qty < 0:
                total += abs(p.qty) * self._sell_margin_unit()
            else:
                total += p.qty * p.avg_price
        return total

    def mtm(self) -> float:
        return round(self.broker.equity() - self.risk.day_start_equity, 2)

    # --- market data tick ---------------------------------------------------------------
    def tick(self) -> None:
        with self.lock:
            now = self.clock()
            self.chain = self.p.chain(self.underlying, self.expiry)
            self.chains[(self.underlying, self.expiry)] = self.chain
            held = {(m["underlying"], m["expiry"]) for s, m in self.meta.items() if self.broker.get_position(s)}
            for u, exp in held - {(self.underlying, self.expiry)}:
                try:
                    self.chains[(u, exp)] = self.p.chain(u, exp)
                except Exception as e:
                    log.warning("chain %s %s failed: %s", u, exp, e)
            # Mark positions; per-position stop-losses fire inside the paper broker
            for pos in self.broker.open_positions():
                px = self._price(pos.instrument.symbol)
                if px:
                    n = len(self.broker.trades)
                    self.broker.on_bar(pos.instrument, now, px, px, px, px)
                    for t in self.broker.trades[n:]:
                        self._log("CLOSED", t.symbol, qty=t.qty, exit=t.exit_price, net_pnl=round(t.net_pnl, 2), why=t.exit_reason)
                        self._note("warn", f"Stop-loss hit: {t.symbol} closed, P&L Rs{t.net_pnl:,.0f}")
            self.risk.new_bar(now, self.broker.equity())
            m = self.mtm()
            if not self.risk.halted:
                if self.cfg.mtm_stop_loss and m <= -abs(self.cfg.mtm_stop_loss):
                    self._halt(f"MTM stop-loss Rs{self.cfg.mtm_stop_loss:,.0f} hit")
                elif self.cfg.mtm_target and m >= self.cfg.mtm_target:
                    self._halt(f"MTM target Rs{self.cfg.mtm_target:,.0f} reached")
            elif self.broker.open_positions():
                self._exit_all_now(f"halt: {self.risk.halt_reason}")
            if self.cfg.product == "INTRADAY" and self.risk.is_square_off_time(now) and self.broker.open_positions():
                self._exit_all_now("intraday square-off")
                self._note("info", "15:15 square-off: all positions closed")
            self.save()

    def _halt(self, why: str):
        self.risk.kill(why)
        self.halt_note = why
        self._exit_all_now(why)
        self._log("HALT", "ALL", why=why)
        self._note("error", f"{why}: all positions closed, no new trades today")

    # --- actions --------------------------------------------------------------------------
    def _stop_for(self, side: Side, price: float, tick: float = 0.05) -> float:
        """Premium stop-loss, always at least one tick away from the price."""
        if side is Side.BUY:
            return max(round(round(price * (1 - self.cfg.buy_sl_pct / 100) / tick) * tick, 2), 0.0)
        return max(round(round(price * (1 + self.cfg.sell_sl_pct / 100) / tick) * tick, 2), round(price + tick, 2))

    def _evaluate(self, side: str, opt: str, strike: float, lots: int, for_real: bool) -> dict:
        """Everything about a prospective order, including the risk agent's decision."""
        if not self.chain:
            raise TerminalError("No market data yet")
        side_e, strike, lots = Side(side.upper()), float(strike), int(lots)
        if lots < 1:
            raise TerminalError("Lots must be at least 1")
        ltp = self.chain.ltp(strike, opt)
        if not ltp:
            raise TerminalError(f"No price for {strike:g} {opt}")
        lot = self.chain.lot_size
        inst = self._instrument(strike, opt, lot)
        qty = lots * lot
        existing = self.broker.get_position(inst.symbol)
        ev = {"symbol": inst.symbol, "side": side_e.value, "opt": opt, "strike": strike, "lots": lots, "qty": qty,
              "lot_size": lot, "ltp": ltp, "inst": inst, "existing": existing,
              "est_price": inst.round_price(ltp * (1 + self.broker.slippage * side_e.sign))}
        if existing and (existing.qty > 0) != (side_e is Side.BUY):
            ev.update(closing=True, approved=True, close_qty=min(qty, abs(existing.qty)),
                      reason=f"This closes {min(qty, abs(existing.qty)) // lot} lot(s) of your open position")
            return ev
        ev["closing"] = False
        if ltp < self.cfg.min_premium:
            ev.update(approved=False, max_lots=0,
                      reason=f"Premium Rs{ltp:g} is below Rs{self.cfg.min_premium:g}: almost nothing to gain and hard to trade")
            return ev
        sl = self._stop_for(side_e, ltp, inst.tick_size)
        sig = Signal(inst, Action.ENTER, side_e, ltp, self.clock(), "manual", stop_loss=sl)
        n_open = len(self.broker.open_positions()) - (1 if existing else 0)
        d = self.risk.check_entry(sig, self.broker.equity(), n_open, self.used_margin(), live_clock=for_real,
                                  margin_per_unit=self._sell_margin_unit() if side_e is Side.SELL else None)
        held = abs(existing.qty) if existing else 0          # adds count against the same risk budget
        max_lots = max(d.qty - held, 0) // lot if d.approved else 0
        ev.update(stop=sl, max_lots=max_lots)
        if not d.approved:
            ev.update(approved=False, reason=d.reason)
        elif qty > d.qty - held:
            ev.update(approved=False, reason=(
                f"max {d.qty // lot} lot(s) in this position under your risk rules; you hold {held // lot}, "
                f"so {max_lots} more allowed (asked {lots})") if held else
                f"max {d.qty // lot} lot(s) under your risk rules (asked {lots})")
        else:
            ev.update(approved=True, reason="Passes the risk agent")
        return ev

    def preview(self, side: str, opt: str, strike: float, lots: int) -> dict:
        """Plain-language order ticket: cost, margin, stop-loss, breakeven, and the risk decision."""
        with self.lock:
            ev = self._evaluate(side, opt, strike, lots, for_real=False)
            p, q, k, und = ev["est_price"], ev["qty"], ev["strike"], self.underlying
            out = {k2: v for k2, v in ev.items() if k2 not in ("inst", "existing")}
            out["ok"] = True
            if ev["closing"]:
                out["text"] = [ev["reason"] + f", at about Rs{p:,.2f}."]
                return out
            if "stop" not in ev:          # blocked before sizing (e.g. premium too small)
                out["text"] = []
                return out
            buy, call = ev["side"] == "BUY", ev["opt"] == "CE"
            value = round(p * q, 2)
            margin = value if buy else round(self._sell_margin_unit() * q, 0)
            be = k + p if call else k - p
            charges = self.broker.charges.order_charges(Segment.OPT, ev["inst"].product, Side(ev["side"]), p, q) +                 self.broker.charges.order_charges(Segment.OPT, ev["inst"].product, Side(ev["side"]).opposite(), ev.get("stop") or p, q)
            loss_sl = round(abs(p - (ev.get("stop") or p)) * q + charges, 0)
            dirn = "rises" if call else "falls"
            if buy:
                text = [f"You PAY Rs{value:,.0f} now ({q} qty x Rs{p:,.2f}).",
                        f"You gain if {und} {dirn} enough: at expiry you need {und} {'above' if call else 'below'} {be:,.0f} "
                        f"(breakeven). Before expiry, any rise in this option's price is profit.",
                        f"Most you can lose is the Rs{value:,.0f} paid. The stop-loss (Rs{ev.get('stop', 0):,.2f}) exits earlier, "
                        f"at about Rs{loss_sl:,.0f} loss including charges."]
            else:
                text = [f"You RECEIVE Rs{value:,.0f} now ({q} qty x Rs{p:,.2f}), and Rs{margin:,.0f} margin is blocked.",
                        f"You keep it if {und} stays {'below' if call else 'above'} {k:,.0f} at expiry; you start losing beyond "
                        f"{be:,.0f}. A sharp move against you can lose much more than you received.",
                        f"The stop-loss (Rs{ev.get('stop', 0):,.2f}) exits at about Rs{loss_sl:,.0f} loss including charges."]
            out.update(value=value, margin=margin, breakeven=round(be, 2), loss_at_stop=loss_sl,
                       charges=round(charges, 0), text=text)
            return out

    def place(self, side: str, opt: str, strike: float, lots: int) -> dict:
        with self.lock:
            ev = self._evaluate(side, opt, strike, lots, for_real=True)
            inst, side_e, qty_req = ev["inst"], Side(ev["side"]), ev["qty"]
            if ev["closing"]:
                return self._reduce(ev["existing"], ev["close_qty"], "manual exit")
            if not ev["approved"]:
                self._log("RISK_BLOCK", inst.symbol, side=side_e.value, lots=lots, why=ev["reason"])
                raise TerminalError(f"Blocked by risk agent: {ev['reason']}")
            self.meta[inst.symbol] = {"strike": ev["strike"], "opt": ev["opt"], "expiry": self.expiry,
                                      "underlying": self.underlying}
            order = Order(inst, side_e, qty_req, self.clock(), inst.product, tag="manual", stop_loss=ev["stop"])
            self.broker.fill_now(order, ev["ltp"], self.clock())
            pos = self.broker.get_position(inst.symbol)
            if pos:   # keep the stop relative to the average price after adds
                pos.stop_loss = self._stop_for(pos.side, pos.avg_price, inst.tick_size)
            self.risk.record_entry()
            self._log("ENTRY", inst.symbol, side=side_e.value, qty=qty_req, price=order.fill_price, stop=pos.stop_loss)
            self._note("ok", f"{side_e.value} {ev['lots']} lot(s) {inst.symbol} @ {order.fill_price} (SL {pos.stop_loss})")
            self.save()
            return {"ok": True, "price": order.fill_price, "qty": qty_req}

    def exit_fraction(self, symbol: str, fraction: float) -> dict:
        with self.lock:
            pos = self.broker.get_position(symbol)
            if not pos:
                raise TerminalError("No open position")
            lot = pos.instrument.lot_size
            lots = abs(pos.qty) // lot
            n = max(1, round(lots * fraction)) if fraction < 1 else lots
            return self._reduce(pos, n * lot, f"manual exit {fraction:g}")

    def add_lots(self, symbol: str, lots: int) -> dict:
        with self.lock:
            pos = self.broker.get_position(symbol)
            if not pos:
                raise TerminalError("No open position")
            m = self.meta[symbol]
            if m["expiry"] != self.expiry or m["underlying"] != self.underlying:
                raise TerminalError("Switch the chain to this position's expiry to add")
            return self.place(pos.side.value, m["opt"], m["strike"], lots)

    def move(self, symbol: str, new_strike: float) -> dict:
        """Roll: close this position and open the same lots at another strike (same side, type)."""
        with self.lock:
            pos = self.broker.get_position(symbol)
            if not pos:
                raise TerminalError("No open position")
            m = self.meta[symbol]
            if m["expiry"] != self.expiry or m["underlying"] != self.underlying:
                raise TerminalError("Switch the chain to this position's expiry to move it")
            lots, side = abs(pos.qty) // pos.instrument.lot_size, pos.side.value
            if not self.chain.ltp(float(new_strike), m["opt"]):
                raise TerminalError(f"No price for {new_strike:g} {m['opt']}")
            self._reduce(pos, abs(pos.qty), f"moved to {float(new_strike):g}")
            try:
                return self.place(side, m["opt"], new_strike, lots)
            except TerminalError as e:
                self._note("error", f"Closed {symbol} but new strike was blocked: {e}")
                raise

    def exit_all(self) -> dict:
        with self.lock:
            n = self._exit_all_now("manual exit all")
            self._note("info", f"Exit all: {n} position(s) closed")
            return {"ok": True, "closed": n}

    def set_rules(self, mtm_stop_loss: float | None = None, mtm_target: float | None = None) -> dict:
        with self.lock:
            if mtm_stop_loss is not None:
                self.cfg.mtm_stop_loss = abs(float(mtm_stop_loss))
            if mtm_target is not None:
                self.cfg.mtm_target = abs(float(mtm_target))
            self._note("info", f"Rules: MTM SL Rs{self.cfg.mtm_stop_loss:,.0f}, target Rs{self.cfg.mtm_target:,.0f} (0 = off)")
            return {"ok": True}

    def set_view(self, underlying: str | None = None, expiry: str | None = None) -> dict:
        with self.lock:
            if underlying and underlying != self.underlying:
                if underlying not in UNDERLYINGS:
                    raise TerminalError("Unknown index")
                self.underlying = underlying
                self.expiries = self.p.expiries(underlying)
                self.expiry = self.expiries[0] if self.expiries else ""
            if expiry:
                if expiry not in self.expiries:
                    raise TerminalError("Unknown expiry")
                self.expiry = expiry
            self.chain = self.p.chain(self.underlying, self.expiry)
            self.chains[(self.underlying, self.expiry)] = self.chain
            return {"ok": True}

    def _reduce(self, pos, qty: int, reason: str) -> dict:
        px = self._price(pos.instrument.symbol)
        if not px:
            raise TerminalError("No price to exit at")
        n = len(self.broker.trades)
        order = Order(pos.instrument, pos.side.opposite(), qty, self.clock(), pos.instrument.product, tag=reason)
        self.broker.fill_now(order, px, self.clock(), reason)
        for t in self.broker.trades[n:]:
            self._log("CLOSED", t.symbol, qty=t.qty, exit=t.exit_price, net_pnl=round(t.net_pnl, 2), why=reason)
            self._note("ok", f"Closed {t.qty} {t.symbol} @ {t.exit_price}: P&L Rs{t.net_pnl:,.0f}")
        self.save()
        return {"ok": True, "price": order.fill_price}

    def _exit_all_now(self, reason: str) -> int:
        n = 0
        for p in list(self.broker.open_positions()):
            try:
                self._reduce(p, abs(p.qty), reason)
                n += 1
            except TerminalError as e:
                self._note("error", f"Could not exit {p.instrument.symbol}: {e}")
        return n

    # --- state for the UI -------------------------------------------------------------------
    def state(self) -> dict:
        with self.lock:
            ch = self.chain
            rows = []
            if ch:
                atm = ch.atm()
                for k in ch.window(self.cfg.strikes_each_side):
                    r = ch.rows[k]
                    leg = lambda L: vars(L) | {"key": "", "chg_pct": L.chg_pct}
                    rows.append({"strike": k, "atm": k == atm, "ce": leg(r["CE"]), "pe": leg(r["PE"])})
            positions = []
            for sym, m in self.meta.items():
                p = self.broker.get_position(sym)
                if not p:
                    continue
                px = self._price(sym) or p.avg_price
                call, long = m["opt"] == "CE", p.qty > 0
                be = m["strike"] + p.avg_price if call else m["strike"] - p.avg_price
                u = m["underlying"]
                if long:
                    desc = f"Bought {'call' if call else 'put'}: gains if {u} {'rises' if call else 'falls'}" \
                           f" (expiry breakeven {be:,.0f})"
                else:
                    desc = f"Sold {'call' if call else 'put'}: gains if {u} stays {'below' if call else 'above'} " \
                           f"{m['strike']:,.0f} (loses beyond {be:,.0f})"
                positions.append({"symbol": sym, "strike": m["strike"], "opt": m["opt"], "expiry": m["expiry"],
                                  "underlying": u, "qty": p.qty, "lots": abs(p.qty) // p.instrument.lot_size,
                                  "avg": round(p.avg_price, 2), "ltp": px, "pnl": round(p.unrealized(px), 2),
                                  "pnl_pct": round((px / p.avg_price - 1) * 100 * (1 if long else -1), 1) if p.avg_price else 0,
                                  "stop": p.stop_loss, "breakeven": round(be, 2), "desc": desc,
                                  "movable": m["expiry"] == self.expiry and u == self.underlying})
            positions.sort(key=lambda r: (r["qty"] > 0, r["strike"]))
            closed = [{"symbol": t.symbol, "qty": t.qty, "side": t.side, "entry": t.entry_price, "exit": t.exit_price,
                       "net": round(t.net_pnl, 2), "why": t.exit_reason, "time": t.exit_time.strftime("%H:%M:%S")}
                      for t in self.broker.trades[-15:]][::-1]
            realized = round(sum(t.net_pnl for t in self.broker.trades), 2)
            return {
                "mode": getattr(self.p, "mode", "live"), "time": self.clock().strftime("%H:%M:%S"),
                "underlying": self.underlying, "underlyings": list(UNDERLYINGS), "expiry": self.expiry,
                "expiries": self.expiries, "lot_size": ch.lot_size if ch else UNDERLYINGS[self.underlying][2],
                "spot": ch.spot if ch else None, "prev_close": ch.prev_close if ch else None,
                "high": ch.day_high if ch else None, "low": ch.day_low if ch else None,
                "syn_future": ch.synthetic_future() if ch else None, "straddle": ch.straddle() if ch else None,
                "atm": ch.atm() if ch else None, "chain": rows, "positions": positions, "closed": closed,
                "mtm": self.mtm(), "realized": realized, "unrealized": round(self.mtm() - realized, 2),
                "margin_used": round(self.used_margin(), 0), "equity": round(self.broker.equity(), 2),
                "rules": {"mtm_stop_loss": self.cfg.mtm_stop_loss, "mtm_target": self.cfg.mtm_target,
                          "buy_sl_pct": self.cfg.buy_sl_pct, "sell_sl_pct": self.cfg.sell_sl_pct},
                "halted": self.risk.halted, "halt_reason": self.risk.halt_reason,
                "trades_today": self.risk.trades_today, "max_trades": self.risk.cfg.max_trades_per_day,
                "messages": self.messages[::-1][:8],
                "analytics": ch.analytics() if ch else {},
                "days_to_expiry": self._days_to_expiry(),
                "limits": {"capital": self.cfg.capital,
                           "daily_loss_limit": round(self.risk.day_start_equity * self.risk.cfg.max_daily_loss_pct / 100, 0),
                           "risk_per_trade": round(self.broker.equity() * self.risk.cfg.risk_per_trade_pct / 100, 0),
                           "min_premium": self.cfg.min_premium},
            }

    def _days_to_expiry(self) -> float | None:
        if not self.expiry:
            return None
        now = self.clock()
        exp = datetime.fromisoformat(self.expiry).replace(hour=15, minute=30, tzinfo=now.tzinfo)
        return round(max((exp - now).total_seconds(), 0) / 86400, 1)

    # --- persistence (same files as paper runs, so `report --prefix terminal` works) -----
    def save(self) -> None:
        if self.run_dir is None:
            base = Path(self.cfg.out_dir) / f"terminal_{self.clock():%Y%m%d}"
            run, n = base, 1
            while run.exists():
                n += 1
                run = base.with_name(f"{base.name}_{n}")
            run.mkdir(parents=True, exist_ok=True)
            (run / "meta.json").write_text(f'{{"capital": {self.cfg.capital}}}', encoding="utf-8")
            self.run_dir = run
        pd.DataFrame(self.journal).to_csv(self.run_dir / "journal.csv", index=False)
        pd.DataFrame([{**t.__dict__, "net_pnl": t.net_pnl} for t in self.broker.trades]).to_csv(
            self.run_dir / "trades.csv", index=False)
