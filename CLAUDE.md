# CLAUDE.md: tradebot (Indian market trading agents)

Guide for Claude Code working in this repository. Read it fully before running commands or
changing code. README.md has the user-facing details; this file says how to work here safely.

## 1. What this project is

`tradebot` is a Python package for **backtesting and paper trading** on NSE/BSE: equity cash,
index F&O and stock F&O. **No real orders are ever sent.** It has two parts:

1. **Strategy bot** (`python -m tradebot paper`): runs during market hours and trades
   automatically on paper through a chain of agents:
   `market data → strategy → event guard → risk agent → AI analyst (optional) → paper broker → journal/alerts`
2. **Option terminal** (`python -m tradebot terminal`): a browser screen (http://127.0.0.1:8050)
   for manual index-option trading on paper: option chain with B/S buttons, positions, MTM rules.

The goal is a 2–3 month paper test (100+ trades), judged by `python -m tradebot report`,
before anyone considers real money.

## 2. Who you are helping

- The user is a **trader, not a programmer**, usually on **Windows** with PowerShell.
  Use plain language and short steps. One command per line, in code blocks. Say what output
  to expect and what to send back. Hindi/Hinglish questions are normal; answer clearly.
- Before running anything that takes long or installs software, say what it does.
- When an error appears, explain it in one or two sentences, then fix it.
- Each person runs their **own copy with their own broker account**. Never set up one person's
  Upstox/Kotak credentials on someone else's computer.

## 3. Hard rules (do not break these, even if asked casually)

1. **Paper trading only.** Do not add, enable or suggest code that places real broker orders
   unless the user explicitly asks for live trading. Even then, explain first that it needs a
   broker order adapter, SEBI's retail-algo setup (static IP, algo ID via the broker) and months
   of good paper results.
2. **Every new position goes through `RiskManager.check_entry`** (`tradebot/risk.py`), in both the
   strategy bot (`agents/desk.py`) and the option terminal (`options/engine.py`). Never bypass it.
   Adds to an existing position count against the same risk budget.
3. **No look-ahead.** Strategies may only read bars up to index `i`; `tests/test_core.py` checks this.
4. **Secrets:** Upstox, Kotak, Anthropic and Telegram keys come from environment variables or the
   user's own git-ignored `.env`. **Never ask the user to paste keys, secrets, passwords, PINs or
   OTPs into the chat. Never print, `cat` or `type` the contents of `.env`.** Code must never write
   secrets to files (the daily Upstox token stays in memory, or the OS keyring if asked).
5. **Don't change risk settings** (risk %, stop-losses, kill switch, square-off, option-selling
   permission) without the user's clear approval, and explain the effect in rupees.
6. The option terminal server must listen on **127.0.0.1 only**. It has no password; never bind it
   to 0.0.0.0 or expose it to the internet.
7. **Not investment advice.** Present scans, backtests and reports as research. Never promise profit.
   Be honest when results are weak or could be luck.
8. Don't commit `.env`, `runs/`, `data/` downloads or `.venv/` (see `.gitignore`).

## 4. First-time setup

### Windows (PowerShell)
```
python --version                      # needs 3.10 or newer
cd $HOME\Documents\Santosh            # or wherever the project folder is
python -m venv .venv
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned -Force
.venv\Scripts\Activate.ps1            # prompt now starts with (.venv)
pip install -r requirements.txt       # 1–5 min; Windows Defender scanning makes it slow
python -m pytest -q                   # all tests should pass; warnings are fine
python -m tradebot terminal --demo    # opens the option terminal with simulated prices
```
If Python is missing: `winget install -e --id Python.Python.3.12`, then reopen PowerShell.
Every new PowerShell window needs `cd` into the folder and `.venv\Scripts\Activate.ps1` again.

### macOS / Linux
```
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && python -m pytest -q
```

### Connect the user's own Upstox account (for live prices)
Recommended: **Analytics Token**. https://account.upstox.com/developer/apps → My Apps → **Analytics** tab →
**Generate Token**. Valid 1 year, read-only (cannot place orders), covers market data, no daily login.
1. `Copy-Item .env.example .env` then `notepad .env`. The **user pastes** it after `UPSTOX_ACCESS_TOKEN=`
   themselves and saves. You don't read the file.
2. Alternative: an app (**+ App**, redirect exactly `http://127.0.0.1:5055/callback`) with
   `UPSTOX_API_KEY`/`UPSTOX_API_SECRET`; that path needs the browser login every day (`--login`).
   With a saved token, `--login` is skipped automatically.
3. `python -m tradebot preflight -c config/my.yaml` must end with **READY**.
   "Upstox data ⚠️ normal before today's login" is fine.

