"""Build the decision engine selected by ``Settings.engine``."""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..models import ConfigError
from .base import DecisionEngine
from .claude_engine import ClaudeDecisionEngine
from .hybrid import HybridDecisionEngine
from .rules_engine import RulesDecisionEngine


def _claude(settings: Settings, client: Any) -> ClaudeDecisionEngine:
    return ClaudeDecisionEngine(
        model=settings.claude_model,
        timeout_s=settings.claude_timeout_s,
        max_retries=settings.ai_max_retries,
        max_tokens=settings.claude_max_tokens,
        daily_budget_usd=settings.max_ai_cost_usd_per_day,
        client=client,
    )


def _jev(settings: Settings, client: Any) -> DecisionEngine:
    from .jev_engine import JevDecisionEngine  # imports typesafe_sdk only when selected

    return JevDecisionEngine(
        model=settings.jev_model,
        timeout_s=settings.jev_timeout_s,
        max_retries=settings.ai_max_retries,
        daily_budget_usd=settings.max_ai_cost_usd_per_day,
        price_per_mtok_input=settings.jev_price_per_mtok_input,
        round_trip_cost_pct=2.0 * (settings.fee_pct + settings.slippage_pct),
        max_position_pct=settings.risk.max_position_pct,
        client=client,
    )


def build_engine(settings: Settings, client: Any = None) -> DecisionEngine:
    """"hybrid" | "jev" | "rules" | "claude".

    ``client`` is an optional injected API client for the model the engine uses (TypeSafe
    for jev and hybrid-with-jev, Anthropic for claude and hybrid-with-claude).
    """
    if settings.engine == "rules":
        return RulesDecisionEngine()
    if settings.engine == "jev":
        return _jev(settings, client)
    if settings.engine == "claude":
        return _claude(settings, client)
    if settings.engine == "hybrid":
        confirmer = _jev(settings, client) if settings.hybrid_confirmer == "jev" else _claude(settings, client)
        return HybridDecisionEngine(RulesDecisionEngine(), confirmer, settings.heartbeat_candles)
    raise ConfigError(f"Unknown engine {settings.engine!r}")
