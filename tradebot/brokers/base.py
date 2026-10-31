"""Broker interface. Paper trading implements it today; a live Upstox or
Kotak Neo adapter can implement the same methods later without touching
strategies or risk code."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from ..models import Order, Position


class Broker(ABC):
    @abstractmethod
    def place_order(self, order: Order) -> Order: ...

    @abstractmethod
    def cancel_all(self, symbol: Optional[str] = None) -> None: ...

    @abstractmethod
    def get_position(self, symbol: str) -> Optional[Position]: ...

    @abstractmethod
    def open_positions(self) -> list[Position]: ...

    @abstractmethod
    def equity(self) -> float: ...