Kotak Neo instead of Upstox: `config/my.yaml` has `live_data: kotak` (strategy bot only; quotes → 5-min
candles, history from Yahoo). Needs the Kotak SDK (`pip install "git+https://github.com/Kotak-Neo/kotak-neo-api.git#egg=neo_api_client" pyotp`)
and `KOTAK_CONSUMER_KEY`, `KOTAK_MOBILE`, `KOTAK_UCC`, `KOTAK_MPIN` (+ optional `KOTAK_TOTP_SECRET`) typed into `.env`
by the user. Verify with `python -m tradebot kotak-test` (prints live prices for the config's stocks).

Optional extras (all in README): Telegram alerts (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
then `alert-test`), Kotak Neo backup data (SDK + `KOTAK_*` vars, then `kotak-test`),
Claude AI analyst (`ANTHROPIC_API_KEY`, `analyst.enabled: true`, costs money per call).

## 5. Daily routine (market days, IST)

| Time | Action |
|---|---|
| Before 09:00 | `python -m tradebot preflight` → READY |
| 09:00 | `python -m tradebot paper -c config/my.yaml --login -v`. With the Analytics Token in `.env` it starts directly; with app keys the browser opens the daily Upstox login (PIN/OTP). |
| 09:15–15:30 | Leave the PC on and awake. New entries 09:30–14:30 (my.yaml); all intraday positions close at 15:15. |
| Kill switch | Create a file named `STOP` in today's run folder: `New-Item runs\paper_YYYYMMDD\STOP` (Windows) or `touch ...` |
| After 15:30 | `python -m tradebot report` (all days combined, strict verdict) |
| Manual option practice | `python -m tradebot terminal --login` (live chain) or `--demo` |

Auto-start: `scripts\run_paper.bat` with Windows Task Scheduler, or `scripts/run_paper.sh` with
cron (exact commands in README, "Running the 2–3 month paper test"). Weekends and the holidays
listed in the config are skipped automatically.

## 6. Command reference (`python -m tradebot <command>`)

| Command | What it does | Needs |
|---|---|---|
| `demo` | Backtest on random synthetic data (should roughly lose its charges) | nothing |
| `backtest -c CFG [--from --to]` | Backtest with the config's data sources | depends on source |
| `paper -c CFG [--login]` | Live paper trading loop | Upstox |
| `terminal [--demo\|--login] [--port N]` | Option terminal in the browser | Upstox (live) |
| `preflight -c CFG [--offline]` | Pre-market checklist | — |
| `live [--json] [--port N]` | Live snapshot of terminal + strategy bot with risk warnings (read-only) | something running |
| `report [--prefix paper\|terminal]` | Multi-day results and verdict | saved runs |
| `plan SYMBOL BUY ENTRY STOP [--segment FUT --lot 65 ...]` | Size one trade: qty, risk, charges, breakeven | nothing |
| `scan [--nse\|--google-url URL\|--csv-dir DIR]` | Rank NIFTY 50 stocks for a watchlist | Yahoo/NSE/Google |
| `nse-bhav --days N` | Download official NSE end-of-day prices | NSE site |
| `ban-list`, `events SYMBOL`, `deals [--symbol X]`, `news [SYMBOL]` | NSE ban list, results dates, bulk/block deals, RSS headlines | internet |
| `upstox-login` | Daily Upstox login only | Upstox app keys |
| `find-key SYMBOL [--segment NSE_FO]` | Upstox instrument key lookup | internet |
| `kotak-test`, `kotak-find SYMBOL` | Kotak Neo backup checks | Kotak SDK + keys |
| `alert-test`, `doctor` | Telegram test; which keys are set | — |

Claude Code skills in `.claude/skills/`: `/trader-scan`, `/trader-research`, `/trader-plan`,
`/trader-backtest`, `/trader-paper`, `/trader-journal`, `/trader-live`. Use them when the request matches.
During market hours, questions like "how are my trades doing?" mean `/trader-live`: run `python -m tradebot live`
first and answer from its numbers. That skill is read-only: never place/close/modify positions or create the
STOP file unless the user explicitly asks for that exact action.

## 7. Config files (`config/`)

- `my.yaml`: the strategy bot's paper test. ORB on RELIANCE, HDFCBANK, ICICIBANK, INFY, SBIN;
  ₹5 lakh capital, 1% risk per trade, 3% daily-loss kill switch, entries 09:30–14:30,
  square-off 15:15, results-day guard on, NSE holidays. **Don't change rules mid-test**: it resets
  the evidence. Suggest one change at a time, backtested first.
- `terminal.yaml`: option terminal. Per-position stop-loss (buys −30%, sells +100%), MTM stop-loss,
  ~12% of index value as selling margin per unit, `allow_option_selling: true` (paper only).
- `example.yaml`: every option documented. `daily_yahoo.yaml`: free daily backtest.
- `universe_nifty50.txt`: scanner universe (index membership changes; update it).
- Lot sizes (NIFTY 65, BANKNIFTY 30, FINNIFTY 60, SENSEX 20), charges and holidays change over time.
  Treat them as approximate and tell the user to verify on NSE / their broker.

## 8. Code map

```
tradebot/
  cli.py            all commands; loads .env; UTF-8 console on Windows
  config.py         YAML → instruments, desk, data loading (csv, upstox, yahoo, google, synthetic)
  models.py         Instrument, Signal, Order, Position, Trade
  risk.py           RiskManager.check_entry (sizing, margin, limits, kill switch)  ← safety core
  costs.py          Indian charges: brokerage, STT, exchange, SEBI, stamp duty, GST
  agents/desk.py    the agent chain for the strategy bot (shared by backtest and paper)
  agents/analyst.py Claude analyst (approve/veto only; structured JSON output)
  strategies/       orb, ema_crossover, supertrend (+ base class; register in __init__.py)
  brokers/paper.py  simulated fills, gap-aware stops, P&L and charges
  backtest.py, metrics.py, report.py   results and the strict verdict
  paper_live.py     live loop: Upstox → Kotak failover, warm-up, run folders, kill switch
  context.py        NSE ban list / results-day guard + news brief for the analyst
  data/             upstox, kotak, nse, yahoo, google, csv, synthetic, barbuilder
  options/          option terminal: chain.py (Upstox + simulator), engine.py, server.py, terminal.html
  scanner.py, plan.py, deals.py, news.py, alerts.py, preflight.py, upstox_auth.py
tests/              pytest suite, fully offline (fake providers, fixtures)
scripts/            run_paper.bat / run_paper.sh for the daily auto-start
```

Results land in `runs/paper_YYYYMMDD/` and `runs/terminal_YYYYMMDD/` (`trades.csv`, `journal.csv`).

## 9. Working on the code

- Run `python -m pytest -q` before and after every change; add tests for new behaviour. Tests must
  stay offline (use fakes; never call Upstox/NSE/Telegram/Anthropic in tests).
- Must work on **Windows and Python 3.10+**: use `pathlib`, `encoding="utf-8"` on every
  `read_text`/`write_text`, no shell-specific code in the package, time zones via `zoneinfo`
  with `tzdata` installed.
- All market times are IST (`Asia/Kolkata`). Candles are 5-minute bars from 09:15.
- New strategy: subclass `strategies/base.py`, compute indicators in `prepare()`, decide in
  `on_bar(instrument, df, i, position)` using rows ≤ `i` only, always return a stop-loss, register it, add a
  look-ahead test.
- Keep README.md and the skills in sync when commands or behaviour change.
- External APIs (Upstox v2/v3, Kotak Neo SDK, NSE JSON/archives) were written from public docs and
  are **not tested against the live services**. When the user reports an error from them, read the
  exact message and adjust the small parser/client involved; don't guess silently.
- Claude API code (analyst) uses the Anthropic Python SDK; check current docs before changing
  model IDs or parameters.

## 10. Troubleshooting (Windows first)

| Symptom | Fix |
|---|---|
| `python`/`git` "not recognized" right after installing | Close and reopen PowerShell |
| `Activate.ps1 cannot be loaded … running scripts is disabled` | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned -Force` |
| `No module named tradebot` / `No module named pandas` | Not in the project folder, or venv not active: `cd` there and `.venv\Scripts\Activate.ps1` |
| pip install seems stuck | Wait up to 5 min (Defender scanning). If the title bar says "Select", press Enter/Esc (QuickEdit pause) |
| `ZoneInfoNotFoundError: Asia/Kolkata` | `pip install tzdata` |
| Windows Firewall popup for Python | "Cancel" is fine: everything runs on 127.0.0.1 |
| Port 8050 already in use | Another terminal is running; close it or use `--port 8051` |
| Upstox login page says redirect URI mismatch | App's redirect URL must be exactly `http://127.0.0.1:5055/callback` |
| Upstox `401` / token errors mid-day | Token expired or login missed. Restart with `--login`. The bot fails over to Kotak if configured |
| NSE commands return `403` | NSE blocks bots at times; the bot continues without that data. Retry later |
| `preflight` says not a trading day | Weekend or a holiday in the config's `holidays:` list |
| Report says "NOT ENOUGH DATA" | Normal until 100 trades over ~40 trading days |

## 11. Honest expectations to keep repeating

- On random data, strategies roughly break even before charges and lose after charges: that is
  the correct behaviour of an unbiased backtester.
- Most retail intraday/F&O traders lose money (SEBI studies). Paper results flatter real trading.
- With ₹5 lakh, option selling fits about 1 NIFTY lot (~₹1.8 lakh margin).
- A strategy "passes" only with 100+ trades, ~40+ days, profit factor ≥ 1.2 and t-stat ≥ 2 after
  charges. Then the next step is a small real-money test, not scaling up.
