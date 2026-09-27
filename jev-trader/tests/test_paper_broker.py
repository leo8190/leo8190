"""Tests for jev.execution.paper.PaperBroker (and its fit with Portfolio/RiskManager)."""

from __future__ import annotations

import json
import math

import pytest

from jev.config import RiskConfig
from jev.execution.paper import PaperBroker
from jev.models import (
    Action,
    ConfigError,
    Decision,
    IndicatorSet,
    InsufficientFundsError,
    MarketSnapshot,
    OrderRejectedError,
    OrderRequest,
    Side,
)
from jev.portfolio import Portfolio
from jev.risk import RiskManager

T0 = 1767225600000
M5 = 300_000
SYMBOL = "BTC/USDT"


def broker(**kwargs) -> PaperBroker:
    args = dict(symbol=SYMBOL, start_cash=1000.0, fee_pct=0.1, slippage_pct=0.05) | kwargs
    return PaperBroker(**args)


def order(side: Side, qty: float, symbol: str = SYMBOL) -> OrderRequest:
    return OrderRequest(symbol=symbol, side=side, quantity=qty, reference_price=100.0, reason="test")


def test_initial_balances():
    b = broker(start_base=0.25)
    bal = b.balances()
    assert bal.cash == 1000.0 and bal.base_qty == 0.25


def test_buy_applies_slippage_and_fee():
    b = broker()
    f = b.execute(order(Side.BUY, 1.0), 100.0, T0)
    assert f.price == pytest.approx(100.05)
    assert f.quantity == 1.0
    assert f.fee == pytest.approx(0.10005)  # 0.1 % of 100.05
    assert f.side is Side.BUY and f.symbol == SYMBOL and f.timestamp == T0
    assert f.order_id == "paper-1"
    assert b.cash == pytest.approx(1000.0 - 100.05 - 0.10005)  # 899.84995
    assert b.base_qty == 1.0


def test_sell_applies_slippage_and_fee():
    b = broker(start_cash=0.0, start_base=1.0)
    f = b.execute(order(Side.SELL, 1.0), 110.0, T0)
    assert f.price == pytest.approx(109.945)
    assert f.fee == pytest.approx(0.109945)
    assert b.cash == pytest.approx(109.945 - 0.109945)  # 109.835055
    assert b.base_qty == 0.0


def test_round_trip_cash_flow_matches_portfolio_pnl():
    b = broker()
    p = Portfolio(SYMBOL, "USDT", "BTC")
    p.apply_fill(b.execute(order(Side.BUY, 2.0), 100.0, T0))
    p.apply_fill(b.execute(order(Side.SELL, 0.5), 105.0, T0 + M5))
    p.apply_fill(b.execute(order(Side.SELL, 1.5), 95.0, T0 + 2 * M5))
    assert b.base_qty == 0.0 and not p.in_position
    assert p.realized_pnl == pytest.approx(b.cash - 1000.0)
    assert p.fees_paid == pytest.approx(sum(t.fees for t in p.trades))


def test_order_ids_increment_only_on_success():
    b = broker()
    assert b.execute(order(Side.BUY, 0.1), 100.0, T0).order_id == "paper-1"
    with pytest.raises(InsufficientFundsError):
        b.execute(order(Side.BUY, 100.0), 100.0, T0)
    assert b.execute(order(Side.SELL, 0.1), 100.0, T0).order_id == "paper-2"


def test_buy_insufficient_funds_leaves_balances_untouched():
    b = broker(start_cash=100.0, fee_pct=0.1, slippage_pct=0.0)
    with pytest.raises(InsufficientFundsError):
        b.execute(order(Side.BUY, 1.0), 100.0, T0)  # needs 100.1
    assert b.cash == 100.0 and b.base_qty == 0.0


def test_buy_exactly_affordable():
    b = broker(start_cash=100.1, fee_pct=0.1, slippage_pct=0.0)
    b.execute(order(Side.BUY, 1.0), 100.0, T0)
    assert b.cash == pytest.approx(0.0, abs=1e-12) and b.base_qty == 1.0


