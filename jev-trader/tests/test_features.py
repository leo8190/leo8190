from __future__ import annotations

import math

import pytest

from jev import features as ft
from jev import indicators as ind
from jev.models import Candle, IndicatorSet, MarketDataError, MarketSnapshot

STEP = 300_000  # 5m in ms
T0 = 1_700_000_000_000


def make_candles(closes, volumes=None, spread=0.5) -> list[Candle]:
    volumes = volumes or [1.0] * len(closes)
    out, prev = [], closes[0]
    for i, (c, v) in enumerate(zip(closes, volumes)):
        out.append(
            Candle(
                timestamp=T0 + i * STEP,
                open=prev,
                high=max(prev, c) + spread,
                low=min(prev, c) - spread,
                close=c,
                volume=v,
            )
        )
        prev = c
    return out


def wavy(n: int) -> list[float]:
    return [100.0 + 5 * math.sin(i / 3.0) + 0.1 * i for i in range(n)]


# --------------------------------------------------------------------------- compute_indicators


def test_warmup_constant():
    assert ft.WARMUP_CANDLES == 50


def test_empty_candles_give_all_none():
    assert ft.compute_indicators([]) == IndicatorSet()


def test_full_history_fills_every_field():
    ind_set = ft.compute_indicators(make_candles(wavy(ft.WARMUP_CANDLES)))
    missing = [k for k, v in ind_set.model_dump().items() if v is None]
    assert missing == []


def test_insufficient_history_gives_none():
    one = ft.compute_indicators(make_candles([100.0]))
    assert one.ema_fast is None and one.rsi is None and one.return_1_pct is None

    ind_set = ft.compute_indicators(make_candles(wavy(49)))
    assert ind_set.ema_trend is None  # needs 50
    assert ind_set.ema_slow is not None
    assert ind_set.macd_signal is not None  # needs 34

    ind_set = ft.compute_indicators(make_candles(wavy(33)))
    assert ind_set.macd is not None  # needs 26
    assert ind_set.macd_signal is None and ind_set.macd_hist is None

    ind_set = ft.compute_indicators(make_candles(wavy(20)))
    assert ind_set.volatility_pct is None  # needs 21 closes
    assert ind_set.bb_middle is not None and ind_set.volume_ratio is not None


def test_constant_series():
    candles = make_candles([200.0] * 60, spread=1.0)  # every candle: high 201, low 199
    s = ft.compute_indicators(candles)
    for value in (s.ema_fast, s.ema_slow, s.ema_trend, s.bb_middle):
        assert value == pytest.approx(200.0)
    assert s.bb_upper == s.bb_lower == 200.0
    assert s.bb_pct_b == 0.5
    assert s.rsi == 50.0
    assert s.macd == pytest.approx(0.0, abs=1e-9)
    assert s.atr == pytest.approx(2.0)
    assert s.atr_pct == pytest.approx(1.0)
    assert s.return_1_pct == 0.0 and s.return_12_pct == 0.0
    assert s.volatility_pct == 0.0
    assert s.volume_ratio == pytest.approx(1.0)


def test_uptrend_signals():
    closes = [100.0 * 1.005**i for i in range(80)]
    s = ft.compute_indicators(make_candles(closes))
    assert s.rsi == 100.0
    assert s.macd > 0 and s.macd_hist > 0
    assert s.ema_fast > s.ema_slow > s.ema_trend
    assert s.return_1_pct == pytest.approx(0.5)
    assert s.return_12_pct == pytest.approx((1.005**12 - 1) * 100)
    assert s.bb_pct_b > 0.5


def test_atr_pct_and_volume_ratio():
    volumes = [1.0] * 59 + [2.0]
    candles = make_candles(wavy(60), volumes)
    s = ft.compute_indicators(candles)
    assert s.atr_pct == pytest.approx(s.atr / candles[-1].close * 100)
    assert s.volume_ratio == pytest.approx(2.0 / (21.0 / 20.0))


def test_zero_volume_ratio_is_none():
    s = ft.compute_indicators(make_candles(wavy(30), [0.0] * 30))
    assert s.volume_ratio is None


def test_prefix_matches_full_series_no_look_ahead():
    candles = make_candles(wavy(120))
    closes = [c.close for c in candles]
    ema_full = ind.ema(closes, 9)
    rsi_full = ind.rsi(closes, 14)
    atr_full = ind.atr(candles, 14)
    for k in (10, 30, 50, 80, 120):
        s = ft.compute_indicators(candles[:k])
        assert s.ema_fast == ema_full[k - 1]
        assert s.rsi == rsi_full[k - 1]
        assert s.atr == atr_full[k - 1]
        # later candles cannot change a prefix result
        assert ft.compute_indicators(candles[:k]) == s


# --------------------------------------------------------------------------- build_snapshot


def test_build_snapshot_fields():
    candles = make_candles(wavy(60))
    snap = ft.build_snapshot("BTC/USDT", "5m", candles)
    assert isinstance(snap, MarketSnapshot)
    assert snap.symbol == "BTC/USDT" and snap.timeframe == "5m"
    assert snap.timestamp == candles[-1].timestamp
    assert snap.price == candles[-1].close
    assert snap.recent_closes == [c.close for c in candles[-20:]]
    assert snap.indicators == ft.compute_indicators(candles)


def test_build_snapshot_recent_window():
    candles = make_candles(wavy(10))
    assert ft.build_snapshot("X/Y", "1m", candles, recent=3).recent_closes == [
        c.close for c in candles[-3:]
    ]
    assert ft.build_snapshot("X/Y", "1m", candles, recent=0).recent_closes == []
    assert len(ft.build_snapshot("X/Y", "1m", candles, recent=50).recent_closes) == 10
    single = ft.build_snapshot("X/Y", "1m", candles[:1])
    assert single.recent_closes == [candles[0].close]


def _replace(candles: list[Candle], index: int, **changes) -> list[Candle]:
    out = list(candles)
    out[index] = candles[index].model_copy(update=changes)
    return out


def _invalid_cases():
    base = make_candles(wavy(5))
    return {
        "empty": [],
        "duplicate_ts": _replace(base, 2, timestamp=base[1].timestamp),
        "decreasing_ts": _replace(base, 3, timestamp=base[1].timestamp - 1),
        "nan_close": _replace(base, 4, close=float("nan")),
        "inf_high": _replace(base, 0, high=float("inf")),
        "zero_close": _replace(base, 1, close=0.0),
        "negative_open": _replace(base, 2, open=-1.0),
        "high_below_low": _replace(base, 2, high=base[2].low - 1),
        "negative_volume": _replace(base, 2, volume=-5.0),
        "nan_volume": _replace(base, 2, volume=float("nan")),
    }


@pytest.mark.parametrize("case", list(_invalid_cases()))
def test_build_snapshot_rejects_invalid_candles(case):
    candles = _invalid_cases()[case]
    with pytest.raises(ValueError) as exc_info:
        ft.build_snapshot("BTC/USDT", "5m", candles)
    assert isinstance(exc_info.value, MarketDataError)
    assert isinstance(exc_info.value, ft.InvalidCandlesError)


@pytest.mark.parametrize("case", [c for c in _invalid_cases() if c != "empty"])
def test_compute_indicators_rejects_invalid_candles(case):
    with pytest.raises(ft.InvalidCandlesError):
        ft.compute_indicators(_invalid_cases()[case])


@pytest.mark.parametrize("recent", [-1, 1.5, True])
def test_build_snapshot_rejects_bad_recent(recent):
    with pytest.raises(ValueError):
        ft.build_snapshot("BTC/USDT", "5m", make_candles(wavy(5)), recent=recent)
