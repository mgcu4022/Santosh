"""Daily operations: Upstox login, run folders, .env loading, multi-day report."""
import json
import os
import threading
import time
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest
import requests

from tradebot import upstox_auth as ua
from tradebot.cli import load_dotenv
from tradebot.paper_live import is_trading_day, run_dir_for_today, run_paper
from tradebot.report import compute, load_runs, render, short_summary, verdict

IST = ZoneInfo("Asia/Kolkata")


# --- Upstox login -------------------------------------------------------------------
def test_login_url_and_token_exchange():
    url = ua.login_url("KEY", "http://127.0.0.1:5055/callback")
    assert "client_id=KEY" in url and "response_type=code" in url and "127.0.0.1%3A5055" in url
    sent = {}

    def fake_post(url, data, headers, timeout):
        sent.update(data)
        return SimpleNamespace(status_code=200, content=b"x", json=lambda: {"access_token": "TOK"})

    assert ua.exchange_code("C0DE", "KEY", "SECRET", "http://r", post=fake_post) == "TOK"
    assert sent["grant_type"] == "authorization_code" and sent["code"] == "C0DE"

    bad = lambda *a, **k: SimpleNamespace(status_code=401, content=b"x", json=lambda: {"errors": ["bad"]})
    with pytest.raises(ua.UpstoxAuthError):
        ua.exchange_code("C", "K", "S", "http://r", post=bad)


def test_local_callback_server_captures_code():
    uri = "http://127.0.0.1:5077/callback"
    result = {}
    th = threading.Thread(target=lambda: result.update(code=ua.wait_for_code(uri, 10)))
    th.start()
    for _ in range(50):          # wait for the server to start listening
        try:
            r = requests.get(uri + "?code=abc123&state=tradebot", timeout=1,
                             proxies={"http": None, "https": None})
            break
        except requests.ConnectionError:
            time.sleep(0.1)
    th.join(5)
    assert result["code"] == "abc123" and "login done" in r.text


def test_callback_times_out_without_login():
    t0 = time.time()
    assert ua.wait_for_code("http://127.0.0.1:5078/callback", 1.5) is None
    assert time.time() - t0 < 5


def test_interactive_login_needs_keys(monkeypatch):
    monkeypatch.delenv("UPSTOX_API_KEY", raising=False)
    with pytest.raises(ua.UpstoxAuthError, match="UPSTOX_API_KEY"):
        ua.interactive_login(0.01, open_browser=False)


# --- daily housekeeping ---------------------------------------------------------------
def test_trading_day_and_run_folders(tmp_path):
    assert is_trading_day(date(2026, 10, 30), set())
    assert not is_trading_day(date(2026, 10, 31), set())                 # Saturday
    assert not is_trading_day(date(2026, 11, 10), {"2026-11-10"})        # holiday
    now = datetime(2026, 10, 30, 9, 0, tzinfo=IST)
    a = run_dir_for_today(str(tmp_path), now)
    a.mkdir()
    b = run_dir_for_today(str(tmp_path), now)
    assert a.name == "paper_20261030" and b.name == "paper_20261030_2"


def test_paper_exits_immediately_on_holiday(monkeypatch, capsys):
    import tradebot.paper_live as pl
    today = datetime.now(IST).date().isoformat()
    cfg = {"holidays": [today], "instruments": [{"symbol": "X", "strategy": {"name": "orb"}}]}
    monkeypatch.setattr(pl, "UpstoxData", lambda **k: pytest.fail("should not fetch data on a holiday"))
    run_paper(cfg)
    assert "Not a trading day" in capsys.readouterr().out


