"""Command-line interface: ``jeb <command>``.

Commands: backtest, decide, paper, live, download, status. Settings come from the
environment / ``.env`` (``jeb.config.load_settings``); flags override them.
Exit codes: 0 ok, 1 runtime error, 2 configuration or usage error.
Secrets are never printed. Heavy imports (ccxt, anthropic) happen per command.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .config import LIVE_CONFIRM_PHRASE, Settings, load_dotenv, load_settings
from .models import Candle, ConfigError, Decision, JebError, MarketSnapshot, PortfolioView

logger = logging.getLogger(__name__)

EXIT_OK, EXIT_RUNTIME, EXIT_CONFIG = 0, 1, 2
DEFAULT_REPORT = "reports/backtest.html"
DEFAULT_CANDLES = 2000
DEFAULT_MAX_LLM_CALLS = 200
DEFAULT_WARMUP = 60
LLM_COST_CONFIRM_USD = 1.0  # a Claude backtest above this worst case needs --yes
TYPICAL_OUTPUT_TOKENS = 150
SCHEMA_OVERHEAD_TOKENS = 400  # structured-output schema + formatting, rough and conservative
CHARS_PER_TOKEN = 3  # conservative (real ratio is closer to 3.5-4 for this prompt)


class UsageError(Exception):
    """Invalid combination of command-line flags (exit code 2)."""


# ---------------------------------------------------------------------------- helpers


class _StderrHandler(logging.StreamHandler):
    """Always writes to the *current* ``sys.stderr`` (safe when stderr is swapped)."""

    def __init__(self) -> None:
        super().__init__()

    @property  # type: ignore[override]
    def stream(self) -> Any:
        return sys.stderr

    @stream.setter
    def stream(self, value: Any) -> None:  # StreamHandler assigns it in __init__
        pass


def _configure_logging(verbosity: int) -> None:
    level = logging.WARNING if verbosity <= 0 else logging.INFO if verbosity == 1 else logging.DEBUG
    root = logging.getLogger()
    if not any(isinstance(h, _StderrHandler) for h in root.handlers):
        handler = _StderrHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
        root.addHandler(handler)
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "anthropic", "ccxt", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING if verbosity < 2 else logging.DEBUG)


def _fmt(value: float | None, decimals: int = 2, signed: bool = False) -> str:
    from .metrics import format_number

    return "n/d" if value is None else format_number(value, decimals, signed)


def _fmt_price(value: float | None) -> str:
    if value is None:
        return "n/d"
    return _fmt(value, 2 if abs(value) >= 1 else 6)


def _iso(ts: int | None) -> str:
    from .engine import iso_utc

    return iso_utc(ts)


def _export_anthropic_key(env_file: str | None) -> None:
    """Make ANTHROPIC_API_KEY from .env visible to the Anthropic SDK (it only reads os.environ)."""
    if os.environ.get("ANTHROPIC_API_KEY") or not env_file:
        return
    key = load_dotenv(env_file).get("ANTHROPIC_API_KEY")
    if key:
        os.environ["ANTHROPIC_API_KEY"] = key


def _has_anthropic_credentials() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def _load_settings(args: argparse.Namespace) -> Settings:
    from .market.timeframes import is_valid_timeframe

    settings = load_settings(dotenv_path=args.env_file)
    overrides: dict[str, Any] = {}
    for attr in ("engine", "symbol", "timeframe"):
        value = getattr(args, attr, None)
        if value:
            overrides[attr] = value
    journal = getattr(args, "journal", None)
    if journal and getattr(args, "journal_overrides_settings", True):
        overrides["journal_path"] = journal
    if overrides:
        settings = settings.with_overrides(**overrides)
    if not is_valid_timeframe(settings.timeframe):
        raise ConfigError(f"JEB_TIMEFRAME {settings.timeframe!r} is not valid (use e.g. 1m, 5m, 1h, 1d)")
    return settings


def _flat_view(settings: Settings) -> PortfolioView:
    cash = settings.paper_start_cash
    return PortfolioView(
        quote_currency=settings.quote_currency, base_currency=settings.base_currency,
        cash=cash, base_qty=0.0, equity=cash,
    )


def _warn_missing_llm_key(settings: Settings) -> None:
    if settings.engine != "rules" and not _has_anthropic_credentials():
        print(
            "Aviso: ANTHROPIC_API_KEY no está configurada; cada consulta a Claude devolverá HOLD "
            "(fallback seguro). Usá --engine rules o configurá la clave en .env.",
            file=sys.stderr,
        )


def _public_market(settings: Settings) -> Any:
    """Read-only public market data from the real exchange (no keys, never places orders)."""
    from .market.ccxt_source import CcxtMarket

    return CcxtMarket(settings.exchange, use_testnet=False)


def _parse_day(value: str) -> int:
    from .market.csv_source import parse_timestamp

    try:
        return parse_timestamp(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"fecha inválida {value!r} (usá YYYY-MM-DD): {exc}") from None


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"se esperaba un entero, no {value!r}") from None
    if number < 1:
        raise argparse.ArgumentTypeError(f"debe ser >= 1, no {number}")
    return number


def _non_negative_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"se esperaba un entero, no {value!r}") from None
    if number < 0:
        raise argparse.ArgumentTypeError(f"debe ser >= 0, no {number}")
    return number


# ---------------------------------------------------------------------------- backtest


def _backtest_candles(args: argparse.Namespace, settings: Settings) -> tuple[list[Candle], str, bool]:
    """(candles, source label, is historical market data)."""
    if args.source == "synthetic":
        from .market.synthetic import SyntheticMarket

        count = args.candles or DEFAULT_CANDLES
        market = SyntheticMarket(seed=args.seed, timeframe=settings.timeframe, initial_history=0)
        return market.generate(count), f"sintético (seed {args.seed})", False
    if args.source == "csv":
        if not args.csv:
            raise UsageError("--source csv requiere --csv RUTA")
        from .market.csv_source import CsvMarket

        candles = CsvMarket(args.csv, settings.timeframe).candles
        if args.candles:
            candles = candles[-args.candles:]
        return candles, f"CSV {args.csv}", True
    from .market.timeframes import timeframe_to_ms

    count = args.candles or DEFAULT_CANDLES
    tf_ms = timeframe_to_ms(settings.timeframe)
    until = int(time.time() * 1000)
    candles = _public_market(settings).download_history(
        settings.symbol, settings.timeframe, until - (count + 1) * tf_ms, until
    )
    return candles[-count:], f"{settings.exchange} (datos públicos)", True


def _llm_cost_per_call(settings: Settings, candles: Sequence[Candle]) -> tuple[float, float]:
    """(typical, worst case) USD per Claude call, from a real prompt of this data set."""
    from .brain.claude_engine import SYSTEM_PROMPT, format_prompt, price_for
    from .features import build_snapshot

    snapshot = build_snapshot(settings.symbol, settings.timeframe, candles[: settings.history_candles])
    prompt = format_prompt(snapshot, _flat_view(settings))
    in_tokens = (len(SYSTEM_PROMPT) + len(prompt)) / CHARS_PER_TOKEN + SCHEMA_OVERHEAD_TOKENS
    in_price, out_price = price_for(settings.model)
    typical = (in_tokens * in_price + TYPICAL_OUTPUT_TOKENS * out_price) / 1_000_000
    worst = (in_tokens * in_price + settings.llm_max_tokens * out_price) / 1_000_000
    return typical, worst


def _settings_summary(
    settings: Settings, source: str, candles: int, warmup: int, max_llm_calls: int | None
) -> dict[str, str]:
    r = settings.risk
    summary = {
        "Datos": source,
        "Símbolo": settings.symbol,
        "Timeframe": settings.timeframe,
        "Velas": str(candles),
        "Warm-up (velas)": str(warmup),
        "Motor": settings.engine,
    }
    if settings.engine != "rules":
        summary["Modelo"] = settings.model
        summary["Máx. llamadas LLM"] = str(max_llm_calls)
        summary["Heartbeat híbrido (velas)"] = str(settings.hybrid_heartbeat_candles)
    summary.update({
        "Capital inicial": _fmt(settings.paper_start_cash),
        "Comisión por lado": f"{_fmt(settings.fee_pct, 3)} %",
        "Slippage por lado": f"{_fmt(settings.slippage_pct, 3)} %",
        "Riesgo por operación": f"{_fmt(r.risk_per_trade_pct)} %",
        "Posición máxima": f"{_fmt(r.max_position_pct)} %",
        "Pérdida diaria máx.": f"{_fmt(r.max_daily_loss_pct)} %",
        "Drawdown máx.": f"{_fmt(r.max_drawdown_pct)} %",
        "Operaciones/día máx.": str(r.max_trades_per_day),
        "Confianza mínima": _fmt(r.min_confidence),
        "Cooldown (velas)": str(r.cooldown_candles),
        "Stop / TP por defecto": f"{_fmt(r.default_stop_pct)} % / {_fmt(r.default_take_profit_pct)} %",
        "Historia por decisión (velas)": str(settings.history_candles),
    })
    return summary


def cmd_backtest(args: argparse.Namespace, settings: Settings) -> int:
    from .backtest import run_backtest
    from .brain.factory import build_engine
    from .journal import Journal
    from .metrics import render_text_summary
    from .report import render_html_report, write_report

    candles, source, historical = _backtest_candles(args, settings)
    if len(candles) < args.warmup + 1:
        raise UsageError(f"hacen falta al menos {args.warmup + 1} velas (warm-up {args.warmup}), hay {len(candles)}")
    uses_llm = settings.engine != "rules"
    max_calls = args.max_llm_calls
    if uses_llm and max_calls is None:
        max_calls = DEFAULT_MAX_LLM_CALLS
    print(f"Backtest {settings.symbol} {settings.timeframe} · {len(candles)} velas · datos: {source} · "
          f"motor: {settings.engine}")
    if uses_llm:
        typical, worst = _llm_cost_per_call(settings, candles)
        calls = min(max_calls or 0, len(candles) - args.warmup)
        print(f"Coste LLM estimado ({settings.model}): hasta {calls} llamadas · típico ~US${typical * calls:.2f}, "
              f"peor caso ~US${worst * calls:.2f} (US${typical:.4f}-{worst:.4f} por llamada). "
              f"El presupuesto diario (US${settings.max_llm_cost_usd_per_day:.2f}) también limita el gasto real.")
        if worst * calls > LLM_COST_CONFIRM_USD and not args.yes:
            print(f"El peor caso supera US${LLM_COST_CONFIRM_USD:.2f}: repetí con --yes para confirmar, "
                  "o bajá --max-llm-calls / --candles.", file=sys.stderr)
            return EXIT_CONFIG
        _warn_missing_llm_key(settings)

    engine = build_engine(settings)
    journal = Journal(args.journal) if args.journal else None
    progress = _progress_printer() if uses_llm else None
    try:
        result = run_backtest(
            candles, settings, engine, warmup=args.warmup, max_llm_calls=max_calls,
            journal=journal, historical_data=historical, progress=progress,
        )
    finally:
        if journal is not None:
            journal.close()
    warnings = list(result.warnings)
    if not historical:
        warnings.insert(0, "Datos sintéticos: no son el mercado real; sirven para probar el sistema, no la estrategia.")

    print()
    print(render_text_summary(result.metrics))
    a = result.actions
    print(f"\nDecisiones: {result.decisions} (BUY {a['BUY']} · SELL {a['SELL']} · HOLD {a['HOLD']}) · "
          f"aprobadas por riesgo: {result.approved} · fills: {len(result.fills)}")
    if warnings:
        print("\nAdvertencias:")
        for warning in warnings:
            print(f"  - {warning}")
    title = f"JEB · backtest {settings.symbol} {settings.timeframe} · motor {settings.engine}"
    html = render_html_report(
        result.metrics, result.equity_curve, result.price_curve, result.trades, title,
        _settings_summary(settings, source, len(candles), args.warmup, max_calls), warnings,
    )
    path = write_report(args.report, html)
    print(f"\nInforme HTML: {path}")
    return EXIT_OK


def _progress_printer() -> Any:
    def progress(done: int, total: int) -> None:
        print(f"  progreso: {done}/{total} velas", file=sys.stderr)

    return progress


# ---------------------------------------------------------------------------- decide


def _indicator_line(snapshot: MarketSnapshot) -> str:
    ind = snapshot.indicators
    parts = [
        f"EMA9 {_fmt_price(ind.ema_fast)}",
        f"EMA21 {_fmt_price(ind.ema_slow)}",
        f"EMA50 {_fmt_price(ind.ema_trend)}",
        f"RSI {_fmt(ind.rsi, 1)}",
        f"MACD hist {_fmt(ind.macd_hist, 3)}",
        f"ATR {_fmt(ind.atr_pct, 2)} %",
        f"BB %b {_fmt(ind.bb_pct_b, 2)}",
        f"vol x{_fmt(ind.volume_ratio, 2)}",
    ]
    return " · ".join(parts)


def _print_decision(decision: Decision, elapsed_ms: float) -> None:
    stop = f"{_fmt(decision.stop_loss_pct)} %" if decision.stop_loss_pct is not None else "-"
    tp = f"{_fmt(decision.take_profit_pct)} %" if decision.take_profit_pct is not None else "-"
    print(f"Decisión     {decision.action.value} · confianza {_fmt(decision.confidence)} · "
          f"tamaño {_fmt(decision.size_pct)} · fuente {decision.source}")
    print(f"Stop / TP    {stop} / {tp}")
    print(f"Razonamiento {decision.reasoning or '-'}")
    llm = f" (LLM {_fmt(decision.latency_ms, 0)} ms)" if decision.latency_ms is not None else ""
    print(f"Latencia     {_fmt(elapsed_ms, 1)} ms total{llm}")
    model = f" · {decision.model}" if decision.model else ""
    print(f"Coste        US${decision.cost_usd:.5f} · tokens {decision.input_tokens} in / "
          f"{decision.output_tokens} out{model}")


def cmd_decide(args: argparse.Namespace, settings: Settings) -> int:
    from .brain.factory import build_engine
    from .engine import safe_decide
    from .features import build_snapshot

    if args.synthetic:
        from .market.synthetic import SyntheticMarket

        market: Any = SyntheticMarket(seed=args.seed, timeframe=settings.timeframe)
        source = f"sintético (seed {args.seed})"
    else:
        market = _public_market(settings)
        source = f"{settings.exchange} (datos públicos)"
    candles = market.fetch_candles(settings.symbol, settings.timeframe, settings.history_candles)
    if not candles:
        raise JebError("el mercado no devolvió velas cerradas")
    snapshot = build_snapshot(settings.symbol, settings.timeframe, candles)
    view = _flat_view(settings)
    _warn_missing_llm_key(settings)
    engine = build_engine(settings)

    started = time.perf_counter()
    decision = safe_decide(engine, snapshot, view)
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    print("JEB · decisión rápida (demo: NO se envían órdenes)")
    print(f"Mercado      {settings.symbol} {settings.timeframe} · {source} · {len(candles)} velas cerradas")
    print(f"Última vela  {_iso(snapshot.timestamp)} (apertura, UTC) · cierre {_fmt_price(snapshot.price)}")
    print(f"Indicadores  {_indicator_line(snapshot)}")
    print(f"Cartera      sin posición · efectivo {_fmt(view.cash)} {view.quote_currency}")
    print(f"Motor        {getattr(engine, 'name', settings.engine)}")
    _print_decision(decision, elapsed_ms)
    return EXIT_OK


# ---------------------------------------------------------------------------- paper / live


def _tick_printer(settings: Settings, totals: dict[str, float]) -> Any:
    def on_tick(result: Any) -> None:
        totals["steps"] += 1
        decision = result.decision
        if decision is not None:
            totals["llm_cost"] += decision.cost_usd
        if result.status != "ok":
            print(f"[{_iso(result.timestamp)}] {result.status} {', '.join(result.events)}".rstrip())
            return
        if decision is not None:
            verdict = "aprobada" if result.verdict and result.verdict.approved else "rechazada"
            what = f"{decision.action.value} ({decision.source}, conf {_fmt(decision.confidence)}, {verdict})"
            if decision.action.value == "HOLD":
                what = f"HOLD ({decision.source})"
        else:
            what = "sin decisión"
        halted = f" · HALT {result.halted}" if result.halted else ""
        print(f"[{_iso(result.timestamp)}] precio {_fmt_price(result.price)} · {what} · "
              f"equity {_fmt(result.equity)} {settings.quote_currency}{halted}")
        for fill in result.fills:
            print(f"    -> {fill.side.value.upper()} {fill.quantity:.8g} @ {_fmt_price(fill.price)} "
                  f"(comisión {_fmt(fill.fee, 4)})")
        for trade in result.trades:
            print(f"    <- operación cerrada ({trade.exit_reason}): PnL {_fmt(trade.pnl, 2, signed=True)} "
                  f"({_fmt(trade.pnl_pct, 2, signed=True)} %)")
        for kind in result.events:
            print(f"    !! {kind}")

    return on_tick


def _run_engine(engine: Any, **kwargs: Any) -> None:
    """``engine.run_forever`` with SIGTERM mapped to a graceful stop (restored afterwards)."""
    previous = None
    try:
        previous = signal.signal(signal.SIGTERM, lambda *_: engine.stop())
    except (ValueError, OSError):  # not in the main thread / unsupported platform
        pass
    try:
        engine.run_forever(**kwargs)
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)


def _print_session_end(engine: Any, totals: dict[str, float]) -> None:
    p = engine.portfolio
    try:
        balances = engine.broker.balances()
        cash = f"{_fmt(balances.cash)} {p.quote_currency}"
    except JebError:
        cash = "n/d"
    position = f"{p.qty:.8g} {p.base_currency} @ {_fmt_price(p.avg_entry_price)}" if p.in_position else "sin posición"
    print(f"\nFin de la sesión: {int(totals['steps'])} pasos · efectivo {cash} · {position} · "
          f"operaciones cerradas {len(p.trades)} · PnL realizado {_fmt(p.realized_pnl, 2, signed=True)} · "
          f"coste LLM de la sesión US${totals['llm_cost']:.4f}")
    if p.halted_reason:
        print(f"Trading detenido por kill switch: {p.halted_reason} (ver `jeb status --reset-halt`).")


def cmd_paper(args: argparse.Namespace, settings: Settings) -> int:
    from .brain.factory import build_engine
    from .engine import TradingEngine
    from .execution.paper import PaperBroker
    from .journal import Journal

    if args.synthetic:
        from .market.synthetic import SyntheticMarket

        market: Any = SyntheticMarket(seed=args.seed, timeframe=settings.timeframe)
        label = "synthetic"
    else:
        if args.fast:
            raise UsageError("--fast solo tiene sentido con --synthetic")
        market = _public_market(settings)
        label = settings.exchange
    _warn_missing_llm_key(settings)
    broker = PaperBroker(settings.symbol, settings.paper_start_cash, settings.fee_pct, settings.slippage_pct)
    journal = Journal(settings.journal_path)
    try:
        engine = TradingEngine(
            settings, market, broker, build_engine(settings), journal,
            state_key=f"paper:{label}:{settings.symbol}:{settings.timeframe}",
        )
        resumed = False if (args.synthetic or args.fresh) else engine.restore_state()
        print(f"JEB · PAPER TRADING (dinero simulado) · {settings.symbol} {settings.timeframe} · datos: "
              f"{'sintéticos' if args.synthetic else label + ' (públicos)'} · motor {settings.engine} · "
              f"journal {settings.journal_path}" + (" · sesión reanudada" if resumed else ""))
        totals = {"steps": 0.0, "llm_cost": 0.0}
        printer = _tick_printer(settings, totals)

        def on_tick(result: Any) -> None:
            printer(result)
            if args.synthetic:
                market.advance()

        _run_engine(
            engine,
            max_iterations=args.max_iterations,
            wait_for_close=not args.synthetic,
            interval_s=0.0 if args.fast else 1.0,
            on_tick=on_tick,
        )
        _print_session_end(engine, totals)
    finally:
        journal.close()
    return EXIT_OK


def _confirm_mainnet(args: argparse.Namespace, settings: Settings) -> bool:
    if args.yes and settings.live_confirm == LIVE_CONFIRM_PHRASE:
        print("Confirmación por --yes + JEB_LIVE_CONFIRM.")
        return True
    try:
        answer = input(f"Escribí el símbolo exacto ({settings.symbol}) para operar con DINERO REAL: ")
    except EOFError:
        return False
    return answer.strip() == settings.symbol


def cmd_live(args: argparse.Namespace, settings: Settings) -> int:
    if not settings.is_live:
        print(
            "El modo live está desactivado: requiere JEB_MODE=live y JEB_API_KEY/JEB_API_SECRET "
            "(para mainnet además JEB_USE_TESTNET=false y JEB_LIVE_CONFIRM). Usá `jeb paper` para simular.",
            file=sys.stderr,
        )
        return EXIT_CONFIG
    from .brain.factory import build_engine
    from .engine import TradingEngine
    from .execution.ccxt_broker import CcxtBroker
    from .journal import Journal
    from .market.ccxt_source import CcxtMarket
    from .portfolio import Portfolio

    network = "TESTNET (fondos de prueba)" if settings.use_testnet else "MAINNET — DINERO REAL"
    bar = "=" * 72
    print(f"{bar}\n  JEB LIVE · órdenes REALES en {settings.exchange} {network}\n"
          f"  {settings.symbol} {settings.timeframe} · motor {settings.engine} · journal {settings.journal_path}\n"
          f"  Usá claves SIN permiso de retiro y con whitelist de IP. Ctrl+C detiene y guarda el estado.\n{bar}")
    allow_mainnet = False
    if not settings.use_testnet:
        if not _confirm_mainnet(args, settings):
            print("Confirmación inválida: no se opera en mainnet.", file=sys.stderr)
            return EXIT_CONFIG
        allow_mainnet = True
    _warn_missing_llm_key(settings)
    broker = CcxtBroker(
        settings.symbol, exchange_id=settings.exchange, api_key=settings.api_key, api_secret=settings.api_secret,
        use_testnet=settings.use_testnet, allow_mainnet=allow_mainnet, fee_pct_estimate=settings.fee_pct,
    )
    market = CcxtMarket(
        settings.exchange, use_testnet=settings.use_testnet, api_key=settings.api_key,
        api_secret=settings.api_secret, exchange=broker.exchange,
    )
    portfolio = Portfolio(
        settings.symbol, settings.quote_currency, settings.base_currency,
        dust_notional=settings.risk.min_order_notional,
    )
    net_label = "testnet" if settings.use_testnet else "mainnet"
    journal = Journal(settings.journal_path)
    try:
        engine = TradingEngine(
            settings, market, broker, build_engine(settings), journal, portfolio=portfolio,
            state_key=f"live:{net_label}:{settings.exchange}:{settings.symbol}:{settings.timeframe}",
        )
        if engine.restore_state():
            print("Estado anterior reanudado.")
        totals = {"steps": 0.0, "llm_cost": 0.0}
        _run_engine(engine, max_iterations=args.max_iterations, on_tick=_tick_printer(settings, totals))
        _print_session_end(engine, totals)
    finally:
        journal.close()
    return EXIT_OK


# ---------------------------------------------------------------------------- download / status


def cmd_download(args: argparse.Namespace, settings: Settings) -> int:
    from .market.csv_source import save_candles

    until = args.until if args.until is not None else int(time.time() * 1000)
    if until <= args.since:
        raise UsageError("--until debe ser posterior a --since")
    candles = _public_market(settings).download_history(settings.symbol, settings.timeframe, args.since, until)
    save_candles(args.out, candles)
    span = f"{_iso(candles[0].timestamp)} → {_iso(candles[-1].timestamp)}" if candles else "sin datos"
    print(f"{len(candles)} velas {settings.symbol} {settings.timeframe} de {settings.exchange} ({span}) "
          f"guardadas en {args.out}")
    return EXIT_OK


def _print_states(journal: Any, reset_halt: bool, settings: Settings) -> None:
    from .portfolio import Portfolio
    from .risk import RiskManager

    for key in journal.state_keys("engine:"):
        state = journal.load_state(key) or {}
        raw = state.get("portfolio")
        if not raw:
            continue
        p = Portfolio.from_state(raw)
        position = "sin posición"
        if p.in_position:
            position = (f"{p.qty:.8g} {p.base_currency} @ {_fmt_price(p.avg_entry_price)} "
                        f"(stop {_fmt_price(p.stop_loss)}, tp {_fmt_price(p.take_profit)})")
        halt = f" · HALT {p.halted_reason}" if p.halted_reason else ""
        print(f"  {key.removeprefix('engine:')}: {position} · PnL realizado {_fmt(p.realized_pnl, 2, signed=True)} · "
              f"última vela {_iso(state.get('last_candle_ts'))}{halt}")
        if reset_halt and p.halted_reason:
            previous = p.halted_reason
            RiskManager(settings.risk, settings.fee_pct, settings.slippage_pct).reset_kill_switch(p)
            state["portfolio"] = p.to_state()
            journal.save_state(key, state)
            journal.record_event(int(time.time() * 1000), "halt_reset", f"{key}: {previous} cleared by the operator")
            print(f"    halt '{previous}' eliminado (pico y equity diaria re-basados al último valor).")


def cmd_status(args: argparse.Namespace, settings: Settings) -> int:
    from .journal import Journal

    path = settings.journal_path
    if path != ":memory:" and not Path(path).exists():
        print(f"Todavía no hay journal en {path}: ejecutá `jeb paper` o `jeb live` primero.")
        return EXIT_OK
    with Journal(path) as journal:
        s = journal.summary()
        a = s["actions"]
        print(f"Journal {path}")
        print(f"Decisiones   {s['decisions']} (BUY {a['BUY']} · SELL {a['SELL']} · HOLD {a['HOLD']}) · "
              f"aprobadas {s['approved']}")
        if s["sources"]:
            print("Fuentes      " + " · ".join(f"{k} {v}" for k, v in s["sources"].items()))
        latency = f" · latencia media {_fmt(s['avg_latency_ms'], 0)} ms" if s["avg_latency_ms"] is not None else ""
        print(f"LLM          {s['llm_calls']} llamadas · US${s['llm_cost_usd']:.4f}{latency}")
        print(f"Fills        {s['fills']} · comisiones {_fmt(s['fees_paid'], 4)}")
        print(f"Operaciones  {s['trades']} (ganadoras {s['winning_trades']}) · PnL realizado "
              f"{_fmt(s['realized_pnl'], 2, signed=True)}")
        print(f"Eventos      {s['events']}")
        if journal.state_keys("engine:"):
            print("Estado guardado:")
            _print_states(journal, args.reset_halt, settings)
        decisions = journal.recent_decisions(args.limit)
        if decisions:
            print(f"\nÚltimas {len(decisions)} decisiones:")
            for d in decisions:
                ok = "-" if d["action"] == "HOLD" else {True: "aprobada", False: "rechazada", None: "-"}[d["approved"]]
                reason = " ".join((d["reasoning"] or "").split())[:90]
                print(f"  {_iso(d['ts'])} {d['action']:<4} {d['source']:<8} conf {_fmt(d['confidence'])} "
                      f"{ok:<9} {reason}")
        events = journal.recent_events(args.limit)
        if events:
            print(f"\nÚltimos {len(events)} eventos:")
            for e in events:
                print(f"  {_iso(e['ts'])} {e['kind']}: {' '.join(e['message'].split())[:110]}")
    return EXIT_OK


# ---------------------------------------------------------------------------- parser


def _common(parser: argparse.ArgumentParser, *, market: bool = True, engine: bool = True) -> None:
    parser.add_argument("-v", "--verbose", action="count", default=argparse.SUPPRESS,
                        help="más logs (-v INFO, -vv DEBUG)")
    parser.add_argument("--env-file", default=argparse.SUPPRESS, help="archivo .env (por defecto ./.env)")
    if market:
        parser.add_argument("--symbol", help="par BASE/QUOTE (por defecto JEB_SYMBOL)")
        parser.add_argument("--timeframe", help="p. ej. 1m, 5m, 1h (por defecto JEB_TIMEFRAME)")
    if engine:
        parser.add_argument("--engine", choices=("rules", "hybrid", "claude"), help="motor (por defecto JEB_ENGINE)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jeb",
        description="JEB: trading de cripto spot con Claude Haiku 4.5 como cerebro rápido, reglas "
                    "deterministas y un gestor de riesgo con la última palabra. Paper trading por defecto.",
    )
    parser.add_argument("-v", "--verbose", action="count", default=0, help="más logs (-v INFO, -vv DEBUG)")
    parser.add_argument("--env-file", default=".env", help="archivo .env (por defecto ./.env)")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMANDO")

    p = sub.add_parser("backtest", help="backtest sin look-ahead con informe HTML")
    _common(p)
    p.add_argument("--source", choices=("synthetic", "csv", "exchange"), default="synthetic")
    p.add_argument("--csv", help="archivo CSV de velas (con --source csv)")
    p.add_argument("--candles", type=_positive_int, help=f"número de velas (por defecto {DEFAULT_CANDLES}; CSV: todas)")
    p.add_argument("--seed", type=int, default=42, help="semilla del mercado sintético")
    p.add_argument("--warmup", type=_positive_int, default=DEFAULT_WARMUP, help="velas de historia antes de decidir")
    p.add_argument("--max-llm-calls", type=_non_negative_int,
                   help=f"tope de llamadas al LLM; luego sigue con reglas (por defecto {DEFAULT_MAX_LLM_CALLS})")
    p.add_argument("--report", default=DEFAULT_REPORT, help=f"ruta del informe HTML (por defecto {DEFAULT_REPORT})")
    p.add_argument("--journal", help="registrar el backtest en este journal SQLite (opcional)")
    p.add_argument("--yes", action="store_true", help="aceptar un coste LLM estimado > US$1")
    p.set_defaults(handler=cmd_backtest, journal_overrides_settings=False)

    p = sub.add_parser("decide", help="una decisión rápida de demostración (no envía órdenes)")
    _common(p)
    p.add_argument("--synthetic", action="store_true", help="usar datos sintéticos (offline)")
    p.add_argument("--seed", type=int, default=42)
    p.set_defaults(handler=cmd_decide)

    p = sub.add_parser("paper", help="paper trading en tiempo real (dinero simulado)")
    _common(p)
    p.add_argument("--synthetic", action="store_true", help="mercado sintético offline")
    p.add_argument("--fast", action="store_true", help="con --synthetic: sin esperas entre velas")
    p.add_argument("--max-iterations", type=_positive_int, help="detenerse tras N velas")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--journal", help="ruta del journal SQLite (por defecto JEB_JOURNAL_PATH)")
    p.add_argument("--fresh", action="store_true", help="no reanudar el estado guardado")
    p.set_defaults(handler=cmd_paper)

    p = sub.add_parser("live", help="órdenes reales vía ccxt (testnet por defecto)")
    _common(p, market=False)
    p.add_argument("--max-iterations", type=_positive_int, help="detenerse tras N velas")
    p.add_argument("--journal", help="ruta del journal SQLite (por defecto JEB_JOURNAL_PATH)")
    p.add_argument("--yes", action="store_true", help="mainnet: omitir la confirmación interactiva "
                   "(solo si JEB_LIVE_CONFIRM también está configurado)")
    p.set_defaults(handler=cmd_live)

    p = sub.add_parser("download", help="descargar velas históricas a CSV")
    _common(p, engine=False)
    p.add_argument("--since", type=_parse_day, required=True, help="inicio, YYYY-MM-DD (UTC)")
    p.add_argument("--until", type=_parse_day, help="fin (exclusivo), YYYY-MM-DD; por defecto ahora")
    p.add_argument("--out", required=True, help="archivo CSV de salida")
    p.set_defaults(handler=cmd_download)

    p = sub.add_parser("status", help="resumen del journal y últimas decisiones")
    _common(p, market=False, engine=False)
    p.add_argument("--journal", help="ruta del journal SQLite (por defecto JEB_JOURNAL_PATH)")
    p.add_argument("--limit", type=_positive_int, default=10, help="decisiones/eventos a mostrar")
    p.add_argument("--reset-halt", action="store_true",
                   help="quitar un kill switch activo del estado guardado (detené el bot antes)")
    p.set_defaults(handler=cmd_status)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --help or a usage error
        return exc.code if isinstance(exc.code, int) else EXIT_CONFIG
    _configure_logging(args.verbose)
    try:
        _export_anthropic_key(args.env_file)
        settings = _load_settings(args)
        return args.handler(args, settings)
    except (ConfigError, UsageError) as exc:
        print(f"Error de configuración: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except KeyboardInterrupt:
        print("\nInterrumpido.", file=sys.stderr)
        return EXIT_RUNTIME
    except (JebError, OSError, ValueError) as exc:
        logger.debug("command failed", exc_info=True)
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_RUNTIME
    except Exception as exc:  # last resort: no raw traceback for the user (use -vv to see it)
        logger.debug("unexpected error", exc_info=True)
        print(f"Error inesperado ({type(exc).__name__}): {exc}", file=sys.stderr)
        return EXIT_RUNTIME


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
