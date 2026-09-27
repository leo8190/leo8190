"""Tests for jeb.risk: protective exits, kill switch, BUY/SELL evaluation and sizing."""

from __future__ import annotations

import math
from dataclasses import replace

import pytest

from jeb.config import RiskConfig
from jeb.models import (
    Action,
    Balances,
    ConfigError,
    Decision,
    Fill,
    IndicatorSet,
    MarketSnapshot,
    PortfolioView,
    Side,
)
from jeb.portfolio import DAY_MS, Portfolio
from jeb.risk import RiskManager, _parse_timeframe, timeframe_ms

T0 = 1767225600000  # 2026-01-01T00:00:00Z
M5 = 300_000
SYMBOL = "BTC/USDT"
CFG = RiskConfig()  # risk 1%, max position 25%, min confidence 0.6, cooldown 3, min notional 10


def rm(config: RiskConfig = CFG, fee: float = 0.1, slip: float = 0.05) -> RiskManager:
    return RiskManager(config, fee_pct=fee, slippage_pct=slip)


def portfolio() -> Portfolio:
    return Portfolio(SYMBOL, "USDT", "BTC")


def snap(price: float = 100.0, ts: int = T0, timeframe: str = "5m", symbol: str = SYMBOL) -> MarketSnapshot:
    return MarketSnapshot(symbol=symbol, timeframe=timeframe, timestamp=ts, price=price, indicators=IndicatorSet())


def view(cash: float = 1000.0, base: float = 0.0, price: float = 100.0, equity: float | None = None) -> PortfolioView:
    value = base * price
    return PortfolioView(
        quote_currency="USDT",
        base_currency="BTC",
        cash=cash,
        base_qty=base,
        equity=cash + value if equity is None else equity,
        position_value=value,
    )


def buy_decision(**kwargs) -> Decision:
    base = dict(action=Action.BUY, confidence=0.8, size_pct=1.0, stop_loss_pct=2.0, take_profit_pct=4.0,
                reasoning="trend up", source="rules")
    return Decision(**(base | kwargs))


def sell_decision(**kwargs) -> Decision:
    base = dict(action=Action.SELL, confidence=0.8, size_pct=1.0, reasoning="exit", source="claude")
    return Decision(**(base | kwargs))


def holding(qty: float = 1.0, price: float = 100.0, ts: int = T0, stop: float | None = None,
            tp: float | None = None) -> Portfolio:
    p = portfolio()
    p.apply_fill(
        Fill(order_id="1", symbol=SYMBOL, side=Side.BUY, quantity=qty, price=price, fee=0.0, timestamp=ts),
        stop_loss=stop, take_profit=tp,
    )
    return p


def exit_position(p: Portfolio, price: float, ts: int) -> None:
    p.apply_fill(Fill(order_id="2", symbol=SYMBOL, side=Side.SELL, quantity=p.qty, price=price, fee=0.0,
                      timestamp=ts), reason="test")


# ---------------------------------------------------------------- construction and helpers


@pytest.mark.parametrize("fee, slip", [(-0.1, 0.0), (0.1, -0.01), (math.nan, 0.0), (0.1, math.inf)])
def test_constructor_rejects_bad_costs(fee, slip):
    with pytest.raises(ConfigError):
        RiskManager(CFG, fee_pct=fee, slippage_pct=slip)


def test_constructor_rejects_unusable_stop_bounds():
    with pytest.raises(ConfigError):
        RiskManager(replace(CFG, min_stop_pct=0.0), fee_pct=0.0)
    with pytest.raises(ConfigError):
        RiskManager(replace(CFG, min_stop_pct=5.0, max_stop_pct=1.0), fee_pct=0.0)


def test_kill_switch_rejects_non_finite_equity():
    p = portfolio()
    p.mark(T0, 1000.0)
    with pytest.raises(ValueError):
        rm().update_kill_switch(p, math.nan)


def test_timeframe_helpers():
    assert timeframe_ms("5m") == 300_000
    assert _parse_timeframe("1h") == 3_600_000
    assert _parse_timeframe("2d") == 2 * 86_400_000
    with pytest.raises(ValueError):
        _parse_timeframe("1M")
    with pytest.raises(ValueError):
        timeframe_ms("bogus")


