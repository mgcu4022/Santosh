# Indian Market Trading Agents

Backtesting and paper trading for NSE/BSE **equity cash**, **index F&O** and **stock F&O**.
Rule-based strategies place trades, a risk agent sizes and guards them, and an optional
Claude AI analyst can veto trades based on news and price action.

> **No real orders are sent.** This version runs backtests and paper trading only.
> Not investment advice. Most retail F&O traders lose money (SEBI study: about 9 in 10).
> Prove a strategy on paper for months before considering live trading.

## How it works

Each new candle passes through a chain of agents:

```
Market data ──► Strategy agent ──► Risk agent ──► AI analyst ──► Execution (paper broker)
 (Upstox/CSV)    ORB, EMA,          sizing, limits,   Claude: approve     fills, slippage,
                 Supertrend         kill switch       or veto only        Indian charges
```

| Agent | File | Job |
|---|---|---|
| Strategy | `tradebot/strategies/` | Turns candles into entry/exit signals with a stop-loss |
| Risk | `tradebot/risk.py` | Sizes trades (1% risk default), rounds to F&O lots, checks margin, entry window, max positions/trades, daily-loss kill switch, order-rate limit, blocks option writing and delivery shorts |
| AI analyst | `tradebot/agents/analyst.py` | Claude reviews each approved entry with web search (results, ban list, SEBI action, events). Can only **veto**, never create or resize trades |
| Execution | `tradebot/brokers/paper.py` | Simulated fills at next bar open, gap-aware stops, STT/brokerage/GST/stamp duty |
| Desk | `tradebot/agents/desk.py` | Runs the chain. The same code runs backtests and paper trading |

Strategies included:
- **orb**: Opening Range Breakout (first 15 min), stop at the other side of the range
- **ema_crossover**: EMA 9/21 cross, ATR stop, 2:1 target
- **supertrend**: Supertrend flip with trailing stop

## Quick start

```bash
pip install -r requirements.txt
python -m tradebot demo          # synthetic data, no accounts needed
python -m pytest -q              # 24 tests
```

The demo uses **random synthetic prices**, so it should lose about what it pays in charges.
That's the expected result: on random data no strategy has an edge.

## Using real data (Upstox)

Of SBI Securities, Kotak and Upstox, Upstox has the best free API, so it's the data source here.

1. Create an app at the [Upstox developer console](https://account.upstox.com/developer/apps) (free).
2. Get a daily access token with the Upstox login (OAuth) flow. It expires each morning around 3:30 AM IST.
3. Set it in your shell (never commit it):
   ```bash
   export UPSTOX_ACCESS_TOKEN=...
   ```
4. Find instrument keys:
   ```bash
   python -m tradebot find-key RELIANCE                  # NSE_EQ|INE002A01018
   python -m tradebot find-key NIFTY --segment NSE_FO    # futures/options keys
   ```
5. Copy `config/example.yaml`, edit instruments and strategies, then:
   ```bash
   python -m tradebot fetch    -c config/my.yaml   # save history to data/*.csv
   python -m tradebot backtest -c config/my.yaml   # results in runs/
   python -m tradebot paper    -c config/my.yaml   # live paper trading in market hours
   ```

Paper trading writes `journal.csv` (every signal, block, veto, fill) and `trades.csv` to
`runs/paper_<time>/`. **Kill switch:** create a file named `STOP` in that folder to close
everything and stop trading.

## AI analyst (optional)

Set `analyst.enabled: true` in the config and export `ANTHROPIC_API_KEY`.
It uses the Claude API with structured JSON output, web search limited to 3 searches
per review, and server-side model fallback. Leave it off for backtests: it costs money
per call and can't see historical news anyway. Low-confidence vetoes (below 0.6) are
ignored, and `on_error` decides what happens if the API is unreachable.

## Rules and limits to know

- **SEBI retail algo framework (2025):** API orders must go through your broker's
  approved API from a registered static IP, carry an algo tag, and strategies above
  the order-rate threshold (10 orders/second) must be registered with the exchange.
  The risk agent caps the rate at 5/s. Check your broker's current requirements before going live.
- **Charges and lot sizes change** (budgets, NSE circulars). Defaults in `tradebot/costs.py`
  and the example config are approximate. Override them in the config.
- **Index futures backtests** use index candles as a proxy (ignores the futures premium/discount).
- **Options**: buying is supported if you supply premium candles (CSV or Upstox key).
  Option writing is blocked by default.

## Project layout

```
tradebot/
  agents/       desk (orchestrator), AI analyst
  strategies/   orb, ema_crossover, supertrend (+ base class to add your own)
  brokers/      broker interface, paper broker
  data/         CSV loader, Upstox client, synthetic data
  risk.py  costs.py  indicators.py  backtest.py  metrics.py  paper_live.py  cli.py
config/example.yaml
tests/
```

## Next steps

- Live order adapter for Upstox (and Kotak Neo) behind the same `Broker` interface
- Option strategies on index signals (pick ATM/OTM strike from the option chain)
- Telegram alerts for signals and fills
- Walk-forward testing and parameter sweeps
