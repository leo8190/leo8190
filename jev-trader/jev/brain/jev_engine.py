"""Jev decision engine: TypeSafe AI's System One model as the fast trading brain.

Jev does not generate text. It answers typed questions (choice, score, noul) about a
JSON ``state`` with calibrated probabilities in a single non-autoregressive pass
(~70-500 ms). A whole trading decision is therefore ONE ``system_one`` call: every
question about the same state is asked together, so the state is sent once and there is a
single round trip (each question still adds billed input tokens).

Entries are cost-aware: the stop comes from the ATR (2x, clamped) and the take-profit is
twice the stop, and Jev is asked about those concrete price levels (vague questions such as
"does the take-profit hit first?" without the levels get ~0.4 back whatever the market does).
A BUY needs Jev to choose BUY *and* a positive expected value after round-trip costs:
``p(tp first) * tp - p(stop first) * stop - costs > 0``.

The engine never raises: any failure (no key, timeout, rate limit, bad response,
exhausted budget) becomes a HOLD with ``source="fallback"``. Protective exits and
risk limits live elsewhere and never depend on Jev being reachable.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any, Callable

import typesafe_sdk as ts
from typesafe_sdk import Choice, Noul, Score

from ..models import Action, Decision, MarketSnapshot, PortfolioView

logger = logging.getLogger(__name__)

DEFAULT_JEV_MODEL = "jev-latest"
# USD per 1M input tokens; Jev output tokens are free (TypeSafe pricing, Sep 2026).
DEFAULT_PRICE_PER_MTOK_INPUT = 0.042

STOP_ATR_MULTIPLIER = 2.0
FALLBACK_STOP_PCT = 2.0  # when ATR is unknown
SIZE_RUBRIC = [
    "Weak setup: allocate 25% of the allowed position size.",
    "Moderate setup: allocate 50% of the allowed position size.",
    "Strong setup: allocate 75% of the allowed position size.",
    "Exceptional setup: allocate 100% of the allowed position size.",
]
REWARD_RISK = 2.0
MIN_STOP_PCT, MAX_STOP_PCT = 0.3, 10.0
RETRY_BACKOFF_MAX_S = 1.0


def _level(value: float) -> str:
    return f"{value:.6g}"


def _flat_questions(horizon: str, price: float, stop_pct: float, take_profit_pct: float,
                    cost_pct: float) -> dict[str, Any]:
    """Questions for an entry with concrete levels: Jev cannot judge "does the take-profit hit
    first?" without knowing where the stop and the take-profit are."""
    stop, target = _level(price * (1 - stop_pct / 100)), _level(price * (1 + take_profit_pct / 100))
    entry = _level(price)
    levels = f"take-profit {target} (+{take_profit_pct:.2f}%)", f"stop-loss {stop} (-{stop_pct:.2f}%)"
    return {
        "action": Choice(
            instructions=(
                "The account trades spot crypto, long only, no leverage, and is currently FLAT. "
                f"A long opened now at {entry} would get a {levels[1]} and a {levels[0]}. "
                f"Round-trip trading costs are {cost_pct:.2f}% of the position. "
                f"Decide what to do now for the next {horizon}."
            ),
            criteria={
                "BUY": "Open the long: trend and momentum point up and the take-profit is more likely "
                "than usual to be reached before the stop-loss, enough to pay the trading costs.",
                "HOLD": "Stay flat: signals conflict, the trend is down or sideways, or the trade has "
                "no clear edge after trading costs.",
            },
        ),
        "tp_first": Noul(
            instructions=(
                f"A long is opened now at {entry}. Within the next {horizon}, price touches the "
                f"{levels[0]} before it touches the {levels[1]}."
            ),
            criteria={
                "true": "The take-profit is touched first.",
                "false": "The stop-loss is touched first, or neither level is touched in the window.",
            },
        ),
        "stop_first": Noul(
            instructions=(
                f"A long is opened now at {entry}. Within the next {horizon}, price touches the "
                f"{levels[1]} before it touches the {levels[0]}."
            ),
            criteria={
                "true": "The stop-loss is touched first.",
                "false": "The take-profit is touched first, or neither level is touched in the window.",
            },
        ),
        "size": Score(instructions="How strong is this long setup?", criteria=SIZE_RUBRIC),
    }


def _holding_questions(horizon: str) -> dict[str, Any]:
    return {
        "action": Choice(
            instructions=(
                "The account HOLDS a spot long position (no leverage). Decide whether to keep "
                f"it or exit now, looking at the next {horizon}."
            ),
            criteria={
                "SELL": "Exit now: the uptrend is breaking, momentum turned down, or the "
                "remaining upside no longer justifies the risk.",
                "HOLD": "Keep the position: the trend is intact and the active stop-loss and "
                "take-profit are still appropriate.",
            },
        ),
    }


