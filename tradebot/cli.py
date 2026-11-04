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
  datacheck -c CONFIG       compare Kotak / Upstox / Yahoo live prices: uptime, speed, agreement
  terminal [--demo]         option-trading screen in the browser (paper orders)
  live [--json]             live snapshot of the terminal and strategy bot, with risk warnings
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
        (out / "summary.json").write_text(json.dumps(res.summary, indent=2, default=str), encoding="utf-8")
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
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        val = val.strip()
        val = "" if val.startswith("#") else val.split(" #")[0].strip().strip('"').strip("'")
        if key.strip() and val and key.strip() not in os.environ:
            os.environ[key.strip()] = val


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):   # Windows consoles/logs: print ✅ and ₹ without crashing
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    load_dotenv()
    p = argparse.ArgumentParser(prog="tradebot", description="Indian market trading agents (backtest + paper)")
    p.add_argument("-v", "--verbose", action="store_true")
    # -v also works after the command (e.g. `paper -c config/my.yaml -v`)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo", parents=[common])
    d.add_argument("--out", default=None)
    b = sub.add_parser("backtest", parents=[common])
    b.add_argument("-c", "--config", required=True)
    b.add_argument("--out", default=None)
    b.add_argument("--from", dest="from_", default=None, help="YYYY-MM-DD (overrides config)")
    b.add_argument("--to", default=None, help="YYYY-MM-DD (overrides config)")
    pp = sub.add_parser("paper", parents=[common])
    pp.add_argument("-c", "--config", required=True)
    pp.add_argument("--poll", type=int, default=20)
    pp.add_argument("--login", action="store_true", help="open the Upstox login page first (daily token)")
    pp.add_argument("--login-timeout", type=float, default=10, help="minutes to wait for the login")
    f = sub.add_parser("fetch", parents=[common])
    f.add_argument("-c", "--config", required=True)
    k = sub.add_parser("find-key", parents=[common])
    k.add_argument("symbol")
    k.add_argument("--segment", default="NSE_EQ", help="NSE_EQ, BSE_EQ, NSE_FO, NSE_INDEX ...")
    sc = sub.add_parser("scan", parents=[common])
    sc.add_argument("--universe", default="config/universe_nifty50.txt")
    sc.add_argument("--top", type=int, default=10)
    sc.add_argument("--csv-dir", default=None, help="read daily CSVs (SYMBOL.csv) instead of Yahoo")
    sc.add_argument("--google-url", default=None, help="published Google Sheet (GOOGLEFINANCE) instead of Yahoo")
    sc.add_argument("--nse", action="store_true", help="official NSE bhavcopy data instead of Yahoo")
    sc.add_argument("--all", action="store_true", help="with --nse: scan every EQ-series NSE stock, not just the universe")
    sc.add_argument("--min-price", type=float, default=None, help="default 50 (20 when --max-price is set)")
    sc.add_argument("--max-price", type=float, default=None, help="e.g. 200 for a small account")
    sc.add_argument("--min-turnover", type=float, default=None, help="Rs crore/day (default 10; 25 with --max-price)")
    sc.add_argument("--write-config", default=None, help="write a ready config: base file + these stocks")
    sc.add_argument("--base", default="config/trial_20k_base.yaml", help="base settings for --write-config")
    sc.add_argument("--no-kotak", action="store_true", help="don't look up Kotak tokens")
    sc.add_argument("--watchlist", default=None, help="write a paper-trading instruments block to this file")
    sc.add_argument("--strategy", default="orb")
    dl = sub.add_parser("deals", parents=[common])
    dl.add_argument("--symbol", default=None)
    dl.add_argument("--file", default=None, help="parse a saved NSE bulk.csv/block.csv instead of downloading")
    pl = sub.add_parser("plan", parents=[common])
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
    gs = sub.add_parser("google-sheet", parents=[common])
    gs.add_argument("symbols", nargs="*", help="symbols (default: --universe file)")
    gs.add_argument("--universe", default="config/universe_nifty50.txt")
    gs.add_argument("--start", default=None, help="YYYY-MM-DD (default: 2 years ago)")
    gs.add_argument("--exchange", default="NSE")
    nb = sub.add_parser("nse-bhav", parents=[common])
    nb.add_argument("--days", type=int, default=120, help="calendar days back (~60%% are trading days)")
    sub.add_parser("ban-list", parents=[common])
    ev = sub.add_parser("events", parents=[common])
    ev.add_argument("symbol")
    nw = sub.add_parser("news", parents=[common])
    nw.add_argument("symbol", nargs="?")
    nw.add_argument("--hours", type=int, default=48)
    kt = sub.add_parser("kotak-test", parents=[common])
    kt.add_argument("-c", "--config", default="config/my.yaml")
    kf = sub.add_parser("kotak-find", parents=[common])
    kf.add_argument("symbol")
    kf.add_argument("--segment", default="nse_cm", help="nse_cm, bse_cm, nse_fo")
    ul = sub.add_parser("upstox-login", parents=[common])
    ul.add_argument("--keyring", action="store_true", help="store token in the OS password store (pip install keyring)")
    ul.add_argument("--no-browser", action="store_true")
    rp = sub.add_parser("report", parents=[common])
    rp.add_argument("--runs", default="runs")
    rp.add_argument("--from", dest="from_", default=None)
    rp.add_argument("--to", default=None)
    rp.add_argument("--out", default=None, help="write the report as Markdown (default runs/report.md)")
    rp.add_argument("--telegram", action="store_true", help="also send a short summary to Telegram")
    rp.add_argument("--prefix", default="paper", help="paper (strategy bot) or terminal (manual option trades)")
    tm = sub.add_parser("terminal", parents=[common])
    tm.add_argument("-c", "--config", default="config/terminal.yaml")
    tm.add_argument("--demo", action="store_true", help="simulated prices on a simulated clock (no login)")
    tm.add_argument("--login", action="store_true", help="open the daily Upstox login first")
    tm.add_argument("--port", type=int, default=8050)
    tm.add_argument("--no-browser", action="store_true")
    lv = sub.add_parser("live", parents=[common])
    lv.add_argument("--port", type=int, default=8050, help="option terminal port")
    lv.add_argument("--runs", default="runs")
    lv.add_argument("--json", action="store_true", help="raw data instead of the text summary")
    dc = sub.add_parser("datacheck", parents=[common])
    dc.add_argument("-c", "--config", default="config/trial_20k.yaml")
    dc.add_argument("--minutes", type=float, default=2.0)
    dc.add_argument("--every", type=float, default=10.0, help="seconds between rounds")
    pf = sub.add_parser("preflight", parents=[common])
    pf.add_argument("-c", "--config", default="config/my.yaml")
    pf.add_argument("--offline", action="store_true", help="skip the network checks")
    sub.add_parser("alert-test", parents=[common])
    sub.add_parser("doctor", parents=[common])
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
            deals = parse_deals(Path(args.file).read_text(encoding="utf-8"), kind)
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
        specs = [sp for sp in parse_instruments(load_config(args.config)) if (sp.backup or {}).get("kotak_token")]
        if not specs:
            print("No instruments with backup: {kotak_token: ...} in", args.config)
            return 1
        toks = [(str(sp.backup["kotak_token"]), sp.backup.get("kotak_segment", "nse_cm")) for sp in specs]
        prices = KotakNeoQuotes().ltp(toks)
        print("Kotak Neo login OK. Live prices:")
        for sp, (tok, _) in zip(specs, toks):
            px = prices.get(tok)
            print(f"  {sp.instrument.symbol:<12} token {tok:<6} {'Rs ' + format(px, ',.2f') if px else 'NO PRICE (check the token with kotak-find)'}")
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
        rd = load_runs(args.runs, args.from_, args.to, prefix=args.prefix)
        if not rd.days:
            print(f"No paper runs found in {args.runs}/ yet.")
            return 0
        res = compute(rd, segment="OPT" if args.prefix == "terminal" else "EQ")
        text = render(res)
        print(text)
        if args.telegram:
            from .alerts import TelegramNotifier
            from .report import short_summary
            n = TelegramNotifier()
            n.send(short_summary(res))
            n.close()
        out = Path(args.out or Path(args.runs) / f"report_{args.prefix}.md")
        out.write_text(text, encoding="utf-8")
        print(f"Saved to {out}")
    elif args.cmd == "terminal":
        _terminal(args)
    elif args.cmd == "live":
        from .live import fetch_terminal, latest_paper_state, paper_warnings, render, terminal_warnings
        term = fetch_terminal(args.port)
        paper, pdir = latest_paper_state(args.runs)
        if args.json:
            print(json.dumps({"terminal": term, "terminal_warnings": terminal_warnings(term) if term else [],
                              "strategy_bot": paper, "strategy_bot_warnings": paper_warnings(paper) if paper else []},
                             indent=2, default=str))
        else:
            print(render(term, paper, pdir))
    elif args.cmd == "datacheck":
        from .datacheck import build_sources, run_rounds, save, summarize
        cfg = load_config(args.config)
        built = build_sources(parse_instruments(cfg))
        for name, why in built["skipped"].items():
            print(f"  {name}: not tested ({why})")
        rounds = max(1, int(args.minutes * 60 // args.every) + 1)
        print(f"Checking {', '.join(built['sources'])} for {args.minutes:g} min ({rounds} rounds)...")
        df = run_rounds(built["sources"], rounds, args.every)
        print(summarize(df).to_string())
        print("\nrounds_ok_% = answered with prices; avg_ms = response time; gap_bps = difference from the "
              "median price of all sources (1 bps = 0.01%). Yahoo is often delayed, so its gap is expected to be larger.")
        print(f"Saved to {save(df, cfg.get('runs_dir', 'runs'))}")
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


def _terminal(args):
    import time as _t
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from .options.chain import SimOptions, UpstoxOptions
    from .options.engine import OptionTerminal, TerminalConfig
    from .options.server import serve
    from .risk import RiskConfig
    cfg = load_config(args.config)
    tcfg = TerminalConfig.from_dict({"capital": cfg.get("capital", 500_000), **cfg.get("terminal", {})})
    risk_cfg = RiskConfig.from_dict({"capital": tcfg.capital, **cfg.get("risk", {})})
    ist = ZoneInfo("Asia/Kolkata")
    if args.demo:
        start_real = _t.monotonic()
        start_sim = datetime.now(ist).replace(hour=10, minute=0, second=0, microsecond=0)

        def clock():   # simulated market clock: starts 10:00, runs 5x real time
            return start_sim + timedelta(seconds=(_t.monotonic() - start_real) * 5)
        provider = SimOptions(clock=clock)
        provider.mode = "demo"
    else:
        from .data.upstox import UpstoxData
        from .upstox_auth import get_token
        token = get_token(args.login)
        provider = UpstoxOptions(UpstoxData(access_token=token))
        provider.mode = "live"
        clock = None
    serve(OptionTerminal(provider, tcfg, risk_cfg, clock=clock), port=args.port, open_browser=not args.no_browser)


def _scan(args):
    from datetime import date as _date
    from .scanner import ScanConfig, load_universe, run_scan, watchlist_yaml
    syms = load_universe(args.universe)
    data, failed, isin = {}, [], {}
    if args.nse:
        from .data.nse import NseClient
        bhav, meta = NseClient().daily_history_with_meta(150)
        isin = {k: v.get("isin", "") for k, v in meta.items()}
        if args.all:   # every EQ-series company stock (BE = trade-to-trade: no intraday; ETFs/funds excluded)
            from .data.nse import is_company_stock
            syms = [k for k, v in meta.items() if str(v.get("series", "EQ")) == "EQ" and is_company_stock(k, v)]
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
    small = args.max_price is not None
    cfg = ScanConfig(top=args.top, max_price=args.max_price,
                     min_price=args.min_price if args.min_price is not None else (20.0 if small else 50.0),
                     min_turnover_cr=args.min_turnover if args.min_turnover is not None else (25.0 if small else 10.0))
    res = run_scan(data, cfg)
    print(f"Scanned {len(data)} stocks" + (f" ({len(failed)} failed: {', '.join(failed[:8])})" if failed and not args.all else ""))
    rule = f"price Rs {cfg.min_price:g}-{cfg.max_price:g}" if small else f"price >= Rs {cfg.min_price:g}"
    print(f"Filters: {rule}, turnover >= Rs {cfg.min_turnover_cr:g} cr/day, daily range {cfg.min_atr_pct:g}-{cfg.max_atr_pct:g}%")
    if res.empty:
        print("Nothing passed the liquidity/volatility filters.")
        return
    cols = ["score", "bias", "setups", "close", "chg_pct", "gap_pct", "rel_vol", "atr_pct", "turnover_cr", "trend"]
    print(res[cols].to_string())
    if args.watchlist:
        Path(args.watchlist).write_text(watchlist_yaml(res, args.strategy) + "\n", encoding="utf-8")
        print(f"\nWatchlist written to {args.watchlist}")
    if args.write_config:
        from .watchlist import instruments_yaml, write_config
        tokens: dict[str, str] = {}
        if not args.no_kotak:
            try:
                from .data.kotak import KotakNeoQuotes
                tokens = KotakNeoQuotes().tokens_for(list(res.index))
            except Exception as e:
                print(f"Kotak token lookup failed ({e}); fill them with: python -m tradebot kotak-find SYMBOL")
        out = write_config(args.base, args.write_config, instruments_yaml(res, isin, tokens),
                           header=f"Stocks picked by the scanner on {_date.today():%d %b %Y} ({rule}). Re-run scan to refresh.")
        missing = [s for s in res.index if s not in tokens]
        print(f"\nConfig written to {out} with {len(res)} stocks" +
              (f"; Kotak token missing for: {', '.join(missing)}" if missing else "; all Kotak tokens found"))
        print("Next: python -m tradebot kotak-test -c " + str(out))

if __name__ == "__main__":
    sys.exit(main())
