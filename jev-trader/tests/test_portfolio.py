"""Tests for jev.portfolio: cost basis, realized PnL, dust, days, view and persistence."""

from __future__ import annotations

import json

import pytest

from jev.models import Balances, Fill, Side
from jev.portfolio import DAY_MS, Portfolio, utc_day

T0 = 1767225600000  # 2026-01-01T00:00:00Z
M5 = 300_000
SYMBOL = "BTC/USDT"


def make_portfolio(**kwargs) -> Portfolio:
    return Portfolio(SYMBOL, "USDT", "BTC", **kwargs)


def fill(side: Side, qty: float, price: float, fee: float, ts: int = T0, symbol: str = SYMBOL) -> Fill:
    return Fill(order_id="x", symbol=symbol, side=side, quantity=qty, price=price, fee=fee, timestamp=ts)


def buy(p: Portfolio, qty: float, price: float, fee: float, ts: int = T0, **kwargs):
    return p.apply_fill(fill(Side.BUY, qty, price, fee, ts), **kwargs)


def sell(p: Portfolio, qty: float, price: float, fee: float, ts: int = T0, reason: str = ""):
    return p.apply_fill(fill(Side.SELL, qty, price, fee, ts), reason=reason)


# ---------------------------------------------------------------- cost basis and PnL


def test_buy_cost_basis_includes_fee():
    p = make_portfolio()
    assert buy(p, 0.5, 100.0, 0.05, stop_loss=98.0, take_profit=104.0) is None
    assert p.qty == 0.5
    assert p.avg_entry_price == pytest.approx(100.1)  # (50 + 0.05) / 0.5
    assert p.cost_basis == pytest.approx(50.05)
    assert p.entry_time == T0
    assert (p.stop_loss, p.take_profit) == (98.0, 104.0)
    assert p.fees_paid == pytest.approx(0.05)
    assert p.trades_today == 1
    assert p.in_position


def test_second_buy_weighted_average_and_entry_time_kept():
    p = make_portfolio()
    buy(p, 0.5, 100.0, 0.05)
    buy(p, 0.5, 110.0, 0.055, ts=T0 + M5)
    # total cost = 50.05 + 55 + 0.055 = 105.105 over 1.0 BTC
    assert p.qty == pytest.approx(1.0)
    assert p.avg_entry_price == pytest.approx(105.105)
    assert p.entry_time == T0
    assert p.stop_loss is None and p.take_profit is None


def test_partial_then_full_sell_hand_computed():
    p = make_portfolio()
    buy(p, 0.5, 100.0, 0.05, stop_loss=90.0, take_profit=130.0)
    buy(p, 0.5, 110.0, 0.055)

    first = sell(p, 0.4, 120.0, 0.048, ts=T0 + 2 * M5, reason="signal")
    # proceeds 48 - 0.048 = 47.952; cost 0.4 * 105.105 = 42.042 -> pnl 5.91
    assert first.pnl == pytest.approx(5.91)
    assert first.pnl_pct == pytest.approx(5.91 / 42.042 * 100)
    assert first.quantity == pytest.approx(0.4)
    assert first.entry_price == pytest.approx(105.105)
    assert first.exit_price == 120.0
    assert first.fees == pytest.approx(0.105 * 0.4 + 0.048)  # entry fees share + exit fee
    assert first.entry_time == T0 and first.exit_time == T0 + 2 * M5
    assert first.exit_reason == "signal"
    assert p.qty == pytest.approx(0.6)
    assert p.avg_entry_price == pytest.approx(105.105)  # unchanged by a partial sell
    assert (p.stop_loss, p.take_profit) == (90.0, 130.0)  # still protecting the rest
    assert p.last_exit_ts is None  # not a full exit

    second = sell(p, 0.6, 90.0, 0.054, ts=T0 + 3 * M5, reason="stop_loss")
    # proceeds 54 - 0.054 = 53.946; cost 0.6 * 105.105 = 63.063 -> pnl -9.117
    assert second.pnl == pytest.approx(-9.117)
    assert second.fees == pytest.approx(0.105 * 0.6 + 0.054)
    assert p.realized_pnl == pytest.approx(5.91 - 9.117)
    # the realized PnL equals the net cash flow: 101.898 received - 105.105 spent
    assert p.realized_pnl == pytest.approx((47.952 + 53.946) - (50.05 + 55.055))
    assert p.fees_paid == pytest.approx(0.05 + 0.055 + 0.048 + 0.054)
    assert sum(t.fees for t in p.trades) == pytest.approx(p.fees_paid)
    assert p.qty == 0.0 and not p.in_position
    assert p.avg_entry_price is None and p.entry_time is None
    assert p.stop_loss is None and p.take_profit is None
    assert p.entry_fees == 0.0
    assert p.last_exit_ts == T0 + 3 * M5
    assert len(p.trades) == 2
    assert p.trades_today == 4


