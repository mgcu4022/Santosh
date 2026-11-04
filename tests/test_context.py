"""NSE data, RSS news, market-context guard, Kotak backup and failover (all offline)."""
import io
import zipfile
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from tradebot.agents.analyst import AIAnalyst, AnalystConfig
from tradebot.backtest import run_backtest
from tradebot.config import build_desk, load_history, parse_instruments
from tradebot.context import ContextConfig, MarketContext
from tradebot.data import nse
from tradebot.data.barbuilder import BarBuilder
from tradebot.data.kotak import KotakError, parse_quotes
from tradebot.models import Action, Instrument, Segment, Side, Signal
from tradebot.news import NewsFeed, match, parse_feed
from tradebot.paper_live import LiveFeed

IST = ZoneInfo("Asia/Kolkata")
T = datetime(2026, 10, 30, 10, 0, tzinfo=IST)

# --- NSE ---------------------------------------------------------------------------
BHAV_HEADER = ("TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,"
               "StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,"
               "SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,"
               "Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4")


def bhav_bytes(day: str, rows, zipped=True) -> bytes:
    lines = [BHAV_HEADER]
    for sym, series, o, h, l, c, v in rows:
        lines.append(f"{day},{day},CM,NSE,STK,1,INE000000000,{sym},{series},,,,,{sym} LTD,{o},{h},{l},{c},{c},{o},,"
                     f"{c},,,{v},{v * c},100,F1,1,,,,,")
    raw = ("\n".join(lines) + "\n").encode()
    if not zipped:
        return raw
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"BhavCopy_NSE_CM_0_0_0_{day.replace('-', '')}_F_0000.csv", raw)
    return buf.getvalue()


def test_parse_bhavcopy_keeps_equity_series_only():
    df = nse.parse_bhavcopy(bhav_bytes("2026-10-30", [
        ("RELIANCE", "EQ", 1400, 1420, 1390, 1415, 5_000_000),
        ("SOMEBOND", "GS", 100, 100, 100, 100, 10),
        ("SMALLCO", "BE", 50, 52, 49, 51, 20_000)]))
    assert list(df["symbol"]) == ["RELIANCE", "SMALLCO"]
    assert df.loc[0, "close"] == 1415 and df.loc[0, "date"] == date(2026, 10, 30)


def test_bhav_to_daily_builds_per_symbol_history():
    f1 = nse.parse_bhavcopy(bhav_bytes("2026-10-29", [("SBIN", "EQ", 800, 810, 795, 805, 1000)], zipped=False))
    f2 = nse.parse_bhavcopy(bhav_bytes("2026-10-30", [("SBIN", "EQ", 806, 820, 800, 818, 2000)]))
    d = nse.bhav_to_daily([f2, f1])
    assert list(d["SBIN"]["close"]) == [805, 818] and str(d["SBIN"].index.tz) == "Asia/Kolkata"


def test_daily_history_skips_holidays_and_caches(tmp_path, monkeypatch):
    calls = []

    class R:
        def __init__(self, content, code=200):
            self.content, self.status_code = content, code

    def fake_get(url, timeout):
        calls.append(url)
        if "20261030" in url:
            return R(bhav_bytes("2026-10-30", [("TCS", "EQ", 3000, 3050, 2990, 3040, 9)]))
        return R(b"", 404)

    c = nse.NseClient(cache_dir=str(tmp_path), pause=0)
    monkeypatch.setattr(c.s, "get", fake_get)
    d = c.daily_history(days=5, end=date(2026, 11, 1))          # Sun..Wed; only Fri has data
    assert list(d) == ["TCS"]
    assert (tmp_path / "bhav" / "20261030.csv").exists()
    again = c.daily_history(days=5, end=date(2026, 11, 1))
    assert sum("20261030" in u for u in calls) == 1            # cached day is not re-downloaded
    assert again["TCS"]["close"].iloc[-1] == 3040


