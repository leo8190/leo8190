"""Shared data contracts for every JEB module.

Conventions (all modules rely on them):
- Timestamps are integer milliseconds since the Unix epoch (UTC), as in ccxt.
- A ``Candle.timestamp`` is the candle OPEN time. Only closed candles are ever
  passed to indicators or decision engines.
- Prices and cash are in the quote currency (e.g. USDT); quantities are in the
  base currency (e.g. BTC).
- Percentages named ``*_pct`` are expressed in percent (2.0 means 2 %).
  Fractions named ``*_frac`` or ``size_pct`` in [0, 1] are documented inline.
- JEB is spot, long-only, no leverage: SELL only reduces an existing position.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class JebError(Exception):
    """Base class for JEB errors."""


class ConfigError(JebError):
    """Invalid or unsafe configuration."""


class InsufficientFundsError(JebError):
    """The broker cannot fill the order with the available balance."""


class OrderRejectedError(JebError):
    """The broker or exchange refused the order (limits, precision, API error)."""


class MarketDataError(JebError):
    """Market data could not be fetched or is malformed."""


class Action(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class Candle(BaseModel):
    model_config = ConfigDict(frozen=True)

    timestamp: int  # open time, ms
    open: float
    high: float
    low: float
    close: float
    volume: float


class IndicatorSet(BaseModel):
    """Indicators computed on closed candles. ``None`` means not enough history."""

    ema_fast: float | None = None  # EMA(9)
    ema_slow: float | None = None  # EMA(21)
    ema_trend: float | None = None  # EMA(50)
    rsi: float | None = None  # RSI(14), 0..100, Wilder smoothing
    macd: float | None = None  # EMA(12) - EMA(26)
    macd_signal: float | None = None  # EMA(9) of macd
    macd_hist: float | None = None  # macd - macd_signal
    atr: float | None = None  # ATR(14), Wilder smoothing, quote units
    atr_pct: float | None = None  # atr / close * 100
    bb_upper: float | None = None  # Bollinger(20, 2)
    bb_middle: float | None = None
    bb_lower: float | None = None
    bb_pct_b: float | None = None  # (close - lower) / (upper - lower)
    return_1_pct: float | None = None  # % change of the last candle close
    return_12_pct: float | None = None  # % change over the last 12 candles
    volatility_pct: float | None = None  # stdev of 1-candle log returns (20) * 100
    volume_ratio: float | None = None  # last volume / SMA(20) volume


class MarketSnapshot(BaseModel):
    """Everything a decision engine may look at for one decision."""

    symbol: str
    timeframe: str
    timestamp: int  # open time of the last CLOSED candle, ms
    price: float  # close of the last closed candle
    indicators: IndicatorSet
    recent_closes: list[float] = Field(default_factory=list)  # oldest first, <= 20


class PortfolioView(BaseModel):
    """Read-only view of the account handed to decision engines and risk."""

    quote_currency: str
    base_currency: str
    cash: float  # free quote balance
    base_qty: float  # base held (long only, >= 0)
    avg_entry_price: float | None = None
    equity: float  # cash + base_qty * price
    position_value: float = 0.0  # base_qty * price
    unrealized_pnl_pct: float | None = None  # vs avg_entry_price, percent
    stop_loss: float | None = None  # active protective stop price
    take_profit: float | None = None  # active take-profit price

    @property
    def in_position(self) -> bool:
        return self.base_qty > 0


class Decision(BaseModel):
    """A decision engine's proposal. The risk manager has the final word."""

    action: Action
    confidence: float = 0.0  # 0..1
    # BUY: fraction of equity the engine would like to allocate (0..1).
    # SELL: fraction of the current position to close (0..1).
    size_pct: float = 0.0
    stop_loss_pct: float | None = None  # BUY only: % below entry
    take_profit_pct: float | None = None  # BUY only: % above entry
    reasoning: str = ""
    source: str = "unknown"  # "claude", "rules", "hybrid", "fallback"
    model: str | None = None
    latency_ms: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    @classmethod
    def hold(cls, reason: str, source: str = "fallback") -> "Decision":
        return cls(action=Action.HOLD, confidence=0.0, size_pct=0.0, reasoning=reason, source=source)


class OrderRequest(BaseModel):
    symbol: str
    side: Side
    quantity: float  # base units, > 0
    reference_price: float  # price used for sizing at decision time
    reason: str = ""


class Fill(BaseModel):
    order_id: str
    symbol: str
    side: Side
    quantity: float  # base filled
    price: float  # average fill price
    fee: float  # quote currency
    timestamp: int  # ms

    @property
    def notional(self) -> float:
        return self.quantity * self.price


class RiskVerdict(BaseModel):
    approved: bool
    order: OrderRequest | None = None
    reasons: list[str] = Field(default_factory=list)
    stop_loss: float | None = None  # absolute price, set on approved BUY
    take_profit: float | None = None  # absolute price, set on approved BUY


class Balances(BaseModel):
    cash: float  # free quote
    base_qty: float  # free base


class TradeRecord(BaseModel):
    """A closed (or partially closed) round trip, recorded on each SELL fill."""

    symbol: str
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float  # quote, net of the fees attributable to this quantity
    pnl_pct: float  # percent vs cost basis
    fees: float
    entry_time: int | None = None
    exit_time: int
    exit_reason: str = ""
