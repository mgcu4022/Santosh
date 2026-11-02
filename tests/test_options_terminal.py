"""Option terminal: chain parsing, pricing, engine rules and the local web API (offline)."""
import json
import threading
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tradebot.options.chain import Chain, Leg, SimOptions, bs_price, parse_upstox_chain
from tradebot.options.engine import OptionTerminal, TerminalConfig, TerminalError
from tradebot.risk import RiskConfig

IST = ZoneInfo("Asia/Kolkata")


# --- chain ------------------------------------------------------------------------------
UPSTOX_CHAIN = [
    {"expiry": "2026-11-03", "strike_price": 23200, "underlying_spot_price": 23232.65,
     "call_options": {"instrument_key": "NSE_FO|1", "market_data": {"ltp": 175.15, "oi": 500000, "prev_oi": 450000},
                      "option_greeks": {"iv": 11.5}},
     "put_options": {"instrument_key": "NSE_FO|2", "market_data": {"ltp": 142.5, "oi": 800000, "prev_oi": 820000},
                     "option_greeks": {"iv": 12.1}}},
    {"expiry": "2026-11-03", "strike_price": 23250, "underlying_spot_price": 23232.65,
     "call_options": {"instrument_key": "NSE_FO|3", "market_data": {"ltp": 150.0}},
     "put_options": {"instrument_key": "NSE_FO|4", "market_data": {"ltp": 168.0}}},
]


def test_parse_upstox_chain_and_derived_numbers():
    ch = parse_upstox_chain(UPSTOX_CHAIN, "NIFTY", "2026-11-03")
    assert ch.spot == 23232.65 and ch.strikes == [23200.0, 23250.0] and ch.lot_size == 65
    assert ch.atm() == 23250.0
    assert ch.rows[23200.0]["CE"].oi_chg == 50000 and ch.rows[23200.0]["PE"].iv == 12.1
    assert ch.straddle() == 318.0
    assert ch.synthetic_future() == 23250 + 150 - 168


def test_black_scholes_put_call_parity():
    s, k, t, r = 23200, 23300, 7 / 365, 0.065
    import math
    ce, pe = bs_price(s, k, t, 0.13, "CE", r), bs_price(s, k, t, 0.13, "PE", r)
    assert ce - pe == pytest.approx(s - k * math.exp(-r * t), abs=0.01)


def test_simulator_chain_is_sane():
    clock = lambda: datetime(2026, 11, 2, 10, 0, tzinfo=IST)
    sim = SimOptions(clock=clock)
    exps = sim.expiries("NIFTY")
    assert exps[0] == "2026-11-03"                                   # Tuesday weekly expiry
    ch = sim.chain("NIFTY", exps[1])
    calls = [ch.rows[k]["CE"].ltp for k in ch.strikes]
    assert calls == sorted(calls, reverse=True) and len(ch.strikes) == 41


# --- engine with a controllable market -------------------------------------------------------
class Market:
    """Fake provider: we set prices directly."""
    mode = "test"

    def __init__(self):
        self.spot = 23200.0
        self.prices = {}            # (strike, opt) -> ltp

    def set(self, strike, opt, px):
        self.prices[(float(strike), opt)] = px

    def expiries(self, u):
        return ["2026-11-10", "2026-11-17"]

    def chain(self, u, e):
        rows = {}
        for k in range(23000, 23450, 50):
            rows[float(k)] = {o: Leg(ltp=self.prices.get((float(k), o), 100.0)) for o in ("CE", "PE")}
        return Chain(u, e, self.spot, rows, lot_size=65, step=50)


@pytest.fixture
def term(tmp_path):
    now = {"t": datetime(2026, 11, 2, 10, 0, tzinfo=IST)}
    m = Market()
    t = OptionTerminal(m, TerminalConfig(capital=500_000, slippage_bps=0, out_dir=str(tmp_path), mtm_stop_loss=0),
                       RiskConfig(capital=500_000, max_position_pct=40, max_open_positions=6, max_trades_per_day=50,
                                  max_orders_per_second=1000, allow_option_selling=True),
                       clock=lambda: now["t"])
    t.market, t.now = m, now
    t.tick()
    return t


def test_buy_gets_stop_and_lot_limit(term):
    r = term.place("BUY", "CE", 23200, 1)
    assert r["ok"] and r["qty"] == 65
    pos = term.state()["positions"][0]
    assert pos["stop"] == 70.0 and pos["qty"] == 65                  # 30% below 100
    with pytest.raises(TerminalError, match="max .* lot"):
        term.place("BUY", "PE", 23200, 50)                          # 1% risk = Rs5,000 -> 2 lots at Rs30/unit risk


