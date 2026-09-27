"""Event-driven backtester that reuses the live risk, portfolio and paper-broker code.

No look-ahead, by construction:
- the decision at candle ``i`` sees only ``candles[: i + 1]`` (a bounded window of
  ``settings.history_candles``, like live trading);
- its order fills at ``candles[i + 1].open`` through ``PaperBroker`` (slippage + fees);
- protective stops / take-profits are checked on every candle from the entry candle
  on, using that candle's low/high, and fill at the level (or at the open when the
  candle gapped through a stop, whichever is worse) through the paper broker;
- equity is marked at every close (timestamp = candle close time); the kill switch,
  cooldown and daily trade limits run on candle timestamps.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from .brain.base import DecisionEngine
from .brain.rules_engine import RulesDecisionEngine
from .config import Settings
from .engine import KILL_REASONS, execute_order, iso_utc, safe_decide
from .execution.paper import PaperBroker
from .features import build_snapshot, validate_candles
from .journal import Journal
from .market.timeframes import timeframe_to_ms
from .metrics import PerformanceMetrics, compute_metrics
from .models import (
    Action,
    Candle,
    Fill,
    InsufficientFundsError,
    OrderRejectedError,
    OrderRequest,
    TradeRecord,
)
from .portfolio import Portfolio
from .risk import RiskManager

logger = logging.getLogger(__name__)

MIN_MEANINGFUL_TRADES = 10


@dataclass
class BacktestResult:
    metrics: PerformanceMetrics
    equity_curve: list[tuple[int, float]]  # (candle close time, equity)
    price_curve: list[tuple[int, float]]  # (candle close time, close)
    trades: list[TradeRecord]
    fills: list[Fill]
    decisions: int
    actions: dict[str, int]
    approved: int
    llm_calls: int
    llm_cost_usd: float
    warnings: list[str] = field(default_factory=list)
    engine_name: str = ""
    final_engine_name: str = ""


@dataclass
class _Pending:
    """An order decided at a candle close, filled at the next candle's open."""

    order: OrderRequest
    exit_reason: str
    stop_loss: float | None = None
    take_profit: float | None = None


