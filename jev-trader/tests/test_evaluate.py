"""Tests for `jev evaluate`: walk-forward evaluation of the rules strategy, all offline."""

from __future__ import annotations

import json

import pytest

from jev.cli import main
from jev.config import RiskConfig, Settings
from jev.evaluate import (EDGE, INCONCLUSIVE, LOSES, MIN_TRADES, _verdict, bootstrap_mean_ci, evaluate,
                          render_evaluation, split_folds)
from jev.market.csv_source import save_candles
from jev.market.synthetic import SyntheticMarket
from jev.models import Action, Candle, Decision

T0 = 1_767_225_600_000
HOUR = 3_600_000
WARMUP = 60


def trend(n: int, step_pct: float, start: float = 100.0) -> list[Candle]:
    """Candles moving ``step_pct`` per candle, each opening at the previous close."""
    out, price = [], start
    for i in range(n):
        close = price * (1 + step_pct / 100)
        out.append(Candle(timestamp=T0 + i * HOUR, open=price, close=close, volume=1.0,
                          high=max(price, close) * 1.0002, low=min(price, close) * 0.9998))
        price = close
    return out


def settings() -> Settings:
    return Settings(engine="rules", timeframe="1h", fee_pct=0.1, slippage_pct=0.05, history_candles=WARMUP,
                    risk=RiskConfig(cooldown_candles=0, max_trades_per_day=24))


class InAndOut:
    """Buys when flat and sells two decisions later, whatever the market does."""

    name = "rules"

    def __init__(self) -> None:
        self.held = 0
        self.seen: list[int] = []

    def decide(self, snapshot, view) -> Decision:
        self.seen.append(snapshot.timestamp)
        if not view.in_position:
            self.held = 0
            return Decision(action=Action.BUY, confidence=0.9, size_pct=1.0, stop_loss_pct=8.0,
                            take_profit_pct=40.0, source="rules")
        self.held += 1
        if self.held >= 2:
            return Decision(action=Action.SELL, confidence=0.9, size_pct=1.0, source="rules")
        return Decision.hold("wait", source="rules")


# ---------------------------------------------------------------------------- folds


def test_folds_are_consecutive_and_carry_their_own_warmup():
    candles = trend(60 + 3 * 100 + 7, 0.0)
    folds = split_folds(candles, 3, 60)
    assert [len(f) for f in folds] == [162, 162, 163]  # 307 tradable candles: 102 + 102 + 103
    traded = [c.timestamp for f in folds for c in f[60:]]
    assert traded == [c.timestamp for c in candles[60:]]  # every candle traded exactly once, in order
    assert folds[1][0].timestamp == candles[102].timestamp  # warm-up comes from the candles just before


def test_too_little_history_is_a_clear_error():
    with pytest.raises(ValueError, match="hacen falta al menos 360 velas"):
        split_folds(trend(200, 0.0), 6, 60)
    with pytest.raises(ValueError):
        split_folds(trend(500, 0.0), 0, 60)


# ---------------------------------------------------------------------------- statistics


def test_bootstrap_interval_is_deterministic_and_brackets_the_mean():
    values = [1.0, -0.5, 2.0, 0.3, -1.2, 0.8, 1.5, -0.4, 0.9, 0.1]
    low, high = bootstrap_mean_ci(values)
    assert (low, high) == bootstrap_mean_ci(values)
    assert low < sum(values) / len(values) < high
    assert bootstrap_mean_ci([1.0]) is None and bootstrap_mean_ci([]) is None
    low, high = bootstrap_mean_ci([-1.0, -0.8, -1.2, -0.9] * 10)
    assert high < 0


def test_verdict_rules():
    assert _verdict(MIN_TRADES - 1, (0.5, 1.0), 6, 6)[0] == INCONCLUSIVE  # too few trades, whatever the numbers
    assert _verdict(100, None, 6, 6)[0] == INCONCLUSIVE
    assert _verdict(100, (-1.0, -0.1), 0, 6)[0] == LOSES
    assert _verdict(100, (0.1, 1.0), 4, 6)[0] == EDGE
    assert _verdict(100, (0.1, 1.0), 3, 6)[0] == INCONCLUSIVE  # positive mean but not most folds
    assert _verdict(100, (-0.2, 0.4), 6, 6)[0] == INCONCLUSIVE  # interval includes zero
    assert "dinero real" in _verdict(100, (-1.0, -0.1), 0, 6)[1]


# ---------------------------------------------------------------------------- evaluation


def test_a_winning_strategy_is_reported_as_edge():
    result = evaluate([("BTC/USDT", trend(WARMUP + 3 * 120, 0.4))], settings(), folds=3, warmup=WARMUP,
                      engine_factory=InAndOut)
    assert result.verdict == EDGE
    assert result.trades >= MIN_TRADES and result.trades == sum(f.trades for f in result.folds)
    assert result.mean_trade_pct > 0 and result.ci_low_pct > 0
    assert result.win_rate_pct == 100.0 and result.profit_factor is None
    assert result.folds_positive == 3
    assert result.folds_beating_hold == 0  # it was only invested part of the time in a straight rally


