"""Market data source contract."""

from __future__ import annotations

from typing import Protocol

from ..models import Candle


class MarketDataSource(Protocol):
    def fetch_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        """Return up to ``limit`` CLOSED candles, oldest first, no duplicates.

        The still-forming candle must never be included.
        """
        ...
