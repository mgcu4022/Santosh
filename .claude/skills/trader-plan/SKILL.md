---
name: trader-plan
description: Turn a trade idea into a concrete plan - quantity or lots, stop-loss, target, margin, risk in rupees, Indian charges, breakeven move and win rate needed. Use when the user asks how much to buy, where to put the stop, position size, "plan this trade", or says /trader-plan.
---

# Trader plan (entry, exit, size, risk)

1. Get from the user (ask only for what's missing): symbol, BUY/SELL, entry, stop-loss.
   Optional: target (else 2:1), segment EQ/FUT/OPT, product INTRADAY/DELIVERY/NRML, lot size, capital, risk %.
   If they have no stop, suggest one from structure (opening-range low, prior day low, 1.5×ATR) and say why.
2. Run the calculator. It uses the same risk engine and charge model as the backtester:
   ```bash
   python -m tradebot plan SYMBOL BUY 1400 1390 --capital 500000 --risk 1
   python -m tradebot plan NIFTY-FUT SELL 24500 24560 --segment FUT --lot 65 --capital 1000000
   ```
3. Explain the output plainly: qty/lots, rupees lost at stop (incl. charges), rupees made at target,
   breakeven move, and the win rate needed. If `approved` is false, explain the reason
   (e.g. one F&O lot is too big for this capital) and suggest a smaller instrument or wider capital.
4. Exit plan: stop-loss is mandatory; intraday positions close by 15:15; mention trailing the stop
   after 1R if the user wants.

Rules: never raise risk above 2% per trade or suggest option selling. Lot sizes and charges change;
tell the user to confirm lot size on NSE.
