from __future__ import annotations

import pytest

from jev.config import RiskConfig, Settings
from jev.engine import NO_NEW_CANDLE, OK, SKIPPED, TradingEngine, execute_order, iso_utc
from jev.execution.ccxt_broker import OrderStateUnknownError
from jev.execution.paper import PaperBroker
from jev.journal import Journal
from jev.models import (
    Action,
    Balances,
    Candle,
    Decision,
    InsufficientFundsError,
    JevError,
    MarketDataError,
    OrderRejectedError,
    OrderRequest,
    Side,
)

T0 = 1_767_225_600_000  # 2026-01-01T00:00Z
TF = 300_000  # 5m
SYMBOL = "BTC/USDT"


def candle(i: int, close: float, low: float | None = None, high: float | None = None, open_: float | None = None) -> Candle:
    o = close if open_ is None else open_
    return Candle(
        timestamp=T0 + i * TF, open=o, close=close, volume=1.0,
        low=min(o, close) if low is None else low, high=max(o, close) if high is None else high,
    )


def flat_candles(n: int = 30, price: float = 100.0) -> list[Candle]:
    return [candle(i, price) for i in range(n)]


class FakeMarket:
    """Closed candles [0, visible); price = last close; clock = close of the last candle."""

    def __init__(self, candles: list[Candle]) -> None:
        self.candles = list(candles)
        self.visible = len(self.candles)
        self.fail: Exception | None = None
        self.price: float | None = None

    def fetch_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        if self.fail is not None:
            raise self.fail
        return self.candles[: self.visible][-limit:]

    def fetch_last_price(self, symbol: str) -> float:
        return self.price if self.price is not None else self.candles[self.visible - 1].close

    def now_ms(self) -> int:
        return self.candles[self.visible - 1].timestamp + TF

    def add(self, *new: Candle) -> None:
        self.candles.extend(new)
        self.visible = len(self.candles)


class Scripted:
    """Returns queued decisions, then HOLD; records every snapshot it sees."""

    name = "scripted"

    def __init__(self, *decisions: Decision) -> None:
        self.queue = list(decisions)
        self.calls = 0
        self.snapshots = []

    def decide(self, snapshot, view) -> Decision:
        self.calls += 1
        self.snapshots.append(snapshot)
        return self.queue.pop(0) if self.queue else Decision.hold("idle", source="rules")


class RejectingBroker(PaperBroker):
    def __init__(self, error: Exception) -> None:
        super().__init__(SYMBOL, 1000.0, 0.1, 0.0)
        self.error = error

    def execute(self, order, market_price, timestamp):
        raise self.error


class BalanceFailBroker(PaperBroker):
    def balances(self) -> Balances:
        raise JevError("exchange down")


def buy(stop: float = 2.0, tp: float = 4.0) -> Decision:
    return Decision(action=Action.BUY, confidence=0.9, size_pct=1.0, stop_loss_pct=stop, take_profit_pct=tp,
                    reasoning="test entry", source="rules")


def settings(**risk) -> Settings:
    base = dict(cooldown_candles=0)
    base.update(risk)
    return Settings(engine="rules", fee_pct=0.1, slippage_pct=0.0, history_candles=60, risk=RiskConfig(**base))


def make(market: FakeMarket, *decisions: Decision, cfg: Settings | None = None, journal: Journal | None = None,
         broker=None) -> TradingEngine:
    cfg = cfg or settings()
    broker = broker or PaperBroker(SYMBOL, 1000.0, cfg.fee_pct, cfg.slippage_pct)
    return TradingEngine(cfg, market, broker, Scripted(*decisions), journal or Journal(":memory:"), state_key="test")


def events(journal: Journal) -> list[str]:
    return [e["kind"] for e in reversed(journal.recent_events(100))]


# ---------------------------------------------------------------------------- step flow