# ---------------------------------------------------------------- protective exits


def test_protective_exit_flat_returns_none():
    assert rm().check_protective_exit(portfolio(), low=1.0, high=1e9) is None


def test_protective_exit_stop_and_take_profit():
    p = holding(stop=98.0, tp=104.0)
    r = rm()
    assert r.check_protective_exit(p, low=99.0, high=103.0) is None
    assert r.check_protective_exit(p, low=98.0, high=101.0) == ("stop_loss", 98.0)  # touch counts
    assert r.check_protective_exit(p, low=97.0, high=101.0) == ("stop_loss", 98.0)
    assert r.check_protective_exit(p, low=99.5, high=104.0) == ("take_profit", 104.0)
    assert r.check_protective_exit(p, low=99.5, high=110.0) == ("take_profit", 104.0)


def test_protective_exit_both_touched_prefers_stop():
    p = holding(stop=98.0, tp=104.0)
    assert rm().check_protective_exit(p, low=97.0, high=105.0) == ("stop_loss", 98.0)


def test_protective_exit_gap_below_stop_uses_open():
    p = holding(stop=98.0, tp=104.0)
    r = rm()
    assert r.check_protective_exit(p, low=94.0, high=96.0, open_price=95.0) == ("stop_loss", 95.0)
    assert r.check_protective_exit(p, low=97.0, high=100.0, open_price=99.0) == ("stop_loss", 98.0)
    # a gap above the take-profit is not credited (conservative)
    assert r.check_protective_exit(p, low=106.0, high=108.0, open_price=107.0) == ("take_profit", 104.0)


def test_protective_exit_without_levels_or_bad_data():
    r = rm()
    assert r.check_protective_exit(holding(), low=1.0, high=1e9) is None
    p = holding(stop=98.0, tp=104.0)
    assert r.check_protective_exit(p, low=math.nan, high=101.0) is None


# ---------------------------------------------------------------- kill switch


def test_daily_loss_kill_switch_trips_and_clears_next_day():
    r, p = rm(), portfolio()
    p.mark(T0, 1000.0)
    assert r.update_kill_switch(p, 971.0) is None  # 2.9 %
    assert r.update_kill_switch(p, 970.0) == "daily_loss"  # 3.0 % -> trips at the limit
    assert p.halted_reason == "daily_loss" and p.halted_day == p.current_day
    assert r.update_kill_switch(p, 1000.0) == "daily_loss"  # stays for the rest of the day

    verdict = r.evaluate(buy_decision(), view(), snap(ts=T0 + M5), p)
    assert not verdict.approved
    assert any("halted" in reason for reason in verdict.reasons)

    p.mark(T0 + DAY_MS, 970.0)  # new UTC day
    assert p.halted_reason is None
    assert r.update_kill_switch(p, 970.0) is None
    assert r.evaluate(buy_decision(), view(cash=970.0), snap(ts=T0 + DAY_MS), p).approved


def test_daily_halt_is_ignored_by_buy_checks_on_a_new_day_before_mark():
    r, p = rm(), portfolio()
    p.mark(T0, 1000.0)
    r.update_kill_switch(p, 960.0)
    assert r.evaluate(buy_decision(), view(), snap(ts=T0 + DAY_MS), p).approved


def test_drawdown_kill_switch_persists_until_reset():
    r, p = rm(), portfolio()
    p.mark(T0, 1000.0)
    p.mark(T0 + M5, 1100.0)  # peak
    p.mark(T0 + DAY_MS, 1000.0)  # new day starts at 1000
    assert r.update_kill_switch(p, 991.0) is None  # dd 9.9 %, daily 0.9 %
    assert r.update_kill_switch(p, 990.0) == "max_drawdown"  # dd 10 % from 1100
    p.mark(T0 + 2 * DAY_MS, 1200.0)
    assert p.halted_reason == "max_drawdown"
    assert r.update_kill_switch(p, 1200.0) == "max_drawdown"
    assert not r.evaluate(buy_decision(), view(cash=1200.0), snap(ts=T0 + 2 * DAY_MS), p).approved

    r.reset_kill_switch(p)
    assert p.halted_reason is None
    assert p.equity_peak == 1200.0 and p.day_start_equity == 1200.0  # re-baselined
    assert r.update_kill_switch(p, 1200.0) is None


