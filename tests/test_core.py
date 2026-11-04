from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from tradebot.agents.analyst import AIAnalyst, AnalystConfig
from tradebot.agents.desk import Book, TradingDesk
from tradebot.backtest import run_backtest
from tradebot.brokers.paper import PaperBroker
from tradebot.costs import ChargeModel
from tradebot.data.synthetic import synthetic_intraday
from tradebot.models import (Action, Instrument, Order, Product, Segment, Side, Signal)
from tradebot.risk import RiskConfig, RiskManager
from tradebot.strategies import build_strategy

IST = ZoneInfo("Asia/Kolkata")
T = datetime(2026, 3, 2, 10, 0, tzinfo=IST)
EQ = Instrument("TEST", product=Product.INTRADAY)


def sig(side=Side.BUY, price=100.0, stop=98.0, inst=EQ, ts=T, target=None):
    return Signal(inst, Action.ENTER, side, price, ts, "test", stop_loss=stop, target=target)


# --- costs -------------------------------------------------------------------
def test_intraday_brokerage_capped_and_stt_on_sell_only():
    cm = ChargeModel()
    buy = cm.order_charges(Segment.EQ, Product.INTRADAY, Side.BUY, 1000, 100)    # turnover 1 lakh
    sell = cm.order_charges(Segment.EQ, Product.INTRADAY, Side.SELL, 1000, 100)
    assert sell > buy                       # STT 0.025% on sell side = Rs 25
    assert sell - buy == pytest.approx(25 - 3, abs=0.5)   # minus buy-side stamp duty Rs 3


def test_charge_overrides():
    cm = ChargeModel({"eq_intraday": {"brokerage_flat": 0, "brokerage_pct": 0}})
    assert cm.order_charges(Segment.EQ, Product.INTRADAY, Side.BUY, 100, 1) < 1


# --- risk ----------------------------------------------------------------------
def test_position_size_from_risk_budget():
    rm = RiskManager(RiskConfig(capital=500_000, risk_per_trade_pct=1.0, max_position_pct=100))
    d = rm.check_entry(sig(price=100, stop=98), 500_000, 0, 0)
    assert d.approved and d.qty == 2500          # Rs 5000 risk / Rs 2 per share


def test_size_rounds_down_to_lots():
    fut = Instrument("NIFTY-FUT", segment=Segment.FUT, lot_size=65)
    rm = RiskManager(RiskConfig(capital=10_000_000, risk_per_trade_pct=1.0, max_position_pct=50))
    d = rm.check_entry(sig(price=24000, stop=23900, inst=fut), 10_000_000, 0, 0)
    assert d.approved and d.qty % 65 == 0 and d.qty * 100 <= 100_000


@pytest.mark.parametrize("kwargs,why", [
    (dict(stop=None), "no stop"),
    (dict(stop=101), "wrong side"),
    (dict(ts=T.replace(hour=9, minute=16)), "Outside entry window"),
    (dict(ts=T.replace(hour=15, minute=5)), "Outside entry window"),
])
def test_entry_rejections(kwargs, why):
    rm = RiskManager(RiskConfig())
    d = rm.check_entry(sig(**kwargs), 500_000, 0, 0)
    assert not d.approved and why.lower() in d.reason.lower()


def test_no_short_in_delivery_and_no_option_writing():
    rm = RiskManager(RiskConfig())
    deliv = Instrument("X", product=Product.DELIVERY)
    opt = Instrument("NIFTY24500CE", segment=Segment.OPT, lot_size=65)
    assert not rm.check_entry(sig(Side.SELL, 100, 102, deliv), 5e5, 0, 0).approved
    assert not rm.check_entry(sig(Side.SELL, 100, 102, opt), 5e5, 0, 0).approved


def test_daily_loss_kill_switch_resets_next_day():
    rm = RiskManager(RiskConfig(capital=100_000, max_daily_loss_pct=2))
    rm.new_bar(T, 100_000)
    rm.new_bar(T.replace(hour=11), 97_900)
    assert rm.halted
    assert not rm.check_entry(sig(), 97_900, 0, 0).approved
    rm.new_bar(T.replace(day=3), 97_900)
    assert not rm.halted


# --- paper broker ----------------------------------------------------------------
def _long(broker, stop=95.0, target=110.0):
    broker.place_order(Order(EQ, Side.BUY, 10, T, Product.INTRADAY, tag="t", stop_loss=stop, target=target))
    broker.on_bar(EQ, T, 100, 101, 99, 100)     # fills at open 100


def test_market_order_fills_next_open_with_slippage():
    b = PaperBroker(100_000, slippage_bps=10)
    b.place_order(Order(EQ, Side.BUY, 10, T, Product.INTRADAY))
    b.on_bar(EQ, T, 200, 201, 199, 200)
    assert b.get_position("TEST").avg_price == pytest.approx(200.2)


def test_gap_through_stop_fills_at_open():
    b = PaperBroker(100_000, slippage_bps=0)
    _long(b)
    b.on_bar(EQ, T, 90, 91, 89, 90)              # gaps below 95
    assert b.trades[-1].exit_price == 90 and b.trades[-1].exit_reason == "stop-loss"


def test_stop_wins_when_bar_touches_both():
    b = PaperBroker(100_000, slippage_bps=0)
    _long(b)
    b.on_bar(EQ, T, 100, 111, 94, 100)
    assert b.trades[-1].exit_reason == "stop-loss"