def _round(value: float | None, digits: int = 4) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)


def _sig(value: float | None, figures: int = 6) -> float | None:
    """Round to significant figures so BTC and small-cap prices stay compact."""
    if value is None or not math.isfinite(value) or value == 0:
        return value
    return float(f"{value:.{figures}g}")


_FIXED_DECIMAL_INDICATORS = frozenset({"rsi", "bb_pct_b", "volume_ratio"})


def _indicator_value(name: str, value: float) -> float | None:
    """Ratios and percents keep 4 decimals; price-unit indicators (EMAs, MACD, ATR,
    Bollinger bands) keep 6 significant figures so low-priced coins do not round to 0."""
    if name.endswith("_pct") or name in _FIXED_DECIMAL_INDICATORS:
        return _round(value)
    return _sig(value) if value is not None and math.isfinite(value) else None


def build_state(snapshot: MarketSnapshot, portfolio: PortfolioView, cost_pct: float) -> dict[str, Any]:
    """Deterministic JSON state for Jev: numbers only, no free text from outside."""
    indicators = {
        key: _indicator_value(key, value)
        for key, value in sorted(snapshot.indicators.model_dump().items())
        if value is not None
    }
    position: dict[str, Any] = {"status": "holding" if portfolio.in_position else "flat"}
    if portfolio.in_position:
        position.update(
            entry_price=_sig(portfolio.avg_entry_price),
            unrealized_pnl_pct=_round(portfolio.unrealized_pnl_pct, 2),
            stop_loss=_sig(portfolio.stop_loss),
            take_profit=_sig(portfolio.take_profit),
            position_pct_of_equity=_round(
                100 * portfolio.position_value / portfolio.equity if portfolio.equity > 0 else None, 2
            ),
        )
    return {
        "market": {
            "symbol": snapshot.symbol,
            "timeframe": snapshot.timeframe,
            "price": _sig(snapshot.price),
            "indicators": indicators,
            "recent_closes_oldest_first": [_sig(c) for c in snapshot.recent_closes],
        },
        "position": position,
        "round_trip_cost_pct": _round(cost_pct, 3),
    }


def _utc_day(ms: int) -> int:
    return ms // 86_400_000


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def reference_stop_pct(atr_pct: float | None, low: float = MIN_STOP_PCT, high: float = MAX_STOP_PCT,
                       fallback: float = FALLBACK_STOP_PCT) -> float:
    """Entry stop distance in %: 2x ATR % (``fallback`` without ATR), clamped to [low, high].
    The factory passes the risk manager's bounds, so it never re-clamps a stop Jev evaluated."""
    if atr_pct is None or not math.isfinite(atr_pct) or atr_pct <= 0:
        return _clamp(fallback, low, high)
    return _clamp(STOP_ATR_MULTIPLIER * atr_pct, low, high)


def _probability(value: Any, name: str) -> float:
    p = float(value)
    if not math.isfinite(p):
        raise ValueError(f"non-finite {name}")
    return _clamp(p, 0.0, 1.0)