def test_drawdown_overrides_daily_loss_halt():
    r, p = rm(), portfolio()
    p.mark(T0, 1000.0)
    assert r.update_kill_switch(p, 960.0) == "daily_loss"
    assert r.update_kill_switch(p, 890.0) == "max_drawdown"
    p.mark(T0 + DAY_MS, 890.0)
    assert p.halted_reason == "max_drawdown"  # not cleared by the new day


def test_reset_with_explicit_equity():
    r, p = rm(), portfolio()
    p.mark(T0, 1000.0)
    r.update_kill_switch(p, 850.0)
    r.reset_kill_switch(p, equity=850.0)
    assert p.equity_peak == 850.0
    assert r.update_kill_switch(p, 850.0) is None


def test_kill_switch_state_survives_restart():
    r, p = rm(), portfolio()
    p.mark(T0, 1000.0)
    r.update_kill_switch(p, 890.0)
    restored = Portfolio.from_state(p.to_state())
    assert r.update_kill_switch(restored, 1000.0) == "max_drawdown"


# ---------------------------------------------------------------- generic rejections


def test_hold_is_rejected():
    verdict = rm().evaluate(Decision.hold("nothing"), view(), snap(), portfolio())
    assert not verdict.approved and verdict.order is None
    assert verdict.reasons == ["HOLD: nothing to do"]


@pytest.mark.parametrize(
    "decision",
    [
        buy_decision(confidence=math.nan),
        buy_decision(size_pct=math.inf),
        buy_decision(stop_loss_pct=math.nan),
        buy_decision(take_profit_pct=-math.inf),
        sell_decision(confidence=math.nan),
    ],
)
def test_non_finite_decision_values_rejected(decision):
    verdict = rm().evaluate(decision, view(base=1.0), snap(), holding())
    assert not verdict.approved
    assert "non-finite" in verdict.reasons[0]


@pytest.mark.parametrize("price", [math.nan, math.inf])
def test_non_finite_price_rejected(price):
    verdict = rm().evaluate(buy_decision(), view(), snap(price=price), portfolio())
    assert not verdict.approved and "price" in verdict.reasons[0]


def test_non_finite_view_rejected():
    verdict = rm().evaluate(buy_decision(), view(equity=math.nan), snap(), portfolio())
    assert not verdict.approved and "equity" in verdict.reasons[0]


@pytest.mark.parametrize("price", [0.0, -5.0])
def test_non_positive_price_rejected(price):
    for decision in (buy_decision(), sell_decision()):
        verdict = rm().evaluate(decision, view(base=1.0), snap(price=price), holding())
        assert not verdict.approved and "invalid price" in verdict.reasons[0]


def test_symbol_mismatch_rejected():
    verdict = rm().evaluate(buy_decision(), view(), snap(symbol="ETH/USDT"), portfolio())
    assert not verdict.approved and "symbol" in verdict.reasons[0]


# ---------------------------------------------------------------- BUY rejections


def test_buy_rejected_when_already_holding():
    verdict = rm().evaluate(buy_decision(), view(cash=900.0, base=1.0), snap(), holding())
    assert not verdict.approved
    assert any("already holding" in r and "pyramiding" in r for r in verdict.reasons)


def test_buy_rejected_below_min_confidence():
    verdict = rm().evaluate(buy_decision(confidence=0.59), view(), snap(), portfolio())
    assert not verdict.approved
    assert any("confidence 0.59 below minimum 0.60" in r for r in verdict.reasons)
    assert rm().evaluate(buy_decision(confidence=0.6), view(), snap(), portfolio()).approved


def test_buy_rejected_at_daily_trade_limit():
    r, p = rm(replace(CFG, max_trades_per_day=2, cooldown_candles=0)), portfolio()
    p.apply_fill(Fill(order_id="1", symbol=SYMBOL, side=Side.BUY, quantity=1.0, price=100.0, fee=0.0, timestamp=T0))
    exit_position(p, 100.0, T0 + M5)
    verdict = r.evaluate(buy_decision(), view(), snap(ts=T0 + 2 * M5), p)
    assert not verdict.approved
    assert any("daily trade limit reached (2/2" in reason for reason in verdict.reasons)
    # the next UTC day starts a fresh count even before the first mark
    assert r.evaluate(buy_decision(), view(), snap(ts=T0 + DAY_MS), p).approved


