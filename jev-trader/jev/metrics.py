"""Performance metrics for backtests and live/paper runs, plus a terminal summary.

All ``*_pct`` values are percent (2.0 = 2 %). Sharpe and Sortino are annualized from
per-period simple returns of the equity curve (risk-free rate 0), with
``periods_per_year = 365 * 24 * 3600 * 1000 / timeframe_ms`` (crypto trades 24/7).
"""

from __future__ import annotations

import math
import statistics

from pydantic import BaseModel

from .models import TradeRecord

MS_PER_YEAR = 365 * 24 * 3600 * 1000


class PerformanceMetrics(BaseModel):
    start_equity: float
    end_equity: float
    total_return_pct: float = 0.0
    buy_and_hold_return_pct: float | None = None
    max_drawdown_pct: float = 0.0  # positive number: 12.5 means a 12.5 % peak-to-trough loss
    sharpe: float = 0.0
    sortino: float | None = 0.0  # None: positive returns with no downside at all (unbounded)
    num_trades: int = 0
    win_rate_pct: float = 0.0
    profit_factor: float | None = None  # None when there are no losing trades
    avg_trade_pct: float = 0.0
    best_trade_pct: float = 0.0
    worst_trade_pct: float = 0.0
    exposure_pct: float | None = None  # % of the period with an open position
    fees_paid: float = 0.0
    llm_calls: int = 0
    llm_cost_usd: float = 0.0
    bars: int = 0


# -- formatting helpers (Spanish style: 1.234,56) ------------------------------


def format_number(value: float, decimals: int = 2, signed: bool = False) -> str:
    """Format like ``1.234,56`` (dot thousands, comma decimals). ASCII minus sign."""
    if value is None or not math.isfinite(value):
        return "n/d"
    text = f"{abs(value):,.{decimals}f}".replace(",", "_").replace(".", ",").replace("_", ".")
    if value < 0 and text.strip("0,.") != "":
        return "-" + text
    return ("+" + text) if signed and value > 0 else text


def format_pct(value: float | None, decimals: int = 2, signed: bool = False) -> str:
    """Format a percent value as ``12,35 %``; ``None`` becomes ``n/d``."""
    if value is None:
        return "n/d"
    return f"{format_number(value, decimals, signed)} %"


# -- building blocks ----------------------------------------------------------


def periods_per_year(timeframe_ms: int) -> float:
    if timeframe_ms <= 0:
        raise ValueError("timeframe_ms must be > 0")
    return MS_PER_YEAR / timeframe_ms


def period_returns(values: list[float]) -> list[float]:
    """Simple returns between consecutive equity values (non-positive bases skipped)."""
    return [cur / prev - 1.0 for prev, cur in zip(values, values[1:]) if prev > 0]


def drawdown_curve(
    equity_curve: list[tuple[int, float]], start_equity: float | None = None
) -> list[tuple[int, float]]:
    """Drawdown in percent (<= 0, e.g. -4.2) from the running peak at each point.

    The running peak is seeded with ``start_equity`` when given, so a loss on the
    very first bar still counts as drawdown.
    """
    peak = start_equity if start_equity and start_equity > 0 else None
    out: list[tuple[int, float]] = []
    for ts, equity in equity_curve:
        peak = equity if peak is None else max(peak, equity)
        dd = (equity / peak - 1.0) * 100.0 if peak > 0 else 0.0
        out.append((ts, min(dd, 0.0)))
    return out


def _annualized_ratio(returns: list[float], ppy: float, downside: bool) -> float | None:
    if len(returns) < 2:
        return 0.0
    mean = statistics.fmean(returns)
    if downside:
        denom = math.sqrt(statistics.fmean([min(r, 0.0) ** 2 for r in returns]))
    else:
        denom = statistics.stdev(returns)
    if denom <= 1e-15 or not math.isfinite(denom):
        # Sortino with gains and no downside is unbounded, not 0 (the best case, not a flat one).
        return None if downside and mean > 0 else 0.0
    return mean / denom * math.sqrt(ppy)


def sharpe_ratio(returns: list[float], ppy: float) -> float:
    """Annualized Sharpe (rf = 0, sample stdev). 0.0 if < 2 returns or zero stdev."""
    return _annualized_ratio(returns, ppy, downside=False) or 0.0


def sortino_ratio(returns: list[float], ppy: float) -> float | None:
    """Annualized Sortino (target 0). 0.0 if < 2 returns or no return at all; None when
    there are gains but no downside deviation (unbounded: shown as "n/d (sin caídas)")."""
    return _annualized_ratio(returns, ppy, downside=True)


def _exposure_from_trades(
    trades: list[TradeRecord], equity_curve: list[tuple[int, float]], timeframe_ms: int
) -> float | None:
    """Approximate % of time in position from the union of trade intervals."""
    if not trades or not equity_curve or any(t.entry_time is None for t in trades):
        return None
    start = equity_curve[0][0]
    end = equity_curve[-1][0] + timeframe_ms
    if end <= start:
        return None
    intervals = sorted(
        (max(start, t.entry_time or start), min(end, t.exit_time)) for t in trades
    )
    covered = 0
    current: list[int] | None = None  # [start, end] of the interval being merged
    for a, b in intervals:
        if b <= a:
            continue
        if current is not None and a <= current[1]:
            current[1] = max(current[1], b)
            continue
        if current is not None:
            covered += current[1] - current[0]
        current = [a, b]
    if current is not None:
        covered += current[1] - current[0]
    return max(0.0, min(100.0, covered / (end - start) * 100.0))


