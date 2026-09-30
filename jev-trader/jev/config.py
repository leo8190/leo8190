"""Configuration loaded from environment variables (and an optional .env file).

Safety defaults: paper trading, exchange testnet, hybrid engine (rules + Jev), tight risk limits.
Live trading with real money needs three explicit opt-ins (see ``validate``).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

from .models import ConfigError

LIVE_CONFIRM_PHRASE = "YES_I_ACCEPT_REAL_MONEY_RISK"
DEFAULT_JEV_MODEL = "jev-latest"
DEFAULT_CLAUDE_MODEL = "claude-haiku-4-5"
ENGINES = ("hybrid", "jev", "rules", "claude")
CONFIRMERS = ("jev", "claude")
MODES = ("paper", "live")
_TRUE = {"1", "true", "yes", "y", "on", "si", "sí"}
_FALSE = {"0", "false", "no", "n", "off"}


def _bool(value: str) -> bool:
    """Strict boolean: a typo must never silently mean False (e.g. mainnet or no flatten)."""
    text = value.strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueError(f"not a boolean: {value!r} (use true/false)")


@dataclass(frozen=True)
class RiskConfig:
    risk_per_trade_pct: float = 1.0  # equity lost if the stop is hit
    max_position_pct: float = 25.0  # max position value as % of equity
    max_daily_loss_pct: float = 3.0  # kill switch: loss since UTC day start
    max_drawdown_pct: float = 10.0  # kill switch: loss from equity peak
    max_trades_per_day: int = 10
    min_confidence: float = 0.6  # 0..1, engine confidence needed to trade
    cooldown_candles: int = 3  # candles to wait after closing a position
    default_stop_pct: float = 2.0
    default_take_profit_pct: float = 4.0
    min_stop_pct: float = 0.3
    max_stop_pct: float = 10.0
    min_order_notional: float = 10.0  # quote units
    flatten_on_kill: bool = True  # close the position when a kill switch trips


@dataclass(frozen=True)
class Settings:
    mode: str = "paper"
    live_confirm: str = ""
    exchange: str = "binance"
    use_testnet: bool = True
    api_key: str = field(default="", repr=False)
    api_secret: str = field(default="", repr=False)
    symbol: str = "BTC/USDT"
    timeframe: str = "5m"
    engine: str = "hybrid"
    hybrid_confirmer: str = "jev"  # model that confirms rules entries in hybrid mode
    # hybrid: consult the model at least every N candles; 0 = auto (1 for Jev, 12 for Claude)
    hybrid_heartbeat_candles: int = 0
    jev_model: str = DEFAULT_JEV_MODEL
    jev_timeout_s: float = 3.0
    jev_price_per_mtok_input: float = 0.042  # USD; Jev output tokens are free
    claude_model: str = DEFAULT_CLAUDE_MODEL
    claude_timeout_s: float = 8.0
    claude_max_tokens: int = 400
    ai_max_retries: int = 1
    max_ai_cost_usd_per_day: float = 1.0  # per engine instance
    history_candles: int = 200
    paper_start_cash: float = 1000.0
    # live: most quote the bot may use (plus its realized PnL); 0 = the whole free balance
    live_max_capital: float = 0.0
    fee_pct: float = 0.1
    slippage_pct: float = 0.05
    journal_path: str = "jev_journal.sqlite3"
    risk: RiskConfig = field(default_factory=RiskConfig)

    @property
    def heartbeat_candles(self) -> int:
        """Effective hybrid heartbeat: Jev is fast and nearly free, so it can be asked every candle."""
        if self.hybrid_heartbeat_candles > 0:
            return self.hybrid_heartbeat_candles
        return 1 if self.hybrid_confirmer == "jev" else 12

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    @property
    def base_currency(self) -> str:
        return self.symbol.split("/")[0]

    @property
    def quote_currency(self) -> str:
        return self.symbol.split("/")[1]

    def validate(self) -> "Settings":
        if self.mode not in MODES:
            raise ConfigError(f"JEV_MODE must be one of {MODES}, got {self.mode!r}")
        if self.engine not in ENGINES:
            raise ConfigError(f"JEV_ENGINE must be one of {ENGINES}, got {self.engine!r}")
        if self.hybrid_confirmer not in CONFIRMERS:
            raise ConfigError(
                f"JEV_HYBRID_CONFIRMER must be one of {CONFIRMERS}, got {self.hybrid_confirmer!r}"
            )
        if "/" not in self.symbol:
            raise ConfigError(f"JEV_SYMBOL must look like BASE/QUOTE, got {self.symbol!r}")
        if self.is_live:
            if not self.api_key or not self.api_secret:
                raise ConfigError("Live trading requires JEV_API_KEY and JEV_API_SECRET")
            if not self.use_testnet and self.live_confirm != LIVE_CONFIRM_PHRASE:
                raise ConfigError(
                    "Real-money trading (JEV_MODE=live, JEV_USE_TESTNET=false) requires "
                    f"JEV_LIVE_CONFIRM={LIVE_CONFIRM_PHRASE}"
                )
        r = self.risk
        checks = [
            (0 < r.risk_per_trade_pct <= 5, "JEV_RISK_PER_TRADE_PCT must be in (0, 5]"),
            (0 < r.max_position_pct <= 100, "JEV_MAX_POSITION_PCT must be in (0, 100]"),
            (0 < r.max_daily_loss_pct <= 50, "JEV_MAX_DAILY_LOSS_PCT must be in (0, 50]"),
            (0 < r.max_drawdown_pct <= 90, "JEV_MAX_DRAWDOWN_PCT must be in (0, 90]"),
            (r.max_trades_per_day >= 1, "JEV_MAX_TRADES_PER_DAY must be >= 1"),
            (0 <= r.min_confidence <= 1, "JEV_MIN_CONFIDENCE must be in [0, 1]"),
            (r.cooldown_candles >= 0, "JEV_COOLDOWN_CANDLES must be >= 0"),
            (0 < r.min_stop_pct <= r.default_stop_pct <= r.max_stop_pct,
             "Stops must satisfy 0 < MIN_STOP <= DEFAULT_STOP <= MAX_STOP"),
            (r.default_take_profit_pct > 0, "JEV_DEFAULT_TAKE_PROFIT_PCT must be > 0"),
            (r.min_order_notional > 0, "JEV_MIN_ORDER_NOTIONAL must be > 0"),
            (self.jev_timeout_s > 0, "JEV_TIMEOUT_S must be > 0"),
            (self.jev_price_per_mtok_input >= 0, "JEV_PRICE_PER_MTOK_INPUT must be >= 0"),
            (self.claude_timeout_s > 0, "JEV_CLAUDE_TIMEOUT_S must be > 0"),
            (self.claude_max_tokens >= 64, "JEV_CLAUDE_MAX_TOKENS must be >= 64"),
            (self.ai_max_retries >= 0, "JEV_AI_MAX_RETRIES must be >= 0"),
            (self.max_ai_cost_usd_per_day >= 0, "JEV_MAX_AI_COST_USD_PER_DAY must be >= 0"),
            (self.hybrid_heartbeat_candles >= 0, "JEV_HYBRID_HEARTBEAT_CANDLES must be >= 0 (0 = auto)"),
            (self.history_candles >= 60, "JEV_HISTORY_CANDLES must be >= 60"),
            (self.paper_start_cash > 0, "JEV_PAPER_START_CASH must be > 0"),
            (self.live_max_capital >= 0, "JEV_LIVE_MAX_CAPITAL must be >= 0 (0 = whole free balance)"),
            (0 <= self.fee_pct < 5, "JEV_FEE_PCT must be in [0, 5)"),
            (0 <= self.slippage_pct < 5, "JEV_SLIPPAGE_PCT must be in [0, 5)"),
        ]
        for ok, message in checks:
            if not ok:
                raise ConfigError(message)
        return self

    def with_overrides(self, **changes) -> "Settings":
        return replace(self, **changes).validate()


def _dotenv_value(raw: str) -> str:
    """Value part of a .env line: quotes removed; unquoted values drop a trailing `` # comment``."""
    value = raw.strip()
    if value[:1] in ("'", '"'):
        end = value.find(value[0], 1)
        return value[1:end] if end != -1 else value[1:]
    comment = re.search(r"\s#", value)
    if comment:
        value = value[: comment.start()]
    return value.strip()


