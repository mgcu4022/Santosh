---
name: trader-backtest
description: Backtest ("time machine") a strategy on past NSE data and interpret the results honestly, including charges, drawdown and overfitting risk. Use when the user asks to backtest, test a strategy on history, replay a past period, or says /trader-backtest.
---

# Trader backtest (time machine)

1. Pick or write a config (see `config/example.yaml`, `config/daily_yahoo.yaml`). Data sources:
   `yahoo` (free; 5-min only ~60 days back, daily for years), `upstox` (needs token), `csv`.
2. Run, optionally replaying a specific window:
   ```bash
   python -m tradebot backtest -c config/daily_yahoo.yaml
   python -m tradebot backtest -c config/my.yaml --from 2026-09-01 --to 2026-09-30
   ```
   Results land in `runs/backtest_<time>/` (trades.csv, journal.csv, equity.csv, summary.json).
3. Report: net P&L after charges, gross P&L, total charges, max drawdown, win rate, profit factor,
   trade count, and the exit-reason table.
4. Interpret honestly:
   - fewer than ~100 trades = not enough evidence
   - gross positive but net negative = charges eat the edge; trade less often or hold longer
   - profit factor < 1.2 after charges = no reliable edge
   - if the user tuned parameters on this same period, warn about overfitting and suggest testing
     on a different period (walk-forward)
   - `python -m tradebot demo` uses random data: losses there are expected and prove nothing

Rules: past results don't predict future returns. Never present a backtest as a guarantee.
