"""Broker contract (paper or live)."""

from __future__ import annotations

from typing import Protocol

from ..models import Balances, Fill, OrderRequest


class Broker(Protocol):
    def balances(self) -> Balances:
        """Free quote cash and free base quantity for the traded symbol."""
        ...

    def execute(self, order: OrderRequest, market_price: float, timestamp: int) -> Fill:
        """Execute a market order.

        Raises InsufficientFundsError or OrderRejectedError; never returns a partial
        fake fill. ``market_price`` is the best available current price (paper uses it
        as the fill base; live may ignore it).
        """
        ...
