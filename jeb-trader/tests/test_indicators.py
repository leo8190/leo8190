from __future__ import annotations

import math
import random

import pytest

from jeb import indicators as ind
from jeb.models import Candle


def candle(ts: int, o: float, h: float, low: float, c: float, v: float = 1.0) -> Candle:
    return Candle(timestamp=ts, open=o, high=h, low=low, close=c, volume=v)


def random_walk(n: int, seed: int = 7) -> list[float]:
    rng = random.Random(seed)
    price, out = 100.0, []
    for _ in range(n):
        price *= math.exp(rng.gauss(0, 0.01))
        out.append(price)
    return out


def random_candles(n: int, seed: int = 11) -> list[Candle]:
    rng = random.Random(seed)
    closes = random_walk(n, seed)
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        high = max(prev, c) * (1 + rng.random() * 0.005)
        low = min(prev, c) * (1 - rng.random() * 0.005)
        out.append(candle(i * 60_000, prev, high, low, c, rng.random() * 10))
        prev = c
    return out


# --------------------------------------------------------------------------- validation


@pytest.mark.parametrize("bad", [0, -1, 1.5, True, None])
@pytest.mark.parametrize(
    "fn",
    [
        lambda p: ind.sma([1.0, 2.0], p),
        lambda p: ind.ema([1.0, 2.0], p),
        lambda p: ind.rsi([1.0, 2.0], p),
        lambda p: ind.atr([candle(0, 1, 2, 1, 1)], p),
        lambda p: ind.bollinger([1.0, 2.0], p),
        lambda p: ind.log_return_volatility([1.0, 2.0], p),
        lambda p: ind.pct_change([1.0, 2.0], p),
        lambda p: ind.macd([1.0, 2.0], p, 26, 9),
        lambda p: ind.macd([1.0, 2.0], 12, 26, p),
    ],
)
def test_invalid_period_raises(fn, bad):
    with pytest.raises(ValueError):
        fn(bad)


def test_macd_fast_must_be_below_slow():
    with pytest.raises(ValueError):
        ind.macd([1.0] * 50, fast=26, slow=12)


def test_bollinger_negative_std_raises():
    with pytest.raises(ValueError):
        ind.bollinger([1.0] * 5, 3, -1.0)


def test_none_after_warmup_raises():
    with pytest.raises(ValueError):
        ind.sma([None, 1.0, None, 2.0], 1)


def test_empty_inputs_return_empty_lists():
    assert ind.sma([], 3) == []
    assert ind.ema([], 3) == []
    assert ind.rsi([], 14) == []
    assert ind.atr([], 14) == []
    assert ind.true_range([]) == []
    assert ind.macd([]) == ([], [], [])
    assert ind.bollinger([]) == ([], [], [])
    assert ind.log_return_volatility([]) == []
    assert ind.pct_change([], 1) == []


# --------------------------------------------------------------------------- averages


def test_sma_hand_example():
    assert ind.sma([1, 2, 3, 4, 5], 3) == [None, None, 2, 3, 4]
    assert ind.sma([1, 2, 3], 1) == [1, 2, 3]


