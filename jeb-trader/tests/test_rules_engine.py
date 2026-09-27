from __future__ import annotations

import pytest

from jeb.brain.rules_engine import RulesDecisionEngine, RulesParams, entry_confidence
from jeb.models import Action, IndicatorSet, MarketSnapshot, PortfolioView

BULLISH = dict(ema_fast=101.0, ema_slow=100.0, ema_trend=98.0, macd_hist=0.2, rsi=55.0, atr_pct=0.8)


def snap(price: float = 102.0, **indicators) -> MarketSnapshot:
    return MarketSnapshot(
        symbol="BTC/USDT",
        timeframe="5m",
        timestamp=1_700_000_000_000,
        price=price,
        indicators=IndicatorSet(**indicators),
    )


def bullish(price: float = 102.0, **overrides) -> MarketSnapshot:
    return snap(price, **{**BULLISH, **overrides})


FLAT = PortfolioView(quote_currency="USDT", base_currency="BTC", cash=1000.0, base_qty=0.0, equity=1000.0)
HOLDING = PortfolioView(
    quote_currency="USDT",
    base_currency="BTC",
    cash=0.0,
    base_qty=1.0,
    avg_entry_price=100.0,
    equity=102.0,
    position_value=102.0,
)


# ----------------------------------------------------------------------------- flat: entries


def test_bullish_setup_buys():
    d = RulesDecisionEngine().decide(bullish(), FLAT)
    assert d.action is Action.BUY
    assert d.source == "rules"
    assert d.size_pct == 1.0
    assert 0.6 <= d.confidence <= 0.9
    assert d.stop_loss_pct == pytest.approx(1.6)  # 2.0 * 0.8
    assert d.take_profit_pct == pytest.approx(3.2)  # 2.0 * stop
    for name in ("ema_fast>ema_slow", "price>ema_trend", "macd_hist>0", "rsi=55.0"):
        assert name in d.reasoning


def test_confidence_formula_bounds():
    strong = IndicatorSet(ema_fast=101.0, ema_slow=100.0, macd_hist=0.5, atr_pct=1.0)
    # spread 1% / 1 ATR -> 1 ; macd 0.5% / 0.5 ATR -> 1  => 0.9
    assert entry_confidence(strong, 100.0) == pytest.approx(0.9)
    weak = IndicatorSet(ema_fast=100.1, ema_slow=100.0, macd_hist=0.05, atr_pct=1.0)
    # spread 0.1, macd 0.1 => strength 0.1 => 0.63
    assert entry_confidence(weak, 100.0) == pytest.approx(0.63)
    huge = IndicatorSet(ema_fast=150.0, ema_slow=100.0, macd_hist=30.0, atr_pct=0.1)
    assert entry_confidence(huge, 100.0) == pytest.approx(0.9)


def test_confidence_without_atr_uses_default_scale():
    ind = IndicatorSet(ema_fast=100.5, ema_slow=100.0, macd_hist=0.25)
    # scale 1.0: spread 0.5 -> 0.5 ; macd 0.25% / 0.5 -> 0.5 => 0.6 + 0.3 * 0.5
    assert entry_confidence(ind, 100.0) == pytest.approx(0.75)


@pytest.mark.parametrize(
    "atr_pct, stop",
    [(0.1, 0.5), (0.8, 1.6), (3.0, 6.0), (10.0, 8.0), (None, 2.0)],
)
def test_stop_is_atr_based_and_clamped(atr_pct, stop):
    d = RulesDecisionEngine().decide(bullish(atr_pct=atr_pct), FLAT)
    assert d.action is Action.BUY
    assert d.stop_loss_pct == pytest.approx(stop)
    assert d.take_profit_pct == pytest.approx(2.0 * stop)