def test_buy_cooldown_after_exit():
    r, p = rm(), portfolio()  # cooldown 3 candles
    p.apply_fill(Fill(order_id="1", symbol=SYMBOL, side=Side.BUY, quantity=1.0, price=100.0, fee=0.0, timestamp=T0))
    exit_position(p, 100.0, T0 + 10 * M5)
    exit_ts = T0 + 10 * M5
    for candles in (0, 1, 2):
        verdict = r.evaluate(buy_decision(), view(), snap(ts=exit_ts + candles * M5), p)
        assert not verdict.approved
        assert any(f"cooldown: {candles} of 3 candles" in reason for reason in verdict.reasons)
    assert r.evaluate(buy_decision(), view(), snap(ts=exit_ts + 3 * M5), p).approved
    # the candle length comes from the snapshot timeframe: 3 x 1h
    assert not r.evaluate(buy_decision(), view(), snap(ts=exit_ts + 3 * M5, timeframe="1h"), p).approved
    assert r.evaluate(buy_decision(), view(), snap(ts=exit_ts + 3 * 3_600_000, timeframe="1h"), p).approved


def test_buy_cooldown_with_exit_after_snapshot_time_is_conservative():
    r, p = rm(), holding()
    exit_position(p, 100.0, T0 + 5 * M5 + 7_000)  # wall-clock fill a few seconds after the candle
    verdict = r.evaluate(buy_decision(), view(), snap(ts=T0 + 5 * M5), p)
    assert any("cooldown: 0 of 3" in reason for reason in verdict.reasons)


def test_buy_cooldown_invalid_timeframe_rejects():
    r, p = rm(), holding()
    exit_position(p, 100.0, T0)
    verdict = r.evaluate(buy_decision(), view(), snap(ts=T0 + DAY_MS, timeframe="1M"), p)
    assert not verdict.approved and any("invalid timeframe" in reason for reason in verdict.reasons)


def test_no_cooldown_configured_or_never_exited():
    r = rm(replace(CFG, cooldown_candles=0))
    p = holding()
    exit_position(p, 100.0, T0)
    assert r.evaluate(buy_decision(), view(), snap(ts=T0), p).approved
    assert rm().evaluate(buy_decision(), view(), snap(timeframe="bogus"), portfolio()).approved


def test_buy_collects_all_gate_reasons():
    r, p = rm(), portfolio()
    p.mark(T0, 1000.0)
    r.update_kill_switch(p, 900.0)
    verdict = r.evaluate(buy_decision(confidence=0.1), view(), snap(), p)
    assert len(verdict.reasons) == 2
    assert "halted" in verdict.reasons[0] and "confidence" in verdict.reasons[1]


def test_buy_rejected_with_non_positive_equity():
    verdict = rm().evaluate(buy_decision(), view(cash=0.0), snap(), portfolio())
    assert not verdict.approved and "equity" in verdict.reasons[0]


def test_buy_rejected_below_min_notional():
    # equity 30: risk cap 0.3/0.023 = 13.04, position cap 7.5 < 10
    verdict = rm().evaluate(buy_decision(), view(cash=30.0), snap(), portfolio())
    assert not verdict.approved
    assert "below min notional 10.00" in verdict.reasons[0]
    assert "max position" in verdict.reasons[0]


# ---------------------------------------------------------------- BUY sizing


def test_buy_sizing_reference_numbers():
    # equity 1000, risk 1% -> 10; stop 2%, fee 0.1, slip 0.05 -> 10 / (0.02 + 0.003) = 434.78
    # capped by max position 25% -> 250
    r = rm()
    value, binding, risk_amount = r.buy_value(buy_decision(), view(), 2.0)
    assert risk_amount == pytest.approx(10.0)
    assert value == pytest.approx(250.0) and "max position" in binding

    verdict = r.evaluate(buy_decision(), view(), snap(price=100.0), portfolio())
    assert verdict.approved
    order = verdict.order
    assert order.side is Side.BUY and order.symbol == SYMBOL
    assert order.quantity == pytest.approx(2.5)
    assert order.reference_price == 100.0
    assert verdict.stop_loss == pytest.approx(98.0)
    assert verdict.take_profit == pytest.approx(104.0)
    assert "max position" in order.reason and "rules" in order.reason and "trend up" in order.reason


