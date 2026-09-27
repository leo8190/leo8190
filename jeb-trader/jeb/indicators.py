"""Technical indicators as pure functions over plain lists.

Rules shared by every function:
- No look-ahead: the value at index ``i`` only uses inputs at indexes ``<= i``,
  so appending data never changes earlier outputs.
- Outputs are aligned with the input (same length); ``None`` marks the warm-up
  where there is not enough history yet.
- Periods must be integers ``>= 1`` (``ValueError`` otherwise).
- Divisions by zero and non-positive prices yield ``None`` (or a documented
  neutral value) instead of raising or producing ``inf``/``nan``.
- ``sma`` and ``ema`` accept leading ``None`` values (the warm-up of another
  indicator, e.g. the MACD line) and skip them.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Optional

from .models import Candle

Series = list[Optional[float]]

# Relative band width under which Bollinger bands are considered collapsed.
_FLAT_REL_TOL = 1e-12


# --------------------------------------------------------------------------- helpers


def _check_period(period: int, name: str = "period") -> None:
    if isinstance(period, bool) or not isinstance(period, int) or period < 1:
        raise ValueError(f"{name} must be an integer >= 1, got {period!r}")


def _split_warmup(values: Sequence[Optional[float]]) -> tuple[int, list[float]]:
    """Return (index of the first non-None value, the values from there on)."""
    start = 0
    while start < len(values) and values[start] is None:
        start += 1
    tail = list(values[start:])
    if any(v is None for v in tail):
        raise ValueError("None is only allowed as a leading warm-up")
    return start, tail  # type: ignore[return-value]


def _mean(window: Sequence[float]) -> float:
    first = window[0]
    if all(v == first for v in window):
        return float(first)  # exact for constant windows
    return math.fsum(window) / len(window)


def _pstdev(window: Sequence[float]) -> float:
    """Population standard deviation (exactly 0.0 for constant windows)."""
    first = window[0]
    if all(v == first for v in window):
        return 0.0
    mu = math.fsum(window) / len(window)
    return math.sqrt(math.fsum((v - mu) ** 2 for v in window) / len(window))


def _pad(start: int, tail: list[Optional[float]]) -> Series:
    return [None] * start + tail


# --------------------------------------------------------------------------- averages


def sma(values: Sequence[Optional[float]], period: int) -> Series:
    """Simple moving average; first value at index ``period - 1`` (after warm-up)."""
    _check_period(period)
    start, data = _split_warmup(values)
    out: Series = [None] * len(data)
    for i in range(period - 1, len(data)):
        out[i] = _mean(data[i - period + 1 : i + 1])
    return _pad(start, out)


def ema(values: Sequence[Optional[float]], period: int) -> Series:
    """Exponential moving average, alpha = 2 / (period + 1).

    Seeded with the SMA of the first ``period`` values (index ``period - 1``).
    """
    _check_period(period)
    start, data = _split_warmup(values)
    out: Series = [None] * len(data)
    if len(data) < period:
        return _pad(start, out)
    alpha = 2.0 / (period + 1)
    prev = _mean(data[:period])
    out[period - 1] = prev
    for i in range(period, len(data)):
        prev = alpha * data[i] + (1.0 - alpha) * prev
        out[i] = prev
    return _pad(start, out)


# --------------------------------------------------------------------------- oscillators


def rsi(closes: Sequence[float], period: int = 14) -> Series:
    """Relative Strength Index (Wilder smoothing), 0..100.

    First value at index ``period`` (needs ``period`` price changes).
    Only gains -> 100, only losses -> 0, no movement at all -> 50.
    """
    _check_period(period)
    n = len(closes)
    out: Series = [None] * n
    if n <= period:
        return out
    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        change = closes[i] - closes[i - 1]
        gains[i] = change if change > 0 else 0.0
        losses[i] = -change if change < 0 else 0.0
    avg_gain = math.fsum(gains[1 : period + 1]) / period
    avg_loss = math.fsum(losses[1 : period + 1]) / period
    out[period] = _rsi_value(avg_gain, avg_loss)
    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    total = avg_gain + avg_loss
    if total <= 0:
        return 50.0
    return 100.0 * avg_gain / total  # == 100 - 100 / (1 + RS), without dividing by 0


def macd(
    closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[Series, Series, Series]:
    """MACD line (EMA fast - EMA slow), signal line (EMA of MACD) and histogram.

    The MACD line starts at index ``slow - 1``; signal and histogram at
    ``slow + signal - 2``.
    """
    _check_period(fast, "fast")
    _check_period(slow, "slow")
    _check_period(signal, "signal")
    if fast >= slow:
        raise ValueError(f"fast ({fast}) must be < slow ({slow})")
    fast_ema = ema(closes, fast)
    slow_ema = ema(closes, slow)
    line: Series = [
        f - s if f is not None and s is not None else None for f, s in zip(fast_ema, slow_ema)
    ]
    signal_line = ema(line, signal)
    hist: Series = [
        m - s if m is not None and s is not None else None for m, s in zip(line, signal_line)
    ]
    return line, signal_line, hist


# --------------------------------------------------------------------------- volatility


def true_range(candles: Sequence[Candle]) -> list[float]:
    """True range per candle; the first one has no previous close so it is high - low."""
    out: list[float] = []
    prev_close: Optional[float] = None
    for c in candles:
        hl = c.high - c.low
        if prev_close is None:
            out.append(hl)
        else:
            out.append(max(hl, abs(c.high - prev_close), abs(c.low - prev_close)))
        prev_close = c.close
    return out


def atr(candles: Sequence[Candle], period: int = 14) -> Series:
    """Average True Range with Wilder smoothing, in price units.

    Seeded with the mean of the first ``period`` true ranges (index ``period - 1``).
    """
    _check_period(period)
    tr = true_range(candles)
    out: Series = [None] * len(tr)
    if len(tr) < period:
        return out
    prev = math.fsum(tr[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(tr)):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out


def bollinger(
    closes: Sequence[float], period: int = 20, num_std: float = 2.0
) -> tuple[Series, Series, Series]:
    """Bollinger bands (upper, middle, lower) using the population stdev."""
    _check_period(period)
    if not math.isfinite(num_std) or num_std < 0:
        raise ValueError(f"num_std must be a finite number >= 0, got {num_std!r}")
    n = len(closes)
    upper: Series = [None] * n
    middle: Series = [None] * n
    lower: Series = [None] * n
    for i in range(period - 1, n):
        window = closes[i - period + 1 : i + 1]
        mid = _mean(window)
        width = num_std * _pstdev(window)
        middle[i] = mid
        upper[i] = mid + width
        lower[i] = mid - width
    return upper, middle, lower


def percent_b(close: float, upper: float, lower: float) -> float:
    """Bollinger %B = (close - lower) / (upper - lower).

    Collapsed bands (zero width) return 0.5: the price sits on the middle band.
    """
    width = upper - lower
    if width <= _FLAT_REL_TOL * max(abs(upper), abs(lower), 1.0):
        return 0.5
    return (close - lower) / width


def log_returns(closes: Sequence[float]) -> Series:
    """1-step log returns; index 0 and any step touching a price <= 0 are None."""
    out: Series = [None] * len(closes)
    for i in range(1, len(closes)):
        prev, cur = closes[i - 1], closes[i]
        if prev > 0 and cur > 0:
            out[i] = math.log(cur / prev)
    return out


def log_return_volatility(closes: Sequence[float], period: int = 20) -> Series:
    """Population stdev of the last ``period`` 1-step log returns, times 100.

    First value at index ``period``; windows containing an invalid return are None.
    """
    _check_period(period)
    rets = log_returns(closes)
    out: Series = [None] * len(closes)
    for i in range(period, len(closes)):
        window = rets[i - period + 1 : i + 1]
        if any(r is None for r in window):
            continue
        out[i] = _pstdev(window) * 100.0  # type: ignore[arg-type]
    return out


# --------------------------------------------------------------------------- returns


def pct_change(closes: Sequence[float], lag: int = 1) -> Series:
    """Percent change versus ``lag`` steps earlier (2.0 means +2 %).

    None during the first ``lag`` values or when either price is <= 0.
    """
    _check_period(lag, "lag")
    out: Series = [None] * len(closes)
    for i in range(lag, len(closes)):
        prev, cur = closes[i - lag], closes[i]
        if prev > 0 and cur > 0:
            out[i] = (cur / prev - 1.0) * 100.0
    return out
