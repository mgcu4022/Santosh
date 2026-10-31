"""Command line: python -m tradebot <command>

  demo                      backtest on synthetic data (no accounts needed)
  backtest -c CONFIG        backtest using the config's data sources
  paper -c CONFIG           live paper trading with Upstox market data
  fetch -c CONFIG           download history from Upstox to data/*.csv
  find-key SYMBOL           look up an Upstox instrument key
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
    pp = sub.add_parser("paper")
    pp.add_argument("-c", "--config", required=True)
    pp.add_argument("--poll", type=int, default=20)
    f = sub.add_parser("fetch")
    f.add_argument("-c", "--config", required=True)
    k = sub.add_parser("find-key")
    k.add_argument("symbol")
    k.add_argument("--segment", default="NSE_EQ", help="NSE_EQ, BSE_EQ, NSE_FO, NSE_INDEX ...")
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
