"""Opening Range Breakout (intraday). Range = first N minutes after 09:15 IST."""
from __future__ import annotations

from datetime import time, timedelta, datetime

from ..models import Side
from .base import Strategy


class OpeningRangeBreakout(Strategy):
    name = "orb"

    def __init__(self, range_minutes: int = 15, reward_risk: float = 2.0,
                 last_entry: str = "14:30", max_range_pct: float = 1.5, **kw):
        super().__init__(**kw)
        self.range_minutes = range_minutes
        self.rr = reward_risk
        self.last_entry = time.fromisoformat(last_entry)
        self.max_range_pct = max_range_pct   # skip days with a huge opening range

    def prepare(self, df):
        out = df.copy()
        start = time(9, 15)
        end = (datetime.combine(datetime.today(), start) + timedelta(minutes=self.range_minutes)).time()
        t = out.index.time
        in_range = (t >= start) & (t < end)
        day = out.index.date
        hi = out["high"].where(in_range).groupby(day).transform("max")
        lo = out["low"].where(in_range).groupby(day).transform("min")
        # Range is only known once the window has closed.
        known = t >= end
        out["or_high"] = hi.where(known)
        out["or_low"] = lo.where(known)
        # One breakout per day: has today's range already been broken before this bar?
        brk = ((out["close"] > out["or_high"]) | (out["close"] < out["or_low"])).astype(int)
        out["or_breaks_before"] = brk.groupby(day).cumsum() - brk
        return out

    def on_bar(self, instrument, df, i, position):
        if position is not None and position.is_open:
            return None   # exits handled by stop / target / intraday square-off
        hi, lo = df["or_high"].iloc[i], df["or_low"].iloc[i]
        if hi != hi or lo != lo:   # NaN
            return None
        if df.index[i].time() > self.last_entry or df["or_breaks_before"].iloc[i] > 0:
            return None
        close = df["close"].iloc[i]
        if (hi - lo) / close * 100 > self.max_range_pct:
            return None
        if close > hi:
            risk = close - lo
            return self.enter(instrument, df, i, Side.BUY, f"Broke opening range high {hi:.2f}",
                              stop=lo, target=close + self.rr * risk)
        if close < lo:
            risk = hi - close
            return self.enter(instrument, df, i, Side.SELL, f"Broke opening range low {lo:.2f}",
                              stop=hi, target=close - self.rr * risk)
        return None
