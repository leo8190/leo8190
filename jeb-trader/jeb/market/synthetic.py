"""Deterministic synthetic market: GBM candles with bull / bear / sideways regimes.

Used for offline demos, paper trading without network and tests. The same seed
always yields the same candles, independent of the order methods are called in.

Replay model: an internal cursor splits the series. Candles ``[0, cursor)`` are
closed; candle ``cursor`` is "forming" and is never returned by
``fetch_candles``. The current price is its open (== the last closed close).
"""

from __future__ import annotations

import logging
import math
import random

from ..models import Candle, MarketDataError
from .timeframes import floor_to_timeframe, timeframe_to_ms

logger = logging.getLogger(__name__)

# name -> (drift per candle in units of the base candle sigma, volatility multiplier)
_REGIMES: dict[str, tuple[float, float]] = {
    "bull": (0.10, 0.9),
    "bear": (-0.12, 1.25),
    "sideways": (0.0, 0.7),
}
_REGIME_NAMES = ("bull", "bear", "sideways")
_REGIME_MIN_CANDLES = 80
_REGIME_MAX_CANDLES = 480
_MEAN_REVERSION = 0.03  # sideways: per-candle pull toward the regime's starting log price
_JUMP_PROB = 0.02  # probability that a candle's shock is amplified (fat tails)
_JUMP_SCALE = 2.5
_WICK_SCALE = 0.5  # wick size relative to the candle sigma
_VOLUME_NOISE = 0.35
_QUOTE_VOLUME_PER_MINUTE = 500_000.0
_YEAR_MS = 365 * 86_400_000


class SyntheticMarket:
    """Seeded synthetic market data source implementing ``MarketDataSource``."""

    def __init__(
        self,
        seed: int = 42,
        start_price: float = 60000.0,
        timeframe: str = "5m",
        start_ts: int = 1767225600000,
        annual_vol: float = 0.6,
        initial_history: int = 300,
    ) -> None:
        if not start_price > 0:
            raise ValueError(f"start_price must be > 0, got {start_price}")
        if not annual_vol > 0:
            raise ValueError(f"annual_vol must be > 0, got {annual_vol}")
        if initial_history < 0:
            raise ValueError(f"initial_history must be >= 0, got {initial_history}")
        self.seed = seed
        self.start_price = float(start_price)
        self.timeframe = timeframe
        self.tf_ms = timeframe_to_ms(timeframe)  # ValueError on a bad timeframe
        self.start_ts = floor_to_timeframe(start_ts, timeframe)
        self.annual_vol = float(annual_vol)
        self._sigma = self.annual_vol * math.sqrt(self.tf_ms / _YEAR_MS)
        self._base_volume = _QUOTE_VOLUME_PER_MINUTE * (self.tf_ms / 60_000) / self.start_price

        self._rng = random.Random(seed)
        self._candles: list[Candle] = []
        self._regimes: list[str] = []
        self._prev_close = self.start_price
        self._regime = self._rng.choice(_REGIME_NAMES)
        self._regime_left = self._rng.randint(_REGIME_MIN_CANDLES, _REGIME_MAX_CANDLES)
        self._anchor = math.log(self.start_price)
        self._cursor = initial_history

    # ------------------------------------------------------------------ public API

    @property
    def cursor(self) -> int:
        """Number of closed candles (index of the forming candle)."""
        return self._cursor

    def generate(self, n: int) -> list[Candle]:
        """Return the first ``n`` candles of the series (pure, deterministic)."""
        if n < 0:
            raise ValueError(f"n must be >= 0, got {n}")
        self._ensure(n)
        return self._candles[:n]

    def fetch_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        """Last ``limit`` closed candles strictly before the cursor, oldest first.

        ``symbol`` is ignored (the synthetic series stands in for any symbol).
        Raises MarketDataError if ``timeframe`` differs from this market's.
        """
        self._check_timeframe(timeframe)
        if limit <= 0:
            return []
        self._ensure(self._cursor)
        return self._candles[max(0, self._cursor - limit):self._cursor]

    def advance(self, n: int = 1) -> list[Candle]:
        """Close ``n`` more candles; returns the candles that just closed."""
        if n < 0:
            raise ValueError(f"n must be >= 0, got {n}")
        old = self._cursor
        self._cursor += n
        self._ensure(self._cursor)
        return self._candles[old:self._cursor]

    def last_price(self) -> float:
        """Current price: open of the forming candle (== close of the last closed one)."""
        self._ensure(self._cursor + 1)
        return self._candles[self._cursor].open

    def fetch_last_price(self, symbol: str) -> float:
        """Same as ``last_price``; mirrors ``CcxtMarket.fetch_last_price``."""
        return self.last_price()

    def now_ms(self) -> int:
        """Simulated wall clock: open time of the forming candle."""
        return self.start_ts + self._cursor * self.tf_ms

    def regime_at(self, index: int) -> str:
        """Regime ("bull", "bear" or "sideways") that generated candle ``index``."""
        if index < 0:
            raise ValueError(f"index must be >= 0, got {index}")
        self._ensure(index + 1)
        return self._regimes[index]

    # ------------------------------------------------------------------ internals

    def _check_timeframe(self, timeframe: str) -> None:
        try:
            requested = timeframe_to_ms(timeframe)
        except ValueError as exc:
            raise MarketDataError(str(exc)) from exc
        if requested != self.tf_ms:
            raise MarketDataError(
                f"SyntheticMarket generates {self.timeframe!r} candles, not {timeframe!r}"
            )

    def _ensure(self, n: int) -> None:
        while len(self._candles) < n:
            self._step()

    def _switch_regime(self) -> None:
        previous = self._regime
        self._regime = self._rng.choice([r for r in _REGIME_NAMES if r != previous])
        self._regime_left = self._rng.randint(_REGIME_MIN_CANDLES, _REGIME_MAX_CANDLES)
        self._anchor = math.log(self._prev_close)
        logger.debug("synthetic regime %s -> %s at candle %d", previous, self._regime, len(self._candles))

    def _step(self) -> None:
        if self._regime_left <= 0:
            self._switch_regime()
        drift_k, vol_mult = _REGIMES[self._regime]
        sigma = self._sigma * vol_mult
        rng = self._rng

        shock = rng.gauss(0.0, 1.0)
        if rng.random() < _JUMP_PROB:
            shock *= _JUMP_SCALE
        open_ = self._prev_close
        drift = drift_k * self._sigma
        if self._regime == "sideways":
            drift -= _MEAN_REVERSION * (math.log(open_) - self._anchor)
        close = open_ * math.exp(drift - 0.5 * sigma * sigma + sigma * shock)

        high = max(open_, close) * math.exp(abs(rng.gauss(0.0, _WICK_SCALE * sigma)))
        low = min(open_, close) * math.exp(-abs(rng.gauss(0.0, _WICK_SCALE * sigma)))
        volume = self._base_volume * math.exp(rng.gauss(0.0, _VOLUME_NOISE)) * (1.0 + abs(shock))

        index = len(self._candles)
        self._candles.append(Candle(
            timestamp=self.start_ts + index * self.tf_ms,
            open=open_, high=high, low=low, close=close, volume=volume,
        ))
        self._regimes.append(self._regime)
        self._prev_close = close
        self._regime_left -= 1
