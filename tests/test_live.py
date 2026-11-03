"""Live snapshot (/trader-live): warnings are exact and computed in code."""
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from tradebot.live import fetch_terminal, latest_paper_state, paper_warnings, render, terminal_warnings

IST = ZoneInfo("Asia/Kolkata")


def term_state(**over):
    s = {"mode": "demo", "underlying": "NIFTY", "spot": 23200.0, "prev_close": 23150.0, "expiry": "2026-11-10",
         "days_to_expiry": 7.0, "straddle": 300.0, "mtm": -1000.0, "realized": -500.0, "unrealized": -500.0,
         "margin_used": 100000.0, "equity": 499000.0, "trades_today": 2, "max_trades": 20, "halted": False,
         "halt_reason": "", "rules": {"mtm_stop_loss": 10000, "mtm_target": 0},
         "limits": {"daily_loss_limit": 15000}, "analytics": {"support": 23000.0, "resistance": 23500.0,
                                                              "pcr": 1.0, "max_pain": 23200.0, "atm_iv": 12.0},
         "positions": [], "closed": []}
    s.update(over)
    return s


def pos(**over):
    p = {"strike": 23000.0, "opt": "PE", "qty": -65, "lots": 1, "avg": 50.0, "ltp": 50.0, "pnl": 0.0,
         "pnl_pct": 0.0, "stop": 100.0, "breakeven": 22950.0, "desc": "Sold put"}
    p.update(over)
    return p


def test_calm_state_has_no_warnings():
    assert terminal_warnings(term_state()) == []


def test_daily_loss_and_auto_exit_warnings():
    w = terminal_warnings(term_state(mtm=-12500.0))
    assert any("83% of the Rs15,000 limit" in x for x in w)
    assert any("auto-exit" in x for x in w)
    assert any("STOPPED" in x for x in terminal_warnings(term_state(halted=True, halt_reason="MTM stop-loss hit")))


def test_position_warnings():
    near_stop = pos(ltp=92.0)                                     # stop 100: Rs8 away = 9% of the price
    tested = pos(strike=23200.0, ltp=60.0)                        # sold put, index at strike
    long_expiry = pos(qty=65, opt="CE", strike=23300.0, stop=10.0, ltp=20.0)
    w = terminal_warnings(term_state(positions=[near_stop, tested, long_expiry], days_to_expiry=0.2))
    assert any("within 9% of its stop-loss" in x for x in w)
    assert any("at/through the strike" in x for x in w)
    assert any("time decay" in x for x in w)


def test_support_resistance_and_margin():
    assert any("below resistance" in x for x in terminal_warnings(term_state(spot=23450.0)))
    assert any("above support" in x for x in terminal_warnings(term_state(spot=23050.0)))
    assert any("blocked as margin" in x for x in terminal_warnings(term_state(margin_used=420000.0)))


def test_paper_state_stale_and_stop_distance():
    now = datetime(2026, 11, 3, 12, 0, tzinfo=IST)
    s = {"updated": (now - timedelta(minutes=10)).isoformat(), "mtm": -8000, "daily_loss_limit": 15000,
         "halted": False, "trades_today": 3, "max_trades": 20,
         "positions": [{"symbol": "SBIN", "side": "LONG", "avg": 800.0, "ltp": 791.0, "stop": 788.0}]}
    w = paper_warnings(s, now=now)
    assert any("10 min old" in x for x in w)
    assert any("53% of its Rs15,000" in x for x in w)
    assert any("25% of the distance" in x for x in w)


def test_latest_paper_state_and_render(tmp_path):
    d = tmp_path / "paper_20261103"
    d.mkdir()
    state = {"updated": datetime.now(IST).isoformat(), "mtm": 500.0, "realized": 500.0, "equity": 500500.0,
             "daily_loss_limit": 15000, "halted": False, "trades_today": 1, "max_trades": 8,
             "positions": [{"symbol": "INFY", "side": "LONG", "qty": 100, "avg": 1500.0, "ltp": 1510.0,
                            "pnl": 1000.0, "stop": 1490.0, "target": 1520.0, "strategy": "orb"}],
             "closed": [], "recent_events": [{"event": "RISK_BLOCK", "symbol": "SBIN", "why": "Max open positions"}]}
    (d / "state.json").write_text(json.dumps(state), encoding="utf-8")
    s, folder = latest_paper_state(str(tmp_path), today="20261103")
    assert s["mtm"] == 500.0 and folder == d
    out = render(term_state(positions=[pos()]), s, folder)
    assert "OPTION TERMINAL" in out and "STRATEGY BOT" in out and "INFY" in out and "Max open positions" in out
    assert "Nothing is running" in render(None, None, None)


def test_paper_bot_writes_state_snapshot(tmp_path):
    from tradebot.config import build_desk, load_history
    from tradebot.paper_live import _save
    cfg = {"capital": 500000, "instruments": [{"symbol": "X", "strategy": {"name": "orb"},
                                               "source": {"type": "synthetic", "days": 3, "seed": 5}}]}
    desk, specs = build_desk(cfg)
    df = desk.books["X"].strategy.prepare(load_history(cfg, specs)["X"])
    for i in range(len(df) - 20):
        desk.on_bar("X", df, i)
    _save(desk, tmp_path)
    s = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert {"mtm", "positions", "daily_loss_limit", "closed", "updated"} <= set(s)


def test_fetch_terminal_returns_none_when_not_running():
    assert fetch_terminal(port=1, timeout=0.5) is None
