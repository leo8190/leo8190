"""Hybrid engine: deterministic rules first, a model (Jev by default, or Claude) confirms.

- Rules SELL while holding  -> exit immediately (exits never wait for the model).
- Rules BUY while flat      -> the model must confirm; no confirmation, no entry.
- Rules HOLD                -> the model is consulted only on a heartbeat (every N decisions)
                               for an early exit, or an entry the trend filter allows.
"""

from __future__ import annotations

import logging

from ..models import Action, Decision, MarketSnapshot, PortfolioView
from .base import DecisionEngine

logger = logging.getLogger(__name__)


def _describe_llm(llm: Decision) -> str:
    if llm.source == "fallback":
        return f"unavailable ({llm.reasoning})"
    return f"{llm.action.value} conf={llm.confidence:.2f} - {llm.reasoning}"


def _tighter_stop(rules: Decision, llm: Decision) -> tuple[float | None, float | None]:
    """(stop_loss_pct, take_profit_pct): the smaller stop, with the take-profit from the
    same source (keeps that source's reward/risk coherent), falling back to the other."""
    candidates = [d for d in (llm, rules) if d.stop_loss_pct is not None]
    if not candidates:
        return None, llm.take_profit_pct or rules.take_profit_pct
    chosen = min(candidates, key=lambda d: d.stop_loss_pct)
    other = rules if chosen is llm else llm
    return chosen.stop_loss_pct, chosen.take_profit_pct or other.take_profit_pct


def trend_is_bearish(snapshot: MarketSnapshot) -> bool:
    """True when an available trend check fails: ema_fast < ema_slow or price < ema_trend."""
    ind = snapshot.indicators
    if ind.ema_fast is not None and ind.ema_slow is not None and ind.ema_fast < ind.ema_slow:
        return True
    return ind.ema_trend is not None and snapshot.price < ind.ema_trend


class HybridDecisionEngine:
    """Combines a rules engine and an LLM engine; only pays for the LLM when useful."""

    name = "hybrid"

    def __init__(self, rules: DecisionEngine, llm: DecisionEngine, heartbeat_candles: int = 12) -> None:
        self.rules = rules
        self.llm = llm
        self.heartbeat_candles = max(1, int(heartbeat_candles))
        self._since_llm: int | None = None  # decisions since the last LLM call; None = never
        self.llm_calls = 0

    @property
    def heartbeat_due(self) -> bool:
        return self._since_llm is None or self._since_llm >= self.heartbeat_candles

    def decide(self, snapshot: MarketSnapshot, portfolio: PortfolioView) -> Decision:
        """Never raises: unexpected errors return HOLD (source="fallback")."""
        try:
            if self._since_llm is not None:
                self._since_llm += 1
            return self._decide(snapshot, portfolio)
        except Exception as exc:  # contract: decide() must never raise
            logger.warning("hybrid engine error: %s: %s", type(exc).__name__, exc)
            return Decision.hold(f"hybrid engine error ({type(exc).__name__})", source="fallback")

    # -- routing --------------------------------------------------------------------------

    def _decide(self, snapshot: MarketSnapshot, portfolio: PortfolioView) -> Decision:
        rules = self.rules.decide(snapshot, portfolio)
        holding = portfolio.in_position
        if rules.action is Action.SELL and holding:
            return self._result(rules, rules, None, "exit on rules; llm not consulted")
        if rules.action is Action.BUY and not holding:
            return self._confirm_entry(snapshot, portfolio, rules)
        if rules.action is not Action.HOLD:  # BUY while holding / SELL while flat: invalid
            rules = Decision.hold(f"invalid rules {rules.action.value} ignored: {rules.reasoning}", "rules")
        if not self.heartbeat_due:
            remaining = self.heartbeat_candles - (self._since_llm or 0)
            return self._hold(rules, None, f"llm not called (heartbeat in {remaining})")
        if holding:
            return self._heartbeat_holding(snapshot, portfolio, rules)
        return self._heartbeat_flat(snapshot, portfolio, rules)

    def _confirm_entry(self, snapshot: MarketSnapshot, portfolio: PortfolioView, rules: Decision) -> Decision:
        llm = self._ask_llm(snapshot, portfolio)
        if llm.source == "fallback":
            return self._hold(rules, llm, "no entry without llm confirmation")
        if llm.action is not Action.BUY:
            return self._hold(rules, llm, "llm veto")
        stop, take_profit = _tighter_stop(rules, llm)
        buy = Decision(
            action=Action.BUY,
            confidence=min(rules.confidence, llm.confidence),
            size_pct=llm.size_pct if llm.size_pct > 0 else rules.size_pct,
            stop_loss_pct=stop,
            take_profit_pct=take_profit,
        )
        return self._result(buy, rules, llm, "entry confirmed")

    def _heartbeat_holding(self, snapshot: MarketSnapshot, portfolio: PortfolioView, rules: Decision) -> Decision:
        llm = self._ask_llm(snapshot, portfolio)
        if llm.source != "fallback" and llm.action is Action.SELL:
            sell = Decision(action=Action.SELL, confidence=llm.confidence, size_pct=llm.size_pct or 1.0)
            return self._result(sell, rules, llm, "early exit on llm heartbeat")
        return self._hold(rules, llm, "keep position")

    def _heartbeat_flat(self, snapshot: MarketSnapshot, portfolio: PortfolioView, rules: Decision) -> Decision:
        llm = self._ask_llm(snapshot, portfolio)
        if llm.source == "fallback" or llm.action is not Action.BUY:
            return self._hold(rules, llm, "no entry")
        if trend_is_bearish(snapshot):
            return self._hold(rules, llm, "llm BUY rejected by bearish trend filter")
        buy = llm.model_copy(update={"reasoning": ""})
        return self._result(buy, rules, llm, "llm entry on heartbeat (trend filter ok)")

    # -- helpers --------------------------------------------------------------------------

    def _ask_llm(self, snapshot: MarketSnapshot, portfolio: PortfolioView) -> Decision:
        self._since_llm = 0
        self.llm_calls += 1
        try:
            return self.llm.decide(snapshot, portfolio)
        except Exception as exc:  # LLM engines must not raise, but never trust that blindly
            logger.warning("hybrid: llm engine raised %s", type(exc).__name__)
            return Decision.hold(f"llm engine raised {type(exc).__name__}", source="fallback")

    def _hold(self, rules: Decision, llm: Decision | None, note: str) -> Decision:
        return self._result(Decision(action=Action.HOLD), rules, llm, note)

    @staticmethod
    def _result(base: Decision, rules: Decision, llm: Decision | None, note: str) -> Decision:
        """Final decision: source="hybrid", combined reasoning and the LLM call's cost fields."""
        llm_text = _describe_llm(llm) if llm is not None else "not called"
        update = {
            "source": "hybrid",
            "reasoning": f"rules: {rules.reasoning} | model: {llm_text} => {note}",
            "model": llm.model if llm is not None else None,
            "latency_ms": llm.latency_ms if llm is not None else None,
            "input_tokens": llm.input_tokens if llm is not None else 0,
            "output_tokens": llm.output_tokens if llm is not None else 0,
            "cost_usd": llm.cost_usd if llm is not None else 0.0,
        }
        if base.action is Action.HOLD:
            update.update({"confidence": 0.0, "size_pct": 0.0, "stop_loss_pct": None, "take_profit_pct": None})
        return base.model_copy(update=update)