def test_parse_ban_list():
    text = "Securities in Ban For Trade Date 31-OCT-2026:\n1,RBLBANK\n2,M&M\n3,BAJAJ-AUTO\n"
    assert nse.parse_ban_list(text) == {"RBLBANK", "M&M", "BAJAJ-AUTO"}
    assert nse.parse_ban_list("Securities in Ban For Trade Date 31-OCT-2026: NIL\n") == set()


def test_parse_events_and_actions():
    ev = nse.parse_events([{"symbol": "INFY", "purpose": "Financial Results", "bm_desc": "Q2 results",
                            "date": "30-Oct-2026"}, {"purpose": "x", "date": "bad"}])
    assert ev == [{"date": date(2026, 10, 30), "purpose": "Financial Results", "detail": "Q2 results"}]
    ca = nse.parse_corporate_actions([{"subject": "Dividend - Rs 10 Per Share", "exDate": "05-Nov-2026"}])
    assert ca[0]["date"] == date(2026, 11, 5) and "Dividend" in ca[0]["purpose"]
    an = nse.parse_announcements([{"desc": "Outcome of Board Meeting", "an_dt": "30-Oct-2026 18:01:00"}])
    assert an[0]["title"] == "Outcome of Board Meeting"


# --- news --------------------------------------------------------------------------
RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Reliance Industries Q2 profit rises 9%</title><link>https://x/1</link>
<pubDate>Fri, 30 Oct 2026 09:00:00 +0530</pubDate><description>&lt;p&gt;RIL beats estimates&lt;/p&gt;</description></item>
<item><title>Switch to value funds, says analyst</title><link>https://x/2</link>
<pubDate>Fri, 30 Oct 2026 08:00:00 +0530</pubDate></item>
<item><title>SBI cuts lending rates</title><link>https://x/3</link>
<pubDate>Mon, 19 Oct 2026 08:00:00 +0530</pubDate></item>
</channel></rss>"""
ATOM = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Infosys wins large deal</title>
<link href="https://y/1"/><updated>2026-10-30T04:00:00Z</updated><summary>IT major</summary></entry></feed>"""


def test_parse_rss_and_atom():
    items = parse_feed(RSS, "ET")
    assert len(items) == 3 and items[0]["summary"] == "RIL beats estimates"
    assert items[0]["published"].astimezone(timezone.utc).hour == 3
    atom = parse_feed(ATOM, "BS")
    assert atom[0]["title"] == "Infosys wins large deal" and atom[0]["link"] == "https://y/1"


def test_symbol_matching_avoids_false_hits():
    h = {"title": "Switch to value funds, says analyst", "summary": ""}
    assert not match(h, "ITC", ["ITC"])
    assert match({"title": "ITC shares jump", "summary": ""}, "ITC", [])
    assert match({"title": "Reliance Industries Q2 profit rises", "summary": ""}, "RELIANCE", ["Reliance Industries"])


def test_news_for_symbol_filters_by_time(monkeypatch):
    nf = NewsFeed(feeds={"ET": "u"})
    monkeypatch.setattr(nf, "all", lambda: parse_feed(RSS, "ET"))
    now = datetime(2026, 10, 30, 12, 0, tzinfo=IST)
    assert [h["title"][:8] for h in nf.for_symbol("RELIANCE", now=now)] == ["Reliance"]
    assert nf.for_symbol("SBIN", hours=48, now=now) == []           # 11 days old
    assert len(nf.for_symbol("SBIN", hours=24 * 30, now=now)) == 1


def test_bad_feed_is_skipped(monkeypatch):
    import tradebot.news as news_mod

    def fake_get(url, timeout, headers):
        if "bad" in url:
            raise ConnectionError("down")
        return SimpleNamespace(text=RSS, raise_for_status=lambda: None)

    monkeypatch.setattr(news_mod.requests, "get", fake_get)
    assert len(NewsFeed(feeds={"bad": "bad", "ok": "ok"}).all()) == 3


# --- market context ------------------------------------------------------------------
def sig(symbol="RELIANCE", seg=Segment.EQ, ts=T):
    return Signal(Instrument(symbol, segment=seg), Action.ENTER, Side.BUY, 100, ts, "t", stop_loss=99)


