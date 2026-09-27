"""Local position accounting for one spot symbol.

The portfolio is the source of truth for the cost basis of the position JEB manages
(average entry price including buy fees, entry time, protective levels), realized
PnL, the equity curve and the daily counters used by the kill switch. Cash and base
balances come from the broker; ``view`` merges both into a ``PortfolioView``.

Conventions: timestamps are int ms UTC, UTC days are ``timestamp // 86_400_000``,
``*_pct`` values are percent. Long-only: a SELL only reduces the position.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from .models import Balances, Fill, PortfolioView, Side, TradeRecord

logger = logging.getLogger(__name__)

DAY_MS = 86_400_000
STATE_VERSION = 1
# A SELL may exceed the held quantity by this relative amount (float noise); it is clamped.
_SELL_REL_TOLERANCE = 1e-9


def utc_day(timestamp_ms: int) -> int:
    """UTC day index (days since the Unix epoch) of a millisecond timestamp."""
    return int(timestamp_ms) // DAY_MS


def _finite(value: float) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class Portfolio:
    """Position, cost basis, PnL, equity curve and daily risk counters for one symbol.

    ``dust_qty`` (base units): a remaining position at or below it is treated as closed.
    ``dust_notional`` (quote units, optional): after a SELL, a remainder worth less than
    this at the fill price is also treated as closed. Set it to the exchange minimum
    order notional in live trading, where precision truncation leaves unsellable dust.
    """

    def __init__(
        self,
        symbol: str,
        quote_currency: str,
        base_currency: str,
        dust_qty: float = 1e-9,
        dust_notional: float = 0.0,
    ) -> None:
        if not _finite(dust_qty) or dust_qty < 0:
            raise ValueError(f"dust_qty must be a finite number >= 0, got {dust_qty!r}")
        if not _finite(dust_notional) or dust_notional < 0:
            raise ValueError(f"dust_notional must be a finite number >= 0, got {dust_notional!r}")
        self.symbol = symbol
        self.quote_currency = quote_currency
        self.base_currency = base_currency
        self.dust_qty = float(dust_qty)
        self.dust_notional = float(dust_notional)

        # Open position (all None/0 when flat).
        self.qty: float = 0.0
        self.avg_entry_price: float | None = None  # includes buy fees
        self.entry_time: int | None = None
        self.entry_fees: float = 0.0  # buy fees of the open position not yet attributed to a trade
        self.stop_loss: float | None = None
        self.take_profit: float | None = None

        # Results.
        self.realized_pnl: float = 0.0
        self.fees_paid: float = 0.0
        self.trades: list[TradeRecord] = []
        self.equity_curve: list[tuple[int, float]] = []
        self.equity_peak: float | None = None

        # Daily counters and kill switch.
        self.current_day: int | None = None
        self.day_start_equity: float | None = None
        self.trades_today: int = 0
        self.last_exit_ts: int | None = None
        self.halted_reason: str | None = None
        self.halted_day: int | None = None

    # ------------------------------------------------------------------ properties

    @property
    def in_position(self) -> bool:
        return self.qty > self.dust_qty

    @property
    def cost_basis(self) -> float:
        """Quote spent on the open position, buy fees included."""
        if not self.in_position or self.avg_entry_price is None:
            return 0.0
        return self.qty * self.avg_entry_price

    @property
    def last_equity(self) -> float | None:
        return self.equity_curve[-1][1] if self.equity_curve else None

    # ------------------------------------------------------------------ fills

    def apply_fill(
        self,
        fill: Fill,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        reason: str = "",
    ) -> TradeRecord | None:
        """Book a broker fill. Returns the TradeRecord for a SELL, None for a BUY.

        Raises ValueError (and leaves the state untouched) for a malformed fill, a fill
        for another symbol, or a SELL larger than the held quantity.
        """
        self._validate_fill(fill)
        if fill.side is Side.SELL:
            self._check_sell_qty(fill.quantity)
        self._roll_day(utc_day(fill.timestamp))
        if fill.side is Side.BUY:
            self._apply_buy(fill, stop_loss, take_profit)
            record = None
        else:
            record = self._apply_sell(fill, reason)
        self.fees_paid += fill.fee
        self.trades_today += 1
        return record

    def _validate_fill(self, fill: Fill) -> None:
        if fill.symbol != self.symbol:
            raise ValueError(f"fill symbol {fill.symbol!r} does not match portfolio symbol {self.symbol!r}")
        if not _finite(fill.quantity) or fill.quantity <= 0:
            raise ValueError(f"fill quantity must be finite and > 0, got {fill.quantity!r}")
        if not _finite(fill.price) or fill.price <= 0:
            raise ValueError(f"fill price must be finite and > 0, got {fill.price!r}")
        if not _finite(fill.fee) or fill.fee < 0:
            raise ValueError(f"fill fee must be finite and >= 0, got {fill.fee!r}")

    def _check_sell_qty(self, quantity: float) -> None:
        if not self.in_position:
            raise ValueError(f"cannot sell {quantity} {self.base_currency}: no open position")
        tolerance = max(self.dust_qty, self.qty * _SELL_REL_TOLERANCE)
        if quantity > self.qty + tolerance:
            raise ValueError(
                f"cannot sell {quantity} {self.base_currency}: only {self.qty} held (long-only)"
            )

    def _apply_buy(self, fill: Fill, stop_loss: float | None, take_profit: float | None) -> None:
        if not self.in_position:
            self._reset_position()
            self.entry_time = fill.timestamp
        total_cost = self.cost_basis + fill.quantity * fill.price + fill.fee
        self.qty += fill.quantity
        self.avg_entry_price = total_cost / self.qty
        self.entry_fees += fill.fee
        if stop_loss is not None:
            self.stop_loss = float(stop_loss)
        if take_profit is not None:
            self.take_profit = float(take_profit)
        logger.info(
            "BUY %.10g %s @ %.10g (fee %.6g) -> qty %.10g, avg entry %.10g",
            fill.quantity, self.base_currency, fill.price, fill.fee, self.qty, self.avg_entry_price,
        )

    def _apply_sell(self, fill: Fill, reason: str) -> TradeRecord:
        sold = min(fill.quantity, self.qty)
        avg = self.avg_entry_price or 0.0
        cost = avg * sold
        pnl = (sold * fill.price - fill.fee) - cost
        pnl_pct = pnl / cost * 100.0 if cost > 0 else 0.0
        entry_fee_part = self.entry_fees * (sold / self.qty)
        record = TradeRecord(
            symbol=self.symbol,
            entry_price=avg,
            exit_price=fill.price,
            quantity=sold,
            pnl=pnl,
            pnl_pct=pnl_pct,
            fees=entry_fee_part + fill.fee,
            entry_time=self.entry_time,
            exit_time=fill.timestamp,
            exit_reason=reason,
        )
        self.trades.append(record)
        self.realized_pnl += pnl
        self.entry_fees -= entry_fee_part
        self.qty -= sold
        logger.info(
            "SELL %.10g %s @ %.10g (fee %.6g, %s): pnl %.6g (%.3f%%), remaining %.10g",
            sold, self.base_currency, fill.price, fill.fee, reason or "no reason", pnl, pnl_pct, self.qty,
        )
        if self._is_dust(self.qty, fill.price):
            if self.qty > 0:
                logger.info("writing off dust remainder of %.10g %s", self.qty, self.base_currency)
            self._reset_position()
            self.last_exit_ts = fill.timestamp
        return record

    def _is_dust(self, qty: float, price: float) -> bool:
        return qty <= self.dust_qty or qty * price < self.dust_notional

    def _reset_position(self) -> None:
        self.qty = 0.0
        self.avg_entry_price = None
        self.entry_time = None
        self.entry_fees = 0.0
        self.stop_loss = None
        self.take_profit = None

    def reconcile(self, broker_base_qty: float, timestamp: int) -> float:
        """Shrink the local position to what the broker actually holds.

        Never grows the position (the cost of unknown coins is unknown). Returns the
        quantity written off (0.0 when nothing changed).
        """
        if not _finite(broker_base_qty):
            raise ValueError(f"broker_base_qty must be finite, got {broker_base_qty!r}")
        if not self.in_position:
            return 0.0
        held = max(float(broker_base_qty), 0.0)
        tolerance = max(self.dust_qty, self.qty * _SELL_REL_TOLERANCE)
        if held >= self.qty - tolerance:
            return 0.0
        missing = self.qty - held
        logger.warning(
            "broker holds %.10g %s but the portfolio tracks %.10g: writing off %.10g",
            held, self.base_currency, self.qty, missing,
        )
        self.entry_fees *= held / self.qty
        self.qty = held
        if self.qty <= self.dust_qty:
            self._reset_position()
            self.last_exit_ts = int(timestamp)
        return missing

    # ------------------------------------------------------------------ equity and days

    def mark(self, ts: int, equity: float) -> None:
        """Record the account equity at ``ts`` (equity curve, peak, UTC day start)."""
        if not _finite(equity):
            raise ValueError(f"equity must be finite, got {equity!r}")
        self._roll_day(utc_day(ts))
        if self.day_start_equity is None:
            self.day_start_equity = float(equity)
        self.equity_curve.append((int(ts), float(equity)))
        if self.equity_peak is None or equity > self.equity_peak:
            self.equity_peak = float(equity)

    def _roll_day(self, day: int) -> None:
        """Start a new UTC day when ``day`` is later than the current one."""
        if self.current_day is not None and day <= self.current_day:
            return
        self.current_day = day
        self.trades_today = 0
        self.day_start_equity = None  # set by the first mark of the day
        if self.halted_reason == "daily_loss" and (self.halted_day is None or self.halted_day < day):
            logger.info("new UTC day: daily-loss halt cleared")
            self.halted_reason = None
            self.halted_day = None

    def daily_pnl_pct(self, equity: float) -> float:
        """Loss since the UTC day start in percent. POSITIVE means LOSS (negative = gain)."""
        start = self.day_start_equity
        if start is None or start <= 0:
            return 0.0
        return (start - equity) / start * 100.0

    def drawdown_pct(self, equity: float) -> float:
        """Loss from the equity peak in percent (>= 0; positive means loss)."""
        if self.equity_peak is None:
            return 0.0
        peak = max(self.equity_peak, equity)
        if peak <= 0:
            return 0.0
        return (peak - equity) / peak * 100.0

    def trades_on(self, ts: int) -> int:
        """Fills counted for the UTC day of ``ts`` (0 if that day has not started yet)."""
        if self.current_day is not None and utc_day(ts) > self.current_day:
            return 0
        return self.trades_today

    # ------------------------------------------------------------------ kill switch state

    @property
    def halted(self) -> bool:
        return self.halted_reason is not None

    def active_halt(self, ts: int) -> str | None:
        """Halt reason in force at ``ts`` (a daily-loss halt ends with its UTC day)."""
        if self.halted_reason == "daily_loss" and self.halted_day is not None and utc_day(ts) > self.halted_day:
            return None
        return self.halted_reason

    def halt(self, reason: str) -> None:
        self.halted_reason = reason
        self.halted_day = self.current_day

    def clear_halt(self) -> None:
        self.halted_reason = None
        self.halted_day = None

    # ------------------------------------------------------------------ view

    def managed_qty(self, broker_base_qty: float) -> float:
        """Base quantity JEB manages: local position, capped by what the broker holds."""
        if not self.in_position:
            return 0.0
        return min(self.qty, max(float(broker_base_qty), 0.0))

    def view(self, balances: Balances, price: float) -> PortfolioView:
        """Merge broker balances with the local position at ``price``.

        ``base_qty`` is the managed position (local qty capped by the broker balance), so
        coins JEB did not buy (pre-funded testnet coins, exchange dust) do not count as a
        position nor as equity. Equity = broker cash + managed qty * price.
        """
        if not _finite(price) or price <= 0:
            raise ValueError(f"price must be finite and > 0, got {price!r}")
        if not _finite(balances.cash) or not _finite(balances.base_qty):
            raise ValueError(f"balances must be finite, got {balances!r}")
        qty = self.managed_qty(balances.base_qty)
        position_value = qty * price
        holding = qty > 0
        avg = self.avg_entry_price if holding else None
        unrealized = (price / avg - 1.0) * 100.0 if avg else None
        return PortfolioView(
            quote_currency=self.quote_currency,
            base_currency=self.base_currency,
            cash=balances.cash,
            base_qty=qty,
            avg_entry_price=avg,
            equity=balances.cash + position_value,
            position_value=position_value,
            unrealized_pnl_pct=unrealized,
            stop_loss=self.stop_loss if holding else None,
            take_profit=self.take_profit if holding else None,
        )

    # ------------------------------------------------------------------ persistence

    def to_state(self) -> dict[str, Any]:
        """JSON-serializable snapshot of the full state (lossless with ``from_state``)."""
        return {
            "version": STATE_VERSION,
            "symbol": self.symbol,
            "quote_currency": self.quote_currency,
            "base_currency": self.base_currency,
            "dust_qty": self.dust_qty,
            "dust_notional": self.dust_notional,
            "qty": self.qty,
            "avg_entry_price": self.avg_entry_price,
            "entry_time": self.entry_time,
            "entry_fees": self.entry_fees,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "realized_pnl": self.realized_pnl,
            "fees_paid": self.fees_paid,
            "trades": [t.model_dump(mode="json") for t in self.trades],
            "equity_curve": [[ts, eq] for ts, eq in self.equity_curve],
            "equity_peak": self.equity_peak,
            "current_day": self.current_day,
            "day_start_equity": self.day_start_equity,
            "trades_today": self.trades_today,
            "last_exit_ts": self.last_exit_ts,
            "halted_reason": self.halted_reason,
            "halted_day": self.halted_day,
        }

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> Portfolio:
        """Rebuild a Portfolio from ``to_state`` output. Raises ValueError if incompatible."""
        version = state.get("version")
        if version != STATE_VERSION:
            raise ValueError(f"unsupported portfolio state version {version!r}")
        p = cls(
            symbol=state["symbol"],
            quote_currency=state["quote_currency"],
            base_currency=state["base_currency"],
            dust_qty=state["dust_qty"],
            dust_notional=state.get("dust_notional", 0.0),
        )
        p.qty = float(state["qty"])
        p.avg_entry_price = _opt_float(state["avg_entry_price"])
        p.entry_time = _opt_int(state["entry_time"])
        p.entry_fees = float(state["entry_fees"])
        p.stop_loss = _opt_float(state["stop_loss"])
        p.take_profit = _opt_float(state["take_profit"])
        p.realized_pnl = float(state["realized_pnl"])
        p.fees_paid = float(state["fees_paid"])
        p.trades = [TradeRecord.model_validate(t) for t in state["trades"]]
        p.equity_curve = [(int(ts), float(eq)) for ts, eq in state["equity_curve"]]
        p.equity_peak = _opt_float(state["equity_peak"])
        p.current_day = _opt_int(state["current_day"])
        p.day_start_equity = _opt_float(state["day_start_equity"])
        p.trades_today = int(state["trades_today"])
        p.last_exit_ts = _opt_int(state["last_exit_ts"])
        p.halted_reason = state["halted_reason"]
        p.halted_day = _opt_int(state["halted_day"])
        return p

    def __repr__(self) -> str:
        return (
            f"Portfolio({self.symbol}, qty={self.qty:.10g}, avg_entry={self.avg_entry_price}, "
            f"realized_pnl={self.realized_pnl:.6g}, halted={self.halted_reason})"
        )


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _opt_int(value: Any) -> int | None:
    return None if value is None else int(value)
