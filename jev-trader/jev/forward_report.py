"""Forward-test report (`jev report`): how did paper or testnet trading actually go?

Built only from a journal opened read-only, inside one consistent snapshot, so it is safe
to run while the bot is writing to the same file. Forward results are free of the
training-data contamination that affects backtests of AI engines, but they are only
meaningful with enough trades.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .journal import Journal
from .market.timeframes import timeframe_to_ms
from .metrics import PerformanceMetrics, compute_metrics
from .models import Side
from .report import render_html_report

MIN_TRADES = 30  # below this, win rate and profit factor are mostly noise
DUST_QTY = 1e-9


class ReportUsageError(ValueError):
    """The flags do not match the journal (the CLI maps this to a usage error)."""


@dataclass(frozen=True)
class Session:
    key: str  # state key without the "engine:" prefix
    mode: str  # paper | live
    label: str  # paper: data source (synthetic, binance, ...); live: testnet | mainnet
    symbol: str
    timeframe: str


@dataclass(frozen=True)
class ForwardReport:
    html: str
    metrics: PerformanceMetrics
    warnings: list[str]
    symbol: str
    timeframe: str
    gaps: int
    overlaps: int


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def journal_sessions(journal: Journal) -> list[Session]:
    """Sessions saved in the journal, parsed from state keys like
    ``engine:paper:<source>:<symbol>:<tf>`` and ``engine:live:<net>:<exchange>:<symbol>:<tf>``."""
    sessions = []
    for key in journal.state_keys("engine:"):
        parts = key.split(":")
        if len(parts) < 5:
            continue
        try:
            timeframe_to_ms(parts[-1])
        except ValueError:
            continue
        sessions.append(Session(key=key.removeprefix("engine:"), mode=parts[1], label=parts[2],
                                symbol=parts[-2], timeframe=parts[-1]))
    return sessions


def journal_timeframes(journal: Journal) -> list[str]:
    found: list[str] = []
    for s in journal_sessions(journal):
        if s.timeframe not in found:
            found.append(s.timeframe)
    return found


def count_gaps(curve: list[tuple[int, float]], tf_ms: int) -> tuple[int, float, int]:
    """(pauses longer than two candles, total paused hours, overlapping points).

    An overlap (a timestamp not after the previous one) means several runs were written
    to the journal over the same period, which mixes their curves.
    """
    gaps, missing_ms, overlaps = 0, 0, 0
    for (prev, _), (cur, _) in zip(curve, curve[1:]):
        delta = cur - prev
        if delta <= 0:
            overlaps += 1
        elif delta > 2 * tf_ms:
            gaps += 1
            missing_ms += delta - tf_ms
    return gaps, missing_ms / 3_600_000, overlaps


def external_flows(journal: Journal) -> list[tuple[int, float]]:
    """(ts, signed quote amount) of deposits/withdrawals/manual trades the bot detected."""
    flows = []
    for event in journal.events("external_flow"):
        try:
            flows.append((int(event["ts"]), float(event["message"].split()[0])))
        except (IndexError, ValueError):
            continue
    return flows


def remove_flows(curve: list[tuple[int, float]], flows: list[tuple[int, float]]) -> list[tuple[int, float]]:
    """Equity net of external flows, so they do not count as return or drawdown (as in the bot)."""
    if not flows:
        return curve
    out, total, i = [], 0.0, 0
    for ts, equity in curve:
        while i < len(flows) and flows[i][0] <= ts:  # the engine marks equity right after the flow
            total += flows[i][1]
            i += 1
        out.append((ts, equity - total))
    return out


def exposure_from_fills(fills: list[dict], start: int, end: int) -> float | None:
    """% of [start, end) spent holding a position, including one still open at the end."""
    if end <= start:
        return None
    qty, opened, covered = 0.0, None, 0
    for fill in fills:
        ts = int(fill["ts"])
        qty += fill["quantity"] if fill["side"] == Side.BUY.value else -fill["quantity"]
        if opened is None and qty > DUST_QTY:
            opened = ts
        elif opened is not None and qty <= DUST_QTY:
            covered += max(0, min(ts, end) - max(opened, start))
            opened, qty = None, 0.0
    if opened is not None:
        covered += max(0, end - max(opened, start))
    return 100.0 * covered / (end - start)


def build_forward_report(journal: Journal, *, timeframe: str | None = None, symbol: str | None = None,
                         title: str | None = None) -> ForwardReport:
    with journal.snapshot():  # every read below sees the same state, even mid-tick
        raw_equity = journal.equity_curve()
        sessions = journal_sessions(journal)
        symbols = journal.symbols()
        prices = journal.price_curve()
        trades = journal.trades()
        fills = journal.fills()
        flows = external_flows(journal)
        summary = journal.summary()
        fees = journal.total_fees()
    if len(raw_equity) < 2:
        raise ValueError("el journal todavía no tiene suficientes puntos de equity (hacen falta al menos 2)")

    timeframes = list(dict.fromkeys(s.timeframe for s in sessions))
    if timeframe and timeframes and timeframe not in timeframes:
        raise ReportUsageError(f"el journal tiene sesiones de {', '.join(timeframes)}, no de {timeframe}")
    if symbol and symbols and symbol not in symbols:
        raise ReportUsageError(f"el journal tiene {', '.join(symbols)}, no {symbol}")
    tf = timeframe or (timeframes[0] if len(timeframes) == 1 else None)
    if tf is None:
        raise ReportUsageError("no se pudo deducir el timeframe del journal: pasá --timeframe")
    tf_ms = timeframe_to_ms(tf)
    sym = symbol or (symbols[0] if symbols else "?")

    equity = remove_flows(raw_equity, flows)
    start, end = equity[0][0], equity[-1][0] + tf_ms
    metrics = compute_metrics(
        equity, trades, start_equity=equity[0][1], timeframe_ms=tf_ms,
        first_price=prices[0][1], last_price=prices[-1][1],
        llm_calls=summary["llm_calls"], llm_cost_usd=summary["llm_cost_usd"],
        fees_paid=fees, exposure_pct=exposure_from_fills(fills, start, end),
    )
    gaps, paused_h, overlaps = count_gaps(equity, tf_ms)
    synthetic = any(s.mode == "paper" and s.label == "synthetic" for s in sessions)

    warnings = []
    if synthetic:
        warnings.append("Datos sintéticos: el mercado fue simulado (`jev paper --synthetic`), no es el mercado "
                        "real. Sirve para probar el sistema, no para evaluar la estrategia.")
    if metrics.num_trades < MIN_TRADES:
        warnings.append(f"Muestra chica: {metrics.num_trades} operaciones cerradas. Hacen falta al menos "
                        f"{MIN_TRADES} para que la tasa de acierto y el profit factor signifiquen algo.")
    if len(sessions) > 1 or len(symbols) > 1:
        names = [s.key for s in sessions] or symbols
        warnings.append("El journal mezcla varias sesiones (" + ", ".join(names) + "): las cifras las combinan. "
                        "Usá un journal por sesión.")
    if overlaps:
        warnings.append(f"Hay {overlaps} punto(s) de equity superpuestos en el tiempo: el journal tiene varias "
                        "corridas del mismo período (por ejemplo, varias `--synthetic` o un `--fresh`). La curva y "
                        "las métricas no corresponden a una sola corrida.")
    if gaps:
        warnings.append(f"El bot estuvo detenido {gaps} vez/veces (~{paused_h:.1f} h sin velas). Sharpe y "
                        "Sortino suponen velas consecutivas y quedan aproximados.")
    if flows:
        net = sum(amount for _, amount in flows)
        warnings.append(f"{len(flows)} movimiento(s) externo(s) (depósitos, retiros o trades manuales; neto "
                        f"{net:+.2f}) se descontaron de la equity: no cuentan como ganancia ni pérdida.")
    if any(s.mode == "live" for s in sessions):
        warnings.append("Sesión live: si corre en testnet, los fondos son de prueba y la ejecución puede ser "
                        "más optimista que en mainnet.")

    a = summary["actions"]
    latency = summary["avg_latency_ms"]
    kind = ("simulación con mercado sintético" if synthetic
            else "forward (decisiones en tiempo real, sin contaminación de backtest)")
    settings_summary = {
        "Journal": journal.path,
        "Período": f"{_iso(equity[0][0])} → {_iso(equity[-1][0])}",
        "Símbolo / timeframe": f"{sym} · {tf}",
        "Tipo de prueba": kind,
        "Sesiones": ", ".join(s.key for s in sessions) or "-",
        "Decisiones": f"{summary['decisions']} (BUY {a['BUY']} · SELL {a['SELL']} · HOLD {a['HOLD']}), "
                      f"aprobadas {summary['approved']}",
        "Fuentes": ", ".join(f"{k} {v}" for k, v in summary["sources"].items()) or "-",
        "Llamadas IA": f"{summary['llm_calls']} · US${summary['llm_cost_usd']:.4f}"
                       + (f" · latencia media {latency:.0f} ms" if latency is not None else ""),
        "Pausas": f"{gaps} (~{paused_h:.1f} h)" if gaps else "ninguna",
    }
    label = "simulación" if synthetic else "forward test"
    html = render_html_report(metrics, equity, prices, trades,
                              title or f"Jev Trader · {label} {sym} {tf}", settings_summary, warnings)
    return ForwardReport(html=html, metrics=metrics, warnings=warnings, symbol=sym, timeframe=tf,
                         gaps=gaps, overlaps=overlaps)