def test_buy_flow_books_fill_journals_and_saves_state():
    market = FakeMarket(flat_candles())
    eng = make(market, buy())
    result = eng.step()

    assert result.status == OK
    assert result.timestamp == market.candles[-1].timestamp
    assert result.verdict.approved and len(result.fills) == 1
    fill = result.fills[0]
    assert fill.side is Side.BUY and fill.price == 100.0 and fill.timestamp == market.now_ms()
    assert fill.quantity == pytest.approx(2.5)  # max position 25 % of 1000 at 100
    p = eng.portfolio
    assert p.in_position and p.stop_loss == pytest.approx(98.0) and p.take_profit == pytest.approx(104.0)
    assert eng.last_candle_ts == market.candles[-1].timestamp

    j = eng.journal
    decisions = j.recent_decisions()
    assert len(decisions) == 1 and decisions[0]["approved"] is True and decisions[0]["action"] == "BUY"
    assert len(j.fills()) == 1 and len(j.equity_curve()) == 1
    state = j.load_state("engine:test")
    assert state["portfolio"]["qty"] == pytest.approx(2.5)
    assert state["broker"]["base_qty"] == pytest.approx(2.5)
    assert state["last_candle_ts"] == market.candles[-1].timestamp


def test_same_candle_is_not_decided_twice():
    market = FakeMarket(flat_candles())
    eng = make(market)
    assert eng.step().status == OK
    result = eng.step()
    assert result.status == NO_NEW_CANDLE
    assert eng.decision_engine.calls == 1
    market.add(candle(30, 100.0))
    assert eng.step().status == OK
    assert eng.decision_engine.calls == 2


def test_snapshot_uses_bounded_history_window():
    market = FakeMarket(flat_candles(100))
    eng = make(market)
    eng.step()
    snap = eng.decision_engine.snapshots[0]
    assert snap.timestamp == market.candles[-1].timestamp
    assert snap.price == 100.0 and len(snap.recent_closes) == 20


# ---------------------------------------------------------------------------- protective exits


def test_protective_stop_takes_precedence_over_the_decision_engine():
    market = FakeMarket(flat_candles())
    eng = make(market, buy(), Decision(action=Action.SELL, confidence=0.9, size_pct=1.0, source="rules"))
    eng.step()
    market.add(candle(30, 99.0, low=97.5))  # touches the 98 stop
    result = eng.step()

    assert "stop_loss" in result.events
    assert result.decision is None  # the engine was not consulted
    assert eng.decision_engine.calls == 1
    assert not eng.portfolio.in_position
    trade = eng.portfolio.trades[0]
    assert trade.exit_reason == "stop_loss"
    assert trade.exit_price == pytest.approx(99.0)  # realtime: market exit at the current price
    assert "stop_loss" in events(eng.journal)
    assert eng.journal.trades()[0].exit_reason == "stop_loss"


def test_take_profit_exit():
    market = FakeMarket(flat_candles())
    eng = make(market, buy())
    eng.step()
    market.add(candle(30, 103.0, high=104.5))
    result = eng.step()
    assert "take_profit" in result.events
    assert eng.portfolio.trades[0].exit_reason == "take_profit"


def test_protective_exit_checks_every_candle_missed_since_the_last_step():
    market = FakeMarket(flat_candles())
    eng = make(market, buy())
    eng.step()
    market.add(candle(30, 100.0, low=99.0), candle(31, 99.5, low=97.0), candle(32, 100.0, low=99.5))
    result = eng.step()
    assert "stop_loss" in result.events
    message = eng.journal.recent_events(10)[0]["message"]
    assert iso_utc(T0 + 31 * TF) in message


def test_no_protective_exit_when_levels_are_not_touched():
    market = FakeMarket(flat_candles())
    eng = make(market, buy())
    eng.step()
    market.add(candle(30, 100.5, low=98.5, high=103.5))
    result = eng.step()
    assert result.events == [] and eng.portfolio.in_position
    assert eng.decision_engine.calls == 2


# ---------------------------------------------------------------------------- kill switch


def kill_settings(flatten: bool = True) -> Settings:
    # risk 5 % with a 10 % stop -> the 25 % max position binds (2.5 BTC at 100)
    return settings(max_drawdown_pct=1.0, max_daily_loss_pct=50.0, risk_per_trade_pct=5.0, flatten_on_kill=flatten)