def test_sell_more_than_held_raises_and_leaves_state_untouched():
    p = make_portfolio()
    buy(p, 1.0, 100.0, 0.1)
    before = p.to_state()
    with pytest.raises(ValueError, match="only"):
        sell(p, 1.01, 100.0, 0.1)
    assert p.to_state() == before


def test_sell_when_flat_raises():
    with pytest.raises(ValueError, match="no open position"):
        sell(make_portfolio(), 0.1, 100.0, 0.0)


def test_sell_within_float_tolerance_is_clamped():
    p = make_portfolio()
    buy(p, 1.0, 100.0, 0.0)
    record = sell(p, 1.0 + 1e-13, 100.0, 0.0)
    assert record.quantity == 1.0
    assert record.pnl == pytest.approx(0.0)
    assert not p.in_position


@pytest.mark.parametrize(
    "bad",
    [
        dict(symbol="ETH/USDT"),
        dict(qty=0.0),
        dict(qty=-1.0),
        dict(qty=float("nan")),
        dict(price=0.0),
        dict(price=float("inf")),
        dict(fee=-0.01),
        dict(fee=float("nan")),
    ],
)
def test_malformed_buy_fill_rejected(bad):
    args = dict(qty=1.0, price=100.0, fee=0.1, symbol=SYMBOL) | bad
    p = make_portfolio()
    with pytest.raises(ValueError):
        p.apply_fill(fill(Side.BUY, args["qty"], args["price"], args["fee"], symbol=args["symbol"]))
    assert p.qty == 0.0 and p.trades_today == 0 and p.fees_paid == 0.0


def test_zero_fee_buy_and_loss_pnl_pct():
    p = make_portfolio()
    buy(p, 2.0, 50.0, 0.0)
    record = sell(p, 2.0, 45.0, 0.0)
    assert record.pnl == pytest.approx(-10.0)
    assert record.pnl_pct == pytest.approx(-10.0)


# ---------------------------------------------------------------- dust


def test_dust_remainder_by_quantity_resets_position():
    p = make_portfolio()
    buy(p, 1.0, 100.0, 0.1)
    sell(p, 1.0 - 5e-10, 100.0, 0.1, ts=T0 + M5)
    assert p.qty == 0.0 and p.avg_entry_price is None
    assert p.last_exit_ts == T0 + M5


def test_dust_remainder_by_notional_resets_position():
    p = make_portfolio(dust_notional=10.0)
    buy(p, 1.0, 100.0, 0.0, stop_loss=95.0)
    sell(p, 0.95, 100.0, 0.0, ts=T0 + M5)  # 0.05 BTC * 100 = 5 < 10 left
    assert not p.in_position and p.stop_loss is None
    assert p.last_exit_ts == T0 + M5


def test_remainder_above_dust_notional_is_kept():
    p = make_portfolio(dust_notional=10.0)
    buy(p, 1.0, 100.0, 0.0)
    sell(p, 0.5, 100.0, 0.0)
    assert p.qty == pytest.approx(0.5)
    assert p.last_exit_ts is None