def load_dotenv(path: str | os.PathLike = ".env") -> dict[str, str]:
    """Minimal .env reader (KEY=VALUE lines, optional ``export``, inline ``# comments``).

    Real env vars take precedence (see ``load_settings``).
    """
    values: dict[str, str] = {}
    p = Path(path)
    if not p.is_file():
        return values
    for raw in p.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        values[key] = _dotenv_value(value)
    return values


_SETTINGS_ENV = {
    "mode": "JEV_MODE",
    "live_confirm": "JEV_LIVE_CONFIRM",
    "exchange": "JEV_EXCHANGE",
    "use_testnet": "JEV_USE_TESTNET",
    "api_key": "JEV_API_KEY",
    "api_secret": "JEV_API_SECRET",
    "symbol": "JEV_SYMBOL",
    "timeframe": "JEV_TIMEFRAME",
    "engine": "JEV_ENGINE",
    "hybrid_confirmer": "JEV_HYBRID_CONFIRMER",
    "hybrid_heartbeat_candles": "JEV_HYBRID_HEARTBEAT_CANDLES",
    "jev_model": "JEV_MODEL",
    "jev_timeout_s": "JEV_TIMEOUT_S",
    "jev_price_per_mtok_input": "JEV_PRICE_PER_MTOK_INPUT",
    "claude_model": "JEV_CLAUDE_MODEL",
    "claude_timeout_s": "JEV_CLAUDE_TIMEOUT_S",
    "claude_max_tokens": "JEV_CLAUDE_MAX_TOKENS",
    "ai_max_retries": "JEV_AI_MAX_RETRIES",
    "max_ai_cost_usd_per_day": "JEV_MAX_AI_COST_USD_PER_DAY",
    "history_candles": "JEV_HISTORY_CANDLES",
    "paper_start_cash": "JEV_PAPER_START_CASH",
    "live_max_capital": "JEV_LIVE_MAX_CAPITAL",
    "fee_pct": "JEV_FEE_PCT",
    "slippage_pct": "JEV_SLIPPAGE_PCT",
    "journal_path": "JEV_JOURNAL_PATH",
}


