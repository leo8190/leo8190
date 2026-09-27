"""Deterministic trend-following baseline (also the hybrid engine's first stage).

Entry (flat), all required:
    ema_fast > ema_slow, price > ema_trend, macd_hist > 0, rsi_buy_min <= rsi <= rsi_buy_max
Exit (holding), any of:
    ema_fast < ema_slow, rsi > rsi_exit, (macd_hist < 0 and price < ema_slow)

Entry confidence (0.6..0.9), volatility-normalised trend strength:
    scale        = atr_pct if available else 1.0          (percent of price)
    spread_score = clamp((ema_fast - ema_slow) / price * 100 / scale, 0, 1)
                   -> an EMA spread of one ATR scores 1
    macd_score   = clamp(macd_hist / price * 100 / (0.5 * scale), 0, 1)
                   -> a MACD histogram of half an ATR scores 1
    confidence   = 0.6 + 0.3 * (0.6 * spread_score + 0.4 * macd_score)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..models import Action, Decision, IndicatorSet, MarketSnapshot, PortfolioView

logger = logging.getLogger(__name__)

MIN_STOP_PCT = 0.5
MAX_STOP_PCT = 8.0
DEFAULT_STOP_PCT = 2.0  # used when atr_pct is unavailable
DEFAULT_SCALE_PCT = 1.0  # strength normaliser when atr_pct is unavailable
MIN_BUY_CONFIDENCE = 0.6
MAX_BUY_CONFIDENCE = 0.9
SELL_CONFIDENCE = 0.8

_ENTRY_FIELDS = ("ema_fast", "ema_slow", "ema_trend", "macd_hist", "rsi")


@dataclass(frozen=True)
class RulesParams:
    rsi_buy_min: float = 45.0
    rsi_buy_max: float = 70.0
    rsi_exit: float = 78.0
    atr_stop_mult: float = 2.0
    reward_risk: float = 2.0


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def _missing(ind: IndicatorSet, names: tuple[str, ...]) -> list[str]:
    return [name for name in names if getattr(ind, name) is None]


def entry_confidence(ind: IndicatorSet, price: float) -> float:
    """Trend-strength confidence in [0.6, 0.9] (formula in the module docstring)."""
    scale = ind.atr_pct if ind.atr_pct and ind.atr_pct > 0 else DEFAULT_SCALE_PCT
    spread_pct = (ind.ema_fast - ind.ema_slow) / price * 100.0
    macd_pct = ind.macd_hist / price * 100.0
    spread_score = _clamp(spread_pct / scale, 0.0, 1.0)
    macd_score = _clamp(macd_pct / (0.5 * scale), 0.0, 1.0)
    strength = 0.6 * spread_score + 0.4 * macd_score
    return MIN_BUY_CONFIDENCE + (MAX_BUY_CONFIDENCE - MIN_BUY_CONFIDENCE) * strength


class RulesDecisionEngine:
    """EMA/MACD/RSI trend follower. Sizes at 1.0 and lets the risk manager scale down."""

    name = "rules"

    def __init__(self, params: RulesParams | None = None) -> None:
        self.params = params or RulesParams()

    def decide(self, snapshot: MarketSnapshot, portfolio: PortfolioView) -> Decision:
        """Never raises: any unexpected error returns HOLD (source="fallback")."""
        try:
            if portfolio.in_position:
                return self._decide_holding(snapshot)
            return self._decide_flat(snapshot)
        except Exception as exc:  # contract: decide() must never raise
            logger.warning("rules engine error: %s: %s", type(exc).__name__, exc)
            return Decision.hold(f"rules engine error ({type(exc).__name__})", source="fallback")

    def stop_loss_pct(self, ind: IndicatorSet) -> float:
        if ind.atr_pct is None or ind.atr_pct <= 0:
            return DEFAULT_STOP_PCT
        return _clamp(self.params.atr_stop_mult * ind.atr_pct, MIN_STOP_PCT, MAX_STOP_PCT)

    # -- flat -----------------------------------------------------------------------------

    def _decide_flat(self, snapshot: MarketSnapshot) -> Decision:
        ind, price, p = snapshot.indicators, snapshot.price, self.params
        missing = _missing(ind, _ENTRY_FIELDS)
        if missing:
            return Decision.hold(f"missing indicators: {', '.join(missing)}", source="rules")

        checks = [
            (ind.ema_fast > ind.ema_slow, "ema_fast>ema_slow", "ema_fast<=ema_slow"),
            (price > ind.ema_trend, "price>ema_trend", "price<=ema_trend"),
            (ind.macd_hist > 0, "macd_hist>0", "macd_hist<=0"),
            (
                p.rsi_buy_min <= ind.rsi <= p.rsi_buy_max,
                f"rsi={ind.rsi:.1f} in [{p.rsi_buy_min:g},{p.rsi_buy_max:g}]",
                f"rsi={ind.rsi:.1f} outside [{p.rsi_buy_min:g},{p.rsi_buy_max:g}]",
            ),
        ]
        failed = [bad for ok, _, bad in checks if not ok]
        if failed:
            return Decision.hold(f"no entry: {', '.join(failed)}", source="rules")

        stop = self.stop_loss_pct(ind)
        confidence = entry_confidence(ind, price)
        passed = ", ".join(good for _, good, _ in checks)
        return Decision(
            action=Action.BUY,
            confidence=confidence,
            size_pct=1.0,
            stop_loss_pct=stop,
            take_profit_pct=p.reward_risk * stop,
            reasoning=f"trend entry: {passed}",
            source="rules",
        )

    # -- holding --------------------------------------------------------------------------

    def _decide_holding(self, snapshot: MarketSnapshot) -> Decision:
        """Each exit rule is evaluated only when its indicators exist (exits never wait
        on an unrelated missing indicator); HOLD if nothing could be evaluated."""
        ind, price, p = snapshot.indicators, snapshot.price, self.params
        triggers: list[str] = []
        evaluated = 0
        if ind.ema_fast is not None and ind.ema_slow is not None:
            evaluated += 1
            if ind.ema_fast < ind.ema_slow:
                triggers.append("ema_fast<ema_slow")
        if ind.rsi is not None:
            evaluated += 1
            if ind.rsi > p.rsi_exit:
                triggers.append(f"rsi={ind.rsi:.1f}>{p.rsi_exit:g}")
        if ind.macd_hist is not None and ind.ema_slow is not None:
            evaluated += 1
            if ind.macd_hist < 0 and price < ind.ema_slow:
                triggers.append("macd_hist<0 and price<ema_slow")

        if triggers:
            return Decision(
                action=Action.SELL,
                confidence=SELL_CONFIDENCE,
                size_pct=1.0,
                reasoning=f"exit: {', '.join(triggers)}",
                source="rules",
            )
        if evaluated == 0:
            return Decision.hold("missing indicators: cannot evaluate exits", source="rules")
        missing = _missing(ind, ("ema_fast", "ema_slow", "rsi", "macd_hist"))
        note = f" (missing: {', '.join(missing)})" if missing else ""
        return Decision.hold(f"hold position: no exit signal{note}", source="rules")
