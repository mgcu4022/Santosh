---
name: trader-scan
description: Scan NIFTY stocks for today's intraday watchlist (gaps, volume spikes, 20-day breakouts, NR7, trend) and explain the top picks. Use when the user asks what to watch or trade today, wants a stock scan, or says /trader-scan.
---

# Trader scan

1. Run the scanner (free Yahoo daily data, no keys needed):
   ```bash
   python -m tradebot scan --top 10 --watchlist runs/watchlist.yaml
   ```
   Prefer official prices with `--nse` (run `python -m tradebot nse-bhav` once first; later runs
   only fetch new days). Use `--universe <file>` for another list. If Yahoo is unreachable, use Google Finance via a
   published sheet (`--google-url <link>`, setup in README) or local files (`--csv-dir <dir>`).
   For the user's ₹20,000 trial (stocks under ₹200), use:
   `python -m tradebot scan --nse --all --max-price 200 --top 6 --write-config config/trial_20k.yaml`
   (writes a ready config with Kotak tokens; then `kotak-test -c config/trial_20k.yaml`).
   Warn about very cheap stocks: wide spreads, surveillance (ASM/GSM) and manipulation risk.
2. Present a short table: symbol, score, bias, setups, change %, relative volume, ATR %.
3. For each of the top 3–5, one line on *why* it is interesting, read from the columns:
   - `breakout20` / `breakdown20` with matching `trend` = strongest setups
   - `vol_spike` = unusual participation; check news before trading (use /trader-research)
   - `nr7` = coiled range; ORB tends to suit these
   - big `gap_pct` against the trend = higher reversal risk
4. Remind the user the scan ran on the last *completed* session. Pre-open, gaps are not known yet.
5. Offer next steps: `/trader-research SYMBOL` for news/results, `/trader-plan` to size a trade,
   or paste `runs/watchlist.yaml` into a paper config (instrument keys via `python -m tradebot find-key`).

Rules: never call a scan result a recommendation. Rank ≠ prediction. Skip stocks the user says are
in the F&O ban list or under ASM/GSM surveillance.
