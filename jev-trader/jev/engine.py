"""Realtime trading loop shared by paper and live trading.

One ``step`` processes the latest closed candle:

    market data -> snapshot -> protective exits -> equity mark -> kill switch
    -> decision engine -> risk manager -> broker -> portfolio -> journal

Protective exits and kill switches never depend on the decision engine. Broker
errors and market-data errors are journaled as events and never crash the loop.
The portfolio (and the paper broker balances) are persisted in the journal after
every step so a restart resumes where it left off.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .brain.base import DecisionEngine
from .config import Settings
from .execution.base import Broker
from .execution.ccxt_broker import OrderStateUnknownError
from .execution.paper import PaperBroker
from .features import build_snapshot
from .journal import Journal
from .market.base import MarketDataSource
from .market.timeframes import floor_to_timeframe, timeframe_to_ms
from .models import (
    Balances,
    Candle,
    Decision,
    Fill,
    InsufficientFundsError,
    JevError,
    MarketDataError,
    MarketSnapshot,
    OrderRejectedError,
    OrderRequest,
    PortfolioView,
    RiskVerdict,
    Side,
    TradeRecord,
)
from .portfolio import Portfolio
from .risk import DAILY_LOSS, MAX_DRAWDOWN, RiskManager

logger = logging.getLogger(__name__)

STATE_VERSION = 1
ORDER_STATE_UNKNOWN = "order_state_unknown"  # halt reason: a human must check the exchange
KILL_REASONS = (DAILY_LOSS, MAX_DRAWDOWN)
MAX_CONSECUTIVE_ERRORS = 5
# The newest closed candle may be this late (beyond one timeframe) before new entries
# are skipped: an exchange whose klines stall must not trigger trades on old prices.
STALE_GRACE_MS = 120_000
SAVED_EQUITY_POINTS = 1  # the equity history lives in the journal's equity table

_EVENT_LEVELS = {  # log level per journal event kind (default WARNING)
    "start": logging.INFO,
    "stop": logging.INFO,
    "resume": logging.INFO,
    "stop_loss": logging.INFO,
    "take_profit": logging.INFO,
    ORDER_STATE_UNKNOWN: logging.ERROR,
    "booking_error": logging.ERROR,
}

# TickResult.status values
OK = "ok"
NO_NEW_CANDLE = "no_new_candle"
SKIPPED = "skipped"


def wall_clock_ms() -> int:
    return int(time.time() * 1000)


def iso_utc(ts_ms: int | None) -> str:
    """``2026-01-01T00:05Z`` style UTC time (seconds only when non-zero)."""
    if ts_ms is None:
        return "-"
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ" if dt.second else "%Y-%m-%dT%H:%MZ")


def execute_order(
    broker: Broker,
    portfolio: Portfolio,
    order: OrderRequest,
    market_price: float,
    timestamp: int,
    *,
    exit_reason: str = "",
    stop_loss: float | None = None,
    take_profit: float | None = None,
    journal: Journal | None = None,
) -> tuple[Fill, TradeRecord | None]:
    """Send ``order`` to ``broker`` and book the fill (shared by live loop and backtest).

    The fill is journaled before it is booked, so a booking error never loses a real
    fill; a journal error never stops the booking either. A BUY's stop-loss and
    take-profit keep their distance in % but are moved from ``order.reference_price``
    (the decision close) to the real fill price. Broker errors (InsufficientFundsError,
    OrderRejectedError) propagate.
    """
    fill = broker.execute(order, market_price, timestamp)
    _journal_safely(journal, "record_fill", fill, order.reason)
    if order.side is Side.BUY and order.reference_price > 0:
        scale = fill.price / order.reference_price
        stop_loss = None if stop_loss is None else stop_loss * scale
        take_profit = None if take_profit is None else take_profit * scale
    trade = portfolio.apply_fill(fill, stop_loss=stop_loss, take_profit=take_profit, reason=exit_reason)
    if trade is not None:
        _journal_safely(journal, "record_trade", trade)
    return fill, trade


def _journal_safely(journal: Journal | None, method: str, *args: Any) -> None:
    """Journal write that never prevents booking a real fill (e.g. a locked SQLite file)."""
    if journal is None:
        return
    try:
        getattr(journal, method)(*args)
    except Exception:
        logger.exception("journal %s failed; the fill is still booked in the portfolio", method)


def safe_decide(engine: DecisionEngine, snapshot: MarketSnapshot, view: PortfolioView) -> Decision:
    """``engine.decide`` with a last-resort guard (engines must not raise; never trust it)."""
    try:
        return engine.decide(snapshot, view)
    except Exception as exc:
        logger.exception("decision engine %s raised", getattr(engine, "name", "?"))
        return Decision.hold(f"engine error ({type(exc).__name__})", source="fallback")


@dataclass
class TickResult:
    """What one ``TradingEngine.step`` did (for printing and tests)."""

    status: str
    timestamp: int | None = None  # open time of the last closed candle
    price: float | None = None  # market price used for orders and equity
    decision: Decision | None = None
    verdict: RiskVerdict | None = None
    fills: list[Fill] = field(default_factory=list)
    trades: list[TradeRecord] = field(default_factory=list)
    events: list[str] = field(default_factory=list)  # event kinds journaled this step
    equity: float | None = None
    halted: str | None = None


def _default_clock(market: Any) -> Callable[[], int]:
    """Simulated markets expose ``now_ms()``; real ones use the wall clock."""
    now = getattr(market, "now_ms", None)
    return now if callable(now) else wall_clock_ms


class TradingEngine:
    """Wires market data, decision engine, risk manager, broker, portfolio and journal."""

    def __init__(
        self,
        settings: Settings,
        market: MarketDataSource,
        broker: Broker,
        decision_engine: DecisionEngine,
        journal: Journal,
        *,
        portfolio: Portfolio | None = None,
        risk: RiskManager | None = None,
        clock_ms: Callable[[], int] | None = None,
        state_key: str = "default",
        max_capital: float = 0.0,
    ) -> None:
        """``max_capital`` (> 0): the most quote the bot trades with (plus its realized PnL),
        however much free cash the account holds; 0 uses the whole free balance."""
        if not math.isfinite(max_capital) or max_capital < 0:
            raise ValueError(f"max_capital must be finite and >= 0, got {max_capital!r}")
        self.max_capital = float(max_capital)
        self.settings = settings
        self.market = market
        self.broker = broker
        self.decision_engine = decision_engine
        self.journal = journal
        self.portfolio = portfolio or Portfolio(settings.symbol, settings.quote_currency, settings.base_currency)
        self.risk = risk or RiskManager(settings.risk, settings.fee_pct, settings.slippage_pct)
        self.tf_ms = timeframe_to_ms(settings.timeframe)
        self.clock_ms = clock_ms or _default_clock(market)
        self.state_key = state_key
        self.last_candle_ts: int | None = None
        self._order_in_flight: dict[str, Any] | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ state

    @property
    def state_name(self) -> str:
        return f"engine:{self.state_key}"

    def save_state(self) -> None:
        """Persist the portfolio, paper balances and the last processed candle."""
        state: dict[str, Any] = {
            "version": STATE_VERSION,
            "symbol": self.settings.symbol,
            "timeframe": self.settings.timeframe,
            "last_candle_ts": self.last_candle_ts,
            "portfolio": self.portfolio.to_state(equity_points=SAVED_EQUITY_POINTS),
            "order_in_flight": self._order_in_flight,
        }
        to_state = getattr(self.broker, "to_state", None)
        if callable(to_state):
            state["broker"] = to_state()
        self.journal.save_state(self.state_name, state)

    def restore_state(self) -> bool:
        """Resume from the journal. Returns False when there is nothing (compatible) to load."""
        state = self.journal.load_state(self.state_name)
        if not state:
            return False
        expected = (STATE_VERSION, self.settings.symbol, self.settings.timeframe)
        found = (state.get("version"), state.get("symbol"), state.get("timeframe"))
        if found != expected:
            open_position = bool((state.get("portfolio") or {}).get("qty"))
            logger.log(
                logging.ERROR if open_position else logging.WARNING,
                "saved state %s is incompatible (%s != %s); starting fresh%s", self.state_name, found, expected,
                " - IT HAS AN OPEN POSITION that will not be managed" if open_position else "",
            )
            return False
        self.portfolio = Portfolio.from_state(state["portfolio"])
        self.last_candle_ts = state.get("last_candle_ts")
        if state.get("broker") is not None and isinstance(self.broker, PaperBroker):
            restored = PaperBroker.from_state(state["broker"])
            restored.fee_pct, restored.slippage_pct = self.broker.fee_pct, self.broker.slippage_pct  # settings win
            self.broker = restored
        now = self.clock_ms()
        in_flight = state.get("order_in_flight")
        if in_flight:  # the last session died while an order was being sent: its outcome is unknown
            self.portfolio.halt(ORDER_STATE_UNKNOWN)
            self._event(
                now, ORDER_STATE_UNKNOWN,
                f"the previous session stopped while sending an order ({in_flight}); check the exchange, "
                "then clear the halt with `jev status --reset-halt`",
            )
        self._reconcile(now)
        p = self.portfolio
        self._event(
            now, "resume",
            f"resumed: qty {p.qty:.10g} {p.base_currency}, stop {p.stop_loss}, tp {p.take_profit}, "
            f"halt {p.halted_reason}, last candle {iso_utc(self.last_candle_ts)}",
        )
        return True

    def _reconcile(self, now: int) -> None:
        try:
            balances = self.broker.balances()
        except JevError as exc:
            logger.warning("could not reconcile with the broker: %s", exc)
            return
        missing = self.portfolio.reconcile(balances.base_qty, now)
        if missing > 0:
            self._event(now, "reconcile", f"broker holds less than tracked: wrote off {missing:.10g}")

    # ------------------------------------------------------------------ one step

    def step(self) -> TickResult:
        """Process the latest closed candle once. Never raises for market or broker errors."""
        candles = self._fetch_candles()
        if not candles:
            return TickResult(SKIPPED, events=["market_error"])
        last = candles[-1]
        if self.last_candle_ts is not None and last.timestamp <= self.last_candle_ts:
            return TickResult(NO_NEW_CANDLE, timestamp=last.timestamp)
        now = self.clock_ms()
        try:
            snapshot = build_snapshot(self.settings.symbol, self.settings.timeframe, candles)
        except MarketDataError as exc:
            self._event(now, "market_error", f"invalid candles: {exc}")
            return TickResult(SKIPPED, timestamp=last.timestamp, events=["market_error"])
        price = self._market_price(snapshot)
        result = TickResult(OK, timestamp=snapshot.timestamp, price=price)
        age_ms = now - (last.timestamp + self.tf_ms)  # time since the newest closed candle closed
        stale = age_ms > self.tf_ms + STALE_GRACE_MS
        try:
            self._trade_tick(candles, snapshot, price, now, result, stale_ms=age_ms if stale else None)
        except JevError as exc:  # e.g. balances unavailable: skip this tick, retry on the next one
            self._event(now, "broker_error", str(exc), result)
            result.status = SKIPPED
            self.save_state()  # keep any fill booked before the failure
            return result
        self.last_candle_ts = last.timestamp
        self.save_state()
        return result

    def _trade_tick(
        self, candles: Sequence[Candle], snapshot: MarketSnapshot, price: float, now: int, result: TickResult,
        stale_ms: int | None = None,
    ) -> None:
        exited = self._protective_exits(self._unprocessed(candles), price, now, result)
        view = self._view(price)
        flow = self.portfolio.observe_balances(now, view.cash, view.base_qty, price)
        if flow:
            self._event(
                now, "external_flow",
                f"{flow:+.2f} {view.quote_currency} moved outside the bot (deposit, withdrawal or manual trade): "
                "not counted as PnL; kill-switch baselines adjusted",
                result,
            )
        self.portfolio.mark(now, view.equity)
        self.journal.record_equity(now, view.equity, price)
        result.equity = view.equity
        halt = self._kill_switch(view, price, now, result)
        if exited:
            logger.info("protective exit this tick: decision skipped")
        elif halt and not self.portfolio.in_position:
            logger.info("trading halted (%s) and flat: decision skipped", halt)
        elif stale_ms is not None:
            self._event(
                now, "stale_data",
                f"newest closed candle {iso_utc(snapshot.timestamp)} closed {stale_ms / 60_000:.1f} min ago: "
                "decision skipped (stops and kill switch still checked)",
                result,
            )
        else:
            self._decide_and_trade(snapshot, price, now, result)
        if result.fills:
            result.equity = self._view(price).equity

    def _fetch_candles(self) -> list[Candle] | None:
        s = self.settings
        try:
            candles = self.market.fetch_candles(s.symbol, s.timeframe, s.history_candles)
        except MarketDataError as exc:
            self._event(self.clock_ms(), "market_error", str(exc))
            return None
        if not candles:
            self._event(self.clock_ms(), "market_error", "no closed candles returned")
            return None
        return candles

    def _market_price(self, snapshot: MarketSnapshot) -> float:
        """Current price for orders and equity; falls back to the last close."""
        fetch = getattr(self.market, "fetch_last_price", None)
        if not callable(fetch):
            return snapshot.price
        try:
            price = float(fetch(self.settings.symbol))
        except MarketDataError as exc:
            logger.warning("last price unavailable (%s); using the last close", exc)
            return snapshot.price
        return price if math.isfinite(price) and price > 0 else snapshot.price

    def _view(self, price: float) -> PortfolioView:
        return self.portfolio.view(self._balances(), price)

    def _balances(self) -> Balances:
        """Broker balances, with the cash capped to the bot's own capital when ``max_capital`` is set."""
        balances = self.broker.balances()
        if self.max_capital > 0:
            own = max(self.max_capital + self.portfolio.realized_pnl - self.portfolio.capital_in_use, 0.0)
            if balances.cash > own:
                balances = Balances(cash=own, base_qty=balances.base_qty)
        return balances

    def _unprocessed(self, candles: Sequence[Candle]) -> list[Candle]:
        """Closed candles not seen yet (all missed candles after a restart)."""
        if self.last_candle_ts is None:
            return [candles[-1]]
        return [c for c in candles if c.timestamp > self.last_candle_ts]

    # ------------------------------------------------------------------ exits

    def _protective_exits(self, candles: list[Candle], price: float, now: int, result: TickResult) -> bool:
        """Stop-loss / take-profit on every new candle that closed after the entry."""
        p = self.portfolio
        if not p.in_position:
            return False
        entry = p.entry_time or 0
        for candle in candles:
            if candle.timestamp + self.tf_ms <= entry:
                continue  # closed before the position was opened
            hit = self.risk.check_protective_exit(p, candle.low, candle.high, candle.open)
            if hit is None:
                continue
            reason, level = hit
            message = (
                f"{reason} touched at {level:.8g} (candle {iso_utc(candle.timestamp)}, "
                f"low {candle.low:.8g} high {candle.high:.8g}); market exit at ~{price:.8g}"
            )
            self._event(now, reason, message, result)
            return self._flatten(reason, price, now, result, exit_reason=reason)
        return False

    def _kill_switch(self, view: PortfolioView, price: float, now: int, result: TickResult) -> str | None:
        before = self.portfolio.halted_reason
        reason = self.risk.update_kill_switch(self.portfolio, view.equity)
        active = self.portfolio.active_halt(now) if reason else None
        result.halted = active
        if active is None:
            return None
        if active != before:
            self._event(
                now, "kill_switch",
                f"{active}: drawdown {self.portfolio.drawdown_pct(view.equity):.2f}%, "
                f"daily loss {self.portfolio.daily_pnl_pct(view.equity):.2f}%",
                result,
            )
        if active in KILL_REASONS and self.settings.risk.flatten_on_kill and view.in_position:
            self._flatten(active, price, now, result, exit_reason=f"kill_switch:{active}")
        return active

    def _flatten(self, reason: str, price: float, now: int, result: TickResult, exit_reason: str) -> bool:
        order = self.risk.forced_exit_order(self._view(price), reason, price)
        if order is None:
            return False
        return self._execute(order, price, now, result, exit_reason=exit_reason) is not None

    # ------------------------------------------------------------------ decisions

    def _decide_and_trade(self, snapshot: MarketSnapshot, price: float, now: int, result: TickResult) -> None:
        view = self._view(price)
        decision = safe_decide(self.decision_engine, snapshot, view)
        verdict = self.risk.evaluate(decision, view, snapshot, self.portfolio)
        self.journal.record_decision(snapshot, decision, verdict)
        result.decision, result.verdict = decision, verdict
        if verdict.approved and verdict.order is not None:
            self._execute(
                verdict.order, price, now, result,
                exit_reason="signal", stop_loss=verdict.stop_loss, take_profit=verdict.take_profit,
            )

    def _execute(
        self,
        order: OrderRequest,
        price: float,
        now: int,
        result: TickResult,
        exit_reason: str,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> Fill | None:
        """Execute and book an order; broker errors become journal events.

        The order is marked "in flight" in the saved state while it is sent, so a crash or
        a forced quit (second Ctrl+C) in that window halts new entries on the next start
        instead of silently losing a real fill (see ``restore_state``).
        """
        self._order_in_flight = {
            "side": order.side.value, "quantity": order.quantity, "ts": now, "client_id_prefix": f"jev-{int(now)}-",
        }
        try:
            self.save_state()
        except Exception:
            self._order_in_flight = None
            raise
        try:
            fill, trade = execute_order(
                self.broker, self.portfolio, order, price, now,
                exit_reason=exit_reason, stop_loss=stop_loss, take_profit=take_profit, journal=self.journal,
            )
        except Exception as exc:  # KeyboardInterrupt/SystemExit keep the in-flight marker
            self._order_in_flight = None
            self._order_failed(order, exc, now, result)
            return None
        self._order_in_flight = None
        result.fills.append(fill)
        if trade is not None:
            result.trades.append(trade)
        return fill

    def _order_failed(self, order: OrderRequest, exc: Exception, now: int, result: TickResult) -> None:
        side = order.side.value
        if isinstance(exc, InsufficientFundsError):
            self._event(now, "insufficient_funds", f"{side}: {exc}", result)
        elif isinstance(exc, OrderStateUnknownError):
            logger.error("ORDER STATE UNKNOWN - new entries halted until checked: %s", exc)
            self.portfolio.halt(ORDER_STATE_UNKNOWN)
            self._event(now, ORDER_STATE_UNKNOWN, f"{side}: {exc}; entries halted", result)
        elif isinstance(exc, OrderRejectedError):
            self._event(now, "order_rejected", f"{side}: {exc}", result)
        elif isinstance(exc, ValueError):  # the broker filled but the portfolio refused to book it
            logger.exception("fill could not be booked")
            self._event(now, "booking_error", f"{side}: {exc}", result)
        else:  # unexpected: the order may or may not have been executed
            logger.exception("unexpected error while sending an order")
            self.portfolio.halt(ORDER_STATE_UNKNOWN)
            self._event(
                now, ORDER_STATE_UNKNOWN, f"{side}: unexpected {type(exc).__name__}: {exc}; entries halted", result,
            )

    def _event(self, ts: int, kind: str, message: str, result: TickResult | None = None) -> None:
        logger.log(_EVENT_LEVELS.get(kind, logging.WARNING), "%s: %s", kind, message)
        self.journal.record_event(ts, kind, message)
        if result is not None:
            result.events.append(kind)

    # ------------------------------------------------------------------ loop

    def stop(self) -> None:
        """Ask ``run_forever`` to return after the current step (thread-safe)."""
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def run_forever(
        self,
        max_iterations: int | None = None,
        *,
        wait_for_close: bool = True,
        interval_s: float = 0.0,
        grace_s: float = 2.0,
        poll_s: float = 5.0,
        max_candle_retries: int = 12,
        on_tick: Callable[[TickResult], None] | None = None,
        sleep: Callable[[float], None] | None = None,
        wall_ms: Callable[[], int] = wall_clock_ms,
    ) -> int:
        """Run ``step`` once per closed candle until stopped. Returns the steps run.

        ``wait_for_close``: sleep until the next candle close + ``grace_s`` (the first
        step runs immediately); if the exchange has not published the candle yet, retry
        every ``poll_s`` up to ``max_candle_retries`` times. Otherwise sleep
        ``interval_s`` between steps (0 = as fast as possible, e.g. synthetic replay).
        Ctrl+C stops gracefully; the state is saved on exit.
        """
        sleeper = sleep or (lambda seconds: self._stop.wait(max(seconds, 0.0)))
        self._stop.clear()
        steps = errors = 0
        self._event(self.clock_ms(), "start", f"engine {getattr(self.decision_engine, 'name', '?')} "
                    f"{self.settings.symbol} {self.settings.timeframe}")
        try:
            while not self._stop.is_set() and (max_iterations is None or steps < max_iterations):
                if steps > 0:
                    if wait_for_close:
                        self._wait_for_close(grace_s, poll_s, sleeper, wall_ms)
                    elif interval_s > 0:
                        sleeper(interval_s)
                    if self._stop.is_set():
                        break
                try:
                    result = self._step_with_retries(wait_for_close, poll_s, max_candle_retries, sleeper)
                    errors = 0
                except Exception as exc:
                    errors += 1
                    logger.exception("unexpected error in trading step (%d in a row)", errors)
                    self._event(self.clock_ms(), "error", f"{type(exc).__name__}: {exc}")
                    if errors >= MAX_CONSECUTIVE_ERRORS:
                        raise
                    result = TickResult(SKIPPED, events=["error"])
                steps += 1
                if on_tick is not None:
                    on_tick(result)
        except KeyboardInterrupt:
            logger.warning("interrupted by the user: saving state and stopping")
            self._event(self.clock_ms(), "stop", "interrupted by the user (Ctrl+C)")
        finally:
            self.save_state()
        return steps

    def _step_with_retries(
        self, wait_for_close: bool, poll_s: float, retries: int, sleeper: Callable[[float], None]
    ) -> TickResult:
        result = self.step()
        attempts = 0
        while wait_for_close and result.status == NO_NEW_CANDLE and attempts < retries and not self._stop.is_set():
            attempts += 1
            sleeper(poll_s)
            result = self.step()
        return result

    def _wait_for_close(
        self, grace_s: float, poll_s: float, sleeper: Callable[[float], None], wall_ms: Callable[[], int]
    ) -> None:
        now = wall_ms()
        target = floor_to_timeframe(now, self.settings.timeframe) + self.tf_ms + int(grace_s * 1000)
        while not self._stop.is_set():
            remaining = (target - wall_ms()) / 1000.0
            if remaining <= 0:
                return
            sleeper(min(remaining, max(poll_s, 0.1)))
