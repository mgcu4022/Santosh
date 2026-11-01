"""Command line: python -m tradebot <command>

  demo                      backtest on synthetic data (no accounts needed)
  backtest -c CONFIG        backtest using the config's data sources
  paper -c CONFIG           live paper trading with Upstox market data
  fetch -c CONFIG           download history from Upstox to data/*.csv
  find-key SYMBOL           look up an Upstox instrument key
  scan                      rank NIFTY stocks for today's watchlist (Yahoo or Google data)
  deals [--symbol X]        NSE bulk/block deals (context, not signals)
  plan SYMBOL SIDE ENTRY STOP   size, risk, charges and breakeven for one trade idea
  google-sheet              print GOOGLEFINANCE formulas to paste into a Google Sheet
  nse-bhav [--days N]       download official NSE end-of-day prices (cached in data/nse)
  ban-list                  today's NSE F&O ban list
  events SYMBOL             NSE results/board meetings, corporate actions, announcements
  news [SYMBOL]             RSS headlines (all, or matched to a stock)
  kotak-test / kotak-find   check Kotak Neo login / find an instrument token
  upstox-login              daily Upstox browser login (prints/stores today's token)
  report                    multi-day paper-trading report and verdict
  preflight -c CONFIG       pre-market checklist (config, keys, calendar, data access)
  alert-test                send a test Telegram alert
  doctor                    check which keys/tokens are set
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from .backtest import run_backtest
from .config import build_desk, load_config, load_history, parse_instruments

DEMO_CONFIG = {
    "capital": 1_000_000,
    "timeframe": {"unit": "minutes", "interval": 5},
    "risk": {"risk_per_trade_pct": 1.0, "max_daily_loss_pct": 3.0},
    "instruments": [
        {"symbol": "DEMO-STOCK-A", "segment": "EQ", "product": "INTRADAY",
         "strategy": {"name": "orb", "range_minutes": 15},
         "source": {"type": "synthetic", "days": 60, "start_price": 2500, "seed": 1}},
        {"symbol": "DEMO-STOCK-B", "segment": "EQ", "product": "INTRADAY",
         "strategy": {"name": "ema_crossover", "fast": 9, "slow": 21},
         "source": {"type": "synthetic", "days": 60, "start_price": 800, "seed": 2}},
        {"symbol": "DEMO-STOCK-FUT", "segment": "FUT", "product": "INTRADAY", "lot_size": 500,
         "strategy": {"name": "supertrend", "period": 10, "multiplier": 3},
         "source": {"type": "synthetic", "days": 60, "start_price": 1500, "seed": 3}},
    ],
}


def _print_result(res, out: Path | None):
    print("\n=== Summary ===")
    for k, v in res.summary.items():
        print(f"  {k:<18} {v}")
    if len(res.per_symbol):
        print("\n=== Per instrument ===")
        print(res.per_symbol.to_string())
    if len(res.trades):
        print("\n=== Exit reasons ===")
        print(res.trades.groupby("exit_reason")["net_pnl"].agg(["count", "sum"]).round(2).to_string())
    if out:
        out.mkdir(parents=True, exist_ok=True)
        res.trades.to_csv(out / "trades.csv", index=False)
        res.journal.to_csv(out / "journal.csv", index=False)
        res.equity.rename("equity").to_csv(out / "equity.csv")
        (out / "summary.json").write_text(json.dumps(res.summary, indent=2, default=str))
        print(f"\nSaved trades, journal, equity curve to {out}/")


def load_dotenv(path: str = ".env") -> None:
    """Read KEY=VALUE lines from a private .env file you created (git-ignored).

    Scheduled tasks don't inherit your terminal's variables; this lets them find your keys.
    Existing environment variables always win. Nothing is ever written to the file.
    """
    import os
    f = Path(path)
    if not f.exists():
        return
    for line in f.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        val = val.strip()
        val = "" if val.startswith("#") else val.split(" #")[0].strip().strip('"').strip("'")
        if key.strip() and val and key.strip() not in os.environ:
            os.environ[key.strip()] = val


def main(argv=None):
    load_dotenv()
    p = argparse.ArgumentParser(prog="tradebot", description="Indian market trading agents (backtest + paper)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo")
    d.add_argument("--out", default=None)
    b = sub.add_parser("backtest")
    b.add_argument("-c", "--config", required=True)
    b.add_argument("--out", default=None)
    b.add_argument("--from", dest="from_", default=None, help="YYYY-MM-DD (overrides config)")
    b.add_argument("--to", default=None, help="YYYY-MM-DD (overrides config)")
    pp = sub.add_parser("paper")
    pp.add_argument("-c", "--config", required=True)
    pp.add_argument("--poll", type=int, default=20)
    pp.add_argument("--login", action="store_true", help="open the Upstox login page first (daily token)")
    pp.add_argument("--login-timeout", type=float, default=10, help="minutes to wait for the login")
    f = sub.add_parser("fetch")
    f.add_argument("-c", "--config", required=True)
    k = sub.add_parser("find-key")
    k.add_argument("symbol")
    k.add_argument("--segment", default="NSE_EQ", help="NSE_EQ, BSE_EQ, NSE_FO, NSE_INDEX ...")
    sc = sub.add_parser("scan")
    sc.add_argument("--universe", default="config/universe_nifty50.txt")
    sc.add_argument("--top", type=int, default=10)
    sc.add_argument("--csv-dir", default=None, help="read daily CSVs (SYMBOL.csv) instead of Yahoo")
    sc.add_argument("--google-url", default=None, help="published Google Sheet (GOOGLEFINANCE) instead of Yahoo")
    sc.add_argument("--nse", action="store_true", help="official NSE bhavcopy data instead of Yahoo")
    sc.add_argument("--watchlist", default=None, help="write a paper-trading instruments block to this file")
    sc.add_argument("--strategy", default="orb")
    dl = sub.add_parser("deals")
    dl.add_argument("--symbol", default=None)
    dl.add_argument("--file", default=None, help="parse a saved NSE bulk.csv/block.csv instead of downloading")
    pl = sub.add_parser("plan")
    pl.add_argument("symbol")
    pl.add_argument("side", choices=["BUY", "SELL", "buy", "sell"])
    pl.add_argument("entry", type=float)
    pl.add_argument("stop", type=float)
    pl.add_argument("--target", type=float, default=None)
    pl.add_argument("--rr", type=float, default=2.0, help="reward:risk if no target")
    pl.add_argument("--segment", default="EQ")
    pl.add_argument("--product", default="INTRADAY")
    pl.add_argument("--lot", type=int, default=1)
    pl.add_argument("--capital", type=float, default=500_000)
    pl.add_argument("--risk", type=float, default=1.0, help="% of capital at risk")
    gs = sub.add_parser("google-sheet")
    gs.add_argument("symbols", nargs="*", help="symbols (default: --universe file)")
    gs.add_argument("--universe", default="config/universe_nifty50.txt")
    gs.add_argument("--start", default=None, help="YYYY-MM-DD (default: 2 years ago)")
    gs.add_argument("--exchange", default="NSE")
    nb = sub.add_parser("nse-bhav")
    nb.add_argument("--days", type=int, default=120, help="calendar days back (~60%% are trading days)")
    sub.add_parser("ban-list")
    ev = sub.add_parser("events")
    ev.add_argument("symbol")
    nw = sub.add_parser("news")
    nw.add_argument("symbol", nargs="?")
    nw.add_argument("--hours", type=int, default=48)
    sub.add_parser("kotak-test")
    kf = sub.add_parser("kotak-find")
    kf.add_argument("symbol")
    kf.add_argument("--segment", default="nse_cm", help="nse_cm, bse_cm, nse_fo")
    ul = sub.add_parser("upstox-login")
    ul.add_argument("--keyring", action="store_true", help="store token in the OS password store (pip install keyring)")
    ul.add_argument("--no-browser", action="store_true")
    rp = sub.add_parser("report")
    rp.add_argument("--runs", default="runs")
    rp.add_argument("--from", dest="from_", default=None)
    rp.add_argument("--to", default=None)
    rp.add_argument("--out", default=None, help="write the report as Markdown (default runs/report.md)")
    rp.add_argument("--telegram", action="store_true", help="also send a short summary to Telegram")
    pf = sub.add_parser("preflight")
    pf.add_argument("-c", "--config", default="config/my.yaml")
    pf.add_argument("--offline", action="store_true", help="skip the network checks")
    sub.add_parser("alert-test")
    sub.add_parser("doctor")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    if args.cmd == "demo":
        print("Running demo backtest on SYNTHETIC data (random prices, not real market data).")
        desk, specs = build_desk(DEMO_CONFIG)
        res = run_backtest(desk, load_history(DEMO_CONFIG, specs))
        _print_result(res, Path(args.out) if args.out else None)
    elif args.cmd == "backtest":
        cfg = load_config(args.config)
        if args.from_ or args.to:
            cfg["backtest"] = {**cfg.get("backtest", {}),
                               **({"from": args.from_} if args.from_ else {}), **({"to": args.to} if args.to else {})}
        desk, specs = build_desk(cfg)
        res = run_backtest(desk, load_history(cfg, specs))
        _print_result(res, Path(args.out or f"runs/backtest_{stamp}"))
    elif args.cmd == "paper":
        from .paper_live import run_paper
        run_paper(load_config(args.config), poll_seconds=args.poll, login=args.login,
                  login_timeout_min=args.login_timeout)
    elif args.cmd == "fetch":
        from .data.upstox import UpstoxData
        cfg = load_config(args.config)
        cfg_up = {**cfg, "instruments": [{**i, "source": {"type": "upstox"}} for i in cfg["instruments"]]}
        specs = parse_instruments(cfg_up)
        data = load_history(cfg_up, specs)
        Path("data").mkdir(exist_ok=True)
        for sym, df in data.items():
            path = Path("data") / f"{sym}_{cfg.get('timeframe', {}).get('interval', 5)}.csv"
            df.to_csv(path)
            print(f"{sym}: {len(df)} bars -> {path}")
    elif args.cmd == "find-key":
        from .data.upstox import find_instrument_key
        for item in find_instrument_key(args.symbol, args.segment):
            print(item.get("instrument_key"), item.get("trading_symbol"), item.get("lot_size"), item.get("expiry", ""))
    elif args.cmd == "scan":
        _scan(args)
    elif args.cmd == "deals":
        from .deals import fetch_deals, parse_deals, summarize
        if args.file:
            kind = "block" if "block" in Path(args.file).name.lower() else "bulk"
            deals = parse_deals(Path(args.file).read_text(), kind)
        else:
            deals = fetch_deals()
        if args.symbol:
            deals = deals[deals["symbol"] == args.symbol.upper()]
        if deals.empty:
            print("No bulk/block deals found" + (f" for {args.symbol}" if args.symbol else ""))
        else:
            print("Context only: deals are reported after the fact and are not trade signals.\n")
            print(deals[["kind", "date", "symbol", "client", "side", "qty", "price", "value_cr"]].to_string(index=False))
            print("\n=== Net by symbol (Rs crore) ===")
            print(summarize(deals).head(20).to_string())
    elif args.cmd == "plan":
        from .plan import make_plan
        plan = make_plan(args.symbol, args.side, args.entry, args.stop, args.target, args.rr, args.segment,
                         args.product, args.lot, args.capital, args.risk)
        for k, v in plan.items():
            print(f"  {k:<20} {v}")
    elif args.cmd == "google-sheet":
        from datetime import date, timedelta
        from .data.google import sheet_layout
        from .scanner import load_universe
        syms = [x.upper() for x in args.symbols] or load_universe(args.universe)
        start = date.fromisoformat(args.start) if args.start else date.today() - timedelta(days=730)
        sys.stdout.write(sheet_layout(syms, start, args.exchange))
        print("# Paste the two rows above into cell A1 of a new Google Sheet, then "
              "File -> Share -> Publish to web -> CSV. See tradebot/data/google.py.", file=sys.stderr)
    elif args.cmd == "nse-bhav":
        from .data.nse import NseClient
        data = NseClient().daily_history(args.days)
        days = max((len(v) for v in data.values()), default=0)
        print(f"{len(data)} stocks, up to {days} trading days, cached in data/nse/bhav/")
    elif args.cmd == "ban-list":
        from .data.nse import NseClient
        ban = sorted(NseClient().ban_list())
        print("F&O ban list: " + (", ".join(ban) if ban else "empty"))
    elif args.cmd == "events":
        from .data.nse import NseClient
        nse = NseClient()
        sym = args.symbol.upper()
        evs = nse.events(sym)
        print(f"=== {sym}: events (NSE) ===")
        for e in evs[-10:]:
            print(f"  {e['date']:%d-%b-%Y}  {e['purpose']}  {e['detail'][:100]}")
        if not evs:
            print("  none found")
        print("=== latest announcements ===")
        for a in nse.announcements(sym):
            print(f"  {a['time']}  {a['title']}  {a['detail'][:120]}")
    elif args.cmd == "news":
        from .news import NewsFeed
        nf = NewsFeed()
        items = nf.for_symbol(args.symbol.upper(), hours=args.hours, limit=20) if args.symbol else nf.all()[:30]
        print("Headlines are third-party news, not advice.\n")
        for h in items:
            when = h["published"].astimezone().strftime("%d-%b %H:%M") if h["published"] else "      "
            print(f"  {when}  [{h['source']}] {h['title']}")
        if not items:
            print("  no matching headlines")
    elif args.cmd == "kotak-test":
        from .data.kotak import KotakNeoQuotes
        print(KotakNeoQuotes().ltp([("2885", "nse_cm")]))   # 2885 = RELIANCE on NSE (check)
    elif args.cmd == "kotak-find":
        from .data.kotak import KotakNeoQuotes
        for r in KotakNeoQuotes().find(args.symbol, args.segment):
            print(r)
    elif args.cmd == "upstox-login":
        from .upstox_auth import check_token, interactive_login, save_to_keyring
        token = interactive_login(open_browser=not args.no_browser)
        if not token:
            print("No login received.")
            return 1
        print(f"Logged in as {check_token(token)}. Token valid until ~3:30 AM IST tomorrow.")
        if args.keyring:
            save_to_keyring(token)
            print("Saved to the OS password store; tradebot will pick it up automatically today.")
        else:
            print("To use it in this terminal:\n  export UPSTOX_ACCESS_TOKEN=" + token)
    elif args.cmd == "report":
        from .report import compute, load_runs, render
        rd = load_runs(args.runs, args.from_, args.to)
        if not rd.days:
            print(f"No paper runs found in {args.runs}/ yet.")
            return 0
        res = compute(rd)
        text = render(res)
        print(text)
        if args.telegram:
            from .alerts import TelegramNotifier
            from .report import short_summary
            n = TelegramNotifier()
            n.send(short_summary(res))
            n.close()
        out = Path(args.out or Path(args.runs) / "report.md")
        out.write_text(text)
        print(f"Saved to {out}")
    elif args.cmd == "preflight":
        from .preflight import print_checks, run_checks
        return print_checks(run_checks(load_config(args.config), online=not args.offline))
    elif args.cmd == "alert-test":
        from .alerts import TelegramNotifier
        n = TelegramNotifier()
        n.send("✅ tradebot test alert. Telegram is set up.")
        n.close()
        print("Sent. Check Telegram.")
    elif args.cmd == "doctor":
        from .config import env_check
        for k, ok in env_check().items():
            print(f"  {k:<22} {'set' if ok else 'not set'}")
    return 0


def _scan(args):
    from .scanner import ScanConfig, load_universe, run_scan, watchlist_yaml
    syms = load_universe(args.universe)
    data, failed = {}, []
    if args.nse:
        from .data.nse import NseClient
        bhav = NseClient().daily_history(150)
        for s in syms:
            if s in bhav:
                data[s] = bhav[s]
            else:
                failed.append(s)
    elif args.google_url:
        from .data.google import GoogleSheetData
        sheet = GoogleSheetData(args.google_url).all()
        for s in syms:
            if s in sheet:
                data[s] = sheet[s]
            else:
                failed.append(s)
    elif args.csv_dir:
        from .data.csv_feed import load_csv
        for s in syms:
            path = Path(args.csv_dir) / f"{s}.csv"
            if path.exists():
                data[s] = load_csv(str(path))
            else:
                failed.append(s)
    else:
        from .data.yahoo import YahooData, to_yahoo_ticker
        yd = YahooData()
        for s in syms:
            try:
                data[s] = yd.history(to_yahoo_ticker(s), "1d")
            except Exception as e:
                failed.append(s)
                logging.info("%s: %s", s, e)
    res = run_scan(data, ScanConfig(top=args.top))
    print(f"Scanned {len(data)} stocks" + (f" ({len(failed)} failed: {', '.join(failed[:8])})" if failed else ""))
    if res.empty:
        print("Nothing passed the liquidity/volatility filters.")
        return
    cols = ["score", "bias", "setups", "close", "chg_pct", "gap_pct", "rel_vol", "atr_pct", "turnover_cr", "trend"]
    print(res[cols].to_string())
    if args.watchlist:
        Path(args.watchlist).write_text(watchlist_yaml(res, args.strategy) + "\n")
        print(f"\nWatchlist written to {args.watchlist}")


if __name__ == "__main__":
    sys.exit(main())
