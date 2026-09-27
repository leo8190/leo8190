"""Live broker on a ccxt exchange (spot market orders), testnet by default.

Safety: refuses mainnet unless ``allow_mainnet`` is set, never logs API keys,
validates precision and market limits before sending, tags every order with a
client order id and, when the network fails after sending, reconciles once by
looking the order up on the exchange instead of guessing.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from typing import Any

import ccxt

from ..models import (
    Balances,
    ConfigError,
    Fill,
    InsufficientFundsError,
    JevError,
    OrderRejectedError,
    OrderRequest,
    Side,
)

logger = logging.getLogger(__name__)

RECONCILE_LOOKBACK_MS = 120_000
_FINAL_STATUSES = ("closed", "canceled", "cancelled", "expired", "rejected")


class OrderStateUnknownError(OrderRejectedError):
    """The order may or may not have been executed: check the exchange manually."""


def _num(value: Any) -> float:
    """Float from an exchange field; None/garbage/non-finite -> 0.0."""
    if value is None or isinstance(value, bool):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _opt_positive(value: Any) -> float | None:
    number = _num(value)
    return number if number > 0 else None


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _system_now_ms() -> int:
    return int(time.time() * 1000)


class CcxtBroker:
    """Broker contract implementation on top of a ccxt exchange instance."""

    def __init__(
        self,
        symbol: str,
        exchange_id: str = "binance",
        api_key: str = "",
        api_secret: str = "",
        use_testnet: bool = True,
        allow_mainnet: bool = False,
        fee_pct_estimate: float = 0.1,
        exchange: Any = None,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        if not use_testnet and not allow_mainnet:
            raise ConfigError("refusing to trade on mainnet: set allow_mainnet=True explicitly (real money)")
        if symbol.count("/") != 1 or ":" in symbol:
            raise ConfigError(f"spot symbol must look like BASE/QUOTE, got {symbol!r}")
        if not math.isfinite(fee_pct_estimate) or fee_pct_estimate < 0:
            raise ConfigError(f"fee_pct_estimate must be finite and >= 0, got {fee_pct_estimate!r}")
        self.symbol = symbol
        self.base_currency, self.quote_currency = symbol.split("/")
        self.exchange_id = exchange_id
        self.use_testnet = use_testnet
        self.fee_pct_estimate = float(fee_pct_estimate)
        self._now_ms = now_ms or _system_now_ms
        self._markets_loaded = False
        self._seq = 0
        self.exchange = exchange if exchange is not None else _create_exchange(exchange_id, api_key, api_secret)
        if use_testnet:
            self._enable_sandbox()
        else:
            logger.warning("CcxtBroker on %s MAINNET: orders use real money", exchange_id)

    def _enable_sandbox(self) -> None:
        set_sandbox = getattr(self.exchange, "set_sandbox_mode", None)
        if set_sandbox is None:
            raise ConfigError(f"exchange {self.exchange_id!r} cannot switch to testnet (no set_sandbox_mode)")
        try:
            set_sandbox(True)
        except Exception as exc:
            raise ConfigError(f"exchange {self.exchange_id!r} has no testnet: {_describe(exc)}") from exc

    def __repr__(self) -> str:
        net = "testnet" if self.use_testnet else "MAINNET"
        return f"CcxtBroker({self.exchange_id}, {self.symbol}, {net})"

    # ------------------------------------------------------------------ account

    def balances(self) -> Balances:
        """Free quote and base balances (missing currencies count as 0)."""
        try:
            raw = self.exchange.fetch_balance()
        except Exception as exc:
            raise JevError(f"could not fetch balances from {self.exchange_id}: {_describe(exc)}") from exc
        free = (raw or {}).get("free") or {}
        return Balances(
            cash=max(_num(free.get(self.quote_currency)), 0.0),
            base_qty=max(_num(free.get(self.base_currency)), 0.0),
        )

    def market(self) -> dict[str, Any]:
        """Market description for the symbol (loads markets once, lazily)."""
        if not self._markets_loaded:
            try:
                self.exchange.load_markets()
            except Exception as exc:
                raise OrderRejectedError(f"could not load markets: {_describe(exc)}") from exc
            self._markets_loaded = True
        try:
            market = self.exchange.market(self.symbol)
        except Exception as exc:
            raise OrderRejectedError(f"unknown market {self.symbol} on {self.exchange_id}") from exc
        if market.get("spot") is False:
            raise OrderRejectedError(f"{self.symbol} is not a spot market")
        if market.get("active") is False:
            raise OrderRejectedError(f"market {self.symbol} is not active")
        return market

    # ------------------------------------------------------------------ orders

    def execute(self, order: OrderRequest, market_price: float, timestamp: int) -> Fill:
        """Send a market order and return its real fill, or raise.

        ``timestamp`` names the client order id and bounds the reconciliation lookup;
        the Fill carries the exchange's execution time (``now_ms()`` if missing).
        """
        self._validate(order, market_price)
        market = self.market()
        quantity = self._clamp_sell(order.quantity) if order.side is Side.SELL else order.quantity
        amount = self._to_precision(quantity)
        self._check_limits(market, amount, market_price)
        self._seq += 1
        client_id = f"jev-{int(timestamp)}-{self._seq}"
        params = {"clientOrderId": client_id}
        logger.info(
            "sending market %s %.10g %s (~%.2f %s) id=%s",
            order.side.value, amount, self.base_currency, amount * market_price, self.quote_currency, client_id,
        )
        try:
            raw = self.exchange.create_order(self.symbol, "market", order.side.value, amount, None, params)
        except ccxt.InsufficientFunds as exc:
            raise InsufficientFundsError(f"exchange refused {client_id}: {_describe(exc)}") from exc
        except ccxt.ExchangeError as exc:  # InvalidOrder, auth, permissions, ...: nothing executed
            raise OrderRejectedError(f"exchange rejected {client_id}: {_describe(exc)}") from exc
        except Exception as exc:  # NetworkError or unexpected: the order may have been executed
            logger.warning("create_order %s failed after sending (%s): reconciling", client_id, _describe(exc))
            return self._reconcile(client_id, order.side, market_price, timestamp, exc)
        return self._complete(raw or {}, client_id, order.side, market_price, timestamp)

    def _validate(self, order: OrderRequest, market_price: float) -> None:
        if order.symbol != self.symbol:
            raise OrderRejectedError(f"order symbol {order.symbol!r} does not match broker symbol {self.symbol!r}")
        if not math.isfinite(order.quantity) or order.quantity <= 0:
            raise OrderRejectedError(f"order quantity must be finite and > 0, got {order.quantity!r}")
        if not math.isfinite(market_price) or market_price <= 0:
            raise OrderRejectedError(f"market price must be finite and > 0, got {market_price!r}")

    def _clamp_sell(self, quantity: float) -> float:
        try:
            free = self.balances().base_qty
        except JevError as exc:
            raise OrderRejectedError(f"cannot check the {self.base_currency} balance before selling") from exc
        if free <= 0:
            raise InsufficientFundsError(f"no free {self.base_currency} to sell")
        if quantity > free:
            logger.warning("SELL %.10g clamped to free balance %.10g %s", quantity, free, self.base_currency)
            return free
        return quantity

    def _to_precision(self, quantity: float) -> float:
        try:
            amount = float(self.exchange.amount_to_precision(self.symbol, quantity))
        except Exception as exc:
            raise OrderRejectedError(f"amount {quantity!r} rejected by precision rules: {_describe(exc)}") from exc
        if not math.isfinite(amount) or amount <= 0:
            raise OrderRejectedError(f"amount {quantity!r} rounds to {amount!r} at exchange precision")
        return amount

    def _check_limits(self, market: dict[str, Any], amount: float, market_price: float) -> None:
        limits = market.get("limits") or {}
        for key in ("amount", "market"):
            bounds = limits.get(key) or {}
            low, high = _opt_positive(bounds.get("min")), _opt_positive(bounds.get("max"))
            if low is not None and amount < low:
                raise OrderRejectedError(f"amount {amount:.10g} below exchange minimum {low:.10g} ({key})")
            if high is not None and amount > high:
                raise OrderRejectedError(f"amount {amount:.10g} above exchange maximum {high:.10g} ({key})")
        cost_min = _opt_positive((limits.get("cost") or {}).get("min"))
        if cost_min is not None and amount * market_price < cost_min:
            raise OrderRejectedError(
                f"order value {amount * market_price:.8f} {self.quote_currency} below exchange minimum {cost_min:.8f}"
            )

    # ------------------------------------------------------------------ fills

    def _complete(
        self, raw: dict[str, Any], client_id: str, side: Side, market_price: float, timestamp: int
    ) -> Fill:
        if _needs_fetch(raw):
            order_id = raw.get("id")
            if not order_id:
                logger.warning("create_order %s returned no id: reconciling", client_id)
                return self._reconcile(client_id, side, market_price, timestamp, None)
            try:
                raw = self.exchange.fetch_order(order_id, self.symbol) or {}
            except Exception as exc:
                logger.warning("fetch_order %s failed (%s): reconciling", order_id, _describe(exc))
                return self._reconcile(client_id, side, market_price, timestamp, exc)
        return self._to_fill(raw, client_id, side, market_price)

    def _reconcile(
        self, client_id: str, side: Side, market_price: float, timestamp: int, cause: BaseException | None
    ) -> Fill:
        """One lookup of ``client_id`` among recent orders; a Fill if it executed, else raise."""
        since = min(int(timestamp), self._now_ms()) - RECONCILE_LOOKBACK_MS
        unknown = f"order state unknown — check the exchange (client id {client_id})"
        try:
            orders = self.exchange.fetch_orders(self.symbol, since=since) or []
        except Exception as exc:
            logger.error("reconciliation of %s failed: %s", client_id, _describe(exc))
            raise OrderStateUnknownError(unknown) from (cause or exc)
        match = next(
            (o for o in orders if isinstance(o, dict) and o.get("clientOrderId") == client_id), None
        )
        if match is None:
            logger.error("reconciliation: %s not found on the exchange", client_id)
            raise OrderStateUnknownError(unknown) from cause
        if _num(match.get("filled")) > 0:
            logger.warning("reconciliation: %s was executed; recording its fill", client_id)
            return self._to_fill(match, client_id, side, market_price)
        if match.get("status") in _FINAL_STATUSES:
            raise OrderRejectedError(f"order {client_id} was not filled (status {match.get('status')})") from cause
        raise OrderStateUnknownError(unknown) from cause

    def _to_fill(self, raw: dict[str, Any], client_id: str, side: Side, market_price: float) -> Fill:
        filled = _num(raw.get("filled"))
        status = raw.get("status")
        order_id = str(raw.get("id") or client_id)
        if filled <= 0:
            if status in _FINAL_STATUSES:
                raise OrderRejectedError(f"order {order_id} was not filled (status {status})")
            raise OrderStateUnknownError(
                f"order state unknown — check the exchange (order {order_id} status {status}, nothing filled yet)"
            )
        if status not in (None, "closed"):
            logger.warning("order %s status %s: recording the %.10g filled so far", order_id, status, filled)
        price = self._fill_price(raw, filled, market_price, order_id)
        fee_quote, fee_base = self._fees(raw, filled * price)
        quantity = filled
        if side is Side.BUY and fee_base > 0:
            if fee_base < filled:
                quantity = filled - fee_base  # the fee was taken from the coins received
            else:
                logger.warning("order %s: base fee %.10g >= filled %.10g ignored", order_id, fee_base, filled)
                fee_base = 0.0
        fee = fee_quote + fee_base * price
        fill = Fill(
            order_id=order_id,
            symbol=self.symbol,
            side=side,
            quantity=quantity,
            price=price,
            fee=fee,
            timestamp=self._fill_time(raw),
        )
        logger.info(
            "filled %s %.10g %s @ %.10g fee %.6g %s (order %s)",
            side.value, quantity, self.base_currency, price, fee, self.quote_currency, order_id,
        )
        return fill

    def _fill_price(self, raw: dict[str, Any], filled: float, market_price: float, order_id: str) -> float:
        average = _opt_positive(raw.get("average"))
        if average is not None:
            return average
        cost = _opt_positive(raw.get("cost"))
        if cost is not None:
            return cost / filled
        logger.warning("order %s reports no price: using market price %.10g as an estimate", order_id, market_price)
        return market_price

    def _fees(self, raw: dict[str, Any], notional: float) -> tuple[float, float]:
        """(fee in quote, fee in base). Other currencies (e.g. BNB) are estimated in quote."""
        entries = [e for e in (raw.get("fees") or []) if isinstance(e, dict)]
        if not entries and isinstance(raw.get("fee"), dict):
            entries = [raw["fee"]]
        fee_quote = fee_base = 0.0
        others: list[str] = []
        known = False
        for entry in entries:
            if entry.get("cost") is None:
                continue
            known = True
            cost = max(_num(entry.get("cost")), 0.0)
            currency = entry.get("currency")
            if cost == 0:
                continue
            if currency == self.quote_currency:
                fee_quote += cost
            elif currency == self.base_currency:
                fee_base += cost
            else:
                others.append(str(currency))
        if others or not known:
            estimate = notional * self.fee_pct_estimate / 100.0
            logger.warning(
                "fee %s: estimating %.6g %s (%.3f%% of notional)",
                f"paid in {', '.join(sorted(set(others)))}" if others else "not reported",
                estimate, self.quote_currency, self.fee_pct_estimate,
            )
            fee_quote += estimate
        return fee_quote, fee_base

    def _fill_time(self, raw: dict[str, Any]) -> int:
        for key in ("lastTradeTimestamp", "timestamp"):
            value = _num(raw.get(key))
            if value > 0:
                return int(value)
        return int(self._now_ms())


def _needs_fetch(raw: dict[str, Any]) -> bool:
    """True when a create_order response lacks the data needed to build a Fill."""
    if raw.get("filled") is None:
        return True
    filled = _num(raw.get("filled"))
    if filled <= 0:
        return raw.get("status") not in _FINAL_STATUSES
    return _opt_positive(raw.get("average")) is None and _opt_positive(raw.get("cost")) is None


def _create_exchange(exchange_id: str, api_key: str, api_secret: str) -> Any:
    exchange_class = getattr(ccxt, exchange_id, None)
    if not isinstance(exchange_class, type):
        raise ConfigError(f"unknown ccxt exchange {exchange_id!r}")
    config: dict[str, Any] = {"enableRateLimit": True, "options": {"defaultType": "spot"}}
    if api_key:
        config["apiKey"] = api_key
    if api_secret:
        config["secret"] = api_secret
    return exchange_class(config)