class _Run:
    """Mutable state of one backtest run."""

    def __init__(self, settings: Settings, engine: DecisionEngine, journal: Journal | None) -> None:
        s = settings
        self.settings = settings
        self.engine = engine
        self.journal = journal
        self.tf_ms = timeframe_to_ms(s.timeframe)
        self.broker = PaperBroker(s.symbol, s.paper_start_cash, s.fee_pct, s.slippage_pct)
        self.portfolio = Portfolio(s.symbol, s.quote_currency, s.base_currency)
        self.risk = RiskManager(s.risk, s.fee_pct, s.slippage_pct)
        self.pending: _Pending | None = None
        self.fills: list[Fill] = []
        self.equity_curve: list[tuple[int, float]] = []
        self.price_curve: list[tuple[int, float]] = []
        self.bars_in_position = 0
        self.decisions = 0
        self.approved = 0
        self.actions = {a.value: 0 for a in Action}
        self.llm_calls = 0
        self.llm_failed = 0
        self.fallbacks = 0
        self.llm_cost = 0.0
        self.warnings: list[str] = []
        self.kill_trips: dict[str, list[int]] = {}  # halt reason -> times it tripped

    # -- orders ---------------------------------------------------------------------------

    def execute(self, pending: _Pending, price: float, ts: int) -> bool:
        try:
            fill, _ = execute_order(
                self.broker, self.portfolio, pending.order, price, ts,
                exit_reason=pending.exit_reason, stop_loss=pending.stop_loss,
                take_profit=pending.take_profit, journal=self.journal,
            )
        except (InsufficientFundsError, OrderRejectedError) as exc:
            self.event(ts, "order_rejected", f"{pending.order.side.value}: {exc}")
            return False
        self.fills.append(fill)
        return True

    def protective_exit(self, candle: Candle) -> bool:
        hit = self.risk.check_protective_exit(self.portfolio, candle.low, candle.high, candle.open)
        if hit is None:
            return False
        reason, level = hit
        view = self.portfolio.view(self.broker.balances(), level)
        order = self.risk.forced_exit_order(view, reason, level)
        if order is None:
            return False
        return self.execute(_Pending(order, reason), level, candle.timestamp)

    # -- bookkeeping ----------------------------------------------------------------------

    def mark(self, candle: Candle) -> float:
        ts = candle.timestamp + self.tf_ms
        equity = self.portfolio.view(self.broker.balances(), candle.close).equity
        self.portfolio.mark(ts, equity)
        self.equity_curve.append((ts, equity))
        self.price_curve.append((ts, candle.close))
        self.bars_in_position += int(self.portfolio.in_position)
        if self.journal is not None:
            self.journal.record_equity(ts, equity, candle.close)
        return equity

    def event(self, ts: int, kind: str, message: str) -> None:
        logger.info("backtest %s at %s: %s", kind, iso_utc(ts), message)
        if self.journal is not None:
            self.journal.record_event(ts, kind, message)

    def kill_switch(self, candle: Candle, equity: float) -> str | None:
        ts = candle.timestamp + self.tf_ms
        before = self.portfolio.halted_reason
        reason = self.risk.update_kill_switch(self.portfolio, equity)
        active = self.portfolio.active_halt(ts) if reason else None
        if active is None:
            return None
        if active != before:
            self.kill_trips.setdefault(active, []).append(ts)
            self.event(ts, "kill_switch", active)
        if active in KILL_REASONS and self.settings.risk.flatten_on_kill and self.portfolio.in_position:
            view = self.portfolio.view(self.broker.balances(), candle.close)
            order = self.risk.forced_exit_order(view, active, candle.close)
            if order is not None:
                self.pending = _Pending(order, f"kill_switch:{active}")
        return active

    def decide(self, window: Sequence[Candle], price: float) -> None:
        s = self.settings
        snapshot = build_snapshot(s.symbol, s.timeframe, window)
        view = self.portfolio.view(self.broker.balances(), price)
        decision = safe_decide(self.engine, snapshot, view)
        self.decisions += 1
        self.actions[decision.action.value] += 1
        self.fallbacks += int(decision.source == "fallback")
        if decision.model is not None:  # an LLM was consulted (successfully or not)
            self.llm_calls += 1
            self.llm_failed += int(decision.input_tokens == 0 and decision.output_tokens == 0)
        self.llm_cost += decision.cost_usd
        verdict = self.risk.evaluate(decision, view, snapshot, self.portfolio)
        if self.journal is not None:
            self.journal.record_decision(snapshot, decision, verdict)
        if verdict.approved and verdict.order is not None:
            self.approved += 1
            self.pending = _Pending(verdict.order, "signal", verdict.stop_loss, verdict.take_profit)