def test_a_losing_strategy_is_reported_as_losing():
    result = evaluate([("BTC/USDT", trend(WARMUP + 3 * 120, -0.3))], settings(), folds=3, warmup=WARMUP,
                      engine_factory=InAndOut)
    assert result.verdict == LOSES
    assert result.ci_high_pct < 0 and result.folds_positive == 0
    assert result.profit_factor == 0.0
    assert result.folds_beating_hold == 3  # losing less than holding is not an edge
    text = render_evaluation(result)
    assert "Veredicto: PIERDE" in text and "no la uses con dinero real" in text


def test_costs_alone_make_a_flat_market_lose():
    result = evaluate([("BTC/USDT", trend(WARMUP + 3 * 120, 0.0))], settings(), folds=3, warmup=WARMUP,
                      engine_factory=InAndOut)
    assert result.verdict == LOSES
    assert result.mean_trade_pct == pytest.approx(-0.3, abs=0.05)  # 2 x (0.1 % fee + 0.05 % slippage)


def test_no_fold_sees_candles_from_a_later_fold():
    candles = trend(WARMUP + 3 * 100, 0.1)
    engines: list[InAndOut] = []

    def factory() -> InAndOut:
        engines.append(InAndOut())
        return engines[-1]

    result = evaluate([("BTC/USDT", candles)], settings(), folds=3, warmup=WARMUP, engine_factory=factory)
    assert len(engines) == 3  # a fresh engine and a fresh account per fold
    for engine, fold in zip(engines, result.folds):
        assert engine.seen[0] == fold.start_ts and max(engine.seen) <= fold.end_ts
    assert engines[0].seen[-1] < engines[1].seen[0] < engines[2].seen[0]


def test_several_pairs_are_pooled():
    data = [("BTC/USDT", trend(WARMUP + 2 * 100, 0.4)), ("ETH/USDT", trend(WARMUP + 2 * 100, -0.3))]
    result = evaluate(data, settings(), folds=2, warmup=WARMUP, engine_factory=InAndOut)
    assert [(f.symbol, f.fold) for f in result.folds] == [("BTC/USDT", 1), ("BTC/USDT", 2),
                                                           ("ETH/USDT", 1), ("ETH/USDT", 2)]
    assert result.folds_positive == 2 and 0 < result.win_rate_pct < 100
    with pytest.raises(ValueError):
        evaluate([], settings())


def test_rules_engine_on_synthetic_data_is_deterministic():
    candles = SyntheticMarket(seed=3, timeframe="1h", initial_history=0).generate(1500)
    a = evaluate([("BTC/USDT", candles)], settings(), folds=3, warmup=WARMUP, historical_data=False)
    b = evaluate([("BTC/USDT", candles)], settings(), folds=3, warmup=WARMUP, historical_data=False)
    assert a == b and a.verdict in (LOSES, INCONCLUSIVE, EDGE)


# ---------------------------------------------------------------------------- CLI


def test_cli_synthetic_writes_json(tmp_path, capsys):
    out = tmp_path / "out" / "eval.json"
    assert main(["evaluate", "--synthetic", "--candles", "1500", "--folds", "3", "--timeframe", "1h",
                 "--json", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "Veredicto:" in printed and "datos sintéticos" in printed
    saved = json.loads(out.read_text())
    assert saved["verdict"] in (LOSES, INCONCLUSIVE, EDGE) and len(saved["folds"]) == 3
    assert len(saved["trade_returns_pct"]) == saved["trades"]


def test_cli_evaluates_several_csv_files(tmp_path, capsys):
    btc, eth = tmp_path / "btc.csv", tmp_path / "eth.csv"
    save_candles(btc, SyntheticMarket(seed=1, timeframe="1h", initial_history=0).generate(800))
    save_candles(eth, SyntheticMarket(seed=2, timeframe="1h", initial_history=0).generate(800))
    assert main(["evaluate", "--timeframe", "1h", "--folds", "2", "--csv", f"BTC/USDT={btc}",
                 "--csv", f"ETH/USDT={eth}"]) == 0
    printed = capsys.readouterr().out
    assert "2 par(es)" in printed and "ETH/USDT" in printed and "datos sintéticos" not in printed


def test_cli_usage_errors(tmp_path, capsys):
    small = tmp_path / "small.csv"
    save_candles(small, SyntheticMarket(seed=1, timeframe="1h", initial_history=0).generate(200))
    assert main(["evaluate"]) == 2
    assert main(["evaluate", "--csv", str(tmp_path / "missing.csv")]) == 2
    assert main(["evaluate", "--timeframe", "1h", "--csv", str(small)]) == 2  # not enough candles for 6 folds
    assert main(["evaluate", "--timeframe", "1h", "--csv", str(small), "--csv", str(small)]) == 2  # same pair twice
    assert main(["evaluate", "--csv", f"BTCUSDT={small}"]) == 2
    assert main(["evaluate", "--timeframe", "5m", "--csv", str(small)]) == 2  # the file holds 1h candles
    err = capsys.readouterr().err
    assert "--csv" in err and "no existe el archivo" in err and "hacen falta al menos" in err
    assert "timeframe '5m'" in err and "pasá el --timeframe" in err