def test_invalid_dust_settings_rejected():
    with pytest.raises(ValueError):
        make_portfolio(dust_qty=-1.0)
    with pytest.raises(ValueError):
        make_portfolio(dust_notional=float("nan"))


# ---------------------------------------------------------------- reconcile


def test_reconcile_shrinks_to_broker_balance():
    p = make_portfolio()
    buy(p, 1.0, 100.0, 1.0)
    assert p.reconcile(1.0, T0) == 0.0
    assert p.reconcile(2.0, T0) == 0.0  # never grows
    written_off = p.reconcile(0.75, T0 + M5)
    assert written_off == pytest.approx(0.25)
    assert p.qty == 0.75
    assert p.avg_entry_price == pytest.approx(101.0)  # cost basis per coin unchanged
    assert p.entry_fees == pytest.approx(0.75)
    assert p.reconcile(0.0, T0 + 2 * M5) == pytest.approx(0.75)
    assert not p.in_position and p.last_exit_ts == T0 + 2 * M5


def test_reconcile_flat_is_noop():
    p = make_portfolio()
    assert p.reconcile(5.0, T0) == 0.0
    assert not p.in_position


# ---------------------------------------------------------------- equity curve and days


def test_mark_tracks_curve_peak_and_day_start():
    p = make_portfolio()
    p.mark(T0, 1000.0)
    p.mark(T0 + M5, 1010.0)
    p.mark(T0 + 2 * M5, 990.0)
    assert p.equity_curve == [(T0, 1000.0), (T0 + M5, 1010.0), (T0 + 2 * M5, 990.0)]
    assert p.equity_peak == 1010.0
    assert p.day_start_equity == 1000.0
    assert p.current_day == utc_day(T0)
    assert p.daily_pnl_pct(990.0) == pytest.approx(1.0)  # positive = loss
    assert p.daily_pnl_pct(1020.0) == pytest.approx(-2.0)  # negative = gain
    assert p.drawdown_pct(990.0) == pytest.approx(20 / 1010 * 100)
    assert p.drawdown_pct(1100.0) == 0.0
    assert p.last_equity == 990.0


def test_metrics_before_any_mark_are_zero():
    p = make_portfolio()
    assert p.daily_pnl_pct(500.0) == 0.0
    assert p.drawdown_pct(500.0) == 0.0
    assert p.last_equity is None


def test_mark_rejects_non_finite_equity():
    p = make_portfolio()
    with pytest.raises(ValueError):
        p.mark(T0, float("nan"))
    assert p.equity_curve == []


def test_day_rollover_resets_counters_and_day_start():
    p = make_portfolio()
    p.mark(T0, 1000.0)
    buy(p, 1.0, 100.0, 0.0, ts=T0 + M5)
    sell(p, 1.0, 100.0, 0.0, ts=T0 + 2 * M5)
    p.mark(T0 + 2 * M5, 950.0)
    assert p.trades_today == 2
    next_day = T0 + DAY_MS
    p.mark(next_day, 940.0)
    assert p.current_day == utc_day(next_day)
    assert p.trades_today == 0
    # the day starts from the last equity before midnight, so the candle that closes at
    # 00:00 (950 -> 940) counts toward the new day's loss
    assert p.day_start_equity == 950.0
    p.mark(next_day + M5, 930.0)
    assert p.day_start_equity == 950.0
    assert p.daily_pnl_pct(930.0) == pytest.approx(20 / 950 * 100)


def test_fill_rolls_the_day_and_next_mark_sets_day_start():
    p = make_portfolio()
    p.mark(T0, 1000.0)
    buy(p, 1.0, 100.0, 0.0, ts=T0)
    next_day = T0 + DAY_MS + M5
    sell(p, 1.0, 100.0, 0.0, ts=next_day)
    assert p.current_day == utc_day(next_day)
    assert p.trades_today == 1  # counter restarted with the new day's fill
    assert p.day_start_equity == 1000.0  # last equity of the previous day
    p.mark(next_day, 999.0)
    assert p.day_start_equity == 1000.0