def test_kill_switch_flattens_and_blocks_new_entries():
    market = FakeMarket(flat_candles())
    eng = make(market, buy(stop=10.0, tp=20.0), buy(), cfg=kill_settings())
    eng.step()
    market.add(candle(30, 92.0, low=91.5))  # -8 % on 25 % of equity = -2 % drawdown; stop at 90 untouched
    result = eng.step()

    assert result.halted == "max_drawdown"
    assert "kill_switch" in result.events
    assert not eng.portfolio.in_position
    assert eng.portfolio.trades[0].exit_reason == "kill_switch:max_drawdown"
    assert eng.decision_engine.calls == 1  # halted and flat after the flatten: no decision

    market.add(candle(31, 93.0))
    later = eng.step()
    assert later.halted == "max_drawdown" and later.fills == []
    assert eng.decision_engine.calls == 1
    assert events(eng.journal).count("kill_switch") == 1  # journaled once, when it trips


def test_kill_switch_without_flatten_keeps_position_and_still_allows_exits():
    market = FakeMarket(flat_candles())
    sell = Decision(action=Action.SELL, confidence=0.8, size_pct=1.0, source="rules")
    eng = make(market, buy(stop=10.0, tp=20.0), sell, cfg=kill_settings(flatten=False))
    eng.step()
    market.add(candle(30, 92.0, low=91.5))
    result = eng.step()
    assert result.halted == "max_drawdown"
    assert eng.decision_engine.calls == 2  # still holding: the engine may exit
    assert result.verdict.approved and not eng.portfolio.in_position
    assert eng.portfolio.trades[0].exit_reason == "signal"


# ---------------------------------------------------------------------------- errors never crash


@pytest.mark.parametrize(
    "error, kind",
    [
        (InsufficientFundsError("no cash"), "insufficient_funds"),
        (OrderRejectedError("min notional"), "order_rejected"),
    ],
)
def test_broker_errors_are_journaled_and_do_not_crash(error, kind):
    market = FakeMarket(flat_candles())
    eng = make(market, buy(), buy(), broker=RejectingBroker(error))
    result = eng.step()
    assert result.status == OK and kind in result.events and result.fills == []
    assert not eng.portfolio.in_position
    assert kind in events(eng.journal)
    assert eng.last_candle_ts == market.candles[-1].timestamp  # the candle is not retried

    market.add(candle(30, 100.0))
    assert eng.run_forever(max_iterations=1, wait_for_close=False) == 1


def test_order_state_unknown_halts_new_entries():
    market = FakeMarket(flat_candles())
    eng = make(market, buy(), buy(), broker=RejectingBroker(OrderStateUnknownError("timeout")))
    result = eng.step()
    assert "order_state_unknown" in result.events
    assert eng.portfolio.halted_reason == "order_state_unknown"
    market.add(candle(30, 100.0))
    later = eng.step()
    assert later.halted == "order_state_unknown" and eng.decision_engine.calls == 1


def test_market_data_error_skips_the_tick():
    market = FakeMarket(flat_candles())
    eng = make(market)
    market.fail = MarketDataError("exchange unreachable")
    result = eng.step()
    assert result.status == SKIPPED and result.events == ["market_error"]
    assert eng.last_candle_ts is None and eng.decision_engine.calls == 0
    assert events(eng.journal) == ["market_error"]
    market.fail = None
    assert eng.step().status == OK


def test_invalid_candles_are_a_market_error():
    market = FakeMarket([candle(1, 100.0), candle(0, 100.0)])  # not increasing
    eng = make(market)
    result = eng.step()
    assert result.status == SKIPPED and "market_error" in result.events


def test_balance_errors_skip_the_tick():
    market = FakeMarket(flat_candles())
    eng = make(market, buy(), broker=BalanceFailBroker(SYMBOL, 1000.0, 0.1, 0.0))
    result = eng.step()
    assert result.status == SKIPPED and "broker_error" in result.events
    assert eng.last_candle_ts is None


def test_price_falls_back_to_last_close_when_ticker_is_bad():
    market = FakeMarket(flat_candles())
    market.price = float("nan")
    eng = make(market)
    assert eng.step().price == 100.0


