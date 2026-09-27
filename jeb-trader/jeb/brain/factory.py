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


def build_engine(settings: Settings, client: Any = None) -> DecisionEngine:
    """"rules" | "claude" | "hybrid". ``client`` is an optional injected Anthropic client."""
    if settings.engine == "rules":
        return RulesDecisionEngine()
    if settings.engine == "claude":
        return _claude(settings, client)
    if settings.engine == "hybrid":
        return HybridDecisionEngine(RulesDecisionEngine(), _claude(settings, client), settings.hybrid_heartbeat_candles)
    raise ConfigError(f"Unknown engine {settings.engine!r}")
