"""Tests for `jev report`: forward-test report from a journal opened read-only."""

from __future__ import annotations

import sqlite3

import pytest

from jev.cli import main
from jev.forward_report import (MIN_TRADES, ReportUsageError, build_forward_report, count_gaps,
                                exposure_from_fills, remove_flows)
from jev.journal import Journal
from jev.models import Action, Decision, Fill, IndicatorSet, MarketSnapshot, RiskVerdict, Side, TradeRecord

HOUR = 3_600_000
T0 = 1_767_225_600_000  # 2026-01-01T00:00Z


def snapshot(ts: int, price: float) -> MarketSnapshot:
    return MarketSnapshot(symbol="BTC/USDT", timeframe="1h", timestamp=ts, price=price,
                          indicators=IndicatorSet(), recent_closes=[price])


def make_journal(path, *, hours=48, gap_after=None, gap_hours=0, state_keys=("engine:paper:binance:BTC/USDT:1h",)):
    with Journal(str(path)) as j:
        ts, equity, price = T0, 1000.0, 60000.0
        for i in range(hours):
            if gap_after is not None and i == gap_after:
                ts += gap_hours * HOUR
            price *= 1.001
            equity += 0.5 if i % 3 else -0.4
            j.record_equity(ts, equity, price)
            j.record_decision(snapshot(ts, price),
                              Decision(action=Action.HOLD, source="hybrid", model="jev-latest",
                                       input_tokens=1200, cost_usd=0.00005, latency_ms=300.0),
                              RiskVerdict(approved=False, reasons=["hold"]))
            ts += HOUR
        j.record_fill(Fill(order_id="paper-1", symbol="BTC/USDT", side=Side.BUY, quantity=0.004,
                           price=60100.0, fee=0.24, timestamp=T0 + 2 * HOUR), "entry")
        j.record_trade(TradeRecord(symbol="BTC/USDT", entry_price=60100.0, exit_price=59550.0, quantity=0.004,
                                   pnl=-2.19, pnl_pct=-0.91, fees=0.48, entry_time=T0 + 2 * HOUR,
                                   exit_time=T0 + 9 * HOUR, exit_reason="stop_loss"))
        for key in state_keys:
            j.save_state(key, {"qty": 0.0}, ts=T0)
    return path


def test_report_from_paper_journal(tmp_path):
    path = make_journal(tmp_path / "paper.sqlite3")
    with Journal(str(path), read_only=True) as j:
        report = build_forward_report(j)
    assert (report.symbol, report.timeframe, report.gaps) == ("BTC/USDT", "1h", 0)
    assert report.metrics.num_trades == 1
    assert report.metrics.llm_calls == 48
    assert report.metrics.llm_cost_usd == pytest.approx(48 * 0.00005)
    assert report.metrics.fees_paid == pytest.approx(0.24)
    assert any("Muestra chica" in w and str(MIN_TRADES) in w for w in report.warnings)
    assert "forward test BTC/USDT 1h" in report.html and "forward (decisiones en tiempo real" in report.html


def test_gaps_are_detected_and_reported(tmp_path):
    assert count_gaps([(0, 1.0), (HOUR, 1.0), (10 * HOUR, 1.0)], HOUR) == (1, 8.0, 0)
    path = make_journal(tmp_path / "gap.sqlite3", gap_after=20, gap_hours=24)
    with Journal(str(path), read_only=True) as j:
        report = build_forward_report(j)
    assert report.gaps == 1
    assert any("detenido 1 vez" in w for w in report.warnings)


def test_mixed_sessions_need_an_explicit_timeframe_and_are_flagged(tmp_path):
    path = make_journal(tmp_path / "mixed.sqlite3",
                        state_keys=("engine:paper:binance:BTC/USDT:1h", "engine:paper:binance:BTC/USDT:5m"))
    with Journal(str(path), read_only=True) as j:
        with pytest.raises(ValueError, match="--timeframe"):
            build_forward_report(j)
        report = build_forward_report(j, timeframe="1h")
    assert any("mezcla varias sesiones" in w for w in report.warnings)


def test_read_only_journal_never_writes(tmp_path):
    path = make_journal(tmp_path / "ro.sqlite3")
    with Journal(str(path), read_only=True) as j:
        with pytest.raises(sqlite3.OperationalError):
            j.record_event(T0, "x", "must not be written")
    with pytest.raises(FileNotFoundError):
        Journal(str(tmp_path / "missing.sqlite3"), read_only=True)
    assert not (tmp_path / "missing.sqlite3").exists()


def test_empty_journal_is_a_clear_error(tmp_path):
    with Journal(str(tmp_path / "empty.sqlite3")):
        pass
    with Journal(str(tmp_path / "empty.sqlite3"), read_only=True) as j:
        with pytest.raises(ValueError, match="puntos de equity"):
            build_forward_report(j)