def ctx(**cfg):
    c = MarketContext(ContextConfig(enabled=True, **cfg), today=T.date())
    c.banned = {"RBLBANK"}
    c.events = {"INFY": [{"date": T.date(), "purpose": "Financial Results", "detail": ""}],
                "ITC": [{"date": T.date() + timedelta(days=2), "purpose": "Dividend", "detail": "ex-date"}]}
    return c


def test_ban_list_blocks_fno_by_default_but_not_cash():
    c = ctx()
    assert not c.guard(sig("RBLBANK-FUT", Segment.FUT))[0]
    assert c.guard(sig("RBLBANK"))[0]
    assert not ctx(ban_blocks="all").guard(sig("RBLBANK"))[0]


def test_results_day_blocks_entries_but_other_events_dont():
    c = ctx()
    ok, why = c.guard(sig("INFY"))
    assert not ok and "Financial Results" in why
    assert c.guard(sig("INFY", ts=T + timedelta(days=1)))[0]
    assert c.guard(sig("ITC"))[0]
    assert ctx(block_on_results_day=False).guard(sig("INFY"))[0]


def test_brief_includes_events_ban_and_headlines():
    c = ctx()
    c.news = SimpleNamespace(for_symbol=lambda s, hours: [{"source": "ET", "title": f"{s} news"}])
    b = c.brief("ITC")
    assert "Dividend" in b and "ITC news" in b and "untrusted" in b
    assert "BANNED" in c.brief("RBLBANK-FUT")


def test_context_load_failures_are_not_fatal():
    class Broken:
        def ban_list(self):
            raise ConnectionError("nse down")

        def events(self, s):
            raise ConnectionError("nse down")

    c = MarketContext(ContextConfig(enabled=True), nse=Broken())
    c.refresh(["RELIANCE"])
    assert c.banned == set() and c.guard(sig())[0]


def test_desk_blocks_on_context_and_passes_brief_to_analyst():
    cfg = {"capital": 1_000_000, "instruments": [
        {"symbol": "INFY", "strategy": {"name": "orb"}, "source": {"type": "synthetic", "days": 15, "seed": 3}}]}
    desk, specs = build_desk(cfg)
    data = load_history(cfg, specs)
    blocked_days = set(data["INFY"].index.date[:5])
    c = MarketContext(ContextConfig(enabled=True), today=T.date())
    c.events = {"INFY": [{"date": d, "purpose": "Financial Results", "detail": ""} for d in blocked_days]}
    seen = []
    c.news = SimpleNamespace(for_symbol=lambda s, hours: [{"source": "ET", "title": "Infosys headline"}])

    class Client:
        beta = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: seen.append(kw) or SimpleNamespace(
            stop_reason="end_turn", content=[SimpleNamespace(
                type="text", text='{"decision":"approve","confidence":0.9,"reason":"ok","risks":[]}')])))

    desk.context = c
    desk.analyst = AIAnalyst(AnalystConfig(enabled=True), client=Client())
    res = run_backtest(desk, data)
    j = res.journal
    assert (j["event"] == "EVENT_BLOCK").any()
    entries = pd.to_datetime(j.loc[j["event"] == "ENTRY_SIG", "time"])
    assert not any(t.date() in blocked_days for t in entries)
    assert seen and "Infosys headline" in seen[0]["messages"][0]["content"]


# --- bar builder & Kotak -------------------------------------------------------------
def test_bar_builder_makes_ohlc_and_closes_on_time():
    b = BarBuilder(5)
    t0 = datetime(2026, 10, 30, 9, 14, 50, tzinfo=IST)
    b.update(t0, 99)                                         # pre-open: ignored
    for sec, px in ((0, 100), (60, 103), (120, 98), (240, 101)):
        b.update(t0 + timedelta(seconds=10 + sec), px)
    b.update(datetime(2026, 10, 30, 9, 20, 5, tzinfo=IST), 102)   # next candle
    b.close_due(datetime(2026, 10, 30, 9, 25, 1, tzinfo=IST))
    f = b.frame()
    assert list(f.index.strftime("%H:%M")) == ["09:15", "09:20"]
    assert f.iloc[0][["open", "high", "low", "close"]].tolist() == [100, 103, 98, 101]