class JevDecisionEngine:
    """Asks Jev one batch of typed questions per decision."""

    name = "jev"

    def __init__(
        self,
        model: str = DEFAULT_JEV_MODEL,
        timeout_s: float = 3.0,
        max_retries: int = 1,
        daily_budget_usd: float = 1.0,
        price_per_mtok_input: float = DEFAULT_PRICE_PER_MTOK_INPUT,
        round_trip_cost_pct: float = 0.3,
        horizon_candles: int = 12,
        client: Any | None = None,
        clock_ms: Callable[[], int] | None = None,
        max_position_pct: float = 25.0,
        min_stop_pct: float = MIN_STOP_PCT,
        max_stop_pct: float = MAX_STOP_PCT,
        fallback_stop_pct: float = FALLBACK_STOP_PCT,
    ) -> None:
        """``max_position_pct``: the risk manager's max position (% of equity). Jev scores the
        setup as a fraction of the allowed position; ``Decision.size_pct`` is a fraction of
        equity, so the fraction is scaled by it. ``min_stop_pct``/``max_stop_pct``/
        ``fallback_stop_pct``: the risk manager's stop bounds and default stop, so the bracket
        Jev is asked about is the one that gets traded."""
        if not math.isfinite(max_position_pct) or not 0 < max_position_pct <= 100:
            raise ValueError(f"max_position_pct must be in (0, 100], got {max_position_pct!r}")
        stops = (min_stop_pct, max_stop_pct, fallback_stop_pct)
        if not all(math.isfinite(v) for v in stops) or not 0 < min_stop_pct <= max_stop_pct or fallback_stop_pct <= 0:
            raise ValueError(f"invalid stop bounds min={min_stop_pct!r} max={max_stop_pct!r} "
                             f"fallback={fallback_stop_pct!r}")
        self.max_position_pct = float(max_position_pct)
        self.min_stop_pct, self.max_stop_pct = float(min_stop_pct), float(max_stop_pct)
        self.fallback_stop_pct = float(fallback_stop_pct)
        self.model = model
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.daily_budget_usd = daily_budget_usd
        self.price_per_mtok_input = price_per_mtok_input
        self.round_trip_cost_pct = round_trip_cost_pct
        self.horizon_candles = horizon_candles
        self._client = client
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._budget_day: int | None = None
        self.spent_today_usd = 0.0
        self.calls = 0
        self._warned_unconfigured = False

    # -- plumbing -------------------------------------------------------------

    def _get_client(self) -> Any:
        # Created lazily so the engine can be built (and tested) without TYPESAFE_API_KEY.
        if self._client is None:
            self._client = ts.TypeSafeClient(model=self.model, timeout=self.timeout_s, retry=self.retry_policy())
        return self._client

    def retry_policy(self) -> ts.RetryPolicy:
        """Never sleep for a server Retry-After (the SDK would wait up to its 30 s budget):
        a short capped backoff, and the whole call (attempts + waits) stays bounded so the
        trading loop and its stop checks are never blocked for long."""
        return ts.RetryPolicy(
            max_retries=self.max_retries,
            backoff_max=RETRY_BACKOFF_MAX_S,
            respect_retry_after=False,
            timeout=self.timeout_s + RETRY_BACKOFF_MAX_S,
        )

    def _roll_budget(self) -> None:
        day = _utc_day(self._clock_ms())
        if day != self._budget_day:
            self._budget_day = day
            self.spent_today_usd = 0.0

    def estimate_cost(self, usage: Any) -> float:
        input_tokens = getattr(usage, "input_tokens", None) or 0
        return input_tokens * self.price_per_mtok_input / 1_000_000

    def _fallback(self, reason: str, attempted: bool = True, **extra: Any) -> Decision:
        """HOLD fallback. ``model`` is set only when a request was attempted, so callers that
        count LLM calls by ``decision.model`` (backtest, hybrid) do not count skipped ones."""
        decision = Decision.hold(f"jev fallback: {reason}", source="fallback")
        return decision.model_copy(update={"model": self.model if attempted else None, **extra})

    # -- decision -------------------------------------------------------------

    def decide(self, snapshot: MarketSnapshot, portfolio: PortfolioView) -> Decision:
        try:
            return self._decide(snapshot, portfolio)
        except Exception as exc:  # the contract: never raise
            logger.exception("Unexpected Jev engine failure")
            return self._fallback(f"unexpected error ({type(exc).__name__})")

    def _decide(self, snapshot: MarketSnapshot, portfolio: PortfolioView) -> Decision:
        self._roll_budget()
        if self.spent_today_usd >= self.daily_budget_usd:
            return self._fallback("daily Jev budget exhausted", attempted=False)

        horizon = f"{self.horizon_candles} candles of {snapshot.timeframe}"
        stop_pct = reference_stop_pct(snapshot.indicators.atr_pct, self.min_stop_pct, self.max_stop_pct,
                                      self.fallback_stop_pct)
        if portfolio.in_position:
            questions = _holding_questions(horizon)
        else:
            questions = _flat_questions(horizon, snapshot.price, stop_pct, stop_pct * REWARD_RISK,
                                        self.round_trip_cost_pct)
        state = build_state(snapshot, portfolio, self.round_trip_cost_pct)

        started = time.perf_counter()
        try:
            response = self._get_client().system_one(state=state, questions=questions, model=self.model)
        except (ts.TypeSafeAuthenticationError, ts.TypeSafePermissionDeniedError) as exc:
            logger.error("Jev rejected the credentials (%s); check TYPESAFE_API_KEY", type(exc).__name__)
            return self._fallback("authentication failed")
        except ts.TypeSafeNotFoundError:
            logger.error("Jev model %r not found", self.model)
            return self._fallback("model not found")
        except (ts.TypeSafeBadRequestError, ts.TypeSafeUnprocessableEntityError) as exc:
            logger.error("Jev rejected the request: %s", exc)
            return self._fallback("invalid request")
        except ts.TypeSafeRateLimitError:
            logger.warning("Jev rate limit hit")
            return self._fallback("rate limited")
        except ts.TypeSafeAPITimeoutError:
            logger.warning("Jev timed out after %.1fs", self.timeout_s)
            return self._fallback("timeout")
        except ts.TypeSafeAPIConnectionError:
            logger.warning("Jev unreachable")
            return self._fallback("connection error")
        except ts.TypeSafeAPIResponseValidationError as exc:
            logger.warning("Jev returned malformed data: %s", exc)
            return self._fallback("malformed response")
        except ts.TypeSafeAPIError as exc:
            logger.warning("Jev API error: %s", exc)
            return self._fallback("api error")
        except ts.TypeSafeError as exc:  # e.g. missing API key when the client is created
            if not self._warned_unconfigured:  # once, not on every candle
                logger.error("Jev client error: %s", exc)
                self._warned_unconfigured = True
            return self._fallback("client not configured", attempted=False)
        latency_ms = (time.perf_counter() - started) * 1000

        self.calls += 1
        usage = getattr(response, "usage", None)
        cost = self.estimate_cost(usage)
        self.spent_today_usd += cost
        meta = {
            "model": getattr(response, "model", None) or self.model,
            "latency_ms": latency_ms,
            "input_tokens": getattr(usage, "input_tokens", None) or 0,
            "output_tokens": getattr(usage, "output_tokens", None) or 0,
            "cost_usd": cost,
        }
        try:
            decision = self._interpret(response, portfolio, stop_pct)
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("Jev answers incomplete: %s", exc)
            return self._fallback("incomplete answers", **{k: v for k, v in meta.items() if k != "model"})
        return decision.model_copy(update=meta)

    def _interpret(self, response: Any, portfolio: PortfolioView, stop_pct: float) -> Decision:
        action_answer = response.choices["action"]
        probabilities = {k: float(v) for k, v in action_answer.probabilities.items()}
        if not all(math.isfinite(p) for p in probabilities.values()):
            raise ValueError("non-finite probability")
        choice = action_answer.choice

        if portfolio.in_position:
            p_sell = probabilities.get("SELL", 0.0)
            if choice == "SELL":
                return Decision(
                    action=Action.SELL,
                    confidence=_clamp(p_sell, 0.0, 1.0),
                    size_pct=1.0,
                    reasoning=f"jev: exit (p_sell={p_sell:.2f})",
                    source="jev",
                )
            return Decision(
                action=Action.HOLD,
                confidence=_clamp(probabilities.get("HOLD", 0.0), 0.0, 1.0),
                reasoning=f"jev: keep position (p_sell={p_sell:.2f})",
                source="jev",
            )

        p_buy = probabilities.get("BUY", 0.0)
        p_tp = _probability(response.nouls["tp_first"].noul, "tp_first")
        p_stop = _probability(response.nouls["stop_first"].noul, "stop_first")
        if p_tp + p_stop > 1.0:  # the two outcomes exclude each other
            total = p_tp + p_stop
            p_tp, p_stop = p_tp / total, p_stop / total
        take_profit_pct = stop_pct * REWARD_RISK
        cost = self.round_trip_cost_pct
        # Expected net % of the position over the horizon ("neither level touched" counts as 0).
        expected_pct = p_tp * take_profit_pct - p_stop * stop_pct - cost
        stats = (f"p_buy={p_buy:.2f}, p_tp={p_tp:.2f}, p_stop={p_stop:.2f}, "
                 f"stop={stop_pct:.2f}%, tp={take_profit_pct:.2f}%, ev={expected_pct:+.2f}% after {cost:.2f}% costs")
        hold_confidence = _clamp(probabilities.get("HOLD", 0.0), 0.0, 1.0)
        if choice != "BUY":
            return Decision(action=Action.HOLD, confidence=hold_confidence,
                            reasoning=f"jev: stay flat ({stats})", source="jev")
        if expected_pct <= 0:
            return Decision(action=Action.HOLD, confidence=hold_confidence,
                            reasoning=f"jev: BUY vetoed, no edge after costs ({stats})", source="jev")

        size_score = float(response.scores["size"].score)  # expected level in [0, 3]
        if not math.isfinite(size_score):
            raise ValueError("non-finite size score")
        # Fraction of the allowed position (0.25..1.0), expressed as a fraction of equity.
        fraction = _clamp((size_score + 1) / len(SIZE_RUBRIC), 0.25, 1.0)
        size_pct = fraction * self.max_position_pct / 100.0
        return Decision(
            action=Action.BUY,
            confidence=_clamp(p_buy, 0.0, 1.0),
            size_pct=size_pct,
            stop_loss_pct=stop_pct,
            take_profit_pct=take_profit_pct,
            reasoning=(f"jev: enter long ({stats}, size_score={size_score:.2f}/3 -> "
                       f"{fraction:.0%} of the max position)"),
            source="jev",
        )
