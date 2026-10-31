"""Google Finance (via Google Sheets GOOGLEFINANCE) data source."""
from datetime import date

import pytest

import tradebot.data.google as g
from tradebot.backtest import run_backtest
from tradebot.config import build_desk, load_history
from tradebot.data.synthetic import synthetic_daily

SINGLE_IN = """Date,Open,High,Low,Close,Volume
29/10/2026 15:30:00,1400,1410,1395,1405,"1,200,000"
30/10/2026 15:30:00,1405,1420,1401,1418,1500000
"""


def test_single_layout_indian_dates():
    df = g.parse_sheet_csv(SINGLE_IN)["_single"]
    assert len(df) == 2 and df.index[-1].date() == date(2026, 10, 30)
    assert df["volume"].iloc[0] == 1_200_000 and str(df.index.tz) == "Asia/Kolkata"


def test_us_dates_detected():
    text = "Date,Open,High,Low,Close,Volume\n10/29/2026 15:30:00,1,2,0.5,1.5,10\n10/30/2026 15:30:00,1,2,0.5,1.5,10\n"
    df = g.parse_sheet_csv(text)["_single"]
    assert df.index[0].date() == date(2026, 10, 29)


def _multi_csv(blocks: dict) -> str:
    """Build what Google exports for the sheet_layout() layout."""
    syms = list(blocks)
    n = max(len(v) for v in blocks.values())
    row1, row2 = [], []
    for s in syms:
        row1 += [s] + [""] * 6
        row2 += ["Date", "Open", "High", "Low", "Close", "Volume", ""]
    rows = [row1, row2]
    for i in range(n):
        r = []
        for s in syms:
            rec = blocks[s][i] if i < len(blocks[s]) else [""] * 6
            r += list(map(str, rec)) + [""]
        rows.append(r)
    return "\n".join(",".join(r) for r in rows) + "\n"


def test_multi_layout_and_na_symbol():
    text = _multi_csv({
        "RELIANCE": [["01/10/2026 15:30:00", 1, 2, 0.5, 1.5, 100], ["02/10/2026 15:30:00", 1, 2, 0.5, 1.6, 120]],
        "BADSYM": [["#N/A", "", "", "", "", ""]],
        "TCS": [["01/10/2026 15:30:00", 3, 4, 2.5, 3.5, 50]],
    })
    data = g.parse_sheet_csv(text, dayfirst=True)
    assert set(data) == {"RELIANCE", "TCS"}
    assert len(data["RELIANCE"]) == 2 and data["TCS"]["close"].iloc[0] == 3.5


def test_tickers_layout_and_urls():
    assert g.google_ticker("RELIANCE") == "NSE:RELIANCE"
    assert g.google_ticker("RELIANCE", "BSE") == "BOM:RELIANCE"
    assert g.google_ticker("NIFTY") == "INDEXNSE:NIFTY_50"
    lay = g.sheet_layout(["SBIN", "INFY"], date(2024, 1, 1))
    r1, r2 = lay.splitlines()
    assert r1.split("\t")[0] == "SBIN" and r1.split("\t")[7] == "INFY"
    assert '=GOOGLEFINANCE("NSE:INFY","all",DATE(2024,1,1),TODAY(),"DAILY")' == r2.split("\t")[7]
    assert g.to_csv_url("https://docs.google.com/spreadsheets/d/ABC123/edit#gid=42") == \
        "https://docs.google.com/spreadsheets/d/ABC123/export?format=csv&gid=42"
    assert g.to_csv_url("https://docs.google.com/spreadsheets/d/e/XYZ/pubhtml").endswith("output=csv")


class _Resp:
    def __init__(self, text, status=200):
        self.text, self.status_code = text, status


def test_backtest_from_google_sheet_downloads_once(monkeypatch):
    calls = []
    rows = {}
    for sym, seed in (("AAA", 1), ("BBB", 2)):
        df = synthetic_daily(days=300, start_price=1000, seed=seed)
        rows[sym] = [[d.strftime("%d/%m/%Y 15:30:00"), *r] for d, r in zip(df.index, df.values.tolist())]
    text = _multi_csv(rows)
    monkeypatch.setattr(g.requests, "get", lambda url, timeout: calls.append(url) or _Resp(text))
    cfg = {"capital": 1_000_000, "timeframe": {"unit": "days", "interval": 1},
           "google_sheet_url": "https://docs.google.com/spreadsheets/d/ID/edit#gid=0",
           "instruments": [{"symbol": s, "product": "DELIVERY", "source": {"type": "google"},
                            "strategy": {"name": "ema_crossover", "fast": 10, "slow": 30}} for s in ("AAA", "BBB")]}
    desk, specs = build_desk(cfg)
    data = load_history(cfg, specs)
    assert len(calls) == 1 and len(data["AAA"]) == 300
    assert run_backtest(desk, data).summary["trades"] > 0


def test_google_rejects_intraday_and_explains_unpublished_sheet(monkeypatch):
    cfg = {"instruments": [{"symbol": "X", "strategy": {"name": "orb"}, "source": {"type": "google", "url": "u"}}]}
    desk, specs = build_desk(cfg)
    with pytest.raises(ValueError, match="daily"):
        load_history(cfg, specs)
    monkeypatch.setattr(g.requests, "get", lambda url, timeout: _Resp("<html>sign in</html>"))
    with pytest.raises(g.GoogleSheetError, match="Publish"):
        g.GoogleSheetData("https://docs.google.com/spreadsheets/d/ID/edit").all()
