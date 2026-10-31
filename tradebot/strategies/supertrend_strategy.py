"""Supertrend flip strategy: enter on direction change, trail stop on the Supertrend line."""
from __future__ import annotations

from ..indicators import supertrend
from ..models import Side
from .base import Strategy


class SupertrendStrategy(Strategy):
    name = "supertrend"

    def __init__(self, period: int = 10, multiplier: float = 3.0, **kw):
        super().__init__(**kw)
        self.period, self.multiplier = period, multiplier

    def prepare(self, df):
        out = df.copy()
        st = supertrend(out, self.period, self.multiplier)
        out["supertrend"], out["st_dir"] = st["supertrend"], st["st_dir"]
        return out

    def on_bar(self, instrument, df, i, position):
        if i < 1:
            return None
        d0, d1 = df["st_dir"].iloc[i - 1], df["st_dir"].iloc[i]
        line = df["supertrend"].iloc[i]
        if d0 == 0 or d1 == 0 or line != line:
            return None
        if position is not None and position.is_open:
            # trail the stop on the supertrend line
            if position.qty > 0:
                position.stop_loss = max(position.stop_loss or line, line) if d1 == 1 else position.stop_loss
                if d1 == -1:
                    return self.exit(instrument, df, i, position, "Supertrend flipped down")
            else:
                position.stop_loss = min(position.stop_loss or line, line) if d1 == -1 else position.stop_loss
                if d1 == 1:
                    return self.exit(instrument, df, i, position, "Supertrend flipped up")
            return None
        if d0 == -1 and d1 == 1:
            return self.enter(instrument, df, i, Side.BUY, "Supertrend turned up", stop=line)
        if d0 == 1 and d1 == -1:
            return self.enter(instrument, df, i, Side.SELL, "Supertrend turned down", stop=line)
        return None
