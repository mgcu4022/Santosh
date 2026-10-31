"""Command line: python -m tradebot <command>

  demo                      backtest on synthetic data (no accounts needed)
  backtest -c CONFIG        backtest using the config's data sources
  paper -c CONFIG           live paper trading with Upstox market data
  fetch -c CONFIG           download history from Upstox to data/*.csv
  find-key SYMBOL           look up an Upstox instrument key
  scan                      rank NIFTY stocks for today's watchlist (free Yahoo data)
  deals [--symbol X]        NSE bulk/block deals (context, not signals)
  plan SYMBOL SIDE ENTRY STOP   size, risk, charges and breakeven for one trade idea
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


def main(argv=None):
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
    f = sub.add_parser("fetch")
    f.add_argument("-c", "--config", required=True)
    k = sub.add_parser("find-key")
    k.add_argument("symbol")
    k.add_argument("--segment", default="NSE_EQ", help="NSE_EQ, BSE_EQ, NSE_FO, NSE_INDEX ...")
    sc = sub.add_parser("scan")
    sc.add_argument("--universe", default="config/universe_nifty50.txt")
    sc.add_argument("--top", type=int, default=10)
    sc.add_argument("--csv-dir", default=None, help="read daily CSVs (SYMBOL.csv) instead of Yahoo")
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
        run_paper(load_config(args.config), poll_seconds=args.poll)
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
    if args.csv_dir:
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
