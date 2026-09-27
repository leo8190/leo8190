"""Paper broker: simulated market orders with slippage and fees, no network."""

from __future__ import annotations

import logging
import math
from typing import Any

from ..models import (
    Balances,
    ConfigError,
    Fill,
    InsufficientFundsError,
    OrderRejectedError,
    OrderRequest,
    Side,
)

logger = logging.getLogger(__name__)

STATE_VERSION = 1
# A SELL may exceed the base balance by this relative amount (float noise); it is clamped.
_SELL_REL_TOLERANCE = 1e-9


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class PaperBroker:
    """In-memory broker. BUY fills at price*(1+slip), SELL at price*(1-slip); fee in quote."""

    def __init__(
        self,
        symbol: str,
        start_cash: float,
        fee_pct: float,
        slippage_pct: float,
        start_base: float = 0.0,
    ) -> None:
        checks = (
            ("start_cash", start_cash, 0.0, math.inf),
            ("start_base", start_base, 0.0, math.inf),
            ("fee_pct", fee_pct, 0.0, 100.0),
            ("slippage_pct", slippage_pct, 0.0, 100.0),
        )
        for name, value, low, high in checks:
            if not _finite(value) or not low <= value < high:
                raise ConfigError(f"{name} must be finite and in [{low:g}, {high:g}), got {value!r}")
        self.symbol = symbol
        self.cash = float(start_cash)
        self.base_qty = float(start_base)
        self.fee_pct = float(fee_pct)
        self.slippage_pct = float(slippage_pct)
        self._order_seq = 0

    def balances(self) -> Balances:
        return Balances(cash=self.cash, base_qty=self.base_qty)

    def execute(self, order: OrderRequest, market_price: float, timestamp: int) -> Fill:
        """Fill ``order`` entirely at ``market_price`` adjusted for slippage, or raise."""
        self._validate(order, market_price)
        if order.side is Side.BUY:
            quantity, price, fee = self._buy(order.quantity, market_price)
        else:
            quantity, price, fee = self._sell(order.quantity, market_price)
        self._order_seq += 1
        fill = Fill(
            order_id=f"paper-{self._order_seq}",
            symbol=self.symbol,
            side=order.side,
            quantity=quantity,
            price=price,
            fee=fee,
            timestamp=int(timestamp),
        )
        logger.info(
            "paper %s %.10g @ %.10g fee %.6g -> cash %.6f, base %.10g (%s)",
            order.side.value, quantity, price, fee, self.cash, self.base_qty, fill.order_id,
        )
        return fill

    def _validate(self, order: OrderRequest, market_price: float) -> None:
        if order.symbol != self.symbol:
            raise OrderRejectedError(f"order symbol {order.symbol!r} does not match broker symbol {self.symbol!r}")
        if not _finite(order.quantity) or order.quantity <= 0:
            raise OrderRejectedError(f"order quantity must be finite and > 0, got {order.quantity!r}")
        if not _finite(market_price) or market_price <= 0:
            raise OrderRejectedError(f"market price must be finite and > 0, got {market_price!r}")

    def _buy(self, quantity: float, market_price: float) -> tuple[float, float, float]:
        price = market_price * (1.0 + self.slippage_pct / 100.0)
        cost = quantity * price
        fee = cost * self.fee_pct / 100.0
        if self.cash < cost + fee:
            raise InsufficientFundsError(
                f"BUY needs {cost + fee:.8f} (cost {cost:.8f} + fee {fee:.8f}), cash is {self.cash:.8f}"
            )
        self.cash -= cost + fee
        self.base_qty += quantity
        return quantity, price, fee

    def _sell(self, quantity: float, market_price: float) -> tuple[float, float, float]:
        if quantity > self.base_qty:
            if quantity - self.base_qty > self.base_qty * _SELL_REL_TOLERANCE:
                raise InsufficientFundsError(f"SELL of {quantity!r} exceeds base balance {self.base_qty!r}")
            quantity = self.base_qty
        price = market_price * (1.0 - self.slippage_pct / 100.0)
        proceeds = quantity * price
        fee = proceeds * self.fee_pct / 100.0
        self.base_qty -= quantity
        self.cash += proceeds - fee
        return quantity, price, fee

    # ------------------------------------------------------------------ persistence

    def to_state(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "symbol": self.symbol,
            "cash": self.cash,
            "base_qty": self.base_qty,
            "fee_pct": self.fee_pct,
            "slippage_pct": self.slippage_pct,
            "order_seq": self._order_seq,
        }

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> PaperBroker:
        if state.get("version") != STATE_VERSION:
            raise ValueError(f"unsupported paper broker state version {state.get('version')!r}")
        broker = cls(
            symbol=state["symbol"],
            start_cash=state["cash"],
            fee_pct=state["fee_pct"],
            slippage_pct=state["slippage_pct"],
            start_base=state["base_qty"],
        )
        broker._order_seq = int(state["order_seq"])
        return broker

    def __repr__(self) -> str:
        return f"PaperBroker({self.symbol}, cash={self.cash:.6f}, base={self.base_qty:.10g})"
