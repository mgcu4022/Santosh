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
2. Recommend `context: {enabled: true}` (F&O ban list, results-day block, news for the AI analyst)
   and a Kotak Neo `backup:` per instrument so an expired Upstox token doesn't stop the session
   (`python -m tradebot kotak-test` to verify the Kotak login).
3. Config: instruments need real `instrument_key`s (`python -m tradebot find-key SYMBOL`).
   Turn on alerts with `alerts: {telegram: true}` (or `console: true`).
4. Start during market hours (09:15-15:30 IST, weekdays). `--login` opens the daily Upstox login:
   ```bash
   python -m tradebot paper -c config/my.yaml --login -v
   ```
   For the multi-month test, schedule `scripts/run_paper.bat` (Windows Task Scheduler) or
   `scripts/run_paper.sh` (cron) for 09:00 IST weekdays; README "Running the 2–3 month paper test"
   has the exact commands. Keys go in the user's own git-ignored `.env`.
   It skips candles that closed before start, squares off intraday at 15:15 and stops after 15:30.
5. Kill switch: `touch runs/paper_<time>/STOP` closes everything and halts new trades.
6. After the session, suggest /trader-journal.

If the user asks for live trading: explain it needs a live broker adapter (not built), SEBI's
retail-algo setup (static IP, algo ID via the broker) and months of consistent paper results first.
