"""Scanner, Yahoo parser, deals, alerts, trade plan, daily backtests."""
import numpy as np
import pandas as pd
import pytest

from tradebot.alerts import ConsoleNotifier, TelegramNotifier, format_event
from tradebot.backtest import run_backtest
from tradebot.config import build_desk, load_history
from tradebot.data.synthetic import synthetic_daily
from tradebot.data.yahoo import parse_chart, to_yahoo_ticker
from tradebot.deals import parse_deals, summarize
from tradebot.plan import make_plan
from tradebot.scanner import ScanConfig, run_scan, scan_one, watchlist_yaml


# --- Yahoo -------------------------------------------------------------------
def test_yahoo_ticker_mapping():
    assert to_yahoo_ticker("RELIANCE") == "RELIANCE.NS"
    assert to_yahoo_ticker("RELIANCE", "BSE") == "RELIANCE.BO"
    assert to_yahoo_ticker("^NSEI") == "^NSEI"


def test_parse_yahoo_chart_drops_missing_rows_and_uses_ist():
    payload = {"chart": {"error": None, "result": [{
        "timestamp": [1767585600, 1767672000, 1767758400],
        "indicators": {"quote": [{"open": [100, None, 102], "high": [101, None, 103],
                                  "low": [99, None, 101], "close": [100.5, None, 102.5],
                                  "volume": [1000, None, 1200]}]}}]}}
    df = parse_chart(payload)
    assert len(df) == 2 and str(df.index.tz) == "Asia/Kolkata"


# --- scanner -------------------------------------------------------------------
def _with_breakout(df):
    d = df.copy()
    prior_high = d["high"].iloc[-21:-1].max()
    d.iloc[-1, d.columns.get_loc("close")] = prior_high * 1.02
    d.iloc[-1, d.columns.get_loc("high")] = prior_high * 1.03
    d.iloc[-1, d.columns.get_loc("volume")] = d["volume"].iloc[-21:-1].mean() * 4
    return d


def test_scanner_tags_breakout_and_volume_spike():
    df = _with_breakout(synthetic_daily(days=120, start_price=1500, seed=5))
    m = scan_one(df, ScanConfig())
    assert "breakout20" in m["setups"] and "vol_spike" in m["setups"]


def test_scanner_filters_illiquid_and_ranks():
    liquid = _with_breakout(synthetic_daily(days=120, start_price=1500, seed=5))
    quiet = synthetic_daily(days=120, start_price=1500, seed=6)
    thin = synthetic_daily(days=120, start_price=1500, seed=7)
    thin["volume"] = 100.0                       # ~Rs 0.15 lakh/day: illiquid
    res = run_scan({"A": liquid, "B": quiet, "C": thin}, ScanConfig(top=0))
    assert "C" not in res.index
    assert res.index[0] == "A"
    assert "symbol: A" in watchlist_yaml(res)


def test_scanner_uses_only_past_for_averages():
    df = synthetic_daily(days=120, seed=8)
    m1 = scan_one(df, ScanConfig())
    df2 = df.copy()
    df2.iloc[-1, df2.columns.get_loc("volume")] *= 10   # only today's volume changes
    m2 = scan_one(df2, ScanConfig())
    assert m2["rel_vol"] == pytest.approx(m1["rel_vol"] * 10, rel=1e-3)


# --- deals -----------------------------------------------------------------------
BULK_CSV = """Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,Trade Price / Wght. Avg. Price,Remarks
30-Oct-2026,ABC,ABC Ltd,FUND ONE,BUY,"1,000,000",250.50,-
30-Oct-2026,ABC,ABC Ltd,TRADER TWO,SELL,400000,251.00,-
30-Oct-2026,XYZ,XYZ Ltd,PROMOTER CO,SELL,2000000,100.00,-
"""


def test_parse_and_summarize_bulk_deals():
    d = parse_deals(BULK_CSV, "bulk")
    assert len(d) == 3 and set(d["side"]) == {"BUY", "SELL"}
    assert d.loc[0, "value_cr"] == pytest.approx(25.05)
    s = summarize(d)
    assert s.loc["ABC", "net_cr"] == pytest.approx(25.05 - 10.04, abs=0.01)
    assert s.loc["XYZ", "net_cr"] == pytest.approx(-20.0)
    assert list(summarize(d, "xyz").index) == ["XYZ"]


# --- alerts ----------------------------------------------------------------------------
def test_telegram_sends_in_background_and_never_raises():
    sent = []

    def fake_post(url, data, timeout):
        sent.append(data["text"])
        raise ConnectionError("offline")         # failure must be swallowed

    n = TelegramNotifier(token="t", chat_id="1", _post=fake_post)
    n.send(format_event("ENTRY_SIG", "SBIN", {"side": "BUY", "qty": 10, "why": "ORB"}))
    n.close()
    assert sent and "SBIN" in sent[0] and "[PAPER]" in sent[0]


def test_desk_sends_alerts_for_selected_events(capsys):
    cfg = {"capital": 1_000_000, "instruments": [
        {"symbol": "X", "strategy": {"name": "orb"}, "source": {"type": "synthetic", "days": 10, "seed": 3}}]}
    desk, specs = build_desk(cfg, notifier=ConsoleNotifier(events=("ENTRY_SIG",)))
    run_backtest(desk, load_history(cfg, specs))
    out = capsys.readouterr().out
    assert "[ALERT]" in out and "ENTRY_SIG" in out and "CLOSED" not in out


# --- plan ------------------------------------------------------------------------------
def test_plan_sizes_and_includes_charges():
    p = make_plan("RELIANCE", "BUY", 1400, 1390, capital=500_000, risk_pct=1.0)
    assert p["approved"] and p["qty"] == 446 and p["target"] == 1420
    assert p["risk_at_stop"] > 446 * 10                 # charges add to the loss
    assert p["profit_at_target"] < 446 * 20             # and cut the profit
    assert 33.3 < p["win_rate_needed_pct"] < 40


def test_plan_rejects_bad_stop():
    assert not make_plan("X", "SELL", 100, 99)["approved"]


# --- daily timeframe -----------------------------------------------------------------
def test_daily_backtest_runs_without_intraday_rules():
    cfg = {"capital": 1_000_000, "timeframe": {"unit": "days", "interval": 1},
           "instruments": [{"symbol": "D", "product": "DELIVERY",
                            "strategy": {"name": "ema_crossover", "fast": 10, "slow": 30},
                            "source": {"type": "synthetic", "days": 400, "seed": 4}}]}
    desk, specs = build_desk(cfg)
    res = run_backtest(desk, load_history(cfg, specs))
    assert res.summary["trades"] > 0
    assert not (res.journal["why"].astype(str).str.contains("Outside entry window")).any()
    assert set(res.trades["side"]) == {"LONG"}        # no shorting in delivery


def test_daily_with_intraday_product_is_rejected():
    cfg = {"timeframe": {"unit": "days"}, "instruments": [{"symbol": "D", "strategy": {"name": "orb"}}]}
    with pytest.raises(ValueError, match="DELIVERY"):
        build_desk(cfg)
