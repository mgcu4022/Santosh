"""10-day Rs 20,000 trial: broker charges, sub-Rs 200 scan, config writer, data check."""
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from tradebot.config import build_desk, load_config, parse_instruments
from tradebot.costs import ChargeModel
from tradebot.data.kotak import KotakError, match_tokens
from tradebot.data.nse import bhav_meta, parse_bhavcopy
from tradebot.data.synthetic import synthetic_daily
from tradebot.datacheck import run_rounds, summarize
from tradebot.models import Product, Segment, Side
from tradebot.report import broker_comparison
from tradebot.scanner import ScanConfig, run_scan
from tradebot.watchlist import instruments_yaml, write_config

IST = ZoneInfo("Asia/Kolkata")


# --- brokers --------------------------------------------------------------------------------
def test_kotak_is_cheaper_than_upstox_on_small_intraday_trades():
    def rt(broker, price, qty):
        cm = ChargeModel.for_broker(broker)
        return (cm.order_charges(Segment.EQ, Product.INTRADAY, Side.BUY, price, qty)
                + cm.order_charges(Segment.EQ, Product.INTRADAY, Side.SELL, price, qty))
    assert rt("kotak", 100, 150) < rt("upstox", 100, 150)                 # Rs 15k: 0.05% vs 0.1%
    assert rt("kotak", 100, 1000) < rt("upstox", 100, 1000)               # Rs 1L: Rs 10 vs Rs 20 caps
    with pytest.raises(ValueError):
        ChargeModel.for_broker("zerodhaa")


def test_config_broker_and_overrides():
    cm = ChargeModel.for_broker("kotak", {"eq_intraday": {"brokerage_flat": 0.0}})
    assert cm.rates("eq_intraday")["brokerage_flat"] == 0.0 and cm.rates("eq_intraday")["brokerage_pct"] == 0.0005


def test_report_broker_comparison():
    t = pd.DataFrame([{"side": "LONG", "qty": 150, "entry_price": 100.0, "exit_price": 102.0, "gross_pnl": 300.0},
                      {"side": "SHORT", "qty": 100, "entry_price": 150.0, "exit_price": 151.0, "gross_pnl": -100.0}])
    c = broker_comparison(t)
    assert list(c.index) == ["kotak", "upstox"]
    assert c.loc["kotak", "net_pnl"] > c.loc["upstox", "net_pnl"]
    assert c.loc["upstox", "charges"] > c.loc["kotak", "charges"] > 0


# --- trial config -----------------------------------------------------------------------------
def write_trial(tmp_path, instruments_block):
    out = write_config("config/trial_20k_base.yaml", str(tmp_path / "trial.yaml"), instruments_block, header="test")
    return load_config(str(out))


def test_trial_config_sizes_for_20k():
    block = ('instruments:\n  - symbol: NHPC\n    instrument_key: "NSE_EQ|INE848E01016"\n'
             '    backup: {kotak_token: "17400", kotak_segment: nse_cm}\n    strategy: {name: orb}\n')
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        cfg = write_trial(Path(d), block)
    assert cfg["capital"] == 20000 and cfg["broker"] == "kotak" and cfg["runs_dir"] == "runs/trial20k"
    desk, specs = build_desk(cfg)
    assert desk.risk.cfg.risk_per_trade_pct == 1.0 and desk.risk.cfg.max_open_positions == 2
    assert desk.broker.charges.rates("eq_intraday")["brokerage_flat"] == 10.0
    from tradebot.plan import make_plan
    p = make_plan("NHPC", "BUY", 85.0, 84.0, capital=20000, risk_pct=1.0, max_position_pct=50)
    assert p["approved"] and p["qty"] == 200 and p["margin_needed"] <= 10000      # Rs 200 risk / Re 1 stop


# --- scanning under Rs 200 ----------------------------------------------------------------------
def test_scanner_price_band():
    cheap = synthetic_daily(days=120, start_price=120, seed=5)
    dear = synthetic_daily(days=120, start_price=900, seed=6)
    penny = synthetic_daily(days=120, start_price=8, seed=7)
    for df in (cheap, dear, penny):
        df["volume"] *= 50                                         # liquid enough
    res = run_scan({"CHEAP": cheap, "DEAR": dear, "PENNY": penny},
                   ScanConfig(min_price=20, max_price=200, min_turnover_cr=1, top=0, max_atr_pct=50))
    assert list(res.index) == ["CHEAP"]


