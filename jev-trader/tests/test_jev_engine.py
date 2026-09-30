"""Tests for the Jev engine, driving the real typesafe-sdk client through a mock transport."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx2
import pytest
import typesafe_sdk as ts

from jev.brain.jev_engine import DEFAULT_PRICE_PER_MTOK_INPUT, JevDecisionEngine, build_state
from jev.models import Action, IndicatorSet, MarketSnapshot, PortfolioView

DAY_MS = 86_400_000


def snapshot(atr_pct: float | None = 0.8) -> MarketSnapshot:
    return MarketSnapshot(
        symbol="BTC/USDT",
        timeframe="5m",
        timestamp=1_767_225_600_000,
        price=60_123.456789,
        indicators=IndicatorSet(ema_fast=60_100.0, ema_slow=60_000.0, rsi=58.123456, atr_pct=atr_pct),
        recent_closes=[59_900.0, 60_000.0, 60_123.456789],
    )


def flat() -> PortfolioView:
    return PortfolioView(quote_currency="USDT", base_currency="BTC", cash=1000.0, base_qty=0.0, equity=1000.0)


def holding() -> PortfolioView:
    return PortfolioView(
        quote_currency="USDT", base_currency="BTC", cash=750.0, base_qty=0.004,
        avg_entry_price=59_000.0, equity=990.5, position_value=240.5, unrealized_pnl_pct=1.9,
        stop_loss=58_000.0, take_profit=61_000.0,
    )


def jev_payload(action: str = "BUY", p: float = 0.82, edge: float = 0.71, width: str = "normal",
                size: float = 1.4, input_tokens: int | None = 400) -> dict:
    other = "HOLD" if action != "HOLD" else "BUY"
    answers = {
        "action": {"type": "choice", "choice": action, "confidence": 0.8,
                   "probabilities": {action: p, other: round(1 - p, 6)}},
        "edge": {"type": "noul", "noul": edge},
        "stop_width": {"type": "choice", "choice": width, "confidence": 0.7,
                       "probabilities": {"tight": 0.1, "normal": 0.8, "wide": 0.1}},
        "size": {"type": "score", "score": size, "confidence": 0.6,
                 "legend": {"0": "a", "1": "b", "2": "c", "3": "d"},
                 "probabilities": {"0": 0.1, "1": 0.4, "2": 0.4, "3": 0.1}},
    }
    usage = {"input_tokens": input_tokens, "output_tokens": 9}
    return {"model": "jev-1.13", "usage": usage, "answers": answers}


def make_engine(handler, **kwargs) -> tuple[JevDecisionEngine, list[dict]]:
    sent: list[dict] = []

    def recording(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.content))
        return handler(request)

    client = ts.TypeSafeClient(
        api_key="test-key", transport=httpx2.MockTransport(recording),
        retry=ts.RetryPolicy(max_retries=0), timeout=2.0,
    )
    kwargs.setdefault("clock_ms", lambda: 1_767_225_600_000)
    return JevDecisionEngine(client=client, **kwargs), sent


def ok(payload: dict):
    return lambda request: httpx2.Response(200, json=payload)


def test_buy_decision_maps_answers_and_costs():
    engine, sent = make_engine(ok(jev_payload()))
    decision = engine.decide(snapshot(atr_pct=0.8), flat())

    assert decision.action is Action.BUY
    assert decision.source == "jev"
    assert decision.confidence == pytest.approx(0.71)  # min(p_buy, edge)
    assert decision.stop_loss_pct == pytest.approx(1.6)  # normal = 2x ATR% (0.8)
    assert decision.take_profit_pct == pytest.approx(3.2)
    # (1.4 + 1) / 4 = 60 % of the allowed position; max position 25 % -> 15 % of equity
    assert decision.size_pct == pytest.approx((1.4 + 1) / 4 * 0.25)
    assert decision.model == "jev-1.13"
    assert decision.input_tokens == 400
    assert decision.cost_usd == pytest.approx(400 * DEFAULT_PRICE_PER_MTOK_INPUT / 1e6)
    assert decision.latency_ms is not None and decision.latency_ms >= 0
    assert engine.calls == 1

    body = sent[0]
    assert body["model"] == "jev-latest"
    assert set(body["questions"]) == {"action", "edge", "stop_width", "size"}
    assert set(body["questions"]["action"]["criteria"]) == {"BUY", "HOLD"}
    assert body["state"]["position"] == {"status": "flat"}


def test_holding_asks_only_exit_question_and_sells():
    payload = jev_payload(action="SELL", p=0.77)
    payload["answers"] = {"action": payload["answers"]["action"]}
    engine, sent = make_engine(ok(payload))
    decision = engine.decide(snapshot(), holding())

    assert decision.action is Action.SELL
    assert decision.size_pct == 1.0
    assert decision.confidence == pytest.approx(0.77)
    assert set(sent[0]["questions"]) == {"action"}
    assert set(sent[0]["questions"]["action"]["criteria"]) == {"SELL", "HOLD"}
    assert sent[0]["state"]["position"]["status"] == "holding"


def test_hold_choice_returns_hold():
    engine, _ = make_engine(ok(jev_payload(action="HOLD", p=0.9)))
    decision = engine.decide(snapshot(), flat())
    assert decision.action is Action.HOLD
    assert decision.source == "jev"


def test_stop_falls_back_without_atr_and_is_clamped():
    engine, _ = make_engine(ok(jev_payload(width="wide")))
    assert engine.decide(snapshot(atr_pct=None), flat()).stop_loss_pct == pytest.approx(3.0)
    engine, _ = make_engine(ok(jev_payload(width="wide")))
    assert engine.decide(snapshot(atr_pct=9.0), flat()).stop_loss_pct == pytest.approx(10.0)


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (httpx2.Response(401, json={"error": "bad key"}), "authentication failed"),
        (httpx2.Response(404, json={"error": "no model"}), "model not found"),
        (httpx2.Response(422, json={"detail": "bad"}), "invalid request"),
        (httpx2.Response(429, json={"error": "slow down"}), "rate limited"),
        (httpx2.Response(500, json={"error": "boom"}), "api error"),
        (httpx2.Response(200, json={"model": "jev", "usage": {}, "answers": {"x": {"type": "choice"}}}),
         "malformed response"),
    ],
)
def test_http_failures_become_hold(response, reason):
    engine, _ = make_engine(lambda request: response)
    decision = engine.decide(snapshot(), flat())
    assert decision.action is Action.HOLD
    assert decision.source == "fallback"
    assert reason in decision.reasoning


@pytest.mark.parametrize(
    ("exc", "reason"),
    [(httpx2.ReadTimeout("slow"), "timeout"), (httpx2.ConnectError("down"), "connection error")],
)
def test_transport_failures_become_hold(exc, reason):
    def boom(request):
        raise exc

    engine, _ = make_engine(boom)
    decision = engine.decide(snapshot(), flat())
    assert decision.action is Action.HOLD
    assert reason in decision.reasoning


def test_missing_answers_hold_but_still_count_cost():
    payload = jev_payload()
    del payload["answers"]["edge"]
    engine, _ = make_engine(ok(payload))
    decision = engine.decide(snapshot(), flat())
    assert decision.action is Action.HOLD
    assert decision.source == "fallback"
    assert decision.cost_usd > 0
    assert engine.spent_today_usd == pytest.approx(decision.cost_usd)


def test_missing_api_key_degrades_to_hold(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    engine = JevDecisionEngine()  # constructing without a key must work
    decision = engine.decide(snapshot(), flat())
    assert decision.action is Action.HOLD
    assert "client not configured" in decision.reasoning


def test_daily_budget_blocks_calls_and_resets_next_day():
    now = {"ms": 1_767_225_600_000}
    engine, sent = make_engine(ok(jev_payload(input_tokens=1_000_000)), daily_budget_usd=0.05,
                               clock_ms=lambda: now["ms"])
    assert engine.decide(snapshot(), flat()).source == "jev"  # spends 0.042
    assert engine.decide(snapshot(), flat()).source == "jev"  # 0.084 >= budget afterwards
    blocked = engine.decide(snapshot(), flat())
    assert blocked.source == "fallback" and "budget" in blocked.reasoning
    assert len(sent) == 2
    now["ms"] += DAY_MS
    assert engine.decide(snapshot(), flat()).source == "jev"
    assert len(sent) == 3


def test_unexpected_client_error_never_raises():
    class Exploding:
        def system_one(self, **kwargs):
            raise RuntimeError("bug")

    engine = JevDecisionEngine(client=Exploding())
    decision = engine.decide(snapshot(), flat())
    assert decision.action is Action.HOLD
    assert decision.source == "fallback"


def test_non_finite_probabilities_are_rejected():
    fake = SimpleNamespace(
        model="jev", usage=SimpleNamespace(input_tokens=10, output_tokens=1),
        choices={"action": SimpleNamespace(choice="BUY", probabilities={"BUY": float("nan"), "HOLD": 0.1})},
        nouls={}, scores={},
    )
    engine = JevDecisionEngine(client=SimpleNamespace(system_one=lambda **kw: fake))
    decision = engine.decide(snapshot(), flat())
    assert decision.action is Action.HOLD
    assert decision.source == "fallback"


def test_state_is_deterministic_numeric_and_rounded():
    state = build_state(snapshot(), holding(), cost_pct=0.3)
    assert state == build_state(snapshot(), holding(), cost_pct=0.3)
    assert state["market"]["price"] == 60123.5
    assert state["market"]["indicators"]["rsi"] == 58.1235
    assert "ema_trend" not in state["market"]["indicators"]  # None values are dropped
    assert state["position"]["position_pct_of_equity"] == pytest.approx(24.28, abs=0.01)
    assert json.dumps(state)  # JSON-serializable


# ---------------------------------------------------------------------------- regressions (AI review)


@pytest.mark.parametrize("score, max_position, expected", [(0.0, 25.0, 0.0625), (3.0, 25.0, 0.25), (1.0, 100.0, 0.5)])
def test_size_is_a_fraction_of_the_max_position_in_equity_units(score, max_position, expected):
    # regression ai-2: size_pct meant "fraction of the allowed position" but the risk manager
    # reads it as a fraction of equity, so every weak setup got the full 25 % position
    engine, _ = make_engine(ok(jev_payload(size=score)), max_position_pct=max_position)
    assert engine.decide(snapshot(), flat()).size_pct == pytest.approx(expected)
    with pytest.raises(ValueError):
        JevDecisionEngine(max_position_pct=0.0)


def test_state_keeps_price_unit_indicators_of_low_priced_coins():
    # regression ai-3: EMAs/MACD/ATR/Bollinger were rounded to 4 decimals -> 0.0 for PEPE
    ind = IndicatorSet(ema_fast=1.2401e-05, ema_slow=1.2302e-05, macd=-3.21e-08, atr=4.5e-07, atr_pct=3.6,
                       bb_upper=1.3e-05, rsi=55.123456, bb_pct_b=0.4567891)
    snap = MarketSnapshot(symbol="PEPE/USDT", timeframe="5m", timestamp=1_767_225_600_000, price=1.2345e-05,
                          indicators=ind, recent_closes=[1.23e-05])
    state = build_state(snap, flat(), cost_pct=0.3)["market"]["indicators"]
    assert state["ema_fast"] == 1.2401e-05 and state["ema_slow"] == 1.2302e-05
    assert state["macd"] == -3.21e-08 and state["atr"] == 4.5e-07 and state["bb_upper"] == 1.3e-05
    assert state["rsi"] == 55.1235 and state["bb_pct_b"] == 0.4568 and state["atr_pct"] == 3.6


def test_retry_after_is_not_honoured_and_the_call_stays_bounded():
    # regression ai-4: RetryPolicy(backoff_max=1) still slept for the server Retry-After (up to 30 s)
    import time

    replies = iter([httpx2.Response(429, headers={"retry-after": "25"}, json={"error": "slow"}),
                    httpx2.Response(200, json=jev_payload())])
    engine = JevDecisionEngine(timeout_s=2.0, max_retries=1, clock_ms=lambda: 1_767_225_600_000)
    policy = engine.retry_policy()
    assert policy.respect_retry_after is False and policy.timeout <= 3.0 and policy.backoff_max <= 1.0
    engine._client = ts.TypeSafeClient(api_key="k", transport=httpx2.MockTransport(lambda r: next(replies)),
                                       retry=policy, timeout=2.0)
    started = time.perf_counter()
    decision = engine.decide(snapshot(), flat())
    assert time.perf_counter() - started < 2.0
    assert decision.source == "jev"


def test_budget_fallback_is_not_counted_as_an_llm_call():
    # regression ai-5: the budget-exhausted HOLD carried model=..., so the backtest counted
    # 50 "failed LLM calls" (and switched to rules) without a single request
    from jev.backtest import run_backtest
    from jev.config import Settings
    from jev.market.synthetic import SyntheticMarket

    class NoCalls:
        def system_one(self, **kwargs):
            raise AssertionError("no request expected")

    engine = JevDecisionEngine(daily_budget_usd=0.0, client=NoCalls())
    held = engine.decide(snapshot(), flat())
    assert held.source == "fallback" and "budget" in held.reasoning and held.model is None
    candles = SyntheticMarket(seed=1, initial_history=0).generate(150)
    result = run_backtest(candles, Settings(engine="jev"), engine, warmup=60, max_llm_calls=50,
                          historical_data=False)
    assert result.llm_calls == 0 and result.final_engine_name == "jev"
    assert not any("llamadas al LLM fallaron" in w for w in result.warnings)
