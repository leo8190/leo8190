"""Tests for jeb.execution.ccxt_broker.CcxtBroker with an in-memory fake exchange (offline)."""

from __future__ import annotations

import logging
import math

import ccxt
import pytest

from jeb.execution.ccxt_broker import CcxtBroker, OrderStateUnknownError
from jeb.models import (
    ConfigError,
    InsufficientFundsError,
    JebError,
    OrderRejectedError,
    OrderRequest,
    Side,
)
from jeb.portfolio import Portfolio

T0 = 1767225600000
SYMBOL = "BTC/USDT"
NOW = T0 + 60_000


class FakeExchange:
    """Minimal ccxt-like exchange: 1e-5 BTC step, 1e-4 BTC min amount, 5 USDT min cost."""

    id = "fake"

    def __init__(self, free: dict | None = None, fill_price: float = 100.0, fee: dict | None = None) -> None:
        self.free = {"USDT": 1000.0, "BTC": 0.0} if free is None else free
        self.fill_price = fill_price
        self.fee = {"cost": None, "currency": "USDT", "auto": True} if fee is None else fee
        self.markets = {
            SYMBOL: {
                "symbol": SYMBOL, "base": "BTC", "quote": "USDT", "spot": True, "active": True,
                "precision": {"amount": 0.00001},
                "limits": {"amount": {"min": 0.0001, "max": 9000.0}, "cost": {"min": 5.0, "max": None}},
            }
        }
        self.sandbox: bool | None = None
        self.load_count = 0
        self.created: list[dict] = []
        self.create_error: Exception | None = None
        self.create_response: dict | None = None  # overrides the default filled response
        self.fetch_order_response: dict | None = None
        self.fetch_order_error: Exception | None = None
        self.fetch_order_calls: list[tuple] = []
        self.orders: list[dict] = []  # returned by fetch_orders
        self.fetch_orders_error: Exception | None = None
        self.fetch_orders_calls: list[dict] = []

    def set_sandbox_mode(self, enabled: bool) -> None:
        self.sandbox = enabled

    def load_markets(self) -> dict:
        self.load_count += 1
        return self.markets

    def market(self, symbol: str) -> dict:
        if symbol not in self.markets:
            raise ccxt.BadSymbol(f"fake does not have market symbol {symbol}")
        return self.markets[symbol]

    def amount_to_precision(self, symbol: str, amount: float) -> str:
        step = self.markets[symbol]["precision"]["amount"]
        return f"{math.floor(amount / step + 1e-9) * step:.5f}"

    def fetch_balance(self) -> dict:
        return {"free": dict(self.free), "used": {}, "total": dict(self.free)}

    def filled_order(self, client_id: str, side: str, amount: float, **overrides) -> dict:
        fee = dict(self.fee)
        if fee.get("cost") is None and fee.get("currency") == "USDT":
            fee["cost"] = amount * self.fill_price * 0.001
        order = {
            "id": "ex-1", "clientOrderId": client_id, "symbol": SYMBOL, "type": "market", "side": side,
            "status": "closed", "amount": amount, "filled": amount, "average": self.fill_price,
            "cost": amount * self.fill_price, "fee": fee, "fees": [fee], "timestamp": NOW,
            "lastTradeTimestamp": NOW,
        }
        order.update(overrides)
        return order

    def create_order(self, symbol, type, side, amount, price=None, params=None):  # noqa: A002
        self.created.append(dict(symbol=symbol, type=type, side=side, amount=amount, price=price,
                                 params=dict(params or {})))
        if self.create_error is not None:
            raise self.create_error
        if self.create_response is not None:
            return self.create_response
        return self.filled_order(params["clientOrderId"], side, amount)

    def fetch_order(self, id, symbol=None, params=None):  # noqa: A002
        self.fetch_order_calls.append((id, symbol))
        if self.fetch_order_error is not None:
            raise self.fetch_order_error
        return self.fetch_order_response

    def fetch_orders(self, symbol=None, since=None, limit=None, params=None):
        self.fetch_orders_calls.append(dict(symbol=symbol, since=since))
        if self.fetch_orders_error is not None:
            raise self.fetch_orders_error
        return self.orders


def make(fake: FakeExchange | None = None, **kwargs) -> tuple[CcxtBroker, FakeExchange]:
    fake = fake or FakeExchange()
    return CcxtBroker(SYMBOL, exchange=fake, now_ms=lambda: NOW, **kwargs), fake


def buy_order(qty: float) -> OrderRequest:
    return OrderRequest(symbol=SYMBOL, side=Side.BUY, quantity=qty, reference_price=100.0, reason="t")