def test_pnl_accounting_includes_charges():
    b = PaperBroker(100_000, slippage_bps=0)
    _long(b)
    b.on_bar(EQ, T, 105, 112, 104, 111)          # target 110
    t = b.trades[-1]
    assert t.gross_pnl == pytest.approx(100) and t.charges > 0
    assert b.equity() == pytest.approx(100_000 + t.net_pnl)


# --- strategies: no look-ahead ------------------------------------------------------
@pytest.mark.parametrize("name", ["ema_crossover", "orb", "supertrend"])
def test_strategies_do_not_peek_at_future_bars(name):
    df = synthetic_intraday(days=6, seed=4)
    cut = len(df) - 40
    strat = build_strategy(name)
    full = strat.prepare(df)
    tampered = df.copy()
    tampered.iloc[cut + 1:, :4] *= 1.5          # change the future
    part = strat.prepare(tampered)
    cols = [c for c in full.columns if c not in ("open", "high", "low", "close", "volume")]
    pd.testing.assert_frame_equal(full[cols].iloc[: cut + 1], part[cols].iloc[: cut + 1])


def test_random_walk_has_no_edge_before_costs():
    """Guards against look-ahead bias: on a pure random walk, gross P&L should average ~0."""
    gross = []
    for seed in range(6):
        df = synthetic_intraday(days=60, seed=seed, trend_days=0)
        risk = RiskManager(RiskConfig(capital=1e6))
        broker = PaperBroker(1e6, ChargeModel(), slippage_bps=0)
        desk = TradingDesk([Book(EQ, build_strategy("orb"))], risk, broker)
        res = run_backtest(desk, {"TEST": df})
        gross.append(res.summary.get("gross_pnl", 0) / 1e6)
    assert abs(np.mean(gross)) < 0.03          # under 3% of capital on average


# --- desk ----------------------------------------------------------------------------
def test_intraday_positions_squared_off_and_backtest_runs():
    df = synthetic_intraday(days=20, seed=9)
    risk = RiskManager(RiskConfig(capital=1e6))
    desk = TradingDesk([Book(EQ, build_strategy("orb"))], risk, PaperBroker(1e6))
    res = run_backtest(desk, {"TEST": df})
    assert res.summary["trades"] > 0
    assert all(pd.Timestamp(t).time() <= datetime.strptime("15:15", "%H:%M").time()
               or r != "intraday square-off"
               for t, r in zip(res.trades.exit_time, res.trades.exit_reason))
    # never holds an intraday position overnight
    assert (pd.to_datetime(res.trades.entry_time).dt.date == pd.to_datetime(res.trades.exit_time).dt.date).all()


# --- AI analyst (fake client, no network) ----------------------------------------------
class FakeClient:
    def __init__(self, payload=None, error=None, stop_reason="end_turn"):
        self.payload, self.error, self.stop_reason, self.calls = payload, error, stop_reason, []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        import json
        return SimpleNamespace(stop_reason=self.stop_reason,
                               content=[SimpleNamespace(type="text", text=json.dumps(self.payload))])


BARS = synthetic_intraday(days=2, seed=1)


def _analyst(client, **cfg):
    return AIAnalyst(AnalystConfig(enabled=True, **cfg), client=client)


def test_analyst_veto_and_request_shape():
    c = FakeClient({"decision": "veto", "confidence": 0.9, "reason": "results today", "risks": ["earnings"]})
    v = _analyst(c).review(sig(), BARS)
    assert not v.approve and "results" in v.reason
    kw = c.calls[0]
    assert kw["model"] == "claude-opus-5-5"
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert kw["fallbacks"] == "default"


def test_low_confidence_veto_is_ignored():
    c = FakeClient({"decision": "veto", "confidence": 0.3, "reason": "meh", "risks": []})
    assert _analyst(c).review(sig(), BARS).approve


def test_analyst_error_uses_fallback_policy():
    c = FakeClient(error=RuntimeError("network down"))
    assert _analyst(c).review(sig(), BARS).approve
    assert not _analyst(c, on_error="veto").review(sig(), BARS).approve


def test_refusal_is_treated_as_error():
    c = FakeClient({"decision": "approve", "confidence": 1, "reason": "", "risks": []}, stop_reason="refusal")
    v = _analyst(c, on_error="veto").review(sig(), BARS)
    assert not v.approve and v.source == "fallback"


def test_analyst_caches_per_symbol_side_day():
    c = FakeClient({"decision": "approve", "confidence": 0.8, "reason": "ok", "risks": []})
    a = _analyst(c)
    a.review(sig(), BARS)
    a.review(sig(), BARS)
    assert len(c.calls) == 1


def test_simultaneous_signals_respect_max_open_positions():
    """Regression: 3 stocks signalling on the same candle all got in with max_open_positions=2,
    because entries fill at the next open and were not counted while waiting."""
    from tradebot.models import Signal as Sig
    risk = RiskManager(RiskConfig(capital=20_000, max_open_positions=2, max_position_pct=50))
    broker = PaperBroker(20_000, slippage_bps=0, margin_fn=risk.margin_fraction)
    books = [Book(Instrument(s), build_strategy("orb")) for s in ("A", "B", "C")]
    desk = TradingDesk(books, risk, broker)
    for s in ("A", "B", "C"):
        broker.last_price[s] = 100.0
        desk._try_enter(Sig(Instrument(s), Action.ENTER, Side.BUY, 100.0, T, "orb", stop_loss=99.0), pd.DataFrame())
    assert len(broker.pending_entries()) == 2
    assert any(j["event"] == "RISK_BLOCK" and "Max open positions" in j["why"] for j in desk.journal)
    assert broker.used_margin() > 0                                   # waiting entries reserve margin