def _coerce(value: str, target: type, name: str):
    try:
        if target is bool:
            return _bool(value)
        if target is int:
            return int(value)
        if target is float:
            return float(value)
        return value
    except ValueError as exc:
        raise ConfigError(f"{name}: cannot parse {value!r} as {target.__name__}") from exc


def _field_types(cls) -> dict[str, type]:
    defaults = cls()
    return {f.name: type(getattr(defaults, f.name)) for f in fields(cls)}


def load_settings(env: dict[str, str] | None = None, dotenv_path: str | None = ".env") -> Settings:
    """Build validated Settings from ``env`` (defaults to os.environ + .env file)."""
    if env is None:
        merged = load_dotenv(dotenv_path) if dotenv_path else {}
        merged.update(os.environ)
        env = merged

    types = _field_types(Settings)
    kwargs = {}
    for attr, var in _SETTINGS_ENV.items():
        if var in env and env[var] != "":
            kwargs[attr] = _coerce(env[var], types[attr], var)

    risk_types = _field_types(RiskConfig)
    risk_kwargs = {}
    for attr, target in risk_types.items():
        var = f"JEV_{attr.upper()}"
        if var in env and env[var] != "":
            risk_kwargs[attr] = _coerce(env[var], target, var)

    return Settings(risk=RiskConfig(**risk_kwargs), **kwargs).validate()
