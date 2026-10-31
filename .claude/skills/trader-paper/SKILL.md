---
name: trader-paper
description: Start, monitor or stop live paper trading (real Upstox prices, simulated orders) with Telegram alerts and the kill switch. Use when the user wants to paper trade, run the bot during market hours, set up alerts, stop trading, or says /trader-paper.
---

# Trader paper (execution + alerts)

Paper only. This project sends **no real orders**.

1. Check setup: `python -m tradebot doctor`. Needs `UPSTOX_ACCESS_TOKEN` (daily, from the Upstox
   login flow). Telegram alerts need `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`
   (`python -m tradebot alert-test` to verify). Never ask the user to paste tokens into chat
   or commit them; they go in environment variables.
2. Config: instruments need real `instrument_key`s (`python -m tradebot find-key SYMBOL`).
   Turn on alerts with `alerts: {telegram: true}` (or `console: true`).
3. Start during market hours (09:15-15:30 IST, weekdays):
   ```bash
   python -m tradebot paper -c config/my.yaml -v
   ```
   It skips candles that closed before start, squares off intraday at 15:15 and stops after 15:30.
4. Kill switch: `touch runs/paper_<time>/STOP` closes everything and halts new trades.
5. After the session, suggest /trader-journal.

If the user asks for live trading: explain it needs a live broker adapter (not built), SEBI's
retail-algo setup (static IP, algo ID via the broker) and months of consistent paper results first.