def test_older_timestamps_do_not_roll_back_the_day():
    p = make_portfolio()
    p.mark(T0 + DAY_MS, 1000.0)
    buy(p, 1.0, 100.0, 0.0, ts=T0 + DAY_MS)
    p.mark(T0, 900.0)  # out-of-order mark from the previous day
    assert p.current_day == utc_day(T0 + DAY_MS)
    assert p.trades_today == 1
    assert p.day_start_equity == 1000.0


def test_trades_on_counts_only_the_current_day():
    p = make_portfolio()
    buy(p, 1.0, 100.0, 0.0, ts=T0)
    assert p.trades_on(T0 + M5) == 1
    assert p.trades_on(T0 + DAY_MS) == 0


def test_daily_halt_clears_on_new_day_but_drawdown_halt_persists():
    p = make_portfolio()
    p.mark(T0, 1000.0)
    p.halt("daily_loss")
    assert p.halted and p.halted_day == utc_day(T0)
    assert p.active_halt(T0 + M5) == "daily_loss"
    assert p.active_halt(T0 + DAY_MS) is None  # read-only look-ahead
    assert p.halted_reason == "daily_loss"
    p.mark(T0 + DAY_MS, 1000.0)
    assert p.halted_reason is None and p.halted_day is None

    p.halt("max_drawdown")
    p.mark(T0 + 2 * DAY_MS, 1000.0)
    assert p.halted_reason == "max_drawdown"
    assert p.active_halt(T0 + 3 * DAY_MS) == "max_drawdown"
    p.clear_halt()
    assert not p.halted


# ---------------------------------------------------------------- view


def test_view_flat_uses_broker_cash():
    p = make_portfolio()
    v = p.view(Balances(cash=1000.0, base_qty=0.0), 100.0)
    assert v.cash == 1000.0 and v.base_qty == 0.0 and v.equity == 1000.0
    assert v.position_value == 0.0 and not v.in_position
    assert v.avg_entry_price is None and v.unrealized_pnl_pct is None
    assert v.stop_loss is None and v.take_profit is None
    assert (v.quote_currency, v.base_currency) == ("USDT", "BTC")


def test_view_holding_equity_and_unrealized_pnl():
    p = make_portfolio()
    buy(p, 0.5, 100.0, 0.05, stop_loss=98.0, take_profit=104.0)
    v = p.view(Balances(cash=949.95, base_qty=0.5), 110.0)
    assert v.base_qty == 0.5
    assert v.position_value == pytest.approx(55.0)
    assert v.equity == pytest.approx(1004.95)
    assert v.avg_entry_price == pytest.approx(100.1)
    assert v.unrealized_pnl_pct == pytest.approx((110.0 / 100.1 - 1) * 100)
    assert (v.stop_loss, v.take_profit) == (98.0, 104.0)
    assert v.in_position


def test_view_ignores_unmanaged_coins_and_caps_by_broker():
    p = make_portfolio()
    # pre-funded testnet coins are not Jev Trader's position
    v = p.view(Balances(cash=1000.0, base_qty=1.0), 100.0)
    assert v.base_qty == 0.0 and v.equity == 1000.0 and not v.in_position
    buy(p, 0.5, 100.0, 0.0)
    v = p.view(Balances(cash=950.0, base_qty=1.5), 100.0)
    assert v.base_qty == 0.5 and v.equity == pytest.approx(1000.0)
    # broker holds less than tracked -> only what exists counts
    v = p.view(Balances(cash=950.0, base_qty=0.3), 100.0)
    assert v.base_qty == 0.3 and v.equity == pytest.approx(980.0)


