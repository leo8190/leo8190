"""Pre-flight checks (`jev doctor`): is this machine ready to run Jev Trader?

Read-only by design: it never places orders, never reads balances and never prints
secrets. Network checks only list Jev's models (no tokens spent) and read public
candles from the exchange.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .config import Settings

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"


@dataclass(frozen=True)
class Check:
    name: str
    status: str  # ok | warn | fail | skip
    detail: str


def ai_model(settings: Settings) -> str | None:
    """Which model the configured engine consults: "jev", "claude" or None."""
    if settings.engine == "hybrid":
        return settings.hybrid_confirmer
    return settings.engine if settings.engine in ("jev", "claude") else None


def _default_jev_client(timeout_s: float) -> Any:
    import typesafe_sdk as ts

    return ts.TypeSafeClient(timeout=timeout_s, retry=ts.RetryPolicy(max_retries=0))


def _default_market(settings: Settings) -> Any:
    from .market.ccxt_source import CcxtMarket

    return CcxtMarket(settings.exchange, use_testnet=False)  # public data, no keys


def check_mode(settings: Settings) -> Check:
    if not settings.is_live:
        return Check("Modo", OK, "paper: dinero simulado, nunca envía órdenes")
    if settings.use_testnet:
        return Check("Modo", OK, f"live en TESTNET de {settings.exchange} (fondos de prueba)")
    return Check("Modo", WARN, f"live en MAINNET de {settings.exchange}: DINERO REAL")


def check_jev_key(settings: Settings, env: Mapping[str, str]) -> Check:
    needed = ai_model(settings) == "jev"
    if env.get("TYPESAFE_API_KEY", "").strip():
        return Check("Clave de Jev", OK, "TYPESAFE_API_KEY configurada")
    if needed:
        return Check("Clave de Jev", FAIL,
                     f"falta TYPESAFE_API_KEY; el motor {settings.engine} nunca va a comprar (todo HOLD)")
    return Check("Clave de Jev", SKIP, f"el motor {settings.engine} no usa Jev")


def check_jev_api(settings: Settings, env: Mapping[str, str],
                  client_factory: Callable[[float], Any] = _default_jev_client) -> Check:
    """List Jev's models: proves the key and the network work without spending tokens."""
    if not env.get("TYPESAFE_API_KEY", "").strip():
        return Check("API de Jev", SKIP, "sin TYPESAFE_API_KEY")
    import typesafe_sdk as ts

    started = time.perf_counter()
    try:
        client = client_factory(5.0)
        names = [m.name for m in client.models.list().models]
    except (ts.TypeSafeAuthenticationError, ts.TypeSafePermissionDeniedError):
        return Check("API de Jev", FAIL, "la clave fue rechazada (401/403): revisala en typesafe.ai")
    except ts.TypeSafeAPIConnectionError as exc:
        return Check("API de Jev", FAIL, f"sin conexión a api.typesafe.ai ({type(exc).__name__})")
    except ts.TypeSafeError as exc:
        return Check("API de Jev", FAIL, f"error de la API ({type(exc).__name__}: {exc})")
    elapsed = (time.perf_counter() - started) * 1000
    if settings.jev_model not in names:
        return Check("API de Jev", WARN,
                     f"responde en {elapsed:.0f} ms pero no ofrece {settings.jev_model!r}; "
                     f"modelos: {', '.join(names) or 'ninguno'}")
    return Check("API de Jev", OK, f"responde en {elapsed:.0f} ms · modelos: {', '.join(names)}")


def check_claude_key(settings: Settings, env: Mapping[str, str]) -> Check:
    if ai_model(settings) != "claude":
        return Check("Clave de Claude", SKIP, "opcional: el motor configurado no usa Claude")
    if env.get("ANTHROPIC_API_KEY", "").strip() or env.get("ANTHROPIC_AUTH_TOKEN", "").strip():
        return Check("Clave de Claude", OK, "ANTHROPIC_API_KEY configurada")
    return Check("Clave de Claude", FAIL, "falta ANTHROPIC_API_KEY; Claude responderá HOLD")


def check_market(settings: Settings, market_factory: Callable[[Settings], Any] = _default_market,
                 now_ms: Callable[[], int] | None = None) -> Check:
    from .market.timeframes import timeframe_to_ms

    try:
        candles = market_factory(settings).fetch_candles(settings.symbol, settings.timeframe, 3)
    except Exception as exc:  # any failure here is a reportable result, not a crash
        return Check("Datos del exchange", FAIL,
                     f"no se pudieron leer velas públicas de {settings.exchange} ({type(exc).__name__})")
    if not candles:
        return Check("Datos del exchange", FAIL, f"{settings.exchange} no devolvió velas de {settings.symbol}")
    tf_ms = timeframe_to_ms(settings.timeframe)
    now = now_ms() if now_ms else int(time.time() * 1000)
    age_min = (now - (candles[-1].timestamp + tf_ms)) / 60_000
    detail = f"{settings.symbol} {settings.timeframe} · último cierre {candles[-1].close:g} hace {age_min:.0f} min"
    if age_min > tf_ms / 60_000 + 2:
        return Check("Datos del exchange", WARN, detail + " (datos viejos: el bot no abriría posiciones)")
    return Check("Datos del exchange", OK, detail)


def check_exchange_keys(settings: Settings) -> Check:
    if not settings.is_live:
        return Check("Claves del exchange", SKIP, "no hacen falta en paper")
    if settings.api_key and settings.api_secret:
        net = "testnet" if settings.use_testnet else "MAINNET"
        return Check("Claves del exchange", OK,
                     f"configuradas para {net}; deben ser solo trading spot, sin retiros y con whitelist de IP")
    return Check("Claves del exchange", FAIL, "faltan JEV_API_KEY / JEV_API_SECRET")


def run_checks(settings: Settings, env: Mapping[str, str], *, online: bool = True,
               jev_client_factory: Callable[[float], Any] = _default_jev_client,
               market_factory: Callable[[Settings], Any] = _default_market,
               now_ms: Callable[[], int] | None = None) -> list[Check]:
    checks = [check_mode(settings), check_jev_key(settings, env), check_claude_key(settings, env),
              check_exchange_keys(settings)]
    if online:
        checks.insert(2, check_jev_api(settings, env, jev_client_factory))
        checks.append(check_market(settings, market_factory, now_ms))
    else:
        checks.append(Check("Red", SKIP, "--offline: no se probaron Jev ni el exchange"))
    return checks
