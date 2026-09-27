from __future__ import annotations

import pytest

from jeb.backtest import run_backtest
from jeb.brain.rules_engine import RulesDecisionEngine
from jeb.config import RiskConfig, Settings
from jeb.journal import Journal
from jeb.market.synthetic import SyntheticMarket
from jeb.models import Action, Candle, Decision, Side

T0 = 1_767_225_600_000
TF = 300_000
HOLD = Decision.hold("wait", source="rules")


def candle(i: int, close: float, low: float | None = None, high: float | None = None, open_: float | None = None) -> Candle:
    o = close if open_ is None else open_
    return Candle(
        timestamp=T0 + i * TF, open=o, close=close, volume=1.0,
        low=min(o, close) if low is None else low, high=max(o, close) if high is None else high,
    )


def flat(n: int, price: float = 100.0, start: int = 0) -> list[Candle]:
    return [candle(start + i, price) for i in range(n)]


def settings(slippage: float = 0.0, **risk) -> Settings:
    base = dict(cooldown_candles=0)
    base.update(risk)
    return Settings(engine="rules", fee_pct=0.1, slippage_pct=slippage, history_candles=60, risk=RiskConfig(**base))


def buy(stop: float = 2.0, tp: float = 4.0) -> Decision:
    return Decision(action=Action.BUY, confidence=0.9, size_pct=1.0, stop_loss_pct=stop, take_profit_pct=tp,
                    source="rules", reasoning="spy entry")


SELL = Decision(action=Action.SELL, confidence=0.9, size_pct=1.0, source="rules", reasoning="spy exit")


class Spy:
    """Scripted engine (decision index -> Decision) that records what it saw."""

    def __init__(self, script: dict[int, Decision] | None = None, name: str = "rules") -> None:
        self.name = name
        self.script = script or {}
        self.seen = []

    def decide(self, snapshot, view) -> Decision:
        self.seen.append((snapshot, view))
        return self.script.get(len(self.seen) - 1, HOLD)


class Recorder:
    """Wraps the rules engine and records (timestamp, decision)."""

    name = "rules"

    def __init__(self) -> None:
        self.inner = RulesDecisionEngine()
        self.log = []

    def decide(self, snapshot, view) -> Decision:
        decision = self.inner.decide(snapshot, view)
        self.log.append((snapshot.timestamp, snapshot.indicators, view.in_position, decision))
        return decision