def run_backtest(
    candles: Sequence[Candle],
    settings: Settings,
    engine: DecisionEngine,
    warmup: int = 60,
    max_llm_calls: int | None = None,
    journal: Journal | None = None,
    *,
    historical_data: bool = True,
    progress: Callable[[int, int], None] | None = None,
) -> BacktestResult:
    """Replay ``candles`` (closed, oldest first) through the full trading pipeline.

    The first decision is taken on candle ``warmup - 1`` (a window of ``warmup``
    candles); the equity and price curves start there too, so buy & hold covers the
    same period. ``max_llm_calls``: once reached, the rest runs on the rules engine.
    ``historical_data``: False for synthetic data (no LLM contamination warning).
    ``progress(done, total)`` is called about every 5 % of the candles.
    """
    candles = list(candles)
    if isinstance(warmup, bool) or not isinstance(warmup, int) or warmup < 1:
        raise ValueError(f"warmup must be an int >= 1, got {warmup!r}")
    if len(candles) < warmup + 1:
        raise ValueError(f"need at least warmup + 1 = {warmup + 1} candles, got {len(candles)}")
    if max_llm_calls is not None and max_llm_calls < 0:
        raise ValueError(f"max_llm_calls must be >= 0, got {max_llm_calls}")
    validate_candles(candles)

    run = _Run(settings, engine, journal)
    engine_name = getattr(engine, "name", type(engine).__name__)
    history = settings.history_candles
    first, last_index = warmup - 1, len(candles) - 1
    step = max(1, -(-(len(candles) - first) // 20))  # ~20 progress updates

    for i in range(first, len(candles)):
        candle = candles[i]
        if run.pending is not None:  # decided at the previous close
            pending, run.pending = run.pending, None
            run.execute(pending, candle.open, candle.timestamp)
        exited = run.portfolio.in_position and run.protective_exit(candle)
        equity = run.mark(candle)
        halt = run.kill_switch(candle, equity)
        can_act = i < last_index and run.pending is None and not exited
        if can_act and not (halt and not run.portfolio.in_position):
            _maybe_switch_to_rules(run, max_llm_calls, candle)
            run.decide(candles[max(0, i + 1 - history): i + 1], candle.close)
        if progress is not None and ((i - first) % step == 0 or i == last_index):
            progress(i - first + 1, len(candles) - first)

    _final_warnings(run, engine_name, historical_data)
    trades = list(run.portfolio.trades)
    metrics = compute_metrics(
        run.equity_curve,
        trades,
        start_equity=settings.paper_start_cash,
        timeframe_ms=run.tf_ms,
        first_price=candles[first].close,
        last_price=candles[-1].close,
        llm_calls=run.llm_calls,
        llm_cost_usd=run.llm_cost,
        fees_paid=run.portfolio.fees_paid,
        exposure_pct=run.bars_in_position / len(run.equity_curve) * 100.0,
    )
    return BacktestResult(
        metrics=metrics,
        equity_curve=run.equity_curve,
        price_curve=run.price_curve,
        trades=trades,
        fills=run.fills,
        decisions=run.decisions,
        actions=run.actions,
        approved=run.approved,
        llm_calls=run.llm_calls,
        llm_cost_usd=run.llm_cost,
        warnings=run.warnings,
        engine_name=engine_name,
        final_engine_name=getattr(run.engine, "name", type(run.engine).__name__),
    )


def _maybe_switch_to_rules(run: _Run, max_llm_calls: int | None, candle: Candle) -> None:
    if max_llm_calls is None or run.llm_calls < max_llm_calls:
        return
    if isinstance(run.engine, RulesDecisionEngine):
        return
    run.engine = RulesDecisionEngine()
    run.warnings.append(
        f"Límite de {max_llm_calls} llamadas al LLM alcanzado el {iso_utc(candle.timestamp)}: "
        "el resto del backtest usa solo el motor de reglas."
    )
    run.event(candle.timestamp, "llm_limit", f"max_llm_calls={max_llm_calls} reached; switched to rules")


def _final_warnings(run: _Run, engine_name: str, historical_data: bool) -> None:
    uses_llm = engine_name != "rules"
    if uses_llm and historical_data:
        run.warnings.insert(0, (
            "Contaminación por look-ahead: el LLM pudo haber visto este período histórico durante su "
            "entrenamiento, así que estos resultados probablemente sobreestiman el rendimiento real."
        ))
    if run.llm_failed:
        run.warnings.append(
            f"{run.llm_failed} de {run.llm_calls} llamadas al LLM fallaron (sin clave API, timeout o error) "
            "y se trataron como HOLD: el resultado no refleja decisiones reales del modelo."
        )
    elif run.fallbacks:
        run.warnings.append(f"{run.fallbacks} decisiones fueron HOLD de respaldo (fallo o presupuesto del motor).")
    for reason, times in run.kill_trips.items():
        scope = "el resto del backtest" if reason == "max_drawdown" else "el resto de ese día UTC"
        count = f"{len(times)} veces, la primera" if len(times) > 1 else "una vez,"
        run.warnings.append(
            f"Kill switch '{reason}' activado {count} el {iso_utc(times[0])}: sin nuevas entradas durante {scope}."
        )
    if run.portfolio.in_position:
        run.warnings.append(
            "Queda una posición abierta al final: se valora al último cierre y no cuenta como operación cerrada."
        )
    closed = len(run.portfolio.trades)
    if closed < MIN_MEANINGFUL_TRADES:
        run.warnings.append(
            f"Solo {closed} operaciones cerradas: la muestra es demasiado pequeña para sacar conclusiones."
        )