def test_bhavcopy_series_and_isin():
    hdr = "TradDt,TckrSymb,SctySrs,ISIN,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,TtlTradgVol"
    raw = (hdr + "\n2026-11-03,NHPC,EQ,INE848E01016,NHPC LTD,85,86,84,85.5,1000\n"
                 "2026-11-03,TINYCO,BE,INE000X01011,TINY,10,11,9,10,10\n").encode()
    df = parse_bhavcopy(raw)
    meta = bhav_meta([df])
    assert meta["NHPC"] == {"series": "EQ", "isin": "INE848E01016", "name": "NHPC LTD", "itype": "STK"}
    assert meta["TINYCO"]["series"] == "BE"


def test_kotak_token_matching_variants():
    a = pd.DataFrame({"pSymbol": [17400, 3045, 999], "pTrdSymbol": ["NHPC-EQ", "SBIN-EQ", "SBIN-BL"],
                      "pSymbolName": ["NHPC", "SBIN", "SBIN"], "pGroup": ["EQ", "EQ", "BL"]})
    assert match_tokens(a, ["NHPC", "SBIN", "MISSING"]) == {"NHPC": "17400", "SBIN": "3045"}
    b = pd.DataFrame({"pSymbol": [17400.0], "pSymbolName": ["NHPC"], "pGroup": ["EQ"]})   # no trading-symbol column
    assert match_tokens(b, ["NHPC"]) == {"NHPC": "17400"}
    with pytest.raises(KotakError):
        match_tokens(pd.DataFrame({"x": [1]}), ["NHPC"])


def test_instruments_yaml_round_trip(tmp_path):
    res = pd.DataFrame({"close": [85.5, 150.2], "score": [5.1, 3.2], "setups": ["breakout20", ""],
                        "turnover_cr": [120.0, 60.0]}, index=pd.Index(["NHPC", "IOC"], name="symbol"))
    block = instruments_yaml(res, {"NHPC": "INE848E01016"}, {"NHPC": "17400"})
    assert "FILL: kotak-find IOC" in block
    cfg = write_trial(tmp_path, block)
    specs = parse_instruments(cfg)
    assert specs[0].data_key == "NSE_EQ|INE848E01016" and specs[0].backup["kotak_token"] == "17400"
    assert specs[1].data_key == "" and specs[1].backup["kotak_token"] == ""


# --- data check -----------------------------------------------------------------------------------
def test_datacheck_rounds_and_summary():
    t = {"n": 0.0}

    def clock():
        t["n"] += 0.05
        return t["n"]

    calls = {"k": 0}

    def kotak():
        calls["k"] += 1
        return {"NHPC": 85.50, "IOC": 150.0}

    def upstox():
        raise ConnectionError("401 token expired")

    def yahoo():
        return {"NHPC": 85.70, "IOC": 150.1}

    df = run_rounds({"kotak": kotak, "upstox": upstox, "yahoo": yahoo}, rounds=3, every_s=0,
                    clock=clock, sleep=lambda s: None, now=lambda: datetime(2026, 11, 4, 12, 0, tzinfo=IST))
    s = summarize(df)
    assert s.loc["kotak", "rounds_ok_%"] == 100 and s.loc["upstox", "rounds_ok_%"] == 0
    assert "401" in s.loc["upstox", "last_error"]
    assert s.loc["yahoo", "median_gap_bps"] > 0 and calls["k"] == 3


# --- live snapshot finds the trial folder ------------------------------------------------------------
def test_live_finds_trial_runs_subfolder(tmp_path):
    from tradebot.live import latest_paper_state
    d = tmp_path / "trial20k" / "paper_20261105"
    d.mkdir(parents=True)
    (d / "state.json").write_text(json.dumps({"mtm": 120.0}), encoding="utf-8")
    s, folder = latest_paper_state(str(tmp_path), today="20261105")
    assert s["mtm"] == 120.0 and folder == d


def test_etfs_and_funds_are_not_company_stocks():
    from tradebot.data.nse import is_company_stock
    assert is_company_stock("GAIL", {"name": "GAIL (INDIA) LIMITED", "itype": "STK"})
    assert not is_company_stock("SILVERBEES", {"name": "NIPPON INDIA SILVER ETF", "itype": "STK"})
    assert not is_company_stock("SILVERIETF", {"name": "ICICI PRUDENTIAL SILVER ETF", "itype": "STK"})
    assert not is_company_stock("NIFTYBEES", {"name": "NIPPON INDIA ETF NIFTY 50 BEES", "itype": "STK"})
    assert not is_company_stock("XYZ", {"name": "SOME FUND", "itype": "ETF"})
    assert not is_company_stock("EMBASSY", {"name": "EMBASSY OFFICE PARKS REIT", "itype": "STK"})
