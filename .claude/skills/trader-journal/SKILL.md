---
name: trader-journal
description: Review a paper-trading or backtest journal - what traded, why, what was blocked or vetoed, P&L after charges, and rule breaks - and suggest one improvement. Use after a trading session, when the user asks how the day went, to review trades, or says /trader-journal.
---

# Trader journal (review)

1. Find the latest run: `ls -t runs/ | head`. Read `trades.csv` and `journal.csv` with pandas.
2. Summarise in under 15 lines:
   - trades, wins/losses, net P&L, total charges (and charges as % of gross P&L)
   - best and worst trade with their entry reason
   - exit mix: stop-loss / target / square-off / signal exit
   - events: `RISK_BLOCK` reasons (count each), `AI_VETO`s, any `HALT`
3. Spot patterns, with numbers:
   - losses clustered at a time of day (e.g. first 30 min) or in one symbol
   - many square-off exits = targets too far for intraday
   - many "Size below 1 lot" blocks = capital too small for that F&O instrument
4. End with **one** concrete, testable change (e.g. "skip entries before 09:30, then backtest it
   with /trader-backtest"). One change at a time, tested before it is adopted.
