"""Event-driven backtester. Bars from all instruments are replayed in time order
through the same TradingDesk used for paper trading."""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .agents.desk import Book, TradingDesk
from .metrics import summarize, trades_frame


@dataclass
class BacktestResult:
    summary: dict
    equity: pd.Series
    trades: pd.DataFrame
    journal: pd.DataFrame
    per_symbol: pd.DataFrame = field(default_factory=pd.DataFrame)


def run_backtest(desk: TradingDesk, data: dict[str, pd.DataFrame]) -> BacktestResult:
    prepared = {sym: desk.books[sym].strategy.prepare(df) for sym, df in data.items()}
    events = []
    for sym, df in prepared.items():
        events.append(pd.DataFrame({"ts": df.index, "sym": sym, "i": range(len(df))}))
    ev = pd.concat(events).sort_values(["ts", "sym"], kind="stable")

    eq_points: dict = {}
    for ts, sym, i in ev.itertuples(index=False):
        desk.on_bar(sym, prepared[sym], int(i))
        eq_points[ts] = desk.broker.equity()

    last_ts = ev["ts"].iloc[-1].to_pydatetime()
    desk.flatten_all(last_ts, "end of backtest")
    eq_points[ev["ts"].iloc[-1]] = desk.broker.equity()

    equity = pd.Series(eq_points).sort_index()
    trades = trades_frame(desk.broker.trades)
    summary = summarize(equity, desk.broker.trades, desk.risk.cfg.capital)
    per_symbol = pd.DataFrame()
    if len(trades):
        per_symbol = trades.groupby("symbol").agg(trades=("net_pnl", "size"), net_pnl=("net_pnl", "sum"),
                                                  win_rate=("net_pnl", lambda s: round((s > 0).mean() * 100, 1)),
                                                  charges=("charges", "sum")).round(2)
    return BacktestResult(summary, equity, trades, pd.DataFrame(desk.journal), per_symbol)
