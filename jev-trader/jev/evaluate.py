"""Walk-forward evaluation (`jev evaluate`): does the rules strategy have an edge after costs?

Forward testing collects a few trades per month, far too slow to judge a strategy. This
replays real historical candles through the same pipeline as live trading (risk manager,
fees, slippage, next-open fills), split into consecutive folds, and asks one question:
is the average trade positive after costs, with a confidence interval?

Only the deterministic rules engine is evaluated: it has no training data, so history is a
fair test as long as its parameters were not tuned on the same candles. AI engines (Jev,
Claude) may have seen the period, so their historical results would be contaminated.
"""

from __future__ import annotations

import math
import random
from dataclasses import asdict, dataclass, field
from typing import Callable, Sequence

from .backtest import run_backtest
from .brain.base import DecisionEngine
from .brain.rules_engine import RulesDecisionEngine
from .config import Settings
from .metrics import format_number
from .models import Candle

MIN_TRADES = 30  # below this the verdict is always "inconclusive"
MIN_FOLD_CANDLES = 50  # trading candles per fold
LOSES, INCONCLUSIVE, EDGE = "pierde", "no concluyente", "ventaja"


@dataclass(frozen=True)
class FoldResult:
    symbol: str
    fold: int
    start_ts: int
    end_ts: int
    return_pct: float
    buy_hold_pct: float | None
    trades: int
    win_rate_pct: float
    max_drawdown_pct: float
    fees: float
    halted: bool  # a kill switch tripped during the fold


@dataclass(frozen=True)
class Evaluation:
    timeframe: str
    folds: list[FoldResult]
    trade_returns_pct: list[float] = field(repr=False)
    trades: int
    mean_trade_pct: float
    ci_low_pct: float | None  # 95 % bootstrap interval of the mean trade, net of costs
    ci_high_pct: float | None
    win_rate_pct: float
    profit_factor: float | None
    folds_positive: int
    folds_beating_hold: int
    verdict: str
    explanation: str

    def to_dict(self) -> dict:
        return asdict(self)


def split_folds(candles: Sequence[Candle], folds: int, warmup: int) -> list[list[Candle]]:
    """Consecutive folds over the candles after the first ``warmup``. Each fold carries the
    ``warmup`` candles before it, so indicators are ready and no fold trades on another's data."""
    if folds < 1:
        raise ValueError(f"folds must be >= 1, got {folds}")
    tradable = len(candles) - warmup
    size = tradable // folds
    if size < MIN_FOLD_CANDLES:
        need = warmup + folds * MIN_FOLD_CANDLES
        raise ValueError(f"hacen falta al menos {need} velas para {folds} tramos (hay {len(candles)}); "
                         "bajá --folds o descargá más historia")
    out = []
    for k in range(folds):
        start = warmup + k * size
        end = len(candles) if k == folds - 1 else start + size
        out.append(list(candles[start - warmup:end]))
    return out


def bootstrap_mean_ci(values: Sequence[float], resamples: int = 4000, seed: int = 7,
                      confidence: float = 0.95) -> tuple[float, float] | None:
    """Percentile bootstrap interval of the mean. Deterministic for a given seed."""
    n = len(values)
    if n < 2:
        return None
    rng = random.Random(seed)
    means = sorted(sum(rng.choices(values, k=n)) / n for _ in range(resamples))
    tail = (1.0 - confidence) / 2.0
    low = means[int(math.floor(tail * (resamples - 1)))]
    high = means[int(math.ceil((1.0 - tail) * (resamples - 1)))]
    return low, high


def _verdict(trades: int, ci: tuple[float, float] | None, folds_positive: int, folds: int) -> tuple[str, str]:
    if trades < MIN_TRADES or ci is None:
        return INCONCLUSIVE, (f"Solo {trades} operaciones: hacen falta al menos {MIN_TRADES}. "
                              "Sumá más historia o más pares.")
    low, high = ci
    if high < 0:
        return LOSES, ("La operación media es negativa después de costos, con 95 % de confianza. "
                       "Esta estrategia pierde plata: no la uses con dinero real.")
    if low > 0 and folds_positive * 2 > folds:
        return EDGE, ("La operación media es positiva después de costos y la mayoría de los tramos ganó. "
                      "Es evidencia de backtest, no una garantía: confirmala en paper antes de arriesgar plata.")
    return INCONCLUSIVE, ("El intervalo de la operación media incluye el cero: no hay evidencia de ventaja. "
                          "Sin ventaja demostrada, no la uses con dinero real.")


