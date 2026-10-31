"""Performance metrics for an equity curve and trade list."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .models import Trade


def summarize(equity: pd.Series, trades: list[Trade], capital: float) -> dict:
    out: dict = {"start_capital": capital}
    final = float(equity.iloc[-1]) if len(equity) else capital
    out["final_equity"] = round(final, 2)
    out["net_pnl"] = round(final - capital, 2)
    out["return_pct"] = round((final / capital - 1) * 100, 2)

    if len(equity):
        daily = equity.groupby(equity.index.date).last()
        rets = daily.pct_change().dropna()
        days = len(daily)
        out["trading_days"] = days
        out["cagr_pct"] = round(((final / capital) ** (252 / max(days, 1)) - 1) * 100, 2) if final > 0 else -100.0
        sd = rets.std()
        out["sharpe"] = round(float(rets.mean() / sd * math.sqrt(252)), 2) if sd and sd > 0 else 0.0
        peak = equity.cummax()
        out["max_drawdown_pct"] = round(float(((equity - peak) / peak).min() * 100), 2)

    out["trades"] = len(trades)
    if trades:
        pnl = np.array([t.net_pnl for t in trades])
        wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
        out["win_rate_pct"] = round(len(wins) / len(pnl) * 100, 1)
        out["avg_win"] = round(float(wins.mean()), 2) if len(wins) else 0.0
        out["avg_loss"] = round(float(losses.mean()), 2) if len(losses) else 0.0
        out["profit_factor"] = round(float(wins.sum() / -losses.sum()), 2) if losses.sum() < 0 else float("inf")
        out["total_charges"] = round(sum(t.charges for t in trades), 2)
        out["gross_pnl"] = round(sum(t.gross_pnl for t in trades), 2)
    return out


def trades_frame(trades: list[Trade]) -> pd.DataFrame:
    rows = [{**t.__dict__, "net_pnl": t.net_pnl} for t in trades]
    return pd.DataFrame(rows)