def _trade_stats(trades: list[TradeRecord]) -> dict:
    if not trades:
        return {}
    pnls = [t.pnl for t in trades]
    pcts = [t.pnl_pct for t in trades]
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    return {
        "num_trades": len(trades),
        "win_rate_pct": sum(1 for p in pnls if p > 0) / len(trades) * 100.0,
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else None,
        "avg_trade_pct": statistics.fmean(pcts),
        "best_trade_pct": max(pcts),
        "worst_trade_pct": min(pcts),
    }


def compute_metrics(
    equity_curve: list[tuple[int, float]],
    trades: list[TradeRecord],
    start_equity: float,
    timeframe_ms: int,
    first_price: float | None = None,
    last_price: float | None = None,
    llm_calls: int = 0,
    llm_cost_usd: float = 0.0,
    fees_paid: float = 0.0,
    exposure_pct: float | None = None,
) -> PerformanceMetrics:
    """Compute run metrics. ``equity_curve`` is ``[(ts_ms, equity)]`` oldest first.

    ``exposure_pct`` overrides the estimate derived from trade entry/exit times.
    ``fees_paid`` is passed through (use the sum of fill fees).
    """
    ppy = periods_per_year(timeframe_ms)
    values = [float(v) for _, v in equity_curve]
    end_equity = values[-1] if values else float(start_equity)
    total_return = (end_equity / start_equity - 1.0) * 100.0 if start_equity > 0 else 0.0
    bh = None
    if first_price is not None and last_price is not None and first_price > 0:
        bh = (last_price / first_price - 1.0) * 100.0
    drawdowns = [dd for _, dd in drawdown_curve(equity_curve, start_equity)]
    returns = period_returns(values)
    if exposure_pct is None:
        exposure_pct = _exposure_from_trades(trades, equity_curve, timeframe_ms)
    return PerformanceMetrics(
        start_equity=float(start_equity),
        end_equity=end_equity,
        total_return_pct=total_return,
        buy_and_hold_return_pct=bh,
        max_drawdown_pct=-min(drawdowns) if drawdowns else 0.0,
        sharpe=sharpe_ratio(returns, ppy),
        sortino=sortino_ratio(returns, ppy),
        exposure_pct=exposure_pct,
        fees_paid=float(fees_paid),
        llm_calls=int(llm_calls),
        llm_cost_usd=float(llm_cost_usd),
        bars=len(values),
        **_trade_stats(trades),
    )


def format_profit_factor(m: PerformanceMetrics) -> str:
    if m.num_trades == 0:
        return "n/d (sin operaciones)"
    return "n/d (sin pérdidas)" if m.profit_factor is None else format_number(m.profit_factor)


def format_sortino(m: PerformanceMetrics) -> str:
    return "n/d (sin caídas)" if m.sortino is None else format_number(m.sortino)


def _summary_rows(m: PerformanceMetrics) -> list[tuple[str, str]]:
    pf = format_profit_factor(m)
    return [
        ("Equity inicial", format_number(m.start_equity)),
        ("Equity final", format_number(m.end_equity)),
        ("Retorno total", format_pct(m.total_return_pct, signed=True)),
        ("Buy & hold", format_pct(m.buy_and_hold_return_pct, signed=True)),
        ("Máx. drawdown", format_pct(m.max_drawdown_pct)),
        ("Sharpe (anual.)", format_number(m.sharpe)),
        ("Sortino (anual.)", format_sortino(m)),
        ("Operaciones", str(m.num_trades)),
        ("Tasa de acierto", format_pct(m.win_rate_pct)),
        ("Profit factor", pf),
        ("Operación media", format_pct(m.avg_trade_pct, signed=True)),
        ("Mejor operación", format_pct(m.best_trade_pct, signed=True)),
        ("Peor operación", format_pct(m.worst_trade_pct, signed=True)),
        ("Exposición", format_pct(m.exposure_pct)),
        ("Comisiones", format_number(m.fees_paid)),
        ("Llamadas LLM", str(m.llm_calls)),
        ("Coste LLM (USD)", format_number(m.llm_cost_usd, 4)),
        ("Velas", str(m.bars)),
    ]


def render_text_summary(metrics: PerformanceMetrics) -> str:
    """Aligned plain-text table (labels left, values right) for the terminal."""
    rows = _summary_rows(metrics)
    label_w = max(len(label) for label, _ in rows)
    value_w = max(len(value) for _, value in rows)
    title = "Resumen de rendimiento"
    width = max(len(title), label_w + 2 + value_w)
    lines = [title, "-" * width]
    lines += [f"{label.ljust(label_w)}  {value.rjust(value_w)}" for label, value in rows]
    return "\n".join(lines)
