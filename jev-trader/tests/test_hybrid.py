from __future__ import annotations

from types import SimpleNamespace

import pytest

from jev.brain.claude_engine import ClaudeDecisionEngine, LLMDecision
from jev.brain.factory import build_engine
from jev.brain.hybrid import HybridDecisionEngine
from jev.brain.rules_engine import RulesDecisionEngine
from jev.config import Settings
from jev.models import Action, ConfigError, Decision, IndicatorSet, MarketSnapshot, PortfolioView

BULLISH = dict(ema_fast=101.0, ema_slow=100.0, ema_trend=98.0, macd_hist=0.2, rsi=55.0, atr_pct=0.8)


class ScriptedEngine:
    """Returns the given decisions in order (the last one repeats) and counts calls."""

    def __init__(self, *decisions: Decision, name: str = "scripted") -> None:
        self.name = name
        self.decisions = list(decisions)
        self.calls = 0

    def decide(self, snapshot, portfolio) -> Decision:
        self.calls += 1
        return self.decisions[min(self.calls - 1, len(self.decisions) - 1)]


class RaisingEngine:
    name = "raising"

    def __init__(self) -> None:
        self.calls = 0

    def decide(self, snapshot, portfolio) -> Decision:
        self.calls += 1
        raise RuntimeError("boom")


def snap(price: float = 102.0, **overrides) -> MarketSnapshot:
    return MarketSnapshot(
        symbol="BTC/USDT",
        timeframe="5m",
        timestamp=1_700_000_000_000,
        price=price,
        indicators=IndicatorSet(**{**BULLISH, **overrides}),
    )


FLAT = PortfolioView(quote_currency="USDT", base_currency="BTC", cash=1000.0, base_qty=0.0, equity=1000.0)
HOLDING = PortfolioView(
    quote_currency="USDT", base_currency="BTC", cash=0.0, base_qty=1.0, avg_entry_price=100.0, equity=102.0,
    position_value=102.0,
)

RULES_HOLD = Decision.hold("no entry: rsi outside band", source="rules")
RULES_BUY = Decision(
    action=Action.BUY, confidence=0.85, size_pct=1.0, stop_loss_pct=1.6, take_profit_pct=3.2,
    reasoning="trend entry", source="rules",
)
RULES_SELL = Decision(action=Action.SELL, confidence=0.8, size_pct=1.0, reasoning="exit: ema cross", source="rules")


def claude(action: Action, confidence: float = 0.7, size_pct: float = 0.5, stop=2.5, tp=5.0) -> Decision:
    return Decision(
        action=action,
        confidence=confidence,
        size_pct=size_pct if action is not Action.HOLD else 0.0,
        stop_loss_pct=stop if action is Action.BUY else None,
        take_profit_pct=tp if action is Action.BUY else None,
        reasoning=f"llm says {action.value}",
        source="claude",
        model="claude-haiku-4-5",
        latency_ms=321.0,
        input_tokens=900,
        output_tokens=80,
        cost_usd=0.0013,
    )


LLM_FALLBACK = Decision(
    action=Action.HOLD, reasoning="LLM unavailable: timeout (APITimeoutError)", source="fallback",
    model="claude-haiku-4-5",
)


def assert_llm_meta(d: Decision) -> None:
    assert d.model == "claude-haiku-4-5"
    assert d.latency_ms == 321.0
    assert (d.input_tokens, d.output_tokens) == (900, 80)
    assert d.cost_usd == pytest.approx(0.0013)


# ----------------------------------------------------------------------------- gating


def test_rules_hold_flat_heartbeat_not_due_skips_llm():
    llm = ScriptedEngine(claude(Action.HOLD))
    engine = HybridDecisionEngine(ScriptedEngine(RULES_HOLD), llm, heartbeat_candles=3)
    calls = []
    for _ in range(7):
        d = engine.decide(snap(), FLAT)
        assert d.action is Action.HOLD
        calls.append(llm.calls)
    # first decision is due, then every 3rd decision
    assert calls == [1, 1, 1, 2, 2, 2, 3]


def test_decision_without_llm_call_has_no_cost():
    engine = HybridDecisionEngine(ScriptedEngine(RULES_HOLD), ScriptedEngine(claude(Action.HOLD)), 5)
    engine.decide(snap(), FLAT)
    d = engine.decide(snap(), FLAT)
    assert d.source == "hybrid"
    assert d.model is None and d.cost_usd == 0.0 and d.input_tokens == 0
    assert "llm: not called" in d.reasoning


def test_first_decision_counts_as_heartbeat():
    llm = ScriptedEngine(claude(Action.HOLD))
    engine = HybridDecisionEngine(ScriptedEngine(RULES_HOLD), llm, heartbeat_candles=100)
    assert engine.heartbeat_due
    d = engine.decide(snap(), FLAT)
    assert llm.calls == 1
    assert_llm_meta(d)
    assert not engine.heartbeat_due


