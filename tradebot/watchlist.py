"""Turn scan results into a ready-to-run config (instruments with Kotak tokens and Upstox keys)."""
from __future__ import annotations

from pathlib import Path

import pandas as pd


def instruments_yaml(results: pd.DataFrame, isin: dict[str, str], kotak_tokens: dict[str, str],
                     strategy: str = "orb", strategy_params: str = "range_minutes: 15, reward_risk: 2, max_range_pct: 2.5") -> str:
    lines = ["instruments:"]
    for sym, r in results.iterrows():
        tok = kotak_tokens.get(sym, "")
        key = f"NSE_EQ|{isin[sym]}" if isin.get(sym) else ""
        lines += [f"  - symbol: {sym}            # Rs {r['close']:g}, score {r['score']}, {r['setups'] or 'no setup'}, "
                  f"turnover Rs {r['turnover_cr']} cr/day",
                  f'    instrument_key: "{key}"',
                  f'    backup: {{kotak_token: "{tok}", kotak_segment: nse_cm}}' + ("" if tok else "   # FILL: kotak-find " + sym),
                  f"    strategy: {{name: {strategy}, {strategy_params}}}"]
    return "\n".join(lines) + "\n"


def write_config(base: str, out: str, block: str, header: str = "") -> Path:
    text = Path(base).read_text(encoding="utf-8").rstrip() + "\n\n"
    if header:
        text += "".join(f"# {h}\n" for h in header.splitlines())
    text += block
    p = Path(out)
    p.write_text(text, encoding="utf-8")
    return p
