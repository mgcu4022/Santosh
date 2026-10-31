# Trading agents repo

Python package `tradebot`: backtesting and paper trading for NSE/BSE equity and F&O.
See README.md for architecture. Skills live in `.claude/skills/trader-*`.

- Run tests: `python -m pytest -q`. Run the CLI: `python -m tradebot <command>`.
- Paper trading only. Do not add code that places real broker orders unless the user explicitly asks.
- Every entry must go through `RiskManager.check_entry`; never bypass it.
- Strategies may only read bars up to index `i` (no look-ahead); `tests/test_core.py` checks this.
- Secrets (Upstox, Kotak, Anthropic, Telegram) come from environment variables or the user's own git-ignored
  `.env`. Code must never write secrets to files (the Upstox token stays in memory or the OS keyring).
- Charges, lot sizes and the NIFTY 50 list change over time; treat defaults as approximate.
- Not investment advice. Present scans and backtests as research, not recommendations.