def test_raising_decision_engine_becomes_hold():
    class Boom:
        name = "boom"

        def decide(self, snapshot, view):
            raise RuntimeError("bug")

    market = FakeMarket(flat_candles())
    eng = TradingEngine(settings(), market, PaperBroker(SYMBOL, 1000.0, 0.1, 0.0), Boom(), Journal(":memory:"))
    result = eng.step()
    assert result.decision.action is Action.HOLD and result.decision.source == "fallback"


# ---------------------------------------------------------------------------- persistence


def test_state_is_resumed_after_a_restart(tmp_path):
    path = str(tmp_path / "journal.sqlite3")
    market = FakeMarket(flat_candles())
    with Journal(path) as journal:
        eng = make(market, buy(), journal=journal)
        eng.step()
        cash, qty = eng.broker.cash, eng.portfolio.qty

    with Journal(path) as journal:
        fresh_broker = PaperBroker(SYMBOL, 1000.0, 0.1, 0.0)
        eng2 = make(market, journal=journal, broker=fresh_broker)
        assert eng2.restore_state() is True
        assert eng2.portfolio.qty == pytest.approx(qty) and eng2.portfolio.stop_loss == pytest.approx(98.0)
        assert eng2.broker.cash == pytest.approx(cash) and eng2.broker.base_qty == pytest.approx(qty)
        assert eng2.last_candle_ts == market.candles[-1].timestamp
        assert "resume" in events(journal)
        assert eng2.step().status == NO_NEW_CANDLE
        market.add(candle(30, 97.0, low=96.0))  # the restored stop still protects the position
        result = eng2.step()
        assert "stop_loss" in result.events and not eng2.portfolio.in_position


def test_restore_ignores_missing_or_incompatible_state(tmp_path):
    market = FakeMarket(flat_candles())
    journal = Journal(":memory:")
    eng = make(market, journal=journal)
    assert eng.restore_state() is False
    eng.step()
    other = TradingEngine(
        Settings(symbol="ETH/USDT", engine="rules"), market, PaperBroker("ETH/USDT", 1000.0, 0.1, 0.0),
        Scripted(), journal, state_key="test",
    )
    assert other.restore_state() is False


def test_execute_order_journals_fill_and_trade():
    from jev.portfolio import Portfolio

    journal = Journal(":memory:")
    broker = PaperBroker(SYMBOL, 1000.0, 0.1, 0.0)
    portfolio = Portfolio(SYMBOL, "USDT", "BTC")
    order = OrderRequest(symbol=SYMBOL, side=Side.BUY, quantity=1.0, reference_price=100.0, reason="why")
    fill, trade = execute_order(broker, portfolio, order, 100.0, T0, stop_loss=95.0, journal=journal)
    assert trade is None and portfolio.stop_loss == 95.0
    sell = OrderRequest(symbol=SYMBOL, side=Side.SELL, quantity=1.0, reference_price=110.0)
    _, trade = execute_order(broker, portfolio, sell, 110.0, T0 + TF, exit_reason="signal", journal=journal)
    assert trade.exit_reason == "signal" and trade.pnl > 0
    assert len(journal.fills()) == 2 and len(journal.trades()) == 1


# ---------------------------------------------------------------------------- run_forever


class FakeClock:
    def __init__(self, start: int) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def wall(self) -> int:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += int(seconds * 1000)


def test_run_forever_waits_for_each_candle_close_plus_grace():
    market = FakeMarket(flat_candles())
    eng = make(market)
    clock = FakeClock(T0 + 30 * TF + 10_000)  # 10 s into the forming candle
    ticks = []

    def on_tick(result):
        ticks.append((result.status, clock.now))
        market.add(candle(len(market.candles), 100.0))  # the exchange publishes the next candle

    steps = eng.run_forever(max_iterations=3, on_tick=on_tick, sleep=clock.sleep, wall_ms=clock.wall, poll_s=60)
    assert steps == 3 and [s for s, _ in ticks] == [OK, OK, OK]
    assert ticks[0][1] == T0 + 30 * TF + 10_000  # the first step runs immediately
    assert ticks[1][1] == T0 + 31 * TF + 2_000  # next close + 2 s grace
    assert ticks[2][1] == T0 + 32 * TF + 2_000
    assert max(clock.sleeps) <= 60