def test_sell_more_than_held_raises():
    b = broker(start_base=1.0)
    with pytest.raises(InsufficientFundsError):
        b.execute(order(Side.SELL, 1.001), 100.0, T0)
    assert b.base_qty == 1.0 and b.cash == 1000.0


def test_sell_within_relative_tolerance_is_clamped():
    b = broker(start_cash=0.0, start_base=1.0, fee_pct=0.0, slippage_pct=0.0)
    f = b.execute(order(Side.SELL, 1.0 + 1e-12), 100.0, T0)
    assert f.quantity == 1.0 and b.base_qty == 0.0 and b.cash == 100.0


def test_sell_with_no_base_raises():
    with pytest.raises(InsufficientFundsError):
        broker().execute(order(Side.SELL, 1e-15), 100.0, T0)


@pytest.mark.parametrize("qty", [0.0, -1.0, math.nan, math.inf])
def test_invalid_quantity_rejected(qty):
    with pytest.raises(OrderRejectedError):
        broker().execute(order(Side.BUY, qty), 100.0, T0)


@pytest.mark.parametrize("price", [0.0, -1.0, math.nan, math.inf])
def test_invalid_price_rejected(price):
    with pytest.raises(OrderRejectedError):
        broker(start_base=1.0).execute(order(Side.SELL, 0.5), price, T0)


def test_symbol_mismatch_rejected():
    with pytest.raises(OrderRejectedError):
        broker().execute(order(Side.BUY, 0.1, symbol="ETH/USDT"), 100.0, T0)


@pytest.mark.parametrize(
    "kwargs",
    [dict(start_cash=-1.0), dict(start_base=-0.1), dict(fee_pct=-0.1), dict(slippage_pct=100.0),
     dict(fee_pct=math.nan), dict(start_cash=math.inf)],
)
def test_constructor_validation(kwargs):
    with pytest.raises(ConfigError):
        broker(**kwargs)


def test_state_round_trip():
    b = broker()
    b.execute(order(Side.BUY, 1.2345), 101.7, T0)
    b.execute(order(Side.SELL, 0.3), 99.3, T0 + M5)
    state = json.loads(json.dumps(b.to_state()))
    restored = PaperBroker.from_state(state)
    assert restored.to_state() == b.to_state()
    assert restored.balances() == b.balances()
    assert restored.execute(order(Side.SELL, 0.1), 100.0, T0).order_id == "paper-3"


def test_from_state_rejects_unknown_version():
    state = broker().to_state()
    state["version"] = 0
    with pytest.raises(ValueError):
        PaperBroker.from_state(state)


def test_risk_sized_buy_always_fits_the_paper_cash():
    """Cash-capped BUYs from the risk manager must never raise InsufficientFunds."""
    cfg = RiskConfig(max_position_pct=100.0, risk_per_trade_pct=5.0, min_stop_pct=0.3)
    risk = RiskManager(cfg, fee_pct=0.1, slippage_pct=0.05)
    for cash in (15.0, 99.99, 1000.0, 12345.678):
        b = broker(start_cash=cash)
        p = Portfolio(SYMBOL, "USDT", "BTC")
        price = 37_123.45
        snapshot = MarketSnapshot(symbol=SYMBOL, timeframe="5m", timestamp=T0, price=price,
                                  indicators=IndicatorSet())
        decision = Decision(action=Action.BUY, confidence=0.9, size_pct=1.0, stop_loss_pct=0.3)
        verdict = risk.evaluate(decision, p.view(b.balances(), price), snapshot, p)
        assert verdict.approved, verdict.reasons
        fill = b.execute(verdict.order, price, T0)
        p.apply_fill(fill, stop_loss=verdict.stop_loss, take_profit=verdict.take_profit)
        assert b.cash >= 0.0
        view = p.view(b.balances(), price)
        assert view.equity == pytest.approx(b.cash + b.base_qty * price)
