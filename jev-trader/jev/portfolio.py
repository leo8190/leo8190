"""Local position accounting for one spot symbol.

The portfolio is the source of truth for the cost basis of the position Jev Trader manages
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
# Balance changes no fill explains and smaller than this share of the account value are
# ignored (fee estimates for fees paid in another coin, rounding); bigger ones are
# deposits / withdrawals (see ``observe_balances``).
EXTERNAL_FLOW_TOLERANCE = 0.0005


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
    That remainder is not written off: it is carried (with its cost) into the next
    position, so it is sold with it and its cost ends up in the realized PnL.
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
        # Unsellable remainder of a closed position (below the minimum order), still held
        # by the broker; it joins the next position.
        self.carry_qty: float = 0.0
        self.carry_cost: float = 0.0

        # Broker balances the booked fills explain (None until the first observation).
        self.expected_cash: float | None = None
        self.expected_base: float | None = None

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
    def capital_in_use(self) -> float:
        """Quote spent on coins still held: the open position plus a carried remainder."""
        return self.cost_basis + self.carry_cost

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
        qty_before = self.qty if self.in_position else 0.0
        if fill.side is Side.BUY:
            self._apply_buy(fill, stop_loss, take_profit)
            record = None
        else:
            record = self._apply_sell(fill, reason)
        self.fees_paid += fill.fee
        self.trades_today += 1
        self._expect_fill(fill, qty_before)
        return record

    def _expect_fill(self, fill: Fill, qty_before: float) -> None:
        """Move the balances the next ``observe_balances`` expects by this fill."""
        if self.expected_cash is not None:
            notional = fill.quantity * fill.price
            self.expected_cash += -(notional + fill.fee) if fill.side is Side.BUY else notional - fill.fee
        if self.expected_base is not None:
            qty_after = self.qty if self.in_position else 0.0
            self.expected_base = qty_after if qty_after == 0.0 else self.expected_base + qty_after - qty_before

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
            if self.carry_qty > self.dust_qty:  # the unsellable remainder joins this position
                self.qty, self.avg_entry_price = self.carry_qty, self.carry_cost / self.carry_qty
            self.carry_qty = self.carry_cost = 0.0
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
            if self.qty > self.dust_qty:
                logger.info(
                    "remainder of %.10g %s is below the minimum order: carried into the next position",
                    self.qty, self.base_currency,
                )
                self.carry_qty += self.qty
                self.carry_cost += self.qty * avg
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
        """Shrink the local position (or carried remainder) to what the broker holds.

        Never grows the position (the cost of unknown coins is unknown). The cost of the
        coins written off is charged to the realized PnL. Returns the quantity written
        off (0.0 when nothing changed).
        """
        if not _finite(broker_base_qty):
            raise ValueError(f"broker_base_qty must be finite, got {broker_base_qty!r}")
        held = max(float(broker_base_qty), 0.0)
        if not self.in_position:
            if self.carry_qty <= 0 or held >= self.carry_qty - self.dust_qty:
                return 0.0
            missing = self.carry_qty - held
            logger.warning("broker no longer holds the carried remainder: writing off %.10g %s",
                           missing, self.base_currency)
            charged = self.carry_cost * missing / self.carry_qty
            self.carry_cost -= charged
            self.carry_qty = held
            self.realized_pnl -= charged
            return missing
        tolerance = max(self.dust_qty, self.qty * _SELL_REL_TOLERANCE)
        if held >= self.qty - tolerance:
            return 0.0
        missing = self.qty - held
        logger.warning(
            "broker holds %.10g %s but the portfolio tracks %.10g: writing off %.10g",
            held, self.base_currency, self.qty, missing,
        )
        self.realized_pnl -= missing * (self.avg_entry_price or 0.0)
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

    def observe_balances(self, ts: int, cash: float, base_qty: float, price: float) -> float:
        """Detect deposits and withdrawals before marking the equity at ``ts``.

        ``cash`` and ``base_qty`` are the balances the view uses (free quote cash and the
        managed base quantity). Any change the booked fills do not explain is an external
        flow (the user moved funds, bought or sold by hand): the equity peak and the day
        start move by it, so the kill switch only measures trading PnL. Returns the flow
        in quote units (positive = deposit, 0.0 when none).
        """
        if not all(_finite(v) for v in (cash, base_qty, price)) or price <= 0:
            raise ValueError(f"balances and price must be finite (price > 0), got {cash!r}, {base_qty!r}, {price!r}")
        flow = 0.0
        if self.expected_cash is not None and self.expected_base is not None:
            flow = (cash - self.expected_cash) + (base_qty - self.expected_base) * price
            if abs(flow) <= EXTERNAL_FLOW_TOLERANCE * (abs(cash) + abs(base_qty * price)):
                flow = 0.0
        self.expected_cash, self.expected_base = float(cash), float(base_qty)
        if flow:
            self._roll_day(utc_day(ts))
            if self.equity_peak is not None:
                self.equity_peak = max(self.equity_peak + flow, 0.0)
            if self.day_start_equity is not None:
                self.day_start_equity = max(self.day_start_equity + flow, 0.0)
            logger.warning(
                "balances changed by %+.6g %s outside Jev Trader (deposit/withdrawal or manual trade): "
                "kill-switch baselines adjusted, not counted as PnL", flow, self.quote_currency,
            )
        return flow

    def _roll_day(self, day: int) -> None:
        """Start a new UTC day when ``day`` is later than the current one.

        The new day starts from the last equity marked before the boundary (when it is
        from the previous day), so the move of the candle that closes at 00:00 UTC, and
        every candle on 1d timeframes, counts toward the daily loss.
        """
        if self.current_day is not None and day <= self.current_day:
            return
        last = self.equity_curve[-1] if self.equity_curve else None
        self.current_day = day
        self.trades_today = 0
        # None -> set by the first mark of the day (no recent equity to start from)
        self.day_start_equity = last[1] if last is not None and utc_day(last[0]) >= day - 1 else None
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
        """Base quantity Jev Trader manages: local position, capped by what the broker holds."""
        if not self.in_position:
            return 0.0
        return min(self.qty, max(float(broker_base_qty), 0.0))

    def view(self, balances: Balances, price: float) -> PortfolioView:
        """Merge broker balances with the local position at ``price``.

        ``base_qty`` is the managed position (local qty capped by the broker balance), so
        coins Jev Trader did not buy (pre-funded testnet coins, exchange dust) do not count as a
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

    def to_state(self, equity_points: int | None = None) -> dict[str, Any]:
        """JSON-serializable snapshot of the state (lossless with ``from_state``).

        ``equity_points`` keeps only the last N points of the equity curve (the live loop
        saves after every candle and only needs the last one; the full history is in the
        journal's equity table). None keeps the whole curve.
        """
        curve = self.equity_curve
        if equity_points is not None:
            curve = curve[max(len(curve) - max(equity_points, 0), 0):]
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
            "carry_qty": self.carry_qty,
            "carry_cost": self.carry_cost,
            "expected_cash": self.expected_cash,
            "expected_base": self.expected_base,
            "realized_pnl": self.realized_pnl,
            "fees_paid": self.fees_paid,
            "trades": [t.model_dump(mode="json") for t in self.trades],
            "equity_curve": [[ts, eq] for ts, eq in curve],
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
        p.carry_qty = float(state.get("carry_qty", 0.0))
        p.carry_cost = float(state.get("carry_cost", 0.0))
        p.expected_cash = _opt_float(state.get("expected_cash"))
        p.expected_base = _opt_float(state.get("expected_base"))
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
