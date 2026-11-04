"""Read-only bot dashboard: run discovery, safe file access, plain-language watchlist."""
import json
import threading
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from tradebot.agents.desk import Book, TradingDesk
from tradebot.brokers.paper import PaperBroker
from tradebot.dashboard import list_runs, make_handler, read_state
from tradebot.data.synthetic import synthetic_intraday
from tradebot.models import Instrument
from tradebot.paper_live import process_symbol, snapshot, watchlist
from tradebot.risk import RiskConfig, RiskManager
from tradebot.strategies import build_strategy

IST = ZoneInfo("Asia/Kolkata")


def make_run(root, rel, mtm=0.0):
    d = root / rel
    d.mkdir(parents=True)
    (d / "state.json").write_text(json.dumps({"updated": datetime.now(IST).isoformat(), "mtm": mtm,
                                               "capital": 20000}), encoding="utf-8")
    return d


def test_lists_todays_runs_including_trial_folder(tmp_path):
    make_run(tmp_path, "paper_20261105", 100)
    make_run(tmp_path, "trial20k/paper_20261105", -50)
    make_run(tmp_path, "paper_20261104", 0)                    # another day: not listed
    runs = list_runs(str(tmp_path), day="20261105")
    assert {r["id"] for r in runs} == {"paper_20261105", "trial20k/paper_20261105"}
    assert any("trial20k / today" == r["label"] for r in runs)


def test_read_state_blocks_paths_outside_runs(tmp_path):
    make_run(tmp_path, "paper_20261105")
    assert read_state(str(tmp_path), "paper_20261105")["age_seconds"] is not None
    assert read_state(str(tmp_path), "../../etc") is None
    assert read_state(str(tmp_path), "nope") is None


def test_watchlist_statuses_for_orb():
    risk = RiskManager(RiskConfig(capital=20000))
    desk = TradingDesk([Book(Instrument("X"), build_strategy("orb"))], risk, PaperBroker(20000))
    df = synthetic_intraday(days=3, seed=4, start_price=150)
    day = sorted(set(df.index.date))[-1]
    today = df[(df.index.date == day)]
    history = {"X": df[df.index.date < day]}
    last_done, first = {}, set()
    process_symbol(desk, "X", today[today.index.time < pd.Timestamp("09:25").time()], history, last_done, first)
    assert "Waiting for opening range" in watchlist(desk)[0]["status"]
    process_symbol(desk, "X", today, history, last_done, first)
    row = watchlist(desk)[0]
    assert row["range_high"] is not None and row["status"]
    snap = snapshot(desk)
    assert snap["watch"][0]["symbol"] == "X" and "events" in snap


def test_dashboard_http_is_read_only(tmp_path):
    from http.server import ThreadingHTTPServer
    make_run(tmp_path, "trial20k/paper_" + datetime.now(IST).strftime("%Y%m%d"), 42.0)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(str(tmp_path)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        assert "Strategy bot" in op.open(base + "/").read().decode()
        runs = json.load(op.open(base + "/api/runs"))
        st = json.load(op.open(base + "/api/state?run=" + urllib.request.quote(runs[0]["id"])))
        assert st["mtm"] == 42.0
        req = urllib.request.Request(base + "/api/state", data=b"{}", method="POST")
        try:
            op.open(req)
            raise AssertionError("POST must not be accepted")
        except urllib.error.HTTPError as e:
            assert e.code in (405, 501)
    finally:
        srv.shutdown()