def test_cli_report_writes_html_and_leaves_the_journal_untouched(tmp_path, capsys):
    path = make_journal(tmp_path / "jev_paper_1h.sqlite3")
    before = path.read_bytes()
    out = tmp_path / "out" / "fwd.html"
    assert main(["report", "--journal", str(path), "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert out.is_file() and "Informe HTML" in printed and "Muestra chica" in printed
    assert path.read_bytes() == before


def test_cli_report_missing_journal_is_a_usage_error(tmp_path, capsys):
    assert main(["report", "--journal", str(tmp_path / "nope.sqlite3")]) == 2
    assert "no existe el journal" in capsys.readouterr().err


# --- regressions from the adversarial review ---------------------------------------------


def test_reads_come_from_one_snapshot_even_if_the_bot_writes_mid_report(tmp_path):
    path = make_journal(tmp_path / "live.sqlite3")
    writer = Journal(str(path))  # the running bot keeps its WAL connection open

    class Racing(Journal):
        def equity_curve(self):
            curve = super().equity_curve()
            writer.record_equity(T0 + 500 * HOUR, 900.0, 50000.0)  # a tick lands between reads
            writer.record_trade(TradeRecord(symbol="BTC/USDT", entry_price=1.0, exit_price=0.5, quantity=1.0,
                                            pnl=-48.0, pnl_pct=-50.0, fees=0.1, entry_time=T0,
                                            exit_time=T0 + 500 * HOUR, exit_reason="stop_loss"))
            return curve

    with Racing(str(path), read_only=True) as j:
        report = build_forward_report(j)
    writer.close()
    assert report.metrics.bars == 48 and report.metrics.num_trades == 1  # the later tick is not half-read


def test_read_only_open_of_a_stopped_journal_creates_no_side_files(tmp_path):
    path = make_journal(tmp_path / "stopped.sqlite3")
    assert sorted(f.name for f in tmp_path.iterdir()) == ["stopped.sqlite3"]
    with Journal(str(path), read_only=True) as j:
        build_forward_report(j)
    assert sorted(f.name for f in tmp_path.iterdir()) == ["stopped.sqlite3"]


def test_sessions_with_the_same_timeframe_are_still_flagged(tmp_path):
    path = make_journal(tmp_path / "same_tf.sqlite3", state_keys=(
        "engine:paper:synthetic:BTC/USDT:1h", "engine:paper:binance:BTC/USDT:1h"))
    with Journal(str(path), read_only=True) as j:
        report = build_forward_report(j)
    assert any("mezcla varias sesiones" in w for w in report.warnings)
    assert any("Datos sintéticos" in w for w in report.warnings)
    assert "simulación" in report.html


def test_overlapping_runs_are_flagged(tmp_path):
    path = make_journal(tmp_path / "rerun.sqlite3")
    with Journal(str(path)) as j:  # a second run over the same simulated period
        for i in range(5):
            j.record_equity(T0 + i * HOUR, 1000.0, 60000.0)
    with Journal(str(path), read_only=True) as j:
        report = build_forward_report(j)
    assert report.overlaps > 0
    assert any("superpuestos" in w for w in report.warnings)


def test_flags_must_match_the_journal(tmp_path, capsys):
    path = make_journal(tmp_path / "flags.sqlite3")
    with Journal(str(path), read_only=True) as j:
        with pytest.raises(ReportUsageError, match="no de 5m"):
            build_forward_report(j, timeframe="5m")
        with pytest.raises(ReportUsageError, match="no ETH/USDT"):
            build_forward_report(j, symbol="ETH/USDT")
    assert main(["report", "--journal", str(path), "--timeframe", "5m"]) == 2
    mixed = make_journal(tmp_path / "ambiguous.sqlite3", state_keys=(
        "engine:paper:binance:BTC/USDT:1h", "engine:paper:binance:BTC/USDT:5m"))
    assert main(["report", "--journal", str(mixed)]) == 2
    assert "--timeframe" in capsys.readouterr().err


def test_external_flows_are_not_return():
    curve = [(0, 1000.0), (HOUR, 1010.0), (2 * HOUR, 900.0), (3 * HOUR, 905.0)]
    assert remove_flows(curve, [(2 * HOUR, -110.0)]) == [(0, 1000.0), (HOUR, 1010.0), (2 * HOUR, 1010.0),
                                                          (3 * HOUR, 1015.0)]


def test_external_flow_events_are_removed_and_reported(tmp_path):
    path = make_journal(tmp_path / "flow.sqlite3", state_keys=("engine:live:testnet:binance:BTC/USDT:1h",))
    with Journal(str(path)) as j:
        last = j.equity_curve()[-1]
        j.record_event(last[0] + HOUR, "external_flow", "-110.00 USDT moved outside the bot (withdrawal)")
        j.record_equity(last[0] + HOUR, last[1] - 110.0, 60000.0)
    with Journal(str(path), read_only=True) as j:
        report = build_forward_report(j)
    assert report.metrics.end_equity == pytest.approx(last[1])
    assert any("movimiento(s) externo(s)" in w for w in report.warnings)
    assert any("Sesión live" in w for w in report.warnings)


def test_exposure_counts_a_position_still_open():
    buy = {"ts": 2 * HOUR, "side": "buy", "quantity": 0.01}
    sell = {"ts": 4 * HOUR, "side": "sell", "quantity": 0.01}
    assert exposure_from_fills([buy, sell], 0, 10 * HOUR) == pytest.approx(20.0)
    assert exposure_from_fills([buy], 0, 10 * HOUR) == pytest.approx(80.0)
    assert exposure_from_fills([], 0, 10 * HOUR) == 0.0
