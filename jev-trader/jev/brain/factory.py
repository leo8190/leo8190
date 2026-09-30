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
        model=settings.model,
        timeout_s=settings.llm_timeout_s,
        max_retries=settings.llm_max_retries,
        max_tokens=settings.llm_max_tokens,
        daily_budget_usd=settings.max_llm_cost_usd_per_day,
        client=client,
    )


def _jev(settings: Settings, client: Any) -> DecisionEngine:
    from .jev_engine import JevDecisionEngine  # imports typesafe_sdk only when selected

    return JevDecisionEngine(
        max_retries=settings.llm_max_retries,
        daily_budget_usd=settings.max_llm_cost_usd_per_day,
        round_trip_cost_pct=2.0 * (settings.fee_pct + settings.slippage_pct),
        max_position_pct=settings.risk.max_position_pct,
        client=client,
    )


def build_engine(settings: Settings, client: Any = None) -> DecisionEngine:
    """"rules" | "claude" | "hybrid" | "jev". ``client`` is an optional injected API client
    (Anthropic for claude/hybrid, TypeSafe for jev)."""
    if settings.engine == "rules":
        return RulesDecisionEngine()
    if settings.engine == "claude":
        return _claude(settings, client)
    if settings.engine == "hybrid":
        return HybridDecisionEngine(RulesDecisionEngine(), _claude(settings, client), settings.hybrid_heartbeat_candles)
    if settings.engine == "jev":
        return _jev(settings, client)
    raise ConfigError(f"Unknown engine {settings.engine!r}")