def test_buy_risk_based_value_when_position_cap_is_loose():
    r = rm(replace(CFG, max_position_pct=100.0))
    verdict = r.evaluate(buy_decision(), view(), snap(price=200.0), portfolio())
    assert verdict.approved
    assert verdict.order.quantity * 200.0 == pytest.approx(10 / 0.023)  # 434.78
    assert "risk 1% of equity" in verdict.reasons[0]
    # losing the stop distance plus round-trip costs costs exactly the risk amount
    value = verdict.order.quantity * 200.0
    assert value * (0.02 + 0.003) == pytest.approx(10.0)


def test_buy_engine_size_can_only_shrink():
    r = rm()
    shrunk = r.evaluate(buy_decision(size_pct=0.1), view(), snap(), portfolio())
    assert shrunk.approved and shrunk.order.quantity * 100.0 == pytest.approx(100.0)
    assert "engine size" in shrunk.reasons[0]
    for size in (0.0, 1.0, 0.5):  # 0 = unspecified, 1 = no shrink, 0.5 -> 500 > 250 cap
        verdict = r.evaluate(buy_decision(size_pct=size), view(), snap(), portfolio())
        assert verdict.order.quantity * 100.0 == pytest.approx(250.0)


def test_buy_capped_by_cash_with_fees_and_buffer():
    r = rm()
    verdict = r.evaluate(buy_decision(), view(cash=200.0, equity=1000.0), snap(), portfolio())
    assert verdict.approved
    expected = 200.0 / (1 + 0.001 + 0.0005) * 0.995  # 198.70
    assert verdict.order.quantity * 100.0 == pytest.approx(expected)
    assert "available cash" in verdict.reasons[0]
    # the paper fill (slippage + fee) fits in the cash
    cost = verdict.order.quantity * 100.0 * 1.0005
    assert cost * 1.001 < 200.0


def test_stop_clamping_and_take_profit_rules():
    r = rm()
    assert r.stop_and_target_pct(buy_decision(stop_loss_pct=None, take_profit_pct=None)) == (2.0, 4.0)
    assert r.stop_and_target_pct(buy_decision(stop_loss_pct=0.1, take_profit_pct=1.0)) == (0.3, 1.0)
    assert r.stop_and_target_pct(buy_decision(stop_loss_pct=50.0, take_profit_pct=20.0)) == (10.0, 20.0)
    assert r.stop_and_target_pct(buy_decision(stop_loss_pct=50.0, take_profit_pct=5.0)) == (10.0, 15.0)
    assert r.stop_and_target_pct(buy_decision(stop_loss_pct=3.0, take_profit_pct=2.0)) == (3.0, 4.5)
    assert r.stop_and_target_pct(buy_decision(stop_loss_pct=1.0, take_profit_pct=0.5)) == (1.0, 4.0)
    assert r.stop_and_target_pct(buy_decision(stop_loss_pct=2.0, take_profit_pct=3.0)) == (2.0, 3.0)
    assert r.stop_and_target_pct(buy_decision(stop_loss_pct=-1.0, take_profit_pct=None)) == (0.3, 4.0)


def test_wider_stop_means_smaller_position():
    r = rm(replace(CFG, max_position_pct=100.0))
    verdict = r.evaluate(buy_decision(stop_loss_pct=5.0, take_profit_pct=10.0), view(), snap(price=50.0),
                         portfolio())
    assert verdict.order.quantity * 50.0 == pytest.approx(10 / 0.053)
    assert verdict.stop_loss == pytest.approx(47.5)
    assert verdict.take_profit == pytest.approx(55.0)


# ---------------------------------------------------------------- SELL


def test_sell_rejected_when_flat():
    verdict = rm().evaluate(sell_decision(), view(), snap(), portfolio())
    assert not verdict.approved and "no position" in verdict.reasons[0]


def test_sell_rejected_when_broker_has_nothing():
    verdict = rm().evaluate(sell_decision(), view(base=0.0), snap(), holding())
    assert not verdict.approved and "broker reports" in verdict.reasons[0]