def test_run_forever_retries_when_the_candle_is_late():
    market = FakeMarket(flat_candles())
    eng = make(market)
    clock = FakeClock(T0 + 30 * TF)
    results = []
    steps = eng.run_forever(max_iterations=2, on_tick=results.append, sleep=clock.sleep, wall_ms=clock.wall,
                            poll_s=5, max_candle_retries=2)
    assert steps == 2
    assert results[1].status == NO_NEW_CANDLE
    assert clock.sleeps[-2:] == [5, 5]


def test_run_forever_stop_flag_and_keyboard_interrupt_save_state():
    market = FakeMarket(flat_candles())
    eng = make(market)
    assert eng.run_forever(max_iterations=10, wait_for_close=False, on_tick=lambda r: eng.stop()) == 1

    def interrupt(result):
        raise KeyboardInterrupt

    eng2 = make(FakeMarket(flat_candles()), buy())
    assert eng2.run_forever(max_iterations=5, wait_for_close=False, on_tick=interrupt) == 1
    assert "stop" in events(eng2.journal)
    assert eng2.journal.load_state("engine:test")["portfolio"]["qty"] == pytest.approx(2.5)


def test_run_forever_survives_unexpected_errors_but_not_forever():
    market = FakeMarket(flat_candles())
    eng = make(market)
    market.fail = RuntimeError("bug")
    assert eng.run_forever(max_iterations=3, wait_for_close=False) == 3
    assert events(eng.journal).count("error") == 3
    with pytest.raises(RuntimeError):
        eng.run_forever(max_iterations=10, wait_for_close=False)


# ---------------------------------------------------------------------------- regressions (review)


def test_withdrawal_while_holding_does_not_trip_the_kill_switch_or_force_a_sale():
    # regression money-1 / safety-1 / runtime-2: 110 USDT withdrawn at an unchanged price
    # read as an 11 % drawdown, tripped max_drawdown and market-sold the position
    market = FakeMarket(flat_candles())
    eng = make(market, buy(), cfg=settings(max_daily_loss_pct=3.0, max_drawdown_pct=10.0))
    eng.step()
    assert eng.portfolio.in_position
    eng.broker.cash -= 110.0  # the user withdraws (or spends) 110 USDT from the account
    market.add(candle(30, 100.0))
    result = eng.step()
    assert result.halted is None and "kill_switch" not in result.events
    assert "external_flow" in result.events and eng.portfolio.in_position
    assert [f.side for f in result.fills] == []
    assert "external_flow" in events(eng.journal)


def test_deposit_does_not_hide_a_real_daily_loss():
    market = FakeMarket(flat_candles())
    eng = make(market, buy(stop=50.0, tp=200.0), cfg=settings(risk_per_trade_pct=5.0, max_daily_loss_pct=3.0))
    eng.step()  # 2.5 BTC at 100 (25 % of 1000)
    eng.broker.cash += 60.0  # deposit
    market.add(candle(30, 80.0, low=79.0))  # -20 % on the position = -5 % of the account
    result = eng.step()
    assert result.halted == "daily_loss" and not eng.portfolio.in_position


def test_max_capital_caps_sizing_and_equity_on_a_big_account():
    # regression safety-6 / runtime-3: live sized every trade from the whole free balance
    market = FakeMarket(flat_candles())
    broker = PaperBroker(SYMBOL, 20_000.0, 0.1, 0.0)
    eng = TradingEngine(settings(), market, broker, Scripted(buy()), Journal(":memory:"), state_key="t",
                        max_capital=1000.0)
    result = eng.step()
    assert result.fills[0].quantity * result.fills[0].price == pytest.approx(250.0)  # 25 % of 1000, not of 20000
    assert result.equity == pytest.approx(1000.0 - result.fills[0].fee)
    broker.cash += 5000.0  # a deposit above the cap changes nothing for the bot
    market.add(candle(30, 100.0))
    later = eng.step()
    assert "external_flow" not in later.events and later.equity == pytest.approx(result.equity)
    with pytest.raises(ValueError):
        TradingEngine(settings(), market, broker, Scripted(), Journal(":memory:"), max_capital=-1.0)