def evaluate(
    datasets: Sequence[tuple[str, Sequence[Candle]]],
    settings: Settings,
    *,
    folds: int = 6,
    warmup: int = 60,
    engine_factory: Callable[[], DecisionEngine] = RulesDecisionEngine,
    historical_data: bool = True,
) -> Evaluation:
    """Run every fold of every ``(symbol, candles)`` dataset and pool the closed trades."""
    if not datasets:
        raise ValueError("no hay datos para evaluar")
    results: list[FoldResult] = []
    returns: list[float] = []
    for symbol, candles in datasets:
        fold_settings = settings.with_overrides(symbol=symbol, mode="paper")
        for k, chunk in enumerate(split_folds(candles, folds, warmup), start=1):
            run = run_backtest(chunk, fold_settings, engine_factory(), warmup=warmup,
                               historical_data=historical_data)
            m = run.metrics
            results.append(FoldResult(
                symbol=symbol, fold=k, start_ts=chunk[warmup - 1].timestamp, end_ts=chunk[-1].timestamp,
                return_pct=m.total_return_pct, buy_hold_pct=m.buy_and_hold_return_pct, trades=m.num_trades,
                win_rate_pct=m.win_rate_pct, max_drawdown_pct=m.max_drawdown_pct, fees=m.fees_paid,
                halted=any("Kill switch" in w for w in run.warnings),
            ))
            returns.extend(t.pnl_pct for t in run.trades)

    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r < 0]
    ci = bootstrap_mean_ci(returns)
    folds_positive = sum(1 for f in results if f.return_pct > 0)
    verdict, explanation = _verdict(len(returns), ci, folds_positive, len(results))
    return Evaluation(
        timeframe=settings.timeframe,
        folds=results,
        trade_returns_pct=returns,
        trades=len(returns),
        mean_trade_pct=sum(returns) / len(returns) if returns else 0.0,
        ci_low_pct=ci[0] if ci else None,
        ci_high_pct=ci[1] if ci else None,
        win_rate_pct=100.0 * len(wins) / len(returns) if returns else 0.0,
        profit_factor=sum(wins) / -sum(losses) if losses else None,
        folds_positive=folds_positive,
        folds_beating_hold=sum(1 for f in results if f.buy_hold_pct is not None and f.return_pct > f.buy_hold_pct),
        verdict=verdict,
        explanation=explanation,
    )


def _day(ts: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def render_evaluation(result: Evaluation) -> str:
    """Plain-text summary for the terminal (Spanish)."""
    lines = [f"{'Par':<10} {'Tramo':>5}  {'Período (UTC)':<23} {'Retorno':>9} {'Buy&hold':>9} {'Ops':>4} "
             f"{'Aciertos':>9} {'Máx. DD':>8}"]
    for f in result.folds:
        hold = "n/d" if f.buy_hold_pct is None else format_number(f.buy_hold_pct, signed=True) + " %"
        halt = "  (kill switch)" if f.halted else ""
        lines.append(
            f"{f.symbol:<10} {f.fold:>5}  {_day(f.start_ts)} → {_day(f.end_ts)} "
            f"{format_number(f.return_pct, signed=True) + ' %':>9} {hold:>9} {f.trades:>4} "
            f"{format_number(f.win_rate_pct, 0) + ' %':>9} {format_number(f.max_drawdown_pct) + ' %':>8}{halt}"
        )
    n = len(result.folds)
    interval = "n/d"
    if result.ci_low_pct is not None and result.ci_high_pct is not None:
        interval = (f"{format_number(result.ci_low_pct, signed=True)} % a "
                    f"{format_number(result.ci_high_pct, signed=True)} %")
    factor = "n/d" if result.profit_factor is None else format_number(result.profit_factor)
    lines += [
        "",
        f"Operaciones cerradas   {result.trades}",
        f"Operación media        {format_number(result.mean_trade_pct, signed=True)} % (después de costos)",
        f"Intervalo 95 %         {interval}",
        f"Aciertos               {format_number(result.win_rate_pct, 1)} % · profit factor {factor}",
        f"Tramos con ganancia    {result.folds_positive} de {n}",
        f"Tramos mejor que hold  {result.folds_beating_hold} de {n}",
        "",
        f"Veredicto: {result.verdict.upper()}. {result.explanation}",
    ]
    return "\n".join(lines)