def test_rules_sell_bypasses_llm():
    llm = ScriptedEngine(claude(Action.BUY))
    engine = HybridDecisionEngine(ScriptedEngine(RULES_SELL), llm, heartbeat_candles=1)
    d = engine.decide(snap(), HOLDING)
    assert llm.calls == 0
    assert d.action is Action.SELL
    assert d.size_pct == 1.0
    assert d.confidence == pytest.approx(0.8)
    assert d.source == "hybrid"
    assert d.cost_usd == 0.0
    assert d.reasoning.startswith("rules: exit: ema cross | llm: not called")


def test_rules_buy_confirmed_by_llm():
    llm = ScriptedEngine(claude(Action.BUY, confidence=0.7, size_pct=0.4, stop=2.5, tp=5.0))
    engine = HybridDecisionEngine(ScriptedEngine(RULES_BUY), llm, heartbeat_candles=12)
    d = engine.decide(snap(), FLAT)
    assert llm.calls == 1
    assert d.action is Action.BUY
    assert d.source == "hybrid"
    assert d.confidence == pytest.approx(0.7)  # min(0.85, 0.7)
    assert d.size_pct == pytest.approx(0.4)  # LLM size
    assert d.stop_loss_pct == pytest.approx(1.6)  # tighter of 1.6 / 2.5
    assert d.take_profit_pct == pytest.approx(3.2)  # from the same source as the stop
    assert "rules: trend entry | llm: BUY" in d.reasoning
    assert_llm_meta(d)


def test_rules_buy_uses_llm_stop_when_tighter_and_rules_size_when_llm_size_zero():
    llm = ScriptedEngine(claude(Action.BUY, confidence=0.95, size_pct=0.0, stop=1.0, tp=3.0))
    d = HybridDecisionEngine(ScriptedEngine(RULES_BUY), llm).decide(snap(), FLAT)
    assert d.confidence == pytest.approx(0.85)
    assert d.size_pct == 1.0
    assert d.stop_loss_pct == pytest.approx(1.0)
    assert d.take_profit_pct == pytest.approx(3.0)


def test_rules_buy_keeps_rules_stop_when_llm_has_none():
    llm = ScriptedEngine(claude(Action.BUY, stop=None, tp=None))
    d = HybridDecisionEngine(ScriptedEngine(RULES_BUY), llm).decide(snap(), FLAT)
    assert d.stop_loss_pct == pytest.approx(1.6)
    assert d.take_profit_pct == pytest.approx(3.2)


def test_rules_buy_calls_llm_even_when_heartbeat_not_due():
    llm = ScriptedEngine(claude(Action.HOLD))
    engine = HybridDecisionEngine(ScriptedEngine(RULES_HOLD, RULES_BUY), llm, heartbeat_candles=50)
    engine.decide(snap(), FLAT)  # heartbeat call
    engine.decide(snap(), FLAT)  # rules BUY -> confirmation call
    assert llm.calls == 2


@pytest.mark.parametrize("llm_action", [Action.HOLD, Action.SELL])
def test_llm_veto_blocks_buy(llm_action):
    llm = ScriptedEngine(claude(llm_action))
    d = HybridDecisionEngine(ScriptedEngine(RULES_BUY), llm).decide(snap(), FLAT)
    assert d.action is Action.HOLD
    assert d.size_pct == 0.0 and d.stop_loss_pct is None
    assert "veto" in d.reasoning
    assert_llm_meta(d)  # the vetoing call still cost money


def test_llm_failure_blocks_buy():
    d = HybridDecisionEngine(ScriptedEngine(RULES_BUY), ScriptedEngine(LLM_FALLBACK)).decide(snap(), FLAT)
    assert d.action is Action.HOLD
    assert d.source == "hybrid"
    assert "unavailable" in d.reasoning
    assert "no entry without llm confirmation" in d.reasoning


def test_llm_engine_raising_never_propagates():
    llm = RaisingEngine()
    d = HybridDecisionEngine(ScriptedEngine(RULES_BUY), llm).decide(snap(), FLAT)
    assert llm.calls == 1
    assert d.action is Action.HOLD


# ----------------------------------------------------------------------------- heartbeat paths


def test_heartbeat_holding_accepts_llm_sell():
    llm = ScriptedEngine(claude(Action.SELL, confidence=0.75, size_pct=0.5))
    d = HybridDecisionEngine(ScriptedEngine(RULES_HOLD), llm).decide(snap(), HOLDING)
    assert d.action is Action.SELL
    assert d.size_pct == pytest.approx(0.5)
    assert d.confidence == pytest.approx(0.75)
    assert "early exit" in d.reasoning
    assert_llm_meta(d)


def test_heartbeat_holding_llm_hold_keeps_position():
    d = HybridDecisionEngine(ScriptedEngine(RULES_HOLD), ScriptedEngine(claude(Action.HOLD))).decide(snap(), HOLDING)
    assert d.action is Action.HOLD


