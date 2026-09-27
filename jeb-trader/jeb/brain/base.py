"""Decision engine contract."""

from __future__ import annotations

from typing import Protocol

from ..models import Decision, MarketSnapshot, PortfolioView


class DecisionEngine(Protocol):
    name: str

    def decide(self, snapshot: MarketSnapshot, portfolio: PortfolioView) -> Decision:
        """Propose an action. Must never raise: on any failure return ``Decision.hold``."""
        ...