def test_ema_hand_example():
    # alpha = 0.5, seed = SMA(1, 2, 3) = 2
    assert ind.ema([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]


def test_constant_series_averages_equal_the_constant():
    values = [42.5] * 30
    assert ind.sma(values, 10)[9:] == pytest.approx([42.5] * 21)
    assert ind.ema(values, 10)[9:] == pytest.approx([42.5] * 21)


def test_insufficient_history_gives_none():
    assert ind.sma([1.0, 2.0], 3) == [None, None]
    assert ind.ema([1.0, 2.0], 3) == [None, None]


def test_averages_skip_leading_none():
    assert ind.sma([None, None, 1, 2, 3], 2) == [None, None, None, 1.5, 2.5]
    assert ind.ema([None, 1, 2, 3, 4, 5], 3) == [None, None, None, 2.0, 3.0, 4.0]


# --------------------------------------------------------------------------- RSI


def test_rsi_monotonic_up_is_100_and_down_is_0():
    up = [float(i) for i in range(1, 40)]
    out = ind.rsi(up, 14)
    assert out[:14] == [None] * 14
    assert all(v == 100.0 for v in out[14:])
    down = list(reversed(up))
    assert all(v == 0.0 for v in ind.rsi(down, 14)[14:])


def test_rsi_flat_is_50():
    assert all(v == 50.0 for v in ind.rsi([10.0] * 30, 14)[14:])


def test_rsi_hand_example_wilder():
    # changes +1, -1, +1; period 2
    # idx2: gain 0.5, loss 0.5 -> 50; idx3: gain (0.5+1)/2, loss (0.5+0)/2 -> 75
    assert ind.rsi([1.0, 2.0, 1.0, 2.0], 2) == [None, None, 50.0, 75.0]


def test_rsi_bounds_on_random_walk():
    out = ind.rsi(random_walk(300), 14)
    assert all(0.0 <= v <= 100.0 for v in out[14:])


def test_rsi_insufficient_history():
    assert ind.rsi([1.0] * 14, 14) == [None] * 14


# --------------------------------------------------------------------------- ATR


def test_true_range_uses_previous_close():
    candles = [
        candle(0, 10, 11, 9, 10),  # first: high - low = 2
        candle(1, 13, 14, 12, 13),  # gap up: |14 - 10| = 4
        candle(2, 13, 13.5, 12.5, 13),  # inside: 1
    ]
    assert ind.true_range(candles) == [2.0, 4.0, 1.0]


def test_atr_constructed_series():
    candles = [
        candle(0, 10, 11, 9, 10),
        candle(1, 13, 14, 12, 13),
        candle(2, 13, 13.5, 12.5, 13),
    ]
    # seed = (2 + 4) / 2 = 3, then Wilder: (3 * 1 + 1) / 2 = 2
    assert ind.atr(candles, 2) == [None, 3.0, 2.0]


def test_atr_constant_range():
    candles = [candle(i, 100, 101, 99, 100) for i in range(30)]
    out = ind.atr(candles, 14)
    assert out[:13] == [None] * 13
    assert out[13:] == pytest.approx([2.0] * 17)


# --------------------------------------------------------------------------- MACD


def test_macd_warmup_and_sign_in_trends():
    up = [100.0 + i for i in range(60)]
    line, signal, hist = ind.macd(up)
    assert line[:25] == [None] * 25 and line[25] is not None
    assert signal[:33] == [None] * 33 and signal[33] is not None
    assert hist[:33] == [None] * 33 and hist[33] is not None
    assert all(v > 0 for v in line[25:])

    down = [200.0 - i for i in range(60)]
    line_d, _, _ = ind.macd(down)
    assert all(v < 0 for v in line_d[25:])


def test_macd_hist_positive_in_accelerating_uptrend():
    closes = [100.0 * 1.01**i for i in range(80)]
    line, signal, hist = ind.macd(closes)
    for m, s, h in zip(line[33:], signal[33:], hist[33:]):
        assert h == pytest.approx(m - s)
        assert h > 0


# --------------------------------------------------------------------------- Bollinger


def test_bollinger_constant_series_collapses_to_price():
    upper, middle, lower = ind.bollinger([25_000.0] * 30, 20, 2.0)
    assert upper[:19] == middle[:19] == lower[:19] == [None] * 19
    assert upper[19:] == middle[19:] == lower[19:] == [25_000.0] * 11
    assert ind.percent_b(25_000.0, upper[-1], lower[-1]) == 0.5


def test_bollinger_hand_example_population_stdev():
    upper, middle, lower = ind.bollinger([1.0, 2.0, 3.0], 3, 1.0)
    sd = math.sqrt(2.0 / 3.0)
    assert middle == [None, None, 2.0]
    assert upper[2] == pytest.approx(2.0 + sd)
    assert lower[2] == pytest.approx(2.0 - sd)


def test_percent_b():
    assert ind.percent_b(15.0, 20.0, 10.0) == 0.5
    assert ind.percent_b(20.0, 20.0, 10.0) == 1.0
    assert ind.percent_b(5.0, 20.0, 10.0) == -0.5


# --------------------------------------------------------------------------- volatility & returns


def test_volatility_hand_example():
    closes = [1.0, math.e, 1.0, math.e]  # log returns +1, -1, +1
    out = ind.log_return_volatility(closes, 2)
    assert out[:2] == [None, None]
    assert out[2] == pytest.approx(100.0)
    assert out[3] == pytest.approx(100.0)


def test_volatility_constant_is_zero_and_guards_bad_prices():
    assert ind.log_return_volatility([5.0] * 10, 3)[3:] == [0.0] * 7
    out = ind.log_return_volatility([1.0, 0.0, 1.0, 1.1, 1.2, 1.3], 2)
    assert out[2] is None and out[3] is None  # windows touching the zero price
    assert out[4] is not None


def test_pct_change_hand_example_and_guards():
    assert ind.pct_change([100.0, 110.0, 99.0], 1) == pytest.approx([None, 10.0, -10.0])
    assert ind.pct_change([100.0, 110.0, 99.0], 2) == [None, None, pytest.approx(-1.0)]
    assert ind.pct_change([0.0, 1.0, -1.0], 1) == [None, None, None]


def test_log_returns():
    out = ind.log_returns([1.0, math.e, math.e])
    assert out[0] is None
    assert out[1] == pytest.approx(1.0)
    assert out[2] == 0.0


# --------------------------------------------------------------------------- no look-ahead


def _series_fns():
    return {
        "sma": lambda cs: ind.sma([c.close for c in cs], 20),
        "ema": lambda cs: ind.ema([c.close for c in cs], 9),
        "rsi": lambda cs: ind.rsi([c.close for c in cs], 14),
        "tr": lambda cs: ind.true_range(cs),
        "atr": lambda cs: ind.atr(cs, 14),
        "macd": lambda cs: ind.macd([c.close for c in cs]),
        "bb": lambda cs: ind.bollinger([c.close for c in cs]),
        "vol": lambda cs: ind.log_return_volatility([c.close for c in cs], 20),
        "pct": lambda cs: ind.pct_change([c.close for c in cs], 12),
    }


def _flatten(result):
    if isinstance(result, tuple):
        return list(result)
    return [result]


@pytest.mark.parametrize("name", list(_series_fns()))
def test_no_look_ahead_and_alignment(name):
    fn = _series_fns()[name]
    candles = random_candles(120)
    full = _flatten(fn(candles))
    for k in (1, 5, 14, 15, 26, 34, 50, 90, 119):
        prefix = _flatten(fn(candles[:k]))
        for full_series, prefix_series in zip(full, prefix):
            assert len(prefix_series) == k
            assert prefix_series == full_series[:k]  # exact: future data changes nothing
    assert all(len(s) == len(candles) for s in full)
