from datetime import date, datetime
from zoneinfo import ZoneInfo

from tradebot.config import load_config
from tradebot.preflight import FAIL, OK, WARN, next_trading_day, run_checks

IST = ZoneInfo("Asia/Kolkata")
MON = datetime(2026, 11, 2, 8, 30, tzinfo=IST)


def statuses(res):
    return {check: status for status, check, _ in res}


def test_upstox_mode_needs_upstox_keys(monkeypatch):
    for k in ("UPSTOX_API_KEY", "UPSTOX_API_SECRET", "UPSTOX_ACCESS_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    cfg = {**load_config("config/my.yaml"), "live_data": "upstox"}
    st = statuses(run_checks(cfg, online=False, now=MON))
    assert st["Config loads"] == OK and st["Instrument keys"] == OK and st["Trading day"] == OK
    assert st["Upstox credentials"] == FAIL
    monkeypatch.setenv("UPSTOX_ACCESS_TOKEN", "t")
    assert statuses(run_checks(cfg, online=False, now=MON))["Upstox credentials"] == OK


def test_kotak_mode_needs_kotak_not_upstox(monkeypatch):
    import sys, types
    for k in ("UPSTOX_API_KEY", "UPSTOX_API_SECRET", "UPSTOX_ACCESS_TOKEN", "KOTAK_CONSUMER_KEY"):
        monkeypatch.delenv(k, raising=False)
    cfg = load_config("config/my.yaml")
    assert cfg["live_data"] == "kotak"
    st = statuses(run_checks(cfg, online=False, now=MON))
    assert "Upstox credentials" not in st and st["Kotak Neo (main data)"] == FAIL
    monkeypatch.setitem(sys.modules, "neo_api_client", types.ModuleType("neo_api_client"))
    monkeypatch.setenv("KOTAK_CONSUMER_KEY", "x")                 # quotes need only the consumer key
    st = statuses(run_checks(cfg, online=False, now=MON))
    assert st["Kotak Neo (main data)"] == OK


def test_holidays_and_weekends():
    hol = {"2026-11-09"}
    assert next_trading_day(date(2026, 11, 7), hol) == date(2026, 11, 10)   # Sat -> skip Sun, Mon holiday
    cfg = load_config("config/my.yaml")
    st = statuses(run_checks(cfg, online=False, now=datetime(2026, 11, 9, 8, 0, tzinfo=IST)))
    assert st["Trading day"] == WARN


def test_missing_keys_and_bad_config_fail():
    cfg = {"instruments": [{"symbol": "X", "strategy": {"name": "orb"}}]}
    assert statuses(run_checks(cfg, online=False, now=MON))["Instrument keys"] == FAIL
    bad = {"instruments": [{"symbol": "X", "strategy": {"name": "nope"}}]}
    assert statuses(run_checks(bad, online=False, now=MON))["Config loads"] == FAIL
