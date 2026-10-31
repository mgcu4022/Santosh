"""Strategy interface.

A strategy sees completed bars only. `prepare` adds indicator columns once;
`on_bar` is then called for each bar index `i` and may return a Signal.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

import pandas as pd

from ..models import Action, Instrument, Position, Side, Signal


class Strategy(ABC):
    name: str = "base"

    def __init__(self, allow_short: bool = True, **params):
        self.allow_short = allow_short
        self.params = params

    @abstractmethod
    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        """Return a copy of df with indicator columns added."""

    @abstractmethod
    def on_bar(self, instrument: Instrument, df: pd.DataFrame, i: int,
               position: Optional[Position]) -> Optional[Signal]:
        """Decide on bar i (already closed). Must not read rows after i."""

    # helpers
    def _signal(self, instrument, df, i, action, side, reason, stop=None, target=None):
        return Signal(instrument=instrument, action=action, side=side,
                      price=float(df["close"].iloc[i]), timestamp=df.index[i].to_pydatetime(),
                      strategy=self.name, reason=reason, stop_loss=stop, target=target)

    def enter(self, instrument, df, i, side, reason, stop=None, target=None):
        if side is Side.SELL and not self.allow_short:
            return None
        return self._signal(instrument, df, i, Action.ENTER, side, reason, stop, target)

    def exit(self, instrument, df, i, position: Position, reason):
        return self._signal(instrument, df, i, Action.EXIT, position.side.opposite(), reason)