def test_dotenv_reads_values_and_skips_comments(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("# comment\nA_KEY=abc123   # trailing note\nEMPTY=        # nothing here\n"
                 "QUOTED=\"x y\"\nEXISTING=from_file\n")
    monkeypatch.setenv("EXISTING", "from_env")
    for k in ("A_KEY", "EMPTY", "QUOTED"):
        monkeypatch.delenv(k, raising=False)
    load_dotenv(str(f))
    assert os.environ["A_KEY"] == "abc123" and os.environ["QUOTED"] == "x y"
    assert "EMPTY" not in os.environ and os.environ["EXISTING"] == "from_env"


# --- report -------------------------------------------------------------------------------
def write_runs(root, daily_pnls, capital=1_000_000, unclosed=0, restart_day=None):
    """One folder per day with trades.csv/journal.csv like the paper trader writes."""
    start = pd.Timestamp("2026-01-05", tz=IST)
    days = pd.bdate_range(start, periods=len(daily_pnls), tz=IST)
    for d, pnls in zip(days, daily_pnls):
        run = root / f"paper_{d:%Y%m%d}"
        run.mkdir()
        (run / "meta.json").write_text(json.dumps({"capital": capital}))
        rows, jrows = [], []
        for k, p in enumerate(pnls):
            et = d + pd.Timedelta(hours=10 + k % 4)
            rows.append({"symbol": "AAA" if k % 2 else "BBB", "strategy": "orb", "side": "LONG", "qty": 10,
                         "entry_time": et, "entry_price": 100, "exit_time": et + pd.Timedelta(minutes=30),
                         "exit_price": 101, "gross_pnl": p + 50, "charges": 50, "exit_reason": "target" if p > 0 else "stop-loss",
                         "net_pnl": p})
            jrows.append({"time": et, "symbol": "AAA", "event": "ENTRY_SIG"})
        jrows += [{"time": d, "symbol": "AAA", "event": "ENTRY_SIG"}] * (unclosed if d == days[0] else 0)
        jrows.append({"time": d, "symbol": "AAA", "event": "RISK_BLOCK", "why": "Max open positions reached"})
        pd.DataFrame(rows).to_csv(run / "trades.csv", index=False)
        pd.DataFrame(jrows).to_csv(run / "journal.csv", index=False)
    if restart_day is not None:
        (root / f"paper_{days[restart_day]:%Y%m%d}_2").mkdir()


def test_report_too_early(tmp_path):
    write_runs(tmp_path, [[1000, -500]] * 5)
    r = compute(load_runs(str(tmp_path)))
    assert r["trades"] == 10 and r["days_run"] == 5
    assert any("NOT ENOUGH DATA" in v for v in r["verdict"])


def test_report_passes_strong_consistent_edge(tmp_path):
    rng = np.random.default_rng(0)
    days = [list(rng.choice([3000, -1000], size=3, p=[0.5, 0.5])) for _ in range(45)]
    write_runs(tmp_path, days, restart_day=3)
    r = compute(load_runs(str(tmp_path)))
    assert r["trades"] == 135 and r["days_run"] == 45 and r["restarts"] == 1
    assert r["profit_factor"] > 1.2 and r["t_stat"] > 2
    assert any("PASSES" in v for v in r["verdict"])
    assert "Restarts: 1" in render(r) and "PASSES" in short_summary(r)


def test_report_losing_and_pre_charge_hint(tmp_path):
    write_runs(tmp_path, [[-30, -20, 20]] * 45)          # gross positive, net negative
    r = compute(load_runs(str(tmp_path)))
    assert r["net_pnl"] < 0 < r["gross_pnl"]
    assert any("LOSING" in v for v in r["verdict"]) and any("BEFORE charges" in v for v in r["verdict"])


def test_report_luck_warning_and_unclosed_entries(tmp_path):
    # profitable with PF 1.25, but wins and losses are so large that the edge isn't proven
    days = [[5000, -4000, 5000], [-4000, 5000, -4000]] * 22 + [[5000, -4000, 5000]]
    write_runs(tmp_path, days, unclosed=2)
    r = compute(load_runs(str(tmp_path)))
    assert r["net_pnl"] > 0 and r["profit_factor"] >= 1.2 and r["t_stat"] < 2
    assert any("luck" in v for v in r["verdict"])
    assert r["unclosed_entries"] == 2 and any("no recorded exit" in v for v in r["verdict"])
    assert r["blocked"]["RISK_BLOCK"] == 45


def test_report_date_filter(tmp_path):
    write_runs(tmp_path, [[100]] * 10)
    r = load_runs(str(tmp_path), start="2026-01-12")
    assert r.days[0] == "2026-01-12" and len(r.days) == 5


def test_saved_analytics_token_skips_login(monkeypatch):
    monkeypatch.setenv("UPSTOX_ACCESS_TOKEN", "analytics-token")
    monkeypatch.setattr(ua, "interactive_login", lambda *a, **k: pytest.fail("must not open a login"))
    assert ua.get_token(login=True) == "analytics-token"
    monkeypatch.delenv("UPSTOX_ACCESS_TOKEN")
    monkeypatch.setattr(ua, "token_from_keyring", lambda: None)
    assert ua.get_token(login=False) is None