def test_stale_candles_skip_new_entries_but_keep_protection():
    # regression safety-2: a 6 h old candle (stalled klines / start-up) still triggered a BUY
    market = FakeMarket(flat_candles())
    eng = make(market, buy())
    eng.clock_ms = lambda: market.now_ms() + 6 * 3_600_000
    result = eng.step()
    assert result.status == OK and "stale_data" in result.events
    assert result.decision is None and result.fills == [] and eng.decision_engine.calls == 0


def test_stop_and_target_follow_the_real_fill_price():
    # regression safety-2: stop/TP were set from the old close, so a fill far below it put
    # the stop ABOVE the entry (guaranteed immediate stop-out)
    market = FakeMarket(flat_candles(price=100_000.0))
    market.price = 97_000.0  # the market moved since the close
    eng = make(market, buy(stop=2.0, tp=4.0))
    eng.step()
    p = eng.portfolio
    assert p.stop_loss == pytest.approx(97_000.0 * 0.98) and p.stop_loss < p.avg_entry_price
    assert p.take_profit == pytest.approx(97_000.0 * 1.04)


class InterruptingBroker(PaperBroker):
    def execute(self, order, market_price, timestamp):
        super().execute(order, market_price, timestamp)  # the exchange filled it...
        raise KeyboardInterrupt  # ...but the process is force-quit before the reply is booked


def test_forced_quit_during_an_order_halts_entries_on_restart(tmp_path):
    # regression runtime-1: a fill lost mid-order left an unmanaged position and no trace
    path = str(tmp_path / "j.sqlite3")
    market = FakeMarket(flat_candles())
    with Journal(path) as journal:
        eng = make(market, buy(), journal=journal, broker=InterruptingBroker(SYMBOL, 1000.0, 0.1, 0.0))
        eng.run_forever(max_iterations=1, wait_for_close=False)
        assert journal.load_state("engine:test")["order_in_flight"]["side"] == "buy"
    with Journal(path) as journal:
        eng2 = make(market, buy(), journal=journal)
        assert eng2.restore_state() is True
        assert eng2.portfolio.halted_reason == "order_state_unknown"
        assert "order_state_unknown" in events(journal)
        market.add(candle(30, 100.0))
        assert eng2.step().fills == []  # no new entry until a human checks the exchange


def test_known_order_outcomes_clear_the_in_flight_marker():
    market = FakeMarket(flat_candles())
    eng = make(market, buy(), broker=RejectingBroker(OrderRejectedError("min notional")))
    eng.step()
    assert eng.journal.load_state("engine:test")["order_in_flight"] is None
    eng2 = make(FakeMarket(flat_candles()), buy(), broker=RejectingBroker(RuntimeError("bug")))
    result = eng2.step()  # unexpected broker error: the order state is unknown
    assert eng2.portfolio.halted_reason == "order_state_unknown" and "order_state_unknown" in result.events


def test_a_journal_error_never_loses_a_real_fill():
    class BrokenJournal(Journal):
        def record_fill(self, fill, reason=""):
            import sqlite3

            raise sqlite3.OperationalError("database is locked")

    market = FakeMarket(flat_candles())
    eng = make(market, buy(), journal=BrokenJournal(":memory:"))
    result = eng.step()
    assert len(result.fills) == 1 and eng.portfolio.qty == pytest.approx(2.5)


def test_saved_state_keeps_only_the_last_equity_point():
    # regression runtime-8: the state blob grew with every candle
    market = FakeMarket(flat_candles())
    eng = make(market)
    for i in range(5):
        eng.step()
        market.add(candle(30 + i, 100.0))
    state = eng.journal.load_state("engine:test")
    assert len(state["portfolio"]["equity_curve"]) == 1
    assert len(eng.journal.equity_curve()) == 5  # the history stays in the journal table