@pytest.mark.parametrize(
    "overrides, price, reason",
    [
        ({"ema_fast": 99.0}, 102.0, "ema_fast<=ema_slow"),
        ({"ema_trend": 103.0}, 102.0, "price<=ema_trend"),
        ({"macd_hist": -0.1}, 102.0, "macd_hist<=0"),
        ({"macd_hist": 0.0}, 102.0, "macd_hist<=0"),
        ({"rsi": 44.9}, 102.0, "outside [45,70]"),
        ({"rsi": 70.1}, 102.0, "outside [45,70]"),
    ],
)
def test_each_failed_entry_condition_holds(overrides, price, reason):
    d = RulesDecisionEngine().decide(bullish(price, **overrides), FLAT)
    assert d.action is Action.HOLD
    assert d.source == "rules"
    assert reason in d.reasoning


@pytest.mark.parametrize("rsi", [45.0, 70.0])
def test_rsi_band_is_inclusive(rsi):
    assert RulesDecisionEngine().decide(bullish(rsi=rsi), FLAT).action is Action.BUY


def test_missing_indicators_hold_when_flat():
    d = RulesDecisionEngine().decide(bullish(rsi=None), FLAT)
    assert d.action is Action.HOLD
    assert d.source == "rules"
    assert "missing indicators: rsi" in d.reasoning
    assert RulesDecisionEngine().decide(snap(), FLAT).action is Action.HOLD


def test_custom_params():
    params = RulesParams(rsi_buy_min=60, rsi_buy_max=65, atr_stop_mult=3.0, reward_risk=1.5)
    engine = RulesDecisionEngine(params)
    assert engine.decide(bullish(rsi=55.0), FLAT).action is Action.HOLD
    d = engine.decide(bullish(rsi=62.0), FLAT)
    assert d.action is Action.BUY
    assert d.stop_loss_pct == pytest.approx(2.4)
    assert d.take_profit_pct == pytest.approx(3.6)


# ----------------------------------------------------------------------------- holding: exits


def test_holding_with_trend_intact_holds():
    d = RulesDecisionEngine().decide(bullish(), HOLDING)
    assert d.action is Action.HOLD
    assert d.source == "rules"
    assert "no exit signal" in d.reasoning


@pytest.mark.parametrize(
    "overrides, price, trigger",
    [
        ({"ema_fast": 99.5}, 102.0, "ema_fast<ema_slow"),
        ({"rsi": 80.0}, 102.0, "rsi=80.0>78"),
        ({"macd_hist": -0.1}, 99.0, "macd_hist<0 and price<ema_slow"),
    ],
)
def test_exit_triggers_sell(overrides, price, trigger):
    d = RulesDecisionEngine().decide(bullish(price, **overrides), HOLDING)
    assert d.action is Action.SELL
    assert d.size_pct == 1.0
    assert d.confidence == pytest.approx(0.8)
    assert d.source == "rules"
    assert trigger in d.reasoning
    assert d.stop_loss_pct is None


def test_macd_negative_above_ema_slow_keeps_position():
    d = RulesDecisionEngine().decide(bullish(102.0, macd_hist=-0.1), HOLDING)
    assert d.action is Action.HOLD


def test_multiple_exit_triggers_are_all_named():
    d = RulesDecisionEngine().decide(bullish(99.0, ema_fast=99.5, macd_hist=-0.2, rsi=79.0), HOLDING)
    assert d.action is Action.SELL
    assert "ema_fast<ema_slow" in d.reasoning
    assert "rsi=79.0>78" in d.reasoning
    assert "macd_hist<0 and price<ema_slow" in d.reasoning


def test_holding_all_missing_indicators_holds():
    d = RulesDecisionEngine().decide(snap(), HOLDING)
    assert d.action is Action.HOLD
    assert "missing indicators" in d.reasoning


def test_exit_not_blocked_by_unrelated_missing_indicator():
    d = RulesDecisionEngine().decide(snap(102.0, ema_fast=99.0, ema_slow=100.0), HOLDING)
    assert d.action is Action.SELL


def test_never_raises():
    # price 0 passes "price > ema_trend" against a negative trend EMA -> division by zero inside
    d = RulesDecisionEngine().decide(snap(0.0, **{**BULLISH, "ema_trend": -1.0}), FLAT)
    assert d.action is Action.HOLD
    assert d.source == "fallback"


def test_deterministic():
    engine = RulesDecisionEngine()
    assert engine.decide(bullish(), FLAT) == engine.decide(bullish(), FLAT)
