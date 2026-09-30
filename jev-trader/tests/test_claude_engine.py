from __future__ import annotations

import logging
import math
from types import SimpleNamespace

import anthropic
import httpx2
import pydantic
import pytest

from jev.brain import claude_engine as ce
from jev.brain.claude_engine import (
    PRICING,
    SYSTEM_PROMPT,
    ClaudeDecisionEngine,
    LLMDecision,
    format_prompt,
    price_for,
)
from jev.models import Action, IndicatorSet, MarketSnapshot, PortfolioView

DAY_MS = 86_400_000
T0 = 1_790_000_000_000  # fixed "now" (ms)
REQ = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


# ----------------------------------------------------------------------------- fakes


class FakeMessages:
    def __init__(self, outcomes: list) -> None:
        self.outcomes = outcomes
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes[min(len(self.calls) - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeClient:
    def __init__(self, *outcomes) -> None:
        self.messages = FakeMessages(list(outcomes))


def usage(inp: int = 1000, out: int = 100, cache_write=None, cache_read=None) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=inp,
        output_tokens=out,
        cache_creation_input_tokens=cache_write,
        cache_read_input_tokens=cache_read,
    )


def response(parsed, stop_reason: str = "end_turn", u=None) -> SimpleNamespace:
    return SimpleNamespace(parsed_output=parsed, stop_reason=stop_reason, usage=u or usage())


def llm(action="BUY", confidence=0.7, size_pct=0.5, stop=2.0, tp=4.0, reasoning="Uptrend intact.") -> LLMDecision:
    return LLMDecision(
        action=action,
        confidence=confidence,
        size_pct=size_pct,
        stop_loss_pct=stop,
        take_profit_pct=tp,
        reasoning=reasoning,
    )


def snapshot(price: float = 60000.0) -> MarketSnapshot:
    ind = IndicatorSet(
        ema_fast=60100.0,
        ema_slow=59900.0,
        ema_trend=59000.0,
        rsi=58.123,
        macd=40.0,
        macd_signal=30.0,
        macd_hist=10.0,
        atr=90.0,
        atr_pct=0.15,
        bb_upper=60500.0,
        bb_middle=59900.0,
        bb_lower=59300.0,
        bb_pct_b=0.58333,
        return_1_pct=0.123456,
        return_12_pct=-0.004,
        volatility_pct=0.08,
        volume_ratio=1.2345,
    )
    return MarketSnapshot(
        symbol="BTC/USDT",
        timeframe="5m",
        timestamp=T0 - 300_000,
        price=price,
        indicators=ind,
        recent_closes=[59800.0 + i * 10.123456 for i in range(25)],
    )


def flat() -> PortfolioView:
    return PortfolioView(quote_currency="USDT", base_currency="BTC", cash=1000.0, base_qty=0.0, equity=1000.0)


def holding() -> PortfolioView:
    return PortfolioView(
        quote_currency="USDT",
        base_currency="BTC",
        cash=500.0,
        base_qty=0.01,
        avg_entry_price=59000.0,
        equity=1100.0,
        position_value=600.0,
        unrealized_pnl_pct=1.694915,
        stop_loss=58000.0,
        take_profit=62000.0,
    )


def make_engine(client, clock=lambda: T0, **kwargs) -> ClaudeDecisionEngine:
    kwargs.setdefault("sleep", lambda seconds: None)  # retries never really sleep in tests
    return ClaudeDecisionEngine(client=client, clock_ms=clock, **kwargs)


def status_error(cls, code: int):
    return cls("boom", response=httpx2.Response(code, request=REQ), body=None)


def validation_error() -> pydantic.ValidationError:
    try:
        LLMDecision.model_validate_json('{"action": "BUY", "confid')
    except pydantic.ValidationError as exc:
        return exc
    raise AssertionError("expected a ValidationError")


# ----------------------------------------------------------------------------- success + cost


def test_success_buy_maps_fields_and_costs():
    client = FakeClient(response(llm()))
    engine = make_engine(client)
    d = engine.decide(snapshot(), flat())
    assert d.action is Action.BUY
    assert d.source == "claude"
    assert d.model == "claude-haiku-4-5"
    assert d.confidence == pytest.approx(0.7)
    assert d.size_pct == pytest.approx(0.5)
    assert d.stop_loss_pct == pytest.approx(2.0)
    assert d.take_profit_pct == pytest.approx(4.0)
    assert d.reasoning == "Uptrend intact."
    assert d.latency_ms is not None and d.latency_ms >= 0
    assert (d.input_tokens, d.output_tokens) == (1000, 100)
    # 1000 * $1/M + 100 * $5/M
    assert d.cost_usd == pytest.approx(0.0015)
    assert engine.spent_today_usd == pytest.approx(0.0015)
    assert engine.calls == 1


def test_estimate_cost_includes_cache_writes_and_reads():
    engine = make_engine(FakeClient())
    u = usage(inp=1000, out=200, cache_write=2000, cache_read=10000)
    # (1000 + 2000*1.25 + 10000*0.1) * $1/M + 200 * $5/M
    assert engine.estimate_cost(u) == pytest.approx((1000 + 2500 + 1000 + 1000) / 1e6)
    assert engine.estimate_cost(usage(1000, 100, None, None)) == pytest.approx(0.0015)
    assert engine.estimate_cost(None) == 0.0


def test_cache_tokens_counted_in_decision_input_tokens():
    engine = make_engine(FakeClient(response(llm(), u=usage(100, 50, cache_write=10, cache_read=20))))
    d = engine.decide(snapshot(), flat())
    assert d.input_tokens == 130
    assert d.output_tokens == 50


def test_pricing_table_and_unknown_model_is_conservative():
    assert PRICING["claude-haiku-4-5"] == (1.0, 5.0)
    assert price_for("claude-sonnet-5") == (2.0, 10.0)
    assert price_for("claude-opus-5-5") == (4.0, 20.0)
    assert price_for("claude-haiku-4-5-20251001") == (1.0, 5.0)
    # regression: the most expensive current models were billed at the Opus 5 price
    assert price_for("claude-fable-5") == (10.0, 50.0)
    assert price_for("claude-fable-5-1") == (10.0, 50.0)
    assert price_for("claude-mythos-5-1") == (10.0, 50.0)
    # unknown models cost more than any known one, so the daily budget never under-counts
    unknown = price_for("some-unknown-model")
    assert all(unknown[0] >= i and unknown[1] >= o for i, o in PRICING.values())
    engine = make_engine(FakeClient(), model="mystery")
    assert engine.estimate_cost(usage(1_000_000, 0)) == pytest.approx(unknown[0])


# ----------------------------------------------------------------------------- request shape


def test_exact_kwargs_sent_to_messages_parse():
    client = FakeClient(response(llm()))
    make_engine(client, max_tokens=321).decide(snapshot(), flat())
    (kwargs,) = client.messages.calls
    assert set(kwargs) == {"model", "max_tokens", "system", "messages", "output_format"}
    assert kwargs["model"] == "claude-haiku-4-5"
    assert kwargs["max_tokens"] == 321
    assert kwargs["system"] == SYSTEM_PROMPT
    assert kwargs["output_format"] is LLMDecision
    assert kwargs["messages"] == [{"role": "user", "content": format_prompt(snapshot(), flat())}]
    for forbidden in ("temperature", "thinking", "output_config", "top_p", "top_k"):
        assert forbidden not in kwargs


def test_llm_schema_has_no_numeric_constraints():
    schema = LLMDecision.model_json_schema()
    assert set(schema["required"]) == {
        "action",
        "confidence",
        "size_pct",
        "stop_loss_pct",
        "take_profit_pct",
        "reasoning",
    }
    text = str(schema)
    for keyword in ("minimum", "maximum", "exclusiveMinimum", "maxLength"):
        assert keyword not in text


# ----------------------------------------------------------------------------- failures


@pytest.mark.parametrize(
    "exc, fragment",
    [
        (anthropic.APITimeoutError(request=REQ), "timeout"),
        (anthropic.APIConnectionError(request=REQ), "connection error"),
        (status_error(anthropic.RateLimitError, 429), "rate limited"),
        (status_error(anthropic.BadRequestError, 400), "bad request"),
        (status_error(anthropic.InternalServerError, 500), "HTTP 500"),
        (status_error(anthropic.AuthenticationError, 401), "authentication"),
        (status_error(anthropic.PermissionDeniedError, 403), "permission denied"),
        (status_error(anthropic.NotFoundError, 404), "not found"),
        (RuntimeError("kaboom"), "RuntimeError"),
    ],
)
def test_api_errors_fall_back_to_hold(exc, fragment):
    engine = make_engine(FakeClient(exc), max_retries=0)
    d = engine.decide(snapshot(), flat())
    assert d.action is Action.HOLD
    assert d.source == "fallback"
    assert d.size_pct == 0.0
    assert fragment in d.reasoning
    assert d.cost_usd == 0.0
    assert engine.calls == 1
    assert engine.failures == 1


@pytest.mark.parametrize(
    "exc, level",
    [
        (status_error(anthropic.AuthenticationError, 401), logging.ERROR),
        (status_error(anthropic.PermissionDeniedError, 403), logging.ERROR),
        (status_error(anthropic.NotFoundError, 404), logging.ERROR),
        (anthropic.APITimeoutError(request=REQ), logging.WARNING),
        (status_error(anthropic.RateLimitError, 429), logging.WARNING),
    ],
)
def test_failure_log_levels(exc, level, caplog):
    caplog.set_level(logging.DEBUG, logger="jev.brain.claude_engine")
    make_engine(FakeClient(exc)).decide(snapshot(), flat())
    failures = [r for r in caplog.records if "claude call failed" in r.getMessage()]
    assert [r.levelno for r in failures] == [level]


def test_logs_redact_api_keys(caplog):
    caplog.set_level(logging.DEBUG, logger="jev.brain.claude_engine")
    make_engine(FakeClient(RuntimeError("bad key sk-ant-api03-SECRETSECRET"))).decide(snapshot(), flat())
    assert "SECRETSECRET" not in caplog.text
    assert "sk-ant-***" in caplog.text


def test_invalid_structured_output_charges_conservative_estimate():
    engine = make_engine(FakeClient(validation_error()))
    d = engine.decide(snapshot(), flat())
    assert d.action is Action.HOLD and d.source == "fallback"
    assert "invalid structured output" in d.reasoning
    assert d.cost_usd > 400 * 5 / 1e6  # at least the full max_tokens output
    assert engine.spent_today_usd == pytest.approx(d.cost_usd)


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_refusal_and_max_tokens_hold_but_account_cost(stop_reason):
    engine = make_engine(FakeClient(response(llm(), stop_reason=stop_reason)))
    d = engine.decide(snapshot(), flat())
    assert d.action is Action.HOLD
    assert d.source == "fallback"
    assert d.cost_usd == pytest.approx(0.0015)
    assert d.model == "claude-haiku-4-5"
    assert engine.spent_today_usd == pytest.approx(0.0015)


def test_parsed_output_none_holds():
    engine = make_engine(FakeClient(response(None)))
    d = engine.decide(snapshot(), flat())
    assert d.action is Action.HOLD and d.source == "fallback"
    assert "no structured output" in d.reasoning
    assert engine.spent_today_usd == pytest.approx(0.0015)


def test_dict_parsed_output_is_validated():
    raw = llm().model_dump()
    d = make_engine(FakeClient(response(raw))).decide(snapshot(), flat())
    assert d.action is Action.BUY and d.source == "claude"
    bad = make_engine(FakeClient(response({"action": "YOLO"}))).decide(snapshot(), flat())
    assert bad.action is Action.HOLD and bad.source == "fallback"


def test_decide_never_raises_when_clock_fails():
    def broken_clock() -> int:
        raise ValueError("clock broken")

    d = make_engine(FakeClient(response(llm())), clock=broken_clock).decide(snapshot(), flat())
    assert d.action is Action.HOLD and d.source == "fallback"


# ----------------------------------------------------------------------------- clamping + position


def test_clamping_of_out_of_range_values():
    raw = llm(confidence=1.7, size_pct=3.0, stop=0.0, tp=80.0, reasoning="x" * 1000)
    d = make_engine(FakeClient(response(raw))).decide(snapshot(), flat())
    assert d.action is Action.BUY
    assert d.confidence == 1.0
    assert d.size_pct == 1.0
    assert d.stop_loss_pct is None
    assert d.take_profit_pct is None
    assert len(d.reasoning) == 300


def test_clamping_negative_and_nan():
    raw = llm(confidence=math.nan, size_pct=-0.4, stop=-1.0, tp=50.0)
    d = make_engine(FakeClient(response(raw))).decide(snapshot(), flat())
    assert d.confidence == 0.0
    assert d.size_pct == 0.0
    assert d.stop_loss_pct is None
    assert d.take_profit_pct == 50.0


def test_buy_while_holding_becomes_hold():
    d = make_engine(FakeClient(response(llm("BUY")))).decide(snapshot(), holding())
    assert d.action is Action.HOLD
    assert d.source == "claude"
    assert "BUY while already holding" in d.reasoning
    assert d.size_pct == 0.0 and d.stop_loss_pct is None
    assert d.cost_usd == pytest.approx(0.0015)


def test_sell_while_flat_becomes_hold():
    d = make_engine(FakeClient(response(llm("SELL", size_pct=1.0)))).decide(snapshot(), flat())
    assert d.action is Action.HOLD
    assert "SELL while flat" in d.reasoning


def test_sell_while_holding_drops_levels_and_defaults_to_full_exit():
    d = make_engine(FakeClient(response(llm("SELL", size_pct=0.0, stop=2.0, tp=4.0)))).decide(snapshot(), holding())
    assert d.action is Action.SELL
    assert d.size_pct == 1.0
    assert d.stop_loss_pct is None and d.take_profit_pct is None


def test_hold_from_llm_keeps_confidence_and_zero_size():
    d = make_engine(FakeClient(response(llm("HOLD", confidence=0.55, size_pct=0.3)))).decide(snapshot(), flat())
    assert d.action is Action.HOLD and d.source == "claude"
    assert d.confidence == pytest.approx(0.55)
    assert d.size_pct == 0.0


# ----------------------------------------------------------------------------- budget


def test_budget_exhausted_skips_api_call():
    client = FakeClient(response(llm()))
    engine = make_engine(client, daily_budget_usd=0.002)
    engine.decide(snapshot(), flat())  # 0.0015
    engine.decide(snapshot(), flat())  # 0.0030 >= 0.002
    d = engine.decide(snapshot(), flat())
    assert len(client.messages.calls) == 2
    assert d.action is Action.HOLD
    assert d.source == "fallback"
    assert d.reasoning == "LLM daily budget exhausted"
    assert engine.calls == 2


def test_zero_budget_never_calls():
    client = FakeClient(response(llm()))
    d = make_engine(client, daily_budget_usd=0.0).decide(snapshot(), flat())
    assert client.messages.calls == []
    assert d.action is Action.HOLD


def test_budget_resets_on_next_utc_day():
    now = {"ms": (T0 // DAY_MS) * DAY_MS + DAY_MS - 1000}  # 1s before UTC midnight
    client = FakeClient(response(llm()))
    engine = make_engine(client, clock=lambda: now["ms"], daily_budget_usd=0.001)
    engine.decide(snapshot(), flat())
    assert engine.decide(snapshot(), flat()).reasoning == "LLM daily budget exhausted"
    assert len(client.messages.calls) == 1
    now["ms"] += 2000  # next UTC day
    assert engine.spent_today_usd == 0.0
    d = engine.decide(snapshot(), flat())
    assert d.source == "claude"
    assert len(client.messages.calls) == 2
    assert engine.total_spent_usd == pytest.approx(0.003)


# ----------------------------------------------------------------------------- prompt


def test_prompt_is_deterministic_and_rounded():
    text = format_prompt(snapshot(60123.456789), flat())
    assert text == format_prompt(snapshot(60123.456789), flat())
    assert "price=60123.5" in text
    assert "rsi=58.12" in text
    assert "return_1_pct=0.12" in text
    assert "return_12_pct=0.00" in text  # no "-0.00"
    assert "bb_pct_b=0.583" in text
    assert "state=flat" in text
    assert "allowed_actions=BUY,HOLD" in text
    assert "symbol=BTC/USDT" in text
    assert "timeframe=5m" in text
    closes = text.splitlines()[-1].split(",")
    assert len(closes) == 20
    assert closes[-1] == f"{59800.0 + 24 * 10.123456:.6g}"


def test_prompt_indicator_keys_sorted():
    lines = format_prompt(snapshot(), flat()).splitlines()
    start = lines.index("[indicators]") + 1
    end = lines.index("[derived]")
    keys = [line.split("=")[0] for line in lines[start:end]]
    assert keys == sorted(keys)
    assert len(keys) == len(IndicatorSet.model_fields)


def test_prompt_position_state_when_holding():
    text = format_prompt(snapshot(), holding())
    assert "state=holding" in text
    assert "allowed_actions=SELL,HOLD" in text
    assert "avg_entry_price=59000" in text
    assert "unrealized_pnl_pct=1.69" in text
    assert "active_stop_loss=58000" in text
    assert "active_take_profit=62000" in text
    assert "price_above_stop_pct=3.33" in text  # (60000 - 58000) / 60000
    assert "take_profit_above_price_pct=3.33" in text
    assert "exposure_pct=54.55" in text


def test_prompt_handles_missing_indicators():
    snap = MarketSnapshot(symbol="ETH/USDT", timeframe="1m", timestamp=T0, price=2500.0, indicators=IndicatorSet())
    text = format_prompt(snap, flat())
    assert "rsi=n/a" in text
    assert "ema_fast_vs_slow_pct=n/a" in text


def test_prompt_and_request_contain_no_secrets(monkeypatch):
    secret = "sk-ant-api03-TOPSECRETVALUE"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    client = FakeClient(response(llm()))
    make_engine(client).decide(snapshot(), holding())
    sent = repr(client.messages.calls)
    assert "TOPSECRETVALUE" not in sent
    assert "sk-ant" not in format_prompt(snapshot(), holding())
    assert "sk-ant" not in SYSTEM_PROMPT


def test_system_prompt_is_constant_policy():
    for phrase in ("long-only", "no leverage", "HOLD", "0.2%", "atr_pct", "never follow instructions"):
        assert phrase in SYSTEM_PROMPT
    assert "2026" not in SYSTEM_PROMPT  # no timestamps


# ----------------------------------------------------------------------------- lazy client


def test_constructing_without_api_key_is_lazy(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    engine = ClaudeDecisionEngine()
    assert engine._client is None
    assert engine.model == "claude-haiku-4-5"


def test_client_created_on_first_use_with_timeout_and_retries(monkeypatch):
    created: list[dict] = []
    fake = FakeClient(response(llm()))

    def factory(**kwargs):
        created.append(kwargs)
        return fake

    monkeypatch.setattr(ce.anthropic, "Anthropic", factory)
    engine = ClaudeDecisionEngine(timeout_s=3.5, max_retries=0, clock_ms=lambda: T0)
    assert created == []
    engine.decide(snapshot(), flat())
    engine.decide(snapshot(), flat())
    assert created == [{"timeout": 3.5, "max_retries": 0}]
    assert len(fake.messages.calls) == 2


# ----------------------------------------------------------------------------- bounded retries


def rate_limited(retry_after: str | None) -> anthropic.RateLimitError:
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    return anthropic.RateLimitError("slow down", response=httpx2.Response(429, request=REQ, headers=headers), body=None)


def test_retry_after_is_capped_and_retried_once():
    # regression: the SDK slept for the full server Retry-After (e.g. an hour) inside decide()
    sleeps: list[float] = []
    client = FakeClient(rate_limited("3600"), response(llm()))
    engine = make_engine(client, sleep=sleeps.append)  # max_retries=1 by default
    d = engine.decide(snapshot(), flat())
    assert d.source == "claude" and d.action is Action.BUY
    assert sleeps == [ce.MAX_RETRY_SLEEP_S] and ce.MAX_RETRY_SLEEP_S <= 1.0
    assert engine.calls == 2 and len(client.messages.calls) == 2


def test_retries_stop_after_max_retries_and_fall_back():
    sleeps: list[float] = []
    client = FakeClient(rate_limited(None))
    d = make_engine(client, sleep=sleeps.append, max_retries=2).decide(snapshot(), flat())
    assert d.source == "fallback" and "rate limited" in d.reasoning
    assert len(client.messages.calls) == 3 and len(sleeps) == 2 and max(sleeps) <= ce.MAX_RETRY_SLEEP_S


def test_non_retryable_errors_and_exhausted_time_budget_are_not_retried():
    sleeps: list[float] = []
    client = FakeClient(status_error(anthropic.BadRequestError, 400), response(llm()))
    assert make_engine(client, sleep=sleeps.append).decide(snapshot(), flat()).source == "fallback"
    assert len(client.messages.calls) == 1 and sleeps == []
    # a retry that would not fit in timeout_s is skipped
    client = FakeClient(rate_limited("1"), response(llm()))
    engine = make_engine(client, sleep=sleeps.append, timeout_s=0.5)
    assert engine.decide(snapshot(), flat()).source == "fallback"
    assert len(client.messages.calls) == 1 and sleeps == []


def test_sdk_client_never_retries_on_its_own(monkeypatch):
    created: list[dict] = []
    monkeypatch.setattr(ce.anthropic, "Anthropic", lambda **kw: created.append(kw) or FakeClient(response(llm())))
    ClaudeDecisionEngine(timeout_s=8.0, max_retries=3, clock_ms=lambda: T0).decide(snapshot(), flat())
    assert created == [{"timeout": 8.0, "max_retries": 0}]
