"""EMA crossover with ATR stop and reward:risk target. Works on any timeframe."""
from __future__ import annotations

import math

from ..indicators import atr, ema
from ..models import Side
from .base import Strategy


class EMACrossover(Strategy):
    name = "ema_crossover"

    def __init__(self, fast: int = 9, slow: int = 21, atr_period: int = 14,
                 atr_stop: float = 1.5, reward_risk: float = 2.0, **kw):
        super().__init__(**kw)
        self.fast, self.slow = fast, slow
        self.atr_period, self.atr_stop, self.rr = atr_period, atr_stop, reward_risk

    def prepare(self, df):
        out = df.copy()
        out["ema_fast"] = ema(out["close"], self.fast)
        out["ema_slow"] = ema(out["close"], self.slow)
        out["atr"] = atr(out, self.atr_period)
        return out

    def on_bar(self, instrument, df, i, position):
        if i < 1:
            return None
        f0, s0 = df["ema_fast"].iloc[i - 1], df["ema_slow"].iloc[i - 1]
        f1, s1 = df["ema_fast"].iloc[i], df["ema_slow"].iloc[i]
        a = df["atr"].iloc[i]
        if any(math.isnan(x) for x in (f0, s0, f1, s1, a)):
            return None
        close = df["close"].iloc[i]
        cross_up = f0 <= s0 and f1 > s1
        cross_dn = f0 >= s0 and f1 < s1

        if position is not None and position.is_open:
            if position.qty > 0 and cross_dn:
                return self.exit(instrument, df, i, position, "EMA cross down")
            if position.qty < 0 and cross_up:
                return self.exit(instrument, df, i, position, "EMA cross up")
            return None

        risk = self.atr_stop * a
        if cross_up:
            return self.enter(instrument, df, i, Side.BUY, f"EMA{self.fast} crossed above EMA{self.slow}",
                              stop=close - risk, target=close + self.rr * risk)
        if cross_dn:
            return self.enter(instrument, df, i, Side.SELL, f"EMA{self.fast} crossed below EMA{self.slow}",
                              stop=close + risk, target=close - self.rr * risk)
        return None