def test_adds_count_against_position_risk(term):
    term.place("BUY", "CE", 23200, 2)                               # max is 2 lots (5000 / 30 / 65)
    sym = term.state()["positions"][0]["symbol"]
    with pytest.raises(TerminalError, match="you hold 2"):
        term.add_lots(sym, 1)


def test_selling_uses_index_margin_and_can_be_disabled(term):
    term.market.set(23400, "CE", 20)
    term.tick()
    term.place("SELL", "CE", 23400, 1)                              # margin ~ 12% x 23200 x 65 = Rs1.81L
    assert term.state()["margin_used"] == pytest.approx(0.12 * 23200 * 65, rel=0.01)
    with pytest.raises(TerminalError, match="below 1 lot"):
        term.place("SELL", "CE", 23350, 2)                          # Rs100 premium, stop Rs200: 1 lot risks Rs6,500 > Rs5,000
    term.risk.cfg.allow_option_selling = False
    with pytest.raises(TerminalError, match="selling disabled"):
        term.place("SELL", "PE", 23000, 1)


def test_position_stop_loss_fires_on_tick(term):
    term.market.set(23300, "CE", 50)
    term.tick()
    term.place("SELL", "CE", 23300, 1)                              # stop at 100 (premium doubles)
    term.market.set(23300, "CE", 104)
    term.tick()
    s = term.state()
    assert s["positions"] == [] and s["closed"][0]["why"] == "stop-loss"
    assert s["closed"][0]["net"] < -3000


def test_exit_fraction_and_move(term):
    term.market.set(23250, "PE", 40)
    term.market.set(23200, "PE", 30)
    term.tick()
    term.place("BUY", "PE", 23250, 4)
    sym = term.state()["positions"][0]["symbol"]
    term.exit_fraction(sym, 0.5)
    assert term.state()["positions"][0]["lots"] == 2
    term.move(sym, 23200)
    pos = term.state()["positions"]
    assert len(pos) == 1 and pos[0]["strike"] == 23200 and pos[0]["lots"] == 2 and pos[0]["opt"] == "PE"


def test_mtm_stop_loss_exits_all_and_halts(term):
    term.set_rules(mtm_stop_loss=2000)
    term.place("BUY", "CE", 23200, 1)
    term.place("BUY", "PE", 23200, 1)
    term.market.set(23200, "CE", 75)
    term.market.set(23200, "PE", 75)                                # -25 x 65 x 2 = -3250
    term.tick()
    s = term.state()
    assert s["halted"] and "MTM stop-loss" in s["halt_reason"] and s["positions"] == []
    with pytest.raises(TerminalError, match="halted"):
        term.place("BUY", "CE", 23200, 1)


def test_mtm_target_and_square_off(term):
    term.set_rules(mtm_target=1000)
    term.place("BUY", "CE", 23200, 1)
    term.market.set(23200, "CE", 130)
    term.tick()
    assert term.state()["halted"] and term.state()["positions"] == []
    term.set_rules(mtm_target=0)
    term.risk.halted = False
    term.place("BUY", "PE", 23200, 1)
    term.now["t"] = term.now["t"].replace(hour=15, minute=16)
    term.tick()
    assert term.state()["positions"] == []
    assert any(t["why"] == "intraday square-off" for t in term.state()["closed"])


def test_entry_window_enforced(term):
    term.now["t"] = term.now["t"].replace(hour=15, minute=5)
    term.tick()
    with pytest.raises(TerminalError, match="entry window"):
        term.place("BUY", "CE", 23200, 1)


def test_trades_saved_for_report(term, tmp_path):
    from tradebot.report import compute, load_runs
    term.place("BUY", "CE", 23200, 1)
    term.exit_all()
    rd = load_runs(str(tmp_path), prefix="terminal")
    assert compute(rd)["trades"] == 1


# --- web API -------------------------------------------------------------------------------------
def test_http_api_roundtrip(term):
    from http.server import ThreadingHTTPServer
    from tradebot.options.server import make_handler
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(term))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def post(path, body):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        try:
            return json.load(op.open(req))
        except urllib.error.HTTPError as e:
            return json.load(e)

    try:
        assert "Option chain" in op.open(base + "/").read().decode()
        st = json.load(op.open(base + "/api/state"))
        assert st["underlying"] == "NIFTY" and len(st["chain"]) > 0
        assert post("/api/order", {"side": "BUY", "opt": "CE", "strike": 23200, "lots": 1})["ok"]
        bad = post("/api/order", {"side": "BUY", "opt": "CE", "strike": 99999, "lots": 1})
        assert not bad["ok"] and "No price" in bad["error"]
        assert post("/api/exit_all", {})["closed"] == 1
        assert not post("/api/order", {"side": "BUY"})["ok"]          # malformed request handled
    finally:
        srv.shutdown()