def sell_order(qty: float) -> OrderRequest:
    return OrderRequest(symbol=SYMBOL, side=Side.SELL, quantity=qty, reference_price=100.0, reason="t")


# ---------------------------------------------------------------- construction


def test_mainnet_is_refused_without_explicit_opt_in():
    with pytest.raises(ConfigError, match="mainnet"):
        CcxtBroker(SYMBOL, use_testnet=False, exchange=FakeExchange())
    broker = CcxtBroker(SYMBOL, use_testnet=False, allow_mainnet=True, exchange=(fake := FakeExchange()))
    assert fake.sandbox is None  # never switched to sandbox on mainnet
    assert "MAINNET" in repr(broker)


def test_testnet_enables_sandbox_on_injected_exchange():
    _, fake = make()
    assert fake.sandbox is True
    assert fake.load_count == 0  # markets are loaded lazily


def test_injected_exchange_without_sandbox_support_is_refused():
    class NoSandbox(FakeExchange):
        set_sandbox_mode = None

    with pytest.raises(ConfigError):
        CcxtBroker(SYMBOL, exchange=NoSandbox())


def test_real_ccxt_exchange_built_offline_in_sandbox_without_leaking_keys(caplog):
    caplog.set_level(logging.DEBUG)
    broker = CcxtBroker(SYMBOL, api_key="KEY-123-abc", api_secret="SECRET-456-def")
    ex = broker.exchange
    assert isinstance(ex, ccxt.binance)
    assert ex.enableRateLimit is True
    assert ex.apiKey == "KEY-123-abc" and ex.secret == "SECRET-456-def"
    assert "testnet" in str(ex.urls["api"])
    for text in (repr(broker), caplog.text):
        assert "KEY-123-abc" not in text and "SECRET-456-def" not in text


def test_keys_never_logged_during_execute(caplog):
    caplog.set_level(logging.DEBUG)
    broker = CcxtBroker(SYMBOL, api_key="KEY-123-abc", api_secret="SECRET-456-def")
    fake = FakeExchange()
    for name in ("load_markets", "market", "amount_to_precision", "create_order", "fetch_balance"):
        setattr(broker.exchange, name, getattr(fake, name))  # no network: route calls to the fake
    broker.execute(buy_order(0.5), 100.0, T0)
    assert "KEY-123-abc" not in caplog.text and "SECRET-456-def" not in caplog.text


def test_unknown_exchange_and_bad_symbol_rejected():
    with pytest.raises(ConfigError, match="unknown ccxt exchange"):
        CcxtBroker(SYMBOL, exchange_id="no_such_exchange")
    for symbol in ("BTCUSDT", "BTC/USDT:USDT"):
        with pytest.raises(ConfigError):
            CcxtBroker(symbol, exchange=FakeExchange())


# ---------------------------------------------------------------- balances


def test_balances_free_and_missing_currencies():
    broker, fake = make(FakeExchange(free={"USDT": 123.4, "BTC": 0.5}))
    bal = broker.balances()
    assert bal.cash == 123.4 and bal.base_qty == 0.5
    fake.free = {"ETH": 1.0, "BTC": None}
    bal = broker.balances()
    assert bal.cash == 0.0 and bal.base_qty == 0.0


def test_balances_error_is_wrapped():
    broker, fake = make()

    def boom():
        raise ccxt.NetworkError("down")

    fake.fetch_balance = boom
    with pytest.raises(JebError) as info:
        broker.balances()
    assert isinstance(info.value.__cause__, ccxt.NetworkError)


# ---------------------------------------------------------------- order validation


def test_buy_amount_rounded_to_precision_with_client_order_id():
    broker, fake = make()
    fill = broker.execute(buy_order(0.0123456789), 1000.0, T0)  # ~12.3 USDT
    sent = fake.created[0]
    assert sent["amount"] == pytest.approx(0.01234)
    assert (sent["symbol"], sent["type"], sent["side"], sent["price"]) == (SYMBOL, "market", "buy", None)
    assert sent["params"] == {"clientOrderId": f"jeb-{T0}-1"}
    assert fill.quantity == pytest.approx(0.01234)
    assert fake.load_count == 1
    broker.execute(buy_order(0.5), 100.0, T0)
    assert fake.created[1]["params"]["clientOrderId"] == f"jeb-{T0}-2"
    assert fake.load_count == 1  # loaded once


def test_amount_below_minimum_rejected_before_sending():
    broker, fake = make()
    with pytest.raises(OrderRejectedError, match="below exchange minimum"):
        broker.execute(buy_order(0.00009), 1_000_000.0, T0)
    assert fake.created == []


