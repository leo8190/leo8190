from __future__ import annotations

import math

import pytest

from jeb.metrics import (
    PerformanceMetrics,
    compute_metrics,
    drawdown_curve,
    format_number,
    format_pct,
    periods_per_year,
    render_text_summary,
    sharpe_ratio,
    sortino_ratio,
)
from jeb.models import TradeRecord

HOUR = 3_600_000


def _curve(values: list[float], step: int = HOUR) -> list[tuple[int, float]]:
    return [(i * step, v) for i, v in enumerate(values)]


def _trade(pnl: float, pnl_pct: float, entry: int | None = 0, exit_: int = HOUR) -> TradeRecord:
    return TradeRecord(
        symbol="BTC/USDT", entry_price=100, exit_price=100, quantity=1, pnl=pnl, pnl_pct=pnl_pct,
        fees=0.1, entry_time=entry, exit_time=exit_,
    )


def test_total_return_and_known_drawdown() -> None:
    # peak 120 -> trough 90 = -25 %; later peak 130 -> 117 = -10 %
    curve = _curve([100, 110, 120, 90, 100, 130, 117])
    m = compute_metrics(curve, [], start_equity=100, timeframe_ms=HOUR)
    assert m.end_equity == 117
    assert m.total_return_pct == pytest.approx(17.0)
    assert m.max_drawdown_pct == pytest.approx(25.0)
    assert m.bars == 7


def test_drawdown_seeded_with_start_equity() -> None:
    m = compute_metrics(_curve([95, 100]), [], start_equity=100, timeframe_ms=HOUR)
    assert m.max_drawdown_pct == pytest.approx(5.0)
    assert drawdown_curve(_curve([95, 100]), 100) == [(0, pytest.approx(-5.0)), (HOUR, 0.0)]


def test_trade_statistics() -> None:
    trades = [_trade(10, 5.0), _trade(-4, -2.0), _trade(6, 3.0), _trade(-1, -0.5)]
    m = compute_metrics(_curve([100, 111]), trades, start_equity=100, timeframe_ms=HOUR)
    assert m.num_trades == 4
    assert m.win_rate_pct == pytest.approx(50.0)
    assert m.profit_factor == pytest.approx(16 / 5)
    assert m.avg_trade_pct == pytest.approx((5.0 - 2.0 + 3.0 - 0.5) / 4)
    assert m.best_trade_pct == 5.0
    assert m.worst_trade_pct == -2.0


def test_profit_factor_none_without_losses() -> None:
    m = compute_metrics(_curve([100, 101]), [_trade(1, 1.0)], start_equity=100, timeframe_ms=HOUR)
    assert m.profit_factor is None
    assert m.win_rate_pct == 100.0


def test_zero_std_gives_zero_sharpe_and_sortino() -> None:
    flat = compute_metrics(_curve([100] * 10), [], start_equity=100, timeframe_ms=HOUR)
    assert flat.sharpe == 0.0 and flat.sortino == 0.0
    # constant positive growth: std 0 and no downside -> both 0.0 by contract
    growth = compute_metrics(_curve([100 * 1.01**i for i in range(10)]), [], start_equity=100, timeframe_ms=HOUR)
    assert growth.sharpe == 0.0 and growth.sortino == 0.0


def test_too_few_points_gives_zero_ratios() -> None:
    m = compute_metrics(_curve([100, 120]), [], start_equity=100, timeframe_ms=HOUR)
    assert m.sharpe == 0.0 and m.sortino == 0.0


