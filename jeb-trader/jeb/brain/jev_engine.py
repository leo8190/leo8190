"""Jev decision engine: TypeSafe AI's System One model as the fast trading brain.

Jev does not generate text. It answers typed questions (choice, score, noul) about a
JSON ``state`` with calibrated probabilities in a single non-autoregressive pass
(~70-500 ms). A whole trading decision is therefore ONE ``system_one`` call: every
question about the same state is asked together, which costs about the same as one.

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

STOP_ATR_MULTIPLIER = {"tight": 1.0, "normal": 2.0, "wide": 3.0}
FALLBACK_STOP_PCT = {"tight": 1.0, "normal": 2.0, "wide": 3.0}  # when ATR is unknown
SIZE_RUBRIC = [
    "Weak setup: allocate 25% of the allowed position size.",
    "Moderate setup: allocate 50% of the allowed position size.",
    "Strong setup: allocate 75% of the allowed position size.",
    "Exceptional setup: allocate 100% of the allowed position size.",
]
REWARD_RISK = 2.0
MIN_STOP_PCT, MAX_STOP_PCT = 0.3, 10.0


def _flat_questions(horizon: str) -> dict[str, Any]:
    return {
        "action": Choice(
            instructions=(
                "The account trades spot crypto, long only, no leverage, and is currently FLAT. "
                f"Decide what to do now for the next {horizon}, weighing trend, momentum, "
                "volatility and trading costs."
            ),
            criteria={
                "BUY": "Open a long now: trend and momentum point up and the expected move "
                "clearly exceeds the round-trip trading costs.",
                "HOLD": "Stay flat: signals conflict, the trend is down or sideways, or the "
                "expected move does not beat trading costs.",
            },
        ),
        "edge": Noul(
            instructions=(
                "A long opened at the current price, with a take-profit twice as far as its "
                f"stop-loss, hits the take-profit first within the next {horizon}."
            ),
            criteria={
                "true": "The take-profit is reached before the stop-loss.",
                "false": "The stop-loss is reached first, or neither is reached.",
            },
        ),
        "stop_width": Choice(
            instructions="How much room should the protective stop-loss give this trade?",
            criteria={
                "tight": "About 1x ATR: calm market with a clear invalidation level.",
                "normal": "About 2x ATR: typical conditions.",
                "wide": "About 3x ATR: volatile, noisy price action.",
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


def build_state(snapshot: MarketSnapshot, portfolio: PortfolioView, cost_pct: float) -> dict[str, Any]:
    """Deterministic JSON state for Jev: numbers only, no free text from outside."""
    indicators = {
        key: _round(value)
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
    ) -> None:
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

    # -- plumbing -------------------------------------------------------------

    def _get_client(self) -> Any:
        # Created lazily so the engine can be built (and tested) without TYPESAFE_API_KEY.
        if self._client is None:
            self._client = ts.TypeSafeClient(
                model=self.model,
                timeout=self.timeout_s,
                retry=ts.RetryPolicy(max_retries=self.max_retries, backoff_max=1.0),
            )
        return self._client

    def _roll_budget(self) -> None:
        day = _utc_day(self._clock_ms())
        if day != self._budget_day:
            self._budget_day = day
            self.spent_today_usd = 0.0

    def estimate_cost(self, usage: Any) -> float:
        input_tokens = getattr(usage, "input_tokens", None) or 0
        return input_tokens * self.price_per_mtok_input / 1_000_000

    def _fallback(self, reason: str, **extra: Any) -> Decision:
        decision = Decision.hold(f"jev fallback: {reason}", source="fallback")
        return decision.model_copy(update={"model": self.model, **extra})

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
            return self._fallback("daily Jev budget exhausted")

        horizon = f"{self.horizon_candles} candles of {snapshot.timeframe}"
        questions = _holding_questions(horizon) if portfolio.in_position else _flat_questions(horizon)
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
            logger.error("Jev client error: %s", exc)
            return self._fallback("client not configured")
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
            decision = self._interpret(response, snapshot, portfolio)
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("Jev answers incomplete: %s", exc)
            return self._fallback("incomplete answers", **{k: v for k, v in meta.items() if k != "model"})
        return decision.model_copy(update=meta)

    def _interpret(self, response: Any, snapshot: MarketSnapshot, portfolio: PortfolioView) -> Decision:
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
        edge = float(response.nouls["edge"].noul)
        if not math.isfinite(edge):
            raise ValueError("non-finite edge")
        if choice != "BUY":
            return Decision(
                action=Action.HOLD,
                confidence=_clamp(probabilities.get("HOLD", 0.0), 0.0, 1.0),
                reasoning=f"jev: stay flat (p_buy={p_buy:.2f}, edge={edge:.2f})",
                source="jev",
            )

        width = response.choices["stop_width"].choice
        multiplier = STOP_ATR_MULTIPLIER.get(width, STOP_ATR_MULTIPLIER["normal"])
        atr_pct = snapshot.indicators.atr_pct
        raw_stop = multiplier * atr_pct if atr_pct else FALLBACK_STOP_PCT.get(width, 2.0)
        stop_pct = _clamp(raw_stop, MIN_STOP_PCT, MAX_STOP_PCT)
        size_score = float(response.scores["size"].score)  # expected level in [0, 3]
        if not math.isfinite(size_score):
            raise ValueError("non-finite size score")
        size_pct = _clamp((size_score + 1) / len(SIZE_RUBRIC), 0.25, 1.0)
        # Both the choice and the independent edge estimate must agree for a strong entry.
        confidence = _clamp(min(p_buy, edge), 0.0, 1.0)
        return Decision(
            action=Action.BUY,
            confidence=confidence,
            size_pct=size_pct,
            stop_loss_pct=stop_pct,
            take_profit_pct=stop_pct * REWARD_RISK,
            reasoning=(
                f"jev: enter long (p_buy={p_buy:.2f}, edge={edge:.2f}, stop={width}, "
                f"size_score={size_score:.2f}/3)"
            ),
            source="jev",
        )