def test_amount_rounding_to_zero_rejected():
    broker, fake = make()
    with pytest.raises(OrderRejectedError, match="rounds to"):
        broker.execute(buy_order(0.000001), 100.0, T0)
    assert fake.created == []


def test_cost_below_minimum_rejected_before_sending():
    broker, fake = make()
    with pytest.raises(OrderRejectedError, match="order value"):
        broker.execute(buy_order(0.04), 100.0, T0)  # 4 USDT < 5
    assert fake.created == []


def test_amount_above_maximum_rejected():
    broker, fake = make()
    with pytest.raises(OrderRejectedError, match="maximum"):
        broker.execute(buy_order(10_000.0), 1.0, T0)
    assert fake.created == []


def test_inactive_or_unknown_market_rejected():
    broker, fake = make()
    fake.markets[SYMBOL]["active"] = False
    with pytest.raises(OrderRejectedError, match="not active"):
        broker.execute(buy_order(0.5), 100.0, T0)
    fake.markets.clear()
    with pytest.raises(OrderRejectedError, match="unknown market"):
        broker.execute(buy_order(0.5), 100.0, T0)


@pytest.mark.parametrize("qty, price", [(0.0, 100.0), (-1.0, 100.0), (math.nan, 100.0), (1.0, 0.0), (1.0, math.inf)])
def test_invalid_order_values_rejected(qty, price):
    broker, fake = make()
    with pytest.raises(OrderRejectedError):
        broker.execute(buy_order(qty), price, T0)
    assert fake.created == []


def test_symbol_mismatch_rejected():
    broker, _ = make()
    bad = OrderRequest(symbol="ETH/USDT", side=Side.BUY, quantity=1.0, reference_price=1.0)
    with pytest.raises(OrderRejectedError):
        broker.execute(bad, 100.0, T0)


def test_load_markets_failure_is_a_rejection():
    broker, fake = make()

    def boom():
        raise ccxt.NetworkError("down")

    fake.load_markets = boom
    with pytest.raises(OrderRejectedError, match="load markets"):
        broker.execute(buy_order(0.5), 100.0, T0)
    assert fake.created == []


# ---------------------------------------------------------------- sell clamp


def test_sell_is_clamped_to_free_base():
    broker, fake = make(FakeExchange(free={"USDT": 0.0, "BTC": 0.5}))
    fill = broker.execute(sell_order(0.7), 100.0, T0)
    assert fake.created[0]["amount"] == pytest.approx(0.5)
    assert fake.created[0]["side"] == "sell"
    assert fill.side is Side.SELL and fill.quantity == pytest.approx(0.5)


def test_sell_without_free_base_raises_insufficient_funds():
    broker, fake = make(FakeExchange(free={"USDT": 100.0}))
    with pytest.raises(InsufficientFundsError):
        broker.execute(sell_order(0.5), 100.0, T0)
    assert fake.created == []


def test_sell_within_balance_not_clamped_but_truncated():
    broker, fake = make(FakeExchange(free={"USDT": 0.0, "BTC": 1.0}))
    broker.execute(sell_order(0.123456), 100.0, T0)
    assert fake.created[0]["amount"] == pytest.approx(0.12345)


# ---------------------------------------------------------------- fees and fill parsing


def test_fee_in_quote():
    broker, _ = make(FakeExchange(fill_price=101.0))
    fill = broker.execute(buy_order(0.5), 100.0, T0)
    assert fill.price == 101.0
    assert fill.quantity == 0.5
    assert fill.fee == pytest.approx(0.5 * 101.0 * 0.001)
    assert fill.order_id == "ex-1" and fill.symbol == SYMBOL and fill.side is Side.BUY
    assert fill.timestamp == NOW


def test_fee_in_base_on_buy_reduces_quantity():
    fake = FakeExchange(fee={"cost": 0.0005, "currency": "BTC"})
    broker, _ = make(fake)
    fill = broker.execute(buy_order(0.5), 100.0, T0)
    assert fill.quantity == pytest.approx(0.4995)  # net coins received
    assert fill.fee == pytest.approx(0.0005 * 100.0)
    # the cost basis equals the quote actually spent (0.5 * 100)
    p = Portfolio(SYMBOL, "USDT", "BTC")
    p.apply_fill(fill)
    assert p.cost_basis == pytest.approx(50.0)
    assert p.avg_entry_price == pytest.approx(50.0 / 0.4995)


def test_fee_in_base_on_sell_is_converted():
    fake = FakeExchange(free={"USDT": 0.0, "BTC": 1.0}, fee={"cost": 0.001, "currency": "BTC"})
    broker, _ = make(fake)
    fill = broker.execute(sell_order(1.0), 100.0, T0)
    assert fill.quantity == 1.0
    assert fill.fee == pytest.approx(0.1)