def test_sell_confidence_threshold_is_half_the_entry_one():
    r, p = rm(), holding()
    low = r.evaluate(sell_decision(confidence=0.29), view(cash=0.0, base=1.0), snap(), p)
    assert not low.approved and "exit confidence 0.29 below minimum 0.30" in low.reasons[0]
    assert r.evaluate(sell_decision(confidence=0.3), view(cash=0.0, base=1.0), snap(), p).approved


def test_sell_full_position_by_default():
    r, p = rm(), holding(qty=1.0)
    for size in (1.0, 0.0, 1.5, -0.2):
        verdict = r.evaluate(sell_decision(size_pct=size), view(cash=0.0, base=1.0), snap(), p)
        assert verdict.approved
        assert verdict.order.side is Side.SELL and verdict.order.quantity == 1.0
        assert verdict.order.reference_price == 100.0
        assert verdict.stop_loss is None and verdict.take_profit is None


def test_sell_partial():
    verdict = rm().evaluate(sell_decision(size_pct=0.5), view(cash=0.0, base=1.0), snap(), holding(qty=1.0))
    assert verdict.approved and verdict.order.quantity == pytest.approx(0.5)
    assert "50% of position" in verdict.order.reason and "claude" in verdict.order.reason


def test_sell_everything_when_remainder_below_min_notional():
    # 0.25 BTC @ 100 = 25; selling 70 % leaves 7.5 < 10
    verdict = rm().evaluate(sell_decision(size_pct=0.7), view(cash=0.0, base=0.25), snap(), holding(qty=0.25))
    assert verdict.approved and verdict.order.quantity == 0.25
    assert "whole position" in verdict.reasons[0]


def test_sell_everything_when_order_below_min_notional():
    # 0.15 BTC @ 100 = 15; selling 50 % = 7.5 < 10
    verdict = rm().evaluate(sell_decision(size_pct=0.5), view(cash=0.0, base=0.15), snap(), holding(qty=0.15))
    assert verdict.approved and verdict.order.quantity == 0.15


def test_sell_never_exceeds_tracked_or_broker_quantity():
    r = rm()
    # broker holds extra (unmanaged) coins -> only the tracked 1.0 is sold
    verdict = r.evaluate(sell_decision(), view(cash=0.0, base=3.0), snap(), holding(qty=1.0))
    assert verdict.order.quantity == 1.0
    # broker holds less than tracked -> only what exists is sold
    verdict = r.evaluate(sell_decision(), view(cash=0.0, base=0.6), snap(), holding(qty=1.0))
    assert verdict.order.quantity == 0.6


def test_sell_allowed_while_halted():
    r, p = rm(), holding(qty=1.0)
    p.mark(T0, 1000.0)
    assert r.update_kill_switch(p, 800.0) == "max_drawdown"
    verdict = r.evaluate(sell_decision(), view(cash=0.0, base=1.0), snap(), p)
    assert verdict.approved and verdict.order.quantity == 1.0


def test_sell_ignores_cooldown_and_trade_limit():
    r, p = rm(replace(CFG, max_trades_per_day=1)), holding(qty=1.0)
    assert p.trades_today == 1
    assert r.evaluate(sell_decision(), view(cash=0.0, base=1.0), snap(), p).approved


# ---------------------------------------------------------------- forced exits


def test_forced_exit_order():
    r = rm()
    assert r.forced_exit_order(view(), "max_drawdown") is None
    order = r.forced_exit_order(view(cash=0.0, base=0.5, price=120.0), "max_drawdown")
    assert order.side is Side.SELL and order.symbol == SYMBOL
    assert order.quantity == 0.5 and order.reference_price == pytest.approx(120.0)
    assert "max_drawdown" in order.reason
    assert r.forced_exit_order(view(cash=0.0, base=0.5), "stop_loss", price=98.0).reference_price == 98.0


def test_protective_exit_flow_with_portfolio_view():
    r = rm()
    p = holding(qty=1.0, stop=98.0, tp=104.0)
    hit = r.check_protective_exit(p, low=97.5, high=100.5)
    assert hit == ("stop_loss", 98.0)
    order = r.forced_exit_order(p.view(Balances(cash=0.0, base_qty=1.0), 99.0), hit[0], price=hit[1])
    assert order.quantity == 1.0 and order.reference_price == 98.0
