"""Core data types shared by every agent."""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Segment(str, Enum):
    EQ = "EQ"     # equity cash
    FUT = "FUT"   # futures (index or stock)
    OPT = "OPT"   # options (index or stock)


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class Product(str, Enum):
    INTRADAY = "INTRADAY"   # MIS / I: squared off same day
    DELIVERY = "DELIVERY"   # CNC / D: equity delivery
    NRML = "NRML"           # F&O carry-forward


class Action(str, Enum):
    ENTER = "ENTER"
    EXIT = "EXIT"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class Instrument:
    symbol: str                      # e.g. RELIANCE, NIFTY-FUT, NIFTY24500CE
    exchange: str = "NSE"            # NSE / BSE / NFO
    segment: Segment = Segment.EQ
    lot_size: int = 1
    tick_size: float = 0.05
    instrument_key: str = ""         # Upstox key, e.g. "NSE_EQ|INE002A01018"
    product: Product = Product.INTRADAY
    option_type: Optional[str] = None  # CE / PE
    strike: Optional[float] = None
    expiry: Optional[str] = None

    def round_qty(self, qty: float) -> int:
        """Round a quantity down to a whole number of lots."""
        lots = int(qty // self.lot_size)
        return max(lots, 0) * self.lot_size

    def round_price(self, price: float) -> float:
        return round(round(price / self.tick_size) * self.tick_size, 2)


@dataclass
class Signal:
    instrument: Instrument
    action: Action
    side: Side                      # for EXIT: side of the closing order
    price: float                    # reference price (bar close)
    timestamp: datetime
    strategy: str
    reason: str = ""
    stop_loss: Optional[float] = None
    target: Optional[float] = None
    notes: list[str] = field(default_factory=list)


_order_ids = itertools.count(1)


@dataclass
class Order:
    instrument: Instrument
    side: Side
    qty: int
    timestamp: datetime
    product: Product
    order_type: str = "MARKET"
    limit_price: Optional[float] = None
    tag: str = ""
    stop_loss: Optional[float] = None
    target: Optional[float] = None
    id: int = field(default_factory=lambda: next(_order_ids))
    status: OrderStatus = OrderStatus.PENDING
    fill_price: Optional[float] = None
    fill_time: Optional[datetime] = None
    charges: float = 0.0
    reject_reason: str = ""


@dataclass
class Position:
    instrument: Instrument
    qty: int = 0                    # signed: + long, - short
    avg_price: float = 0.0
    stop_loss: Optional[float] = None
    target: Optional[float] = None
    entry_time: Optional[datetime] = None
    strategy: str = ""
    entry_charges: float = 0.0

    @property
    def is_open(self) -> bool:
        return self.qty != 0

    @property
    def side(self) -> Optional[Side]:
        if self.qty > 0:
            return Side.BUY
        if self.qty < 0:
            return Side.SELL
        return None

    def unrealized(self, price: float) -> float:
        return (price - self.avg_price) * self.qty


@dataclass
class Trade:
    """A completed round trip."""
    symbol: str
    strategy: str
    side: str
    qty: int
    entry_time: datetime
    entry_price: float
    exit_time: datetime
    exit_price: float
    gross_pnl: float
    charges: float
    exit_reason: str

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.charges