def test_parse_kotak_quotes_variants():
    assert parse_quotes({"message": [{"exchange_token": "2885", "ltp": "1401.50"}]}) == {"2885": 1401.5}
    assert parse_quotes([{"instrument_token": 11536, "last_traded_price": 3050}]) == {"11536": 3050.0}
    with pytest.raises(KotakError):
        parse_quotes({"error": "session expired"})


# --- failover ---------------------------------------------------------------------------
def test_live_feed_fails_over_to_kotak_and_back():
    specs = parse_instruments({"instruments": [
        {"symbol": "RELIANCE", "instrument_key": "k", "strategy": {"name": "orb"},
         "backup": {"kotak_token": "2885", "kotak_segment": "nse_cm"}}]})
    day = pd.date_range("2026-10-30 09:15", periods=6, freq="5min", tz=IST)
    candles = pd.DataFrame({"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1.0}, index=day)
    state = {"up": True, "price": 1400.0}
    msgs = []

    class Up:
        def intraday(self, key, unit, interval):
            if not state["up"]:
                raise ConnectionError("401 token expired")
            return candles

    class Kotak:
        def ltp(self, toks):
            state["price"] += 1
            return {"2885": state["price"]}

    feed = LiveFeed(specs, 5, Up(), Kotak(), notify=msgs.append)
    now = datetime(2026, 10, 30, 9, 45, 30, tzinfo=IST)
    assert len(feed.poll(now)["RELIANCE"]) == 6                # primary OK
    state["up"] = False
    for k in range(3):
        out = feed.poll(now + timedelta(seconds=20 * (k + 1)))
    assert "RELIANCE" in feed.on_backup and "Kotak" in msgs[0]
    for k in range(30):                                        # 10 more minutes of quotes
        out = feed.poll(now + timedelta(minutes=1, seconds=20 * k))
    df = out["RELIANCE"]
    assert len(df) > 6 and df.index.is_monotonic_increasing    # real candles + built ones
    assert df["close"].iloc[-1] > 1400
    state["up"] = True
    while "RELIANCE" in feed.on_backup:                        # retried every RETRY_PRIMARY_EVERY polls
        feed.poll(now + timedelta(minutes=20))
    assert "back" in msgs[-1]


def test_no_backup_configured_means_no_switch():
    specs = parse_instruments({"instruments": [{"symbol": "X", "strategy": {"name": "orb"}}]})

    class Up:
        def intraday(self, *a):
            raise ConnectionError("down")

    feed = LiveFeed(specs, 5, Up(), kotak=None)
    for _ in range(5):
        assert feed.poll(T) == {}
    assert feed.on_backup == set()


def test_live_feed_kotak_as_main_source_never_calls_upstox():
    specs = parse_instruments({"instruments": [
        {"symbol": "RELIANCE", "strategy": {"name": "orb"}, "backup": {"kotak_token": "2885"}},
        {"symbol": "NOTOKEN", "strategy": {"name": "orb"}}]})
    px = {"v": 1400.0}

    class Kotak:
        def ltp(self, toks):
            px["v"] += 0.5
            return {"2885": px["v"]}

    feed = LiveFeed(specs, 5, upstox=None, kotak=Kotak())
    t0 = datetime(2026, 11, 4, 9, 15, 2, tzinfo=IST)
    out = {}
    for k in range(80):                                   # ~13 minutes of 10-second polls
        out = feed.poll(t0 + timedelta(seconds=10 * k))
    assert "NOTOKEN" not in out and len(out["RELIANCE"]) == 2   # 09:15 and 09:20 candles complete
    c = out["RELIANCE"].iloc[0]
    assert c["open"] < c["close"] and c["high"] >= c["close"]
