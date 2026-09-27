"""Turn closed candles into an ``IndicatorSet`` and a ``MarketSnapshot``.

Indicators with a warm-up (EMA, Wilder RSI/ATR, MACD) depend on where the
history starts, so live trading and backtests should feed windows of the same
length (e.g. ``Settings.history_candles``) to get identical values.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from typing import Optional

from . import indicators as ind
from .models import Candle, IndicatorSet, MarketDataError, MarketSnapshot

logger = logging.getLogger(__name__)

EMA_FAST = 9
EMA_SLOW = 21
EMA_TREND = 50
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
ATR_PERIOD = 14
BB_PERIOD = 20
BB_STD = 2.0
VOLATILITY_PERIOD = 20
VOLUME_PERIOD = 20
RETURN_LONG_LAG = 12
RECENT_CLOSES = 20

# Candles needed for every field of the IndicatorSet to be non-None (EMA(50) is the
# longest warm-up; MACD signal needs 34, volatility 21, RSI 15).
WARMUP_CANDLES = max(
    EMA_TREND,
    MACD_SLOW + MACD_SIGNAL - 1,
    RSI_PERIOD + 1,
    VOLATILITY_PERIOD + 1,
    RETURN_LONG_LAG + 1,
    BB_PERIOD,
    ATR_PERIOD,
    VOLUME_PERIOD,
)


class InvalidCandlesError(MarketDataError, ValueError):
    """Candles are empty, unordered or contain invalid prices/volumes."""


def validate_candles(candles: Sequence[Candle]) -> None:
    """Raise ``InvalidCandlesError`` unless candles are usable for indicators.

    Requires: non-empty, strictly increasing timestamps, finite positive OHLC,
    high >= low, and finite non-negative volume.
    """
    if not candles:
        raise InvalidCandlesError("no candles")
    prev_ts: Optional[int] = None
    for i, c in enumerate(candles):
        if prev_ts is not None and c.timestamp <= prev_ts:
            raise InvalidCandlesError(
                f"timestamps must be strictly increasing (index {i}: {c.timestamp} <= {prev_ts})"
            )
        prev_ts = c.timestamp
        _validate_candle(i, c)


def _validate_candle(i: int, c: Candle) -> None:
    for name in ("open", "high", "low", "close"):
        value = getattr(c, name)
        if not math.isfinite(value) or value <= 0:
            raise InvalidCandlesError(f"candle {i} ({c.timestamp}): {name}={value!r} must be finite and > 0")
    if c.high < c.low:
        raise InvalidCandlesError(f"candle {i} ({c.timestamp}): high {c.high} < low {c.low}")
    if not math.isfinite(c.volume) or c.volume < 0:
        raise InvalidCandlesError(f"candle {i} ({c.timestamp}): volume={c.volume!r} must be finite and >= 0")


def _last(series: Sequence[Optional[float]]) -> Optional[float]:
    return series[-1] if series else None


def compute_indicators(candles: Sequence[Candle]) -> IndicatorSet:
    """Latest indicator values over closed candles (oldest first).

    Fields lacking history are None; an empty list gives an all-None set.
    Raises ``InvalidCandlesError`` on malformed candles.
    """
    if not candles:
        return IndicatorSet()
    validate_candles(candles)
    return _compute(candles)


def _compute(candles: Sequence[Candle]) -> IndicatorSet:
    closes = [c.close for c in candles]
    volumes = [c.volume for c in candles]
    close = closes[-1]

    macd_line, macd_signal, macd_hist = ind.macd(closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
    bb_upper, bb_middle, bb_lower = ind.bollinger(closes, BB_PERIOD, BB_STD)
    atr = _last(ind.atr(candles, ATR_PERIOD))
    upper, middle, lower = _last(bb_upper), _last(bb_middle), _last(bb_lower)

    return IndicatorSet(
        ema_fast=_last(ind.ema(closes, EMA_FAST)),
        ema_slow=_last(ind.ema(closes, EMA_SLOW)),
        ema_trend=_last(ind.ema(closes, EMA_TREND)),
        rsi=_last(ind.rsi(closes, RSI_PERIOD)),
        macd=_last(macd_line),
        macd_signal=_last(macd_signal),
        macd_hist=_last(macd_hist),
        atr=atr,
        atr_pct=atr / close * 100.0 if atr is not None and close > 0 else None,
        bb_upper=upper,
        bb_middle=middle,
        bb_lower=lower,
        bb_pct_b=_pct_b(close, upper, lower),
        return_1_pct=_last(ind.pct_change(closes, 1)),
        return_12_pct=_last(ind.pct_change(closes, RETURN_LONG_LAG)),
        volatility_pct=_last(ind.log_return_volatility(closes, VOLATILITY_PERIOD)),
        volume_ratio=_volume_ratio(volumes),
    )


def _pct_b(close: float, upper: Optional[float], lower: Optional[float]) -> Optional[float]:
    if upper is None or lower is None:
        return None
    return ind.percent_b(close, upper, lower)


def _volume_ratio(volumes: list[float]) -> Optional[float]:
    avg = _last(ind.sma(volumes, VOLUME_PERIOD))
    if avg is None or avg <= 0:
        return None
    return volumes[-1] / avg


def build_snapshot(
    symbol: str, timeframe: str, candles: Sequence[Candle], recent: int = RECENT_CLOSES
) -> MarketSnapshot:
    """Build the ``MarketSnapshot`` for a decision on the last CLOSED candle.

    Raises ``InvalidCandlesError`` (a ``ValueError`` and ``MarketDataError``) when
    candles are empty, unordered or hold invalid prices, or when ``recent`` < 0.
    """
    if isinstance(recent, bool) or not isinstance(recent, int) or recent < 0:
        raise InvalidCandlesError(f"recent must be an integer >= 0, got {recent!r}")
    validate_candles(candles)
    last = candles[-1]
    if len(candles) < WARMUP_CANDLES:
        logger.debug("%s %s: %d candles < warm-up %d, some indicators are None",
                     symbol, timeframe, len(candles), WARMUP_CANDLES)
    return MarketSnapshot(
        symbol=symbol,
        timeframe=timeframe,
        timestamp=last.timestamp,
        price=last.close,
        indicators=_compute(candles),
        recent_closes=[c.close for c in candles[-recent:]] if recent else [],
    )