@pytest.mark.parametrize("price", [0.0, -1.0, float("nan"), float("inf")])
def test_view_rejects_bad_price(price):
    with pytest.raises(ValueError):
        make_portfolio().view(Balances(cash=1.0, base_qty=0.0), price)


# ---------------------------------------------------------------- persistence


def test_state_round_trip_is_lossless_and_json_serializable():
    p = make_portfolio(dust_qty=1e-8, dust_notional=5.0)
    p.mark(T0, 1000.0)
    buy(p, 0.5, 100.0, 0.05, ts=T0, stop_loss=98.0, take_profit=104.0)
    sell(p, 0.2, 101.3, 0.02026, ts=T0 + M5, reason="partial")
    p.mark(T0 + M5, 1000.4)
    p.mark(T0 + 2 * M5, 999.1)
    p.halt("daily_loss")

    state = p.to_state()
    text = json.dumps(state)
    restored = Portfolio.from_state(json.loads(text))
    assert restored.to_state() == state
    assert restored.trades == p.trades
    assert restored.equity_curve == p.equity_curve
    assert isinstance(restored.equity_curve[0], tuple)
    assert restored.qty == p.qty and restored.avg_entry_price == p.avg_entry_price
    assert restored.halted_reason == "daily_loss" and restored.halted_day == p.halted_day

    # behaviour continues identically after the restore
    rec_a = sell(p, 0.3, 102.0, 0.0306, ts=T0 + 3 * M5)
    rec_b = sell(restored, 0.3, 102.0, 0.0306, ts=T0 + 3 * M5)
    assert rec_a == rec_b
    assert restored.to_state() == p.to_state()


def test_state_round_trip_flat_portfolio():
    p = make_portfolio()
    assert Portfolio.from_state(json.loads(json.dumps(p.to_state()))).to_state() == p.to_state()


def test_from_state_rejects_unknown_version():
    state = make_portfolio().to_state()
    state["version"] = 999
    with pytest.raises(ValueError, match="version"):
        Portfolio.from_state(state)


# ---------------------------------------------------------------- regressions (money / safety review)


def test_daily_loss_counts_the_candle_closing_at_midnight_and_1d_candles():
    # regression money-2: the first mark of a day used to become the day start, so the move
    # of the candle closing at 00:00 UTC (every candle on 1d) never counted as a daily loss
    p = make_portfolio()
    p.mark(T0 + DAY_MS - M5, 1000.0)  # 23:55
    p.mark(T0 + DAY_MS, 900.0)  # the candle closing at 00:00 crashed
    assert p.day_start_equity == 1000.0 and p.daily_pnl_pct(900.0) == pytest.approx(10.0)
    daily = make_portfolio()  # 1d timeframe: one mark per day
    for day, equity in enumerate([1000.0, 950.0, 902.5]):
        daily.mark(T0 + day * DAY_MS, equity)
    assert daily.daily_pnl_pct(902.5) == pytest.approx(5.0)
    # after a gap of several days the first mark of the day starts it (no stale baseline)
    daily.mark(T0 + 10 * DAY_MS, 800.0)
    assert daily.day_start_equity == 800.0


