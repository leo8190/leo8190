"""Exchange market data through ccxt (synchronous API).

Every exchange failure (ccxt.NetworkError, ccxt.ExchangeError, any other
ccxt.BaseError or a malformed response) is raised as MarketDataError with the
original exception as ``__cause__``. API keys are never logged and are redacted
from error messages.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from typing import Any

import ccxt

from ..models import Candle, MarketDataError
from .timeframes import closed_candles, dedupe_candles, make_candle, timeframe_to_ms

logger = logging.getLogger(__name__)

CLOCK_SYNC_INTERVAL_MS = 3_600_000


def _system_now_ms() -> int:
    return int(time.time() * 1000)


class CcxtMarket:
    """Market data from a ccxt exchange; implements ``MarketDataSource``.

    ``exchange`` injects a ready ccxt-like object (tests, or sharing one instance
    with a live broker); it is then used as-is: no sandbox switch is applied.
    The underlying object is exposed as ``self.exchange``.
    """

    def __init__(
        self,
        exchange_id: str = "binance",
        use_testnet: bool = True,
        api_key: str | None = None,
        api_secret: str | None = None,
        exchange: object | None = None,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self.exchange_id = exchange_id
        self.use_testnet = use_testnet
        self._secrets = tuple(s for s in (api_key, api_secret) if s)
        self._now_ms = now_ms or _system_now_ms
        self._clock_offset_ms = 0  # exchange time - local time
        self._clock_checked_at: int | None = None
        if exchange is None:
            exchange = self._create_exchange(api_key, api_secret)
        self.exchange: Any = exchange

    def __repr__(self) -> str:
        return f"CcxtMarket(exchange_id={self.exchange_id!r}, use_testnet={self.use_testnet})"

    # ------------------------------------------------------------------ public API

    def fetch_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        """Last ``limit`` CLOSED candles, oldest first; the forming candle is dropped."""
        self._timeframe_ms(timeframe)
        if limit <= 0:
            return []
        self._require("fetchOHLCV")
        rows = self._call("fetch_ohlcv", symbol, symbol, timeframe, limit=limit + 1)
        candles = dedupe_candles(self._to_candles(rows, symbol))
        return closed_candles(candles, timeframe, self._exchange_now_ms())[-limit:]

    def _exchange_now_ms(self) -> int:
        """Local clock corrected by the exchange's server time (re-synced hourly).

        The closed-candle filter must not trust a local clock that runs ahead: it would
        take the candle still forming on the exchange for a closed one.
        """
        local = self._now_ms()
        if self._clock_checked_at is None or local - self._clock_checked_at >= CLOCK_SYNC_INTERVAL_MS:
            self._clock_checked_at = local
            self._clock_offset_ms = self._measure_clock_offset(local)
        return local + self._clock_offset_ms

    def _measure_clock_offset(self, before: int) -> int:
        has = getattr(self.exchange, "has", None)
        fetch_time = getattr(self.exchange, "fetch_time", None)
        if not callable(fetch_time) or (isinstance(has, dict) and not has.get("fetchTime")):
            return self._clock_offset_ms
        try:
            server = int(fetch_time())
        except Exception as exc:  # optional: keep the last known offset
            logger.debug("%s fetch_time failed: %s", self.exchange_id, type(exc).__name__)
            return self._clock_offset_ms
        offset = server - (before + self._now_ms()) // 2
        if abs(offset) > 1000:
            logger.warning("local clock is %+.1f s off %s time: using the exchange time for closed candles",
                           -offset / 1000, self.exchange_id)
        return offset

    def fetch_last_price(self, symbol: str) -> float:
        """Last traded price from the ticker ("last", then "close", then bid/ask mid)."""
        ticker = self._call("fetch_ticker", symbol, symbol)
        if not isinstance(ticker, dict):
            raise MarketDataError(f"{self.exchange_id}: malformed ticker for {symbol}: {ticker!r}")
        for key in ("last", "close"):
            price = _positive(ticker.get(key))
            if price is not None:
                return price
        bid, ask = _positive(ticker.get("bid")), _positive(ticker.get("ask"))
        if bid is not None and ask is not None:
            return (bid + ask) / 2
        raise MarketDataError(f"{self.exchange_id}: ticker for {symbol} has no usable price")

    def download_history(
        self,
        symbol: str,
        timeframe: str,
        since_ms: int,
        until_ms: int,
        page_limit: int = 1000,
    ) -> list[Candle]:
        """Closed candles with ``since_ms <= open < until_ms``, paginating ``fetch_ohlcv``.

        Stops at ``until_ms``, at the present, on an empty page or when a page
        makes no progress. Result is sorted and deduplicated.
        """
        tf_ms = self._timeframe_ms(timeframe)
        if page_limit < 1:
            raise ValueError(f"page_limit must be >= 1, got {page_limit}")
        if until_ms <= since_ms:
            return []
        self._require("fetchOHLCV")
        now = self._now_ms()
        collected: dict[int, Candle] = {}
        cursor, pages = since_ms, 0
        while cursor < until_ms and cursor + tf_ms <= now:
            rows = self._call("fetch_ohlcv", symbol, symbol, timeframe, since=cursor, limit=page_limit)
            page = self._to_candles(rows, symbol)
            pages += 1
            if not page:
                break
            for c in page:
                if since_ms <= c.timestamp < until_ms and c.timestamp + tf_ms <= now:
                    collected[c.timestamp] = c
            newest = max(c.timestamp for c in page)
            if newest < cursor:
                logger.warning("%s %s: page at %d made no progress; stopping", self.exchange_id, symbol, cursor)
                break
            cursor = newest + tf_ms
        logger.info("%s %s %s: downloaded %d candles in %d page(s)",
                    self.exchange_id, symbol, timeframe, len(collected), pages)
        return [collected[ts] for ts in sorted(collected)]

    # ------------------------------------------------------------------ internals

    def _create_exchange(self, api_key: str | None, api_secret: str | None) -> Any:
        if self.exchange_id not in getattr(ccxt, "exchanges", ()):
            raise MarketDataError(f"unknown ccxt exchange {self.exchange_id!r}")
        config: dict[str, Any] = {"enableRateLimit": True}
        if api_key:
            config["apiKey"] = api_key
        if api_secret:
            config["secret"] = api_secret
        try:
            exchange = getattr(ccxt, self.exchange_id)(config)
        except Exception as exc:
            raise MarketDataError(self._redact(
                f"cannot create ccxt exchange {self.exchange_id!r}: {type(exc).__name__}: {exc}"
            )) from exc
        if self.use_testnet:
            try:
                exchange.set_sandbox_mode(True)
            except Exception as exc:
                raise MarketDataError(
                    f"exchange {self.exchange_id!r} has no testnet/sandbox in ccxt; pick an exchange "
                    "with a testnet or disable the testnet (JEV_USE_TESTNET=false) knowingly"
                ) from exc
            logger.info("ccxt %s: sandbox/testnet mode enabled", self.exchange_id)
        return exchange

    def _call(self, method: str, symbol: str, *args: Any, **kwargs: Any) -> Any:
        try:
            return getattr(self.exchange, method)(*args, **kwargs)
        except ccxt.BaseError as exc:
            kind = "network error" if isinstance(exc, ccxt.NetworkError) else "exchange error"
            raise self._wrap(method, symbol, kind, exc) from exc
        except Exception as exc:  # malformed response or unexpected client failure
            raise self._wrap(method, symbol, "unexpected error", exc) from exc

    def _wrap(self, method: str, symbol: str, kind: str, exc: Exception) -> MarketDataError:
        return MarketDataError(self._redact(
            f"{self.exchange_id} {method}({symbol}) failed, {kind}: {type(exc).__name__}: {exc}"
        ))

    def _require(self, capability: str) -> None:
        has = getattr(self.exchange, "has", None)
        if isinstance(has, dict) and has.get(capability) is False:
            raise MarketDataError(f"exchange {self.exchange_id!r} does not support {capability}")

    def _timeframe_ms(self, timeframe: str) -> int:
        try:
            return timeframe_to_ms(timeframe)
        except ValueError as exc:
            raise MarketDataError(str(exc)) from exc

    def _to_candles(self, rows: Any, symbol: str) -> list[Candle]:
        if not isinstance(rows, (list, tuple)):
            raise MarketDataError(f"{self.exchange_id}: malformed OHLCV response for {symbol}: {rows!r}")
        candles = []
        for row in rows:
            try:
                candles.append(_row_to_candle(row))
            except (TypeError, ValueError) as exc:
                raise MarketDataError(
                    f"{self.exchange_id}: malformed OHLCV row for {symbol}: {row!r} ({exc})"
                ) from exc
        return candles

    def _redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text


def _row_to_candle(row: Any) -> Candle:
    """Convert a ccxt OHLCV row [ts, open, high, low, close, volume]; missing volume -> 0."""
    if not isinstance(row, (list, tuple)) or len(row) < 5:
        raise ValueError("expected [timestamp, open, high, low, close, volume]")
    volume = row[5] if len(row) > 5 and row[5] is not None else 0.0
    return make_candle(row[0], row[1], row[2], row[3], row[4], volume)


def _positive(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None