class FakeLLM:
    """Pretends to be an LLM engine: every decision carries model/usage/cost."""

    def __init__(self, tokens: int = 100, name: str = "claude") -> None:
        self.name = name
        self.tokens = tokens
        self.calls = 0

    def decide(self, snapshot, view) -> Decision:
        self.calls += 1
        return Decision(action=Action.HOLD, source="claude", model="fake-llm", input_tokens=self.tokens,
                        output_tokens=self.tokens // 5, cost_usd=0.001 if self.tokens else 0.0)


def synthetic(n: int, seed: int = 7) -> list[Candle]:
    return SyntheticMarket(seed=seed, initial_history=0).generate(n)


# ---------------------------------------------------------------------------- no look-ahead


def test_each_decision_sees_only_closed_candles_and_fills_at_next_open():
    candles = synthetic(200)
    by_ts = {c.timestamp: i for i, c in enumerate(candles)}
    spy = Spy({5: buy(stop=10.0, tp=20.0), 15: SELL})
    result = run_backtest(candles, settings(slippage=0.05), spy, warmup=60, historical_data=False)

    assert len(spy.seen) == result.decisions == len(candles) - 60  # candles 59 .. n-2 decide
    previous = -1
    for snapshot, _ in spy.seen:
        i = by_ts[snapshot.timestamp]
        assert i > previous
        previous = i
        assert snapshot.price == candles[i].close
        assert snapshot.recent_closes == [c.close for c in candles[i - 19: i + 1]]
    assert by_ts[spy.seen[0][0].timestamp] == 59
    assert by_ts[spy.seen[-1][0].timestamp] == len(candles) - 2

    buy_i = by_ts[spy.seen[5][0].timestamp]
    sell_i = by_ts[spy.seen[15][0].timestamp]
    buy_fill, sell_fill = result.fills
    assert buy_fill.side is Side.BUY and buy_fill.timestamp == candles[buy_i + 1].timestamp
    assert buy_fill.price == pytest.approx(candles[buy_i + 1].open * 1.0005)
    assert sell_fill.side is Side.SELL and sell_fill.timestamp == candles[sell_i + 1].timestamp
    assert sell_fill.price == pytest.approx(candles[sell_i + 1].open * 0.9995)
    assert spy.seen[6][1].in_position and not spy.seen[5][1].in_position


def test_changing_the_future_does_not_change_past_decisions():
    candles = synthetic(400, seed=3)
    cut = 250
    tampered = candles[: cut + 1] + [
        c.model_copy(update={k: getattr(c, k) * 1.5 for k in ("open", "high", "low", "close")})
        for c in candles[cut + 1:]
    ]
    a, b = Recorder(), Recorder()
    run_backtest(candles, settings(), a, warmup=60)
    run_backtest(tampered, settings(), b, warmup=60)
    cut_ts = candles[cut].timestamp
    past_a = [entry for entry in a.log if entry[0] <= cut_ts]
    past_b = [entry for entry in b.log if entry[0] <= cut_ts]
    assert past_a == past_b and len(past_a) > 100


# ---------------------------------------------------------------------------- protective exits


def entry_then(*later: Candle, slippage: float = 0.0, stop: float = 2.0, tp: float = 4.0):
    """BUY decided on candle 9 (close 100, stop/tp from there), filled at candle 10's open."""
    candles = flat(10) + list(later) + flat(3, start=10 + len(later))
    spy = Spy({0: buy(stop, tp)})
    return run_backtest(candles, settings(slippage=slippage), spy, warmup=10), spy, candles


def test_stop_fills_at_the_stop_level():
    result, spy, candles = entry_then(candle(10, 100.0, low=99.5, high=100.5), candle(11, 98.5, low=97.5, open_=99.5))
    trade = result.trades[0]
    assert trade.exit_reason == "stop_loss" and trade.exit_price == pytest.approx(98.0)
    assert result.fills[1].timestamp == candles[11].timestamp
    assert all(s.timestamp != candles[11].timestamp for s, _ in spy.seen)  # no decision on the exit candle


def test_stop_gap_fills_at_the_worse_open():
    result, _, _ = entry_then(candle(10, 100.0), candle(11, 96.0, low=95.0, high=96.5, open_=96.0))
    assert result.trades[0].exit_price == pytest.approx(96.0)


def test_stop_is_checked_on_the_entry_candle_and_uses_the_paper_broker_slippage():
    result, _, candles = entry_then(candle(10, 99.0, low=97.0, open_=100.0), slippage=0.05)
    buy_fill, sell_fill = result.fills
    assert buy_fill.price == pytest.approx(100.0 * 1.0005)
    assert sell_fill.timestamp == candles[10].timestamp
    assert sell_fill.price == pytest.approx(98.0 * 0.9995)
    assert sell_fill.fee == pytest.approx(sell_fill.quantity * sell_fill.price * 0.001)


def test_take_profit_fills_at_the_target():
    result, _, _ = entry_then(candle(10, 100.0), candle(11, 104.5, high=105.0, open_=100.0))
    trade = result.trades[0]
    assert trade.exit_reason == "take_profit" and trade.exit_price == pytest.approx(104.0)


def test_kill_switch_flattens_at_next_open_and_stops_trading():
    later = [candle(10, 100.0), candle(11, 92.0, low=91.5, open_=100.0), candle(12, 93.0, open_=92.5)]
    candles = flat(10) + later + flat(5, 93.0, start=13)
    spy = Spy({i: buy(stop=10.0, tp=20.0) for i in range(20)})
    cfg = settings(max_drawdown_pct=1.0, max_daily_loss_pct=50.0, risk_per_trade_pct=5.0)
    result = run_backtest(candles, cfg, spy, warmup=10)

    exit_fill = result.fills[1]
    assert exit_fill.side is Side.SELL and exit_fill.timestamp == candles[12].timestamp
    assert exit_fill.price == pytest.approx(92.5)
    assert result.trades[0].exit_reason == "kill_switch:max_drawdown"
    assert len(result.fills) == 2  # no new entries after the halt
    assert len(spy.seen) == 2  # candle 9 (BUY) and candle 10 (holding); none while halted and flat
    assert any("max_drawdown" in w for w in result.warnings)


def test_cooldown_uses_candle_timestamps():
    later = [candle(10, 100.0), candle(11, 98.5, low=97.5, open_=99.5)]
    candles = flat(10) + later + flat(10, start=12)
    spy = Spy({i: buy() for i in range(30)})
    result = run_backtest(candles, settings(cooldown_candles=3), spy, warmup=10)
    buys = [f for f in result.fills if f.side is Side.BUY]
    assert len(buys) >= 2
    exit_ts = result.fills[1].timestamp
    assert buys[1].timestamp - exit_ts >= 3 * TF


# ---------------------------------------------------------------------------- LLM limits and warnings


def test_max_llm_calls_switches_to_rules():
    llm = FakeLLM()
    result = run_backtest(synthetic(150), settings(), llm, warmup=60, max_llm_calls=5)
    assert llm.calls == 5 and result.llm_calls == 5
    assert result.llm_cost_usd == pytest.approx(0.005) and result.metrics.llm_cost_usd == pytest.approx(0.005)
    assert result.engine_name == "claude" and result.final_engine_name == "rules"
    assert any("Límite de 5 llamadas" in w for w in result.warnings)
    assert result.decisions <= 150 - 60  # candles with a protective exit skip the decision


def test_zero_llm_calls_means_rules_from_the_start():
    llm = FakeLLM()
    result = run_backtest(synthetic(100), settings(), llm, warmup=60, max_llm_calls=0)
    assert llm.calls == 0 and result.llm_calls == 0 and result.final_engine_name == "rules"


def test_llm_on_historical_data_warns_about_contamination():
    result = run_backtest(synthetic(100), settings(), FakeLLM(), warmup=60)
    assert result.warnings[0].startswith("Contaminación por look-ahead")
    synthetic_run = run_backtest(synthetic(100), settings(), FakeLLM(), warmup=60, historical_data=False)
    assert not any(w.startswith("Contaminación") for w in synthetic_run.warnings)
    rules_run = run_backtest(synthetic(100), settings(), RulesDecisionEngine(), warmup=60)
    assert not any(w.startswith("Contaminación") for w in rules_run.warnings)


def test_failed_llm_calls_and_small_samples_are_flagged():
    result = run_backtest(synthetic(100), settings(), FakeLLM(tokens=0), warmup=60, historical_data=False)
    assert any("40 de 40 llamadas al LLM fallaron" in w for w in result.warnings)
    assert any(w.startswith("Solo 0 operaciones") for w in result.warnings)


def test_open_position_at_the_end_is_flagged():
    candles = flat(20)
    result = run_backtest(candles, settings(), Spy({0: buy(stop=10.0, tp=50.0)}), warmup=10)
    assert result.trades == [] and len(result.fills) == 1
    assert any("posición abierta" in w for w in result.warnings)
    assert result.metrics.exposure_pct == pytest.approx(10 / 11 * 100)


# ---------------------------------------------------------------------------- determinism and outputs


def test_rules_backtest_on_synthetic_market_is_deterministic():
    candles = SyntheticMarket(seed=42, initial_history=0).generate(600)
    a = run_backtest(candles, settings(slippage=0.05), RulesDecisionEngine(), warmup=60, historical_data=False)
    b = run_backtest(candles, settings(slippage=0.05), RulesDecisionEngine(), warmup=60, historical_data=False)
    assert a.metrics == b.metrics and a.trades == b.trades and a.equity_curve == b.equity_curve
    exits = sum(1 for t in a.trades if t.exit_reason in ("stop_loss", "take_profit"))
    assert len(a.fills) > 0 and a.decisions == 600 - 60 - exits  # no decision on a protective-exit candle
    assert len(a.equity_curve) == len(a.price_curve) == 600 - 60 + 1
    assert a.equity_curve[0] == (candles[59].timestamp + TF, 1000.0)
    assert a.price_curve[-1] == (candles[-1].timestamp + TF, candles[-1].close)
    assert a.metrics.buy_and_hold_return_pct == pytest.approx((candles[-1].close / candles[59].close - 1) * 100)
    assert a.metrics.fees_paid == pytest.approx(sum(f.fee for f in a.fills))
    assert a.llm_calls == 0 and a.engine_name == a.final_engine_name == "rules"


def test_backtest_writes_to_the_journal():
    journal = Journal(":memory:")
    result = run_backtest(synthetic(200), settings(), RulesDecisionEngine(), warmup=60, journal=journal)
    summary = journal.summary()
    assert summary["decisions"] == result.decisions
    assert summary["fills"] == len(result.fills)
    assert len(journal.equity_curve()) == len(result.equity_curve)


@pytest.mark.parametrize("warmup", [0, -1, 1.5, True])
def test_invalid_warmup(warmup):
    with pytest.raises(ValueError):
        run_backtest(flat(100), settings(), RulesDecisionEngine(), warmup=warmup)


def test_too_few_candles_and_bad_limits():
    with pytest.raises(ValueError):
        run_backtest(flat(60), settings(), RulesDecisionEngine(), warmup=60)
    with pytest.raises(ValueError):
        run_backtest(flat(100), settings(), RulesDecisionEngine(), max_llm_calls=-1)


def test_progress_callback_reaches_the_end():
    seen = []
    run_backtest(flat(100), settings(), RulesDecisionEngine(), warmup=10, progress=lambda d, t: seen.append((d, t)))
    assert seen[-1] == (91, 91) and len(seen) <= 22
