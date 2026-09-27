"""Timeframe arithmetic and small candle-series helpers shared by market sources.

Timeframes use ccxt notation: ``<amount><unit>`` with unit ``s`` (seconds),
``m`` (minutes), ``h`` (hours), ``d`` (days) or ``w`` (weeks), e.g. "30s",
"5m", "4h", "1w". Variable-length units ("1M" month, "1y" year) are rejected.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable

from ..models import Candle

_UNIT_MS: dict[str, int] = {
    "s": 1_000,
    "m": 60_000,
    "h": 3_600_000,
    "d": 86_400_000,
    "w": 604_800_000,
}
_TIMEFRAME_RE = re.compile(r"([1-9][0-9]*)([smhdw])")
# 1970-01-01 was a Thursday; exchanges start weekly candles on Monday 00:00 UTC.
_WEEK_OFFSET_MS = 4 * _UNIT_MS["d"]


def timeframe_to_ms(tf: str) -> int:
    """Return the length of timeframe ``tf`` in milliseconds.

    Raises ValueError for anything that is not ``<positive int><s|m|h|d|w>``.
    """
    if not isinstance(tf, str):
        raise ValueError(f"timeframe must be a string like '5m', got {tf!r}")
    match = _TIMEFRAME_RE.fullmatch(tf)
    if match is None:
        if tf[-1:] in ("M", "y"):
            raise ValueError(f"variable-length timeframe {tf!r} is not supported (use s/m/h/d/w)")
        raise ValueError(f"invalid timeframe {tf!r}: expected e.g. '30s', '5m', '1h', '1d', '1w'")
    amount, unit = match.groups()
    return int(amount) * _UNIT_MS[unit]


def is_valid_timeframe(tf: str) -> bool:
    """True if ``tf`` is a supported timeframe string."""
    try:
        timeframe_to_ms(tf)
    except ValueError:
        return False
    return True


def floor_to_timeframe(ts_ms: int, tf: str) -> int:
    """Open time (ms) of the ``tf`` candle containing ``ts_ms``.

    Grids are aligned to the Unix epoch (UTC), except weekly timeframes, which are
    aligned to Monday 00:00 UTC like exchange weekly candles.
    """
    step = timeframe_to_ms(tf)
    ts = int(ts_ms)
    offset = _WEEK_OFFSET_MS if tf.endswith("w") else 0
    return ts - ((ts - offset) % step)


def make_candle(
    timestamp: int | float,
    open: float,  # noqa: A002 - mirrors Candle field names
    high: float,
    low: float,
    close: float,
    volume: float,
) -> Candle:
    """Build a Candle after validating it; raises ValueError describing the problem.

    Rules: integral timestamp; finite numbers; prices > 0; volume >= 0;
    low <= min(open, close) and max(open, close) <= high.
    """
    ts = _as_timestamp(timestamp)
    o, h, lo, c, v = (_as_number(name, value) for name, value in (
        ("open", open), ("high", high), ("low", low), ("close", close), ("volume", volume)
    ))
    if min(o, h, lo, c) <= 0:
        raise ValueError(f"prices must be > 0 (open={o}, high={h}, low={lo}, close={c})")
    if v < 0:
        raise ValueError(f"volume must be >= 0, got {v}")
    if lo > min(o, c) or h < max(o, c):
        raise ValueError(f"inconsistent OHLC: open={o}, high={h}, low={lo}, close={c}")
    return Candle(timestamp=ts, open=o, high=h, low=lo, close=c, volume=v)


def dedupe_candles(candles: Iterable[Candle]) -> list[Candle]:
    """Sort by timestamp and drop duplicates; the LAST occurrence of a timestamp wins."""
    by_ts: dict[int, Candle] = {}
    for candle in candles:
        by_ts[candle.timestamp] = candle
    return [by_ts[ts] for ts in sorted(by_ts)]


def closed_candles(candles: Iterable[Candle], tf: str, now_ms: int) -> list[Candle]:
    """Keep only candles that are closed at ``now_ms`` (open + timeframe <= now)."""
    step = timeframe_to_ms(tf)
    return [c for c in candles if c.timestamp + step <= now_ms]


def _as_timestamp(value: int | float) -> int:
    if isinstance(value, bool):
        raise ValueError(f"timestamp must be an integer (ms), got {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    raise ValueError(f"timestamp must be an integer (ms), got {value!r}")


def _as_number(name: str, value: float) -> float:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{name} must be a number, got {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number, got {value!r}") from None
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite, got {value!r}")
    return number