def test_fee_in_third_currency_is_estimated(caplog):
    fake = FakeExchange(fee={"cost": 0.0002, "currency": "BNB"})
    broker, _ = make(fake, fee_pct_estimate=0.075)
    with caplog.at_level(logging.WARNING):
        fill = broker.execute(buy_order(0.5), 100.0, T0)
    assert fill.quantity == 0.5
    assert fill.fee == pytest.approx(50.0 * 0.075 / 100)
    assert "BNB" in caplog.text


def test_missing_fee_is_estimated_and_zero_fee_is_respected():
    broker, fake = make()
    fake.create_response = fake.filled_order(f"jeb-{T0}-1", "buy", 0.5, fee=None, fees=[])
    assert broker.execute(buy_order(0.5), 100.0, T0).fee == pytest.approx(50.0 * 0.1 / 100)
    fake.create_response = fake.filled_order(f"jeb-{T0}-2", "buy", 0.5, fee=None,
                                             fees=[{"cost": 0.0, "currency": "BNB"}])
    assert broker.execute(buy_order(0.5), 100.0, T0).fee == 0.0


def test_fees_list_takes_precedence_and_sums():
    broker, fake = make()
    fake.create_response = fake.filled_order(
        f"jeb-{T0}-1", "buy", 0.5, fee={"cost": 999.0, "currency": "USDT"},
        fees=[{"cost": 0.02, "currency": "USDT"}, {"cost": 0.03, "currency": "USDT"}],
    )
    assert broker.execute(buy_order(0.5), 100.0, T0).fee == pytest.approx(0.05)


def test_price_from_cost_when_average_missing():
    broker, fake = make()
    fake.create_response = fake.filled_order(f"jeb-{T0}-1", "buy", 0.5, average=None, cost=51.0)
    assert broker.execute(buy_order(0.5), 100.0, T0).price == pytest.approx(102.0)


def test_partial_fill_reports_real_filled_quantity():
    broker, fake = make()
    fake.create_response = fake.filled_order(f"jeb-{T0}-1", "buy", 0.5, filled=0.3, cost=30.0)
    fill = broker.execute(buy_order(0.5), 100.0, T0)
    assert fill.quantity == pytest.approx(0.3)


def test_fill_timestamp_falls_back_to_now():
    broker, fake = make()
    fake.create_response = fake.filled_order(f"jeb-{T0}-1", "buy", 0.5, timestamp=None, lastTradeTimestamp=None)
    assert broker.execute(buy_order(0.5), 100.0, T0).timestamp == NOW


def test_fetch_order_fallback_when_response_lacks_fill_info():
    broker, fake = make()
    fake.create_response = {"id": "ex-9", "clientOrderId": f"jeb-{T0}-1", "status": None, "filled": None}
    fake.fetch_order_response = fake.filled_order(f"jeb-{T0}-1", "buy", 0.5, id="ex-9")
    fill = broker.execute(buy_order(0.5), 100.0, T0)
    assert fake.fetch_order_calls == [("ex-9", SYMBOL)]
    assert fill.order_id == "ex-9" and fill.quantity == 0.5 and fill.price == 100.0


def test_fetch_order_fallback_when_price_missing():
    broker, fake = make()
    fake.create_response = fake.filled_order(f"jeb-{T0}-1", "buy", 0.5, average=None, cost=None)
    fake.fetch_order_response = fake.filled_order(f"jeb-{T0}-1", "buy", 0.5, average=99.0)
    assert broker.execute(buy_order(0.5), 100.0, T0).price == 99.0


def test_no_price_anywhere_uses_market_price_estimate():
    broker, fake = make()
    fake.create_response = fake.filled_order(f"jeb-{T0}-1", "buy", 0.5, average=None, cost=None)
    fake.fetch_order_response = fake.create_response
    assert broker.execute(buy_order(0.5), 100.5, T0).price == 100.5


def test_unfilled_closed_order_is_rejected():
    broker, fake = make()
    for status in ("canceled", "expired", "closed"):
        fake.create_response = fake.filled_order(f"jeb-{T0}-1", "buy", 0.5, filled=0.0, status=status)
        with pytest.raises(OrderRejectedError, match="not filled") as info:
            broker.execute(buy_order(0.5), 100.0, T0)
        assert not isinstance(info.value, OrderStateUnknownError)