def test_heartbeat_flat_accepts_llm_buy_when_trend_ok():
    llm = ScriptedEngine(claude(Action.BUY, confidence=0.72, size_pct=0.3, stop=2.0, tp=4.0))
    d = HybridDecisionEngine(ScriptedEngine(RULES_HOLD), llm).decide(snap(), FLAT)
    assert d.action is Action.BUY
    assert d.source == "hybrid"
    assert d.confidence == pytest.approx(0.72)
    assert d.size_pct == pytest.approx(0.3)
    assert (d.stop_loss_pct, d.take_profit_pct) == (2.0, 4.0)
    assert_llm_meta(d)


@pytest.mark.parametrize(
    "price, overrides",
    [(102.0, {"ema_fast": 99.0}), (97.0, {})],  # fast < slow ; price < ema_trend
)
def test_heartbeat_flat_llm_buy_rejected_by_bearish_trend(price, overrides):
    llm = ScriptedEngine(claude(Action.BUY))
    d = HybridDecisionEngine(ScriptedEngine(RULES_HOLD), llm).decide(snap(price, **overrides), FLAT)
    assert llm.calls == 1
    assert d.action is Action.HOLD
    assert "trend filter" in d.reasoning
    assert_llm_meta(d)


def test_heartbeat_flat_llm_buy_allowed_when_trend_unknown():
    llm = ScriptedEngine(claude(Action.BUY))
    s = snap(ema_fast=None, ema_slow=None, ema_trend=None)
    d = HybridDecisionEngine(ScriptedEngine(RULES_HOLD), llm).decide(s, FLAT)
    assert d.action is Action.BUY


def test_invalid_rules_action_treated_as_hold():
    llm = ScriptedEngine(claude(Action.HOLD))
    engine = HybridDecisionEngine(ScriptedEngine(RULES_BUY), llm, heartbeat_candles=10)
    d = engine.decide(snap(), HOLDING)  # rules BUY while holding is invalid
    assert d.action is Action.HOLD
    assert llm.calls == 1  # first decision heartbeat only


# ----------------------------------------------------------------------------- integration with real engines


class FakeClient:
    def __init__(self, parsed: LLMDecision) -> None:
        self.calls: list[dict] = []

        def parse(**kwargs):
            self.calls.append(kwargs)
            usage = SimpleNamespace(
                input_tokens=1000, output_tokens=100, cache_creation_input_tokens=None, cache_read_input_tokens=None
            )
            return SimpleNamespace(parsed_output=parsed, stop_reason="end_turn", usage=usage)

        self.messages = SimpleNamespace(parse=parse)


def test_real_engines_confirmed_entry_propagates_cost():
    parsed = LLMDecision(
        action="BUY", confidence=0.66, size_pct=0.25, stop_loss_pct=1.2, take_profit_pct=2.4, reasoning="Momentum up."
    )
    client = FakeClient(parsed)
    llm = ClaudeDecisionEngine(client=client, clock_ms=lambda: 1_700_000_000_000)
    d = HybridDecisionEngine(RulesDecisionEngine(), llm).decide(snap(), FLAT)
    assert len(client.calls) == 1
    assert d.action is Action.BUY
    assert d.confidence == pytest.approx(0.66)
    assert d.size_pct == pytest.approx(0.25)
    assert d.stop_loss_pct == pytest.approx(1.2)
    assert d.cost_usd == pytest.approx(0.0015)
    assert d.model == "claude-haiku-4-5"
    assert llm.spent_today_usd == pytest.approx(0.0015)


# ----------------------------------------------------------------------------- factory


def test_factory_rules():
    engine = build_engine(Settings(engine="rules"))
    assert isinstance(engine, RulesDecisionEngine)
    assert engine.name == "rules"


def test_factory_claude_uses_settings_and_client():
    client = object()
    settings = Settings(
        engine="claude", model="claude-sonnet-5", llm_timeout_s=3.0, llm_max_retries=0, llm_max_tokens=256,
        max_llm_cost_usd_per_day=0.5,
    )
    engine = build_engine(settings, client=client)
    assert isinstance(engine, ClaudeDecisionEngine)
    assert (engine.model, engine.timeout_s, engine.max_retries, engine.max_tokens) == ("claude-sonnet-5", 3.0, 0, 256)
    assert engine.daily_budget_usd == 0.5
    assert engine._client is client


def test_factory_hybrid(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    engine = build_engine(Settings(engine="hybrid", hybrid_heartbeat_candles=7))
    assert isinstance(engine, HybridDecisionEngine)
    assert engine.name == "hybrid"
    assert engine.heartbeat_candles == 7
    assert isinstance(engine.rules, RulesDecisionEngine)
    assert isinstance(engine.llm, ClaudeDecisionEngine)
    assert engine.llm.model == "claude-haiku-4-5"


def test_factory_unknown_engine():
    with pytest.raises(ConfigError):
        build_engine(Settings(engine="magic"))