def test_external_withdrawal_and_deposit_move_the_baselines_not_the_pnl():
    # regression money-1 / safety-1 / runtime-2: live equity is the account's free cash, so
    # moving funds looked like trading PnL (a withdrawal tripped the drawdown switch)
    p = make_portfolio()
    assert p.observe_balances(T0, 1000.0, 0.0, 100.0) == 0.0  # first observation: baseline
    p.mark(T0, 1000.0)
    buy(p, 2.5, 100.0, 0.25, ts=T0 + M5)
    assert p.observe_balances(T0 + M5, 749.75, 2.5, 100.0) == 0.0  # explained by the fill
    p.mark(T0 + M5, 999.75)
    assert p.observe_balances(T0 + 2 * M5, 639.75, 2.5, 100.0) == pytest.approx(-110.0)  # withdrawal
    p.mark(T0 + 2 * M5, 889.75)
    assert p.drawdown_pct(889.75) == pytest.approx(0.25 / 890 * 100)  # only the fee, not the -110
    assert p.daily_pnl_pct(889.75) == pytest.approx(0.25 / 890 * 100)
    # deposit of 60 while the position really loses 5 % of the account: the loss stays visible
    flow = p.observe_balances(T0 + 3 * M5, 699.75, 2.5, 80.0)
    assert flow == pytest.approx(60.0)
    p.mark(T0 + 3 * M5, 899.75)
    # day start 1000 - 110 + 60 = 950; the 50 lost on the position (+ fee) is a 5.3 % daily loss
    assert p.daily_pnl_pct(899.75) == pytest.approx((950.0 - 899.75) / 950.0 * 100)
    # a manual sale of managed coins at the market is neither a flow nor a loss
    assert p.observe_balances(T0 + 4 * M5, 699.75 + 80.0, 1.5, 80.0) == pytest.approx(0.0)
    # tiny differences (fees paid in another coin) are ignored
    assert p.observe_balances(T0 + 5 * M5, 779.75 + 0.1, 1.5, 80.0) == 0.0


def test_unsellable_remainder_is_carried_into_the_next_position():
    # regression safety-4 / money-3: a remainder below the minimum order (lot-size truncation
    # after a base-coin fee) was written off: stranded coins and PnL that did not match equity
    p = make_portfolio(dust_notional=10.0)
    buy(p, 0.0024975, 100_000.0, 0.25, ts=T0)  # 0.0025 filled, 0.0000025 BTC fee
    first = sell(p, 0.00249, 99_800.0, 0.2485, ts=T0 + M5)  # truncated to the 1e-5 step
    assert not p.in_position and p.stop_loss is None
    assert p.carry_qty == pytest.approx(0.0000075)
    assert p.carry_cost == pytest.approx(0.0000075 * first.entry_price)
    assert p.capital_in_use == pytest.approx(p.carry_cost)
    buy(p, 0.0024975, 100_000.0, 0.25, ts=T0 + 2 * M5)
    assert p.qty == pytest.approx(0.002505) and p.carry_qty == 0.0
    sell(p, 0.002505, 100_000.0, 0.25, ts=T0 + 3 * M5)
    # every coin bought was sold: realized PnL equals the net cash flow
    spent = 2 * (0.0024975 * 100_000.0 + 0.25)
    received = 0.00249 * 99_800.0 - 0.2485 + 0.002505 * 100_000.0 - 0.25
    assert p.realized_pnl == pytest.approx(received - spent)


def test_reconcile_charges_written_off_cost_to_realized_pnl():
    p = make_portfolio()
    buy(p, 1.0, 100.0, 1.0)
    p.reconcile(0.75, T0 + M5)
    assert p.realized_pnl == pytest.approx(-0.25 * 101.0)
    carried = make_portfolio(dust_notional=10.0)
    buy(carried, 1.0, 100.0, 0.0)
    sell(carried, 0.95, 100.0, 0.0)  # 0.05 left, below the 10 notional: carried
    assert carried.reconcile(0.0, T0 + M5) == pytest.approx(0.05)
    assert carried.carry_qty == 0.0 and carried.realized_pnl == pytest.approx(-5.0)


def test_state_can_keep_only_the_last_equity_points():
    # regression runtime-8: the live loop re-serialized the whole equity curve every candle
    p = make_portfolio()
    for i in range(100):
        p.mark(T0 + i * M5, 1000.0 + i)
    state = p.to_state(equity_points=1)
    assert state["equity_curve"] == [[T0 + 99 * M5, 1099.0]]
    restored = Portfolio.from_state(state)
    assert restored.last_equity == 1099.0 and restored.equity_peak == 1099.0
    assert len(p.to_state()["equity_curve"]) == 100