def test_sharpe_and_sortino_hand_computed() -> None:
    # returns: +10 %, -10 %, +10 %  -> mean 1/30, sample std sqrt(1/75), downside dev sqrt(1/300)
    curve = _curve([100, 110, 99, 108.9], step=86_400_000)
    m = compute_metrics(curve, [], start_equity=100, timeframe_ms=86_400_000)
    ppy = 365.0
    mean = (0.1 - 0.1 + 0.1) / 3
    std = math.sqrt(sum((r - mean) ** 2 for r in (0.1, -0.1, 0.1)) / 2)
    dd = math.sqrt((0.1**2) / 3)
    assert m.sharpe == pytest.approx(mean / std * math.sqrt(ppy))
    assert m.sortino == pytest.approx(mean / dd * math.sqrt(ppy))
    assert sharpe_ratio([0.1, -0.1, 0.1], ppy) == pytest.approx(m.sharpe)
    assert sortino_ratio([0.1, -0.1, 0.1], ppy) == pytest.approx(m.sortino)


def test_periods_per_year() -> None:
    assert periods_per_year(HOUR) == pytest.approx(8760)
    assert periods_per_year(5 * 60_000) == pytest.approx(105_120)
    with pytest.raises(ValueError):
        periods_per_year(0)


def test_buy_and_hold() -> None:
    m = compute_metrics(_curve([100, 100]), [], 100, HOUR, first_price=200, last_price=250)
    assert m.buy_and_hold_return_pct == pytest.approx(25.0)
    assert compute_metrics(_curve([100, 100]), [], 100, HOUR).buy_and_hold_return_pct is None


def test_empty_curve_gives_zeros() -> None:
    m = compute_metrics([], [], start_equity=1000, timeframe_ms=HOUR)
    assert m.end_equity == 1000
    assert m.total_return_pct == 0.0 and m.max_drawdown_pct == 0.0
    assert m.sharpe == 0.0 and m.sortino == 0.0 and m.bars == 0 and m.num_trades == 0
    assert m.win_rate_pct == 0.0 and m.profit_factor is None
    assert m.exposure_pct is None


def test_pass_through_fields_and_exposure() -> None:
    curve = _curve([100, 100, 100, 100])  # 4 bars -> period 4h
    trades = [_trade(1, 1, entry=0, exit_=HOUR), _trade(1, 1, entry=HOUR // 2, exit_=2 * HOUR)]
    m = compute_metrics(curve, trades, 100, HOUR, llm_calls=12, llm_cost_usd=0.05, fees_paid=1.5)
    assert (m.llm_calls, m.llm_cost_usd, m.fees_paid) == (12, 0.05, 1.5)
    assert m.exposure_pct == pytest.approx(50.0)  # union [0, 2h] of 4h
    override = compute_metrics(curve, trades, 100, HOUR, exposure_pct=10.0)
    assert override.exposure_pct == 10.0
    no_entry = compute_metrics(curve, [_trade(1, 1, entry=None)], 100, HOUR)
    assert no_entry.exposure_pct is None


def test_formatting_helpers() -> None:
    assert format_number(1234.5) == "1.234,50"
    assert format_number(-0.001) == "0,00"
    assert format_number(2.5, 1, signed=True) == "+2,5"
    assert format_number(-1234567.891, 2) == "-1.234.567,89"
    assert format_pct(None) == "n/d"
    assert format_pct(-3.456, 1) == "-3,5 %"


def test_render_text_summary_aligned() -> None:
    m = PerformanceMetrics(
        start_equity=1000, end_equity=1123.45, total_return_pct=12.345, buy_and_hold_return_pct=5.1,
        max_drawdown_pct=4.2, sharpe=1.234, sortino=1.8, num_trades=12, win_rate_pct=58.33,
        profit_factor=None, llm_calls=150, llm_cost_usd=0.1234, bars=2016,
    )
    text = render_text_summary(m)
    lines = text.splitlines()
    assert lines[0] == "Resumen de rendimiento"
    body = lines[2:]
    assert len({len(line) for line in body}) == 1  # values right-aligned to one column
    joined = "\n".join(body)
    for label in ("Retorno total", "Máx. drawdown", "Sharpe", "Tasa de acierto", "Coste LLM (USD)"):
        assert label in joined
    assert "+12,35 %" in text and "1.123,45" in text and "n/d (sin pérdidas)" in text