def test_unfilled_open_order_state_is_unknown():
    broker, fake = make()
    fake.create_response = fake.filled_order(f"jeb-{T0}-1", "buy", 0.5, filled=0.0, status="open")
    fake.fetch_order_response = fake.create_response
    with pytest.raises(OrderStateUnknownError, match="order state unknown"):
        broker.execute(buy_order(0.5), 100.0, T0)
    assert fake.fetch_order_calls  # it looked again before giving up


# ---------------------------------------------------------------- exchange errors


@pytest.mark.parametrize(
    "error, expected",
    [
        (ccxt.InsufficientFunds("no money"), InsufficientFundsError),
        (ccxt.InvalidOrder("bad lot size"), OrderRejectedError),
        (ccxt.ExchangeError("generic"), OrderRejectedError),
        (ccxt.AuthenticationError("bad key"), OrderRejectedError),
    ],
)
def test_exchange_errors_are_mapped_with_cause(error, expected):
    broker, fake = make()
    fake.create_error = error
    with pytest.raises(expected) as info:
        broker.execute(buy_order(0.5), 100.0, T0)
    assert info.value.__cause__ is error
    assert not isinstance(info.value, OrderStateUnknownError)
    assert fake.fetch_orders_calls == []  # definitive answers need no reconciliation


def test_network_error_reconciled_to_a_fill():
    broker, fake = make()
    fake.create_error = ccxt.RequestTimeout("timeout after sending")
    fake.orders = [
        fake.filled_order("someone-else", "buy", 9.0, id="ex-other"),
        fake.filled_order(f"jeb-{T0}-1", "buy", 0.5, id="ex-42", average=100.2),
    ]
    fill = broker.execute(buy_order(0.5), 100.0, T0)
    assert fake.fetch_orders_calls == [dict(symbol=SYMBOL, since=T0 - 120_000)]
    assert fill.order_id == "ex-42" and fill.quantity == 0.5 and fill.price == 100.2


def test_network_error_reconciliation_not_found():
    broker, fake = make()
    error = ccxt.NetworkError("connection reset")
    fake.create_error = error
    fake.orders = [fake.filled_order("someone-else", "buy", 0.5)]
    with pytest.raises(OrderRejectedError, match="order state unknown — check the exchange") as info:
        broker.execute(buy_order(0.5), 100.0, T0)
    assert isinstance(info.value, OrderStateUnknownError)
    assert info.value.__cause__ is error
    assert len(fake.fetch_orders_calls) == 1  # exactly one reconciliation attempt


def test_network_error_reconciliation_lookup_fails():
    broker, fake = make()
    error = ccxt.NetworkError("down")
    fake.create_error = error
    fake.fetch_orders_error = ccxt.NetworkError("still down")
    with pytest.raises(OrderStateUnknownError) as info:
        broker.execute(buy_order(0.5), 100.0, T0)
    assert info.value.__cause__ is error


def test_network_error_reconciled_order_not_filled():
    broker, fake = make()
    fake.create_error = ccxt.NetworkError("down")
    fake.orders = [fake.filled_order(f"jeb-{T0}-1", "buy", 0.5, filled=0.0, status="canceled")]
    with pytest.raises(OrderRejectedError, match="not filled") as info:
        broker.execute(buy_order(0.5), 100.0, T0)
    assert not isinstance(info.value, OrderStateUnknownError)
    fake.orders = [fake.filled_order(f"jeb-{T0}-2", "buy", 0.5, filled=0.0, status="open")]
    with pytest.raises(OrderStateUnknownError):
        broker.execute(buy_order(0.5), 100.0, T0)


def test_reconciliation_window_uses_the_earlier_of_timestamp_and_now():
    broker, fake = make()
    fake.create_error = ccxt.NetworkError("down")
    with pytest.raises(OrderStateUnknownError):
        broker.execute(buy_order(0.5), 100.0, NOW + 10_000_000)
    assert fake.fetch_orders_calls[0]["since"] == NOW - 120_000


def test_fetch_order_failure_falls_back_to_reconciliation():
    broker, fake = make()
    fake.create_response = {"id": "ex-5", "filled": None}
    fake.fetch_order_error = ccxt.NetworkError("down")
    fake.orders = [fake.filled_order(f"jeb-{T0}-1", "buy", 0.5, id="ex-5")]
    fill = broker.execute(buy_order(0.5), 100.0, T0)
    assert fill.order_id == "ex-5" and fill.quantity == 0.5


def test_response_without_id_is_reconciled():
    broker, fake = make()
    fake.create_response = {}
    fake.orders = [fake.filled_order(f"jeb-{T0}-1", "sell", 0.2, id="ex-7")]
    fake.free["BTC"] = 1.0
    fill = broker.execute(sell_order(0.2), 100.0, T0)
    assert fill.order_id == "ex-7" and fill.side is Side.SELL
