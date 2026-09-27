from __future__ import annotations

import sqlite3

import pytest

from jev.journal import REDACTED, Journal
from jev.models import (
    Action,
    Decision,
    Fill,
    IndicatorSet,
    MarketSnapshot,
    OrderRequest,
    RiskVerdict,
    Side,
    TradeRecord,
)


def _snapshot(ts: int = 1_700_000_000_000, price: float = 100.0) -> MarketSnapshot:
    return MarketSnapshot(symbol="BTC/USDT", timeframe="5m", timestamp=ts, price=price, indicators=IndicatorSet())


def _decision(action: Action = Action.BUY, source: str = "claude", cost: float = 0.001, latency: float | None = 250.0) -> Decision:
    return Decision(
        action=action,
        confidence=0.8,
        size_pct=0.2,
        reasoning="momentum up",
        source=source,
        model="claude-haiku-4-5" if source == "claude" else None,
        latency_ms=latency,
        input_tokens=500 if source == "claude" else 0,
        output_tokens=80 if source == "claude" else 0,
        cost_usd=cost,
    )


def _trade(pnl: float, exit_time: int, reason: str = "take_profit") -> TradeRecord:
    return TradeRecord(
        symbol="BTC/USDT", entry_price=100.0, exit_price=100.0 + pnl, quantity=1.0, pnl=pnl,
        pnl_pct=pnl, fees=0.2, entry_time=exit_time - 60_000, exit_time=exit_time, exit_reason=reason,
    )


@pytest.fixture()
def journal():
    with Journal(":memory:") as j:
        yield j


def test_decision_round_trip_with_verdict(journal: Journal) -> None:
    order = OrderRequest(symbol="BTC/USDT", side=Side.BUY, quantity=0.1, reference_price=100.0)
    verdict = RiskVerdict(approved=False, order=order, reasons=["cooldown", "max trades"])
    journal.record_decision(_snapshot(), _decision(), verdict)
    journal.record_decision(_snapshot(ts=1_700_000_300_000), _decision(Action.HOLD, "rules", 0.0, None), None)

    rows = journal.recent_decisions()
    assert [r["ts"] for r in rows] == [1_700_000_300_000, 1_700_000_000_000]  # newest first
    latest, first = rows
    assert latest["action"] == "HOLD" and latest["approved"] is None and latest["risk_reasons"] == []
    assert first["action"] == "BUY"
    assert first["approved"] is False
    assert first["risk_reasons"] == ["cooldown", "max trades"]
    assert first["model"] == "claude-haiku-4-5"
    assert first["input_tokens"] == 500 and first["output_tokens"] == 80
    assert first["size_pct"] == pytest.approx(0.2)
    assert first["price"] == pytest.approx(100.0)
    assert journal.recent_decisions(limit=1)[0]["ts"] == 1_700_000_300_000


def test_fill_trade_equity_event_round_trip(journal: Journal) -> None:
    fill = Fill(order_id="o-1", symbol="BTC/USDT", side=Side.SELL, quantity=0.5, price=101.0, fee=0.05, timestamp=5)
    journal.record_fill(fill, reason="stop_loss")
    journal.record_trade(_trade(2.0, exit_time=2_000))
    journal.record_trade(_trade(-1.0, exit_time=1_000, reason="stop_loss"))
    journal.record_equity(2, 1010.0, 101.0)
    journal.record_equity(1, 1000.0, 100.0)
    journal.record_event(3, "kill_switch", "daily loss limit")

    fills = journal.fills()
    assert fills == [{"ts": 5, "order_id": "o-1", "symbol": "BTC/USDT", "side": "sell", "quantity": 0.5,
                      "price": 101.0, "fee": 0.05, "reason": "stop_loss"}]
    trades = journal.trades()
    assert [t.exit_time for t in trades] == [1_000, 2_000]  # ordered by exit time
    assert trades[1] == _trade(2.0, exit_time=2_000)
    assert journal.equity_curve() == [(1, 1000.0), (2, 1010.0)]
    assert journal.recent_events() == [{"ts": 3, "kind": "kill_switch", "message": "daily loss limit"}]
    assert journal.total_fees() == pytest.approx(0.05)


def test_state_upsert_and_missing_key(journal: Journal) -> None:
    assert journal.load_state("portfolio") is None
    journal.save_state("portfolio", {"cash": 1000.0, "base_qty": 0.0}, ts=1)
    journal.save_state("portfolio", {"cash": 900.0, "base_qty": 0.001, "stops": [1, 2]}, ts=2)
    assert journal.load_state("portfolio") == {"cash": 900.0, "base_qty": 0.001, "stops": [1, 2]}
    rows = journal.conn.execute("SELECT COUNT(*), MAX(updated_ts) FROM state").fetchone()
    assert tuple(rows) == (1, 2)


def test_state_never_stores_secrets(journal: Journal) -> None:
    journal.save_state("cfg", {"api_key": "abc", "nested": {"API_SECRET": "xyz"}, "llm_max_tokens": 400})
    loaded = journal.load_state("cfg")
    assert loaded == {"api_key": REDACTED, "nested": {"API_SECRET": REDACTED}, "llm_max_tokens": 400}
    raw = journal.conn.execute("SELECT value FROM state").fetchone()[0]
    assert "abc" not in raw and "xyz" not in raw


def test_api_keys_scrubbed_from_free_text(journal: Journal) -> None:
    journal.record_event(1, "error", "auth failed for sk-ant-api03-SECRETSECRET123")
    assert "SECRETSECRET" not in journal.recent_events()[0]["message"]


def test_state_rejects_non_dict(journal: Journal) -> None:
    with pytest.raises(TypeError):
        journal.save_state("x", ["not", "a", "dict"])  # type: ignore[arg-type]


def test_llm_cost_sums(journal: Journal) -> None:
    assert journal.llm_cost_usd() == 0.0
    journal.record_decision(_snapshot(ts=1_000), _decision(cost=0.002), None)
    journal.record_decision(_snapshot(ts=2_000), _decision(cost=0.003), None)
    journal.record_decision(_snapshot(ts=3_000), _decision(cost=0.005), None)
    assert journal.llm_cost_usd() == pytest.approx(0.010)
    assert journal.llm_cost_usd(since_ts=2_000) == pytest.approx(0.008)
    assert journal.llm_cost_usd(since_ts=10_000) == 0.0


def test_summary(journal: Journal) -> None:
    approved = RiskVerdict(approved=True)
    rejected = RiskVerdict(approved=False, reasons=["low confidence"])
    journal.record_decision(_snapshot(), _decision(Action.BUY, "claude", 0.004, 200.0), approved)
    journal.record_decision(_snapshot(), _decision(Action.SELL, "claude", 0.006, 400.0), rejected)
    journal.record_decision(_snapshot(), _decision(Action.HOLD, "rules", 0.0, None), None)
    journal.record_fill(Fill(order_id="a", symbol="BTC/USDT", side=Side.BUY, quantity=1, price=100, fee=0.1, timestamp=1))
    journal.record_trade(_trade(3.0, exit_time=10))
    journal.record_trade(_trade(-1.0, exit_time=20))

    s = journal.summary()
    assert s["decisions"] == 3
    assert s["actions"] == {"BUY": 1, "SELL": 1, "HOLD": 1}
    assert s["sources"] == {"claude": 2, "rules": 1}
    assert s["approved"] == 1
    assert s["llm_calls"] == 2
    assert s["llm_cost_usd"] == pytest.approx(0.010)
    assert s["avg_latency_ms"] == pytest.approx(300.0)
    assert s["fills"] == 1 and s["fees_paid"] == pytest.approx(0.1)
    assert s["trades"] == 2 and s["winning_trades"] == 1
    assert s["realized_pnl"] == pytest.approx(2.0)


def test_empty_summary(journal: Journal) -> None:
    s = journal.summary()
    assert s["decisions"] == 0 and s["actions"] == {"BUY": 0, "SELL": 0, "HOLD": 0}
    assert s["avg_latency_ms"] is None and s["realized_pnl"] == 0.0 and s["llm_cost_usd"] == 0.0


def test_file_persistence_and_idempotent_schema(tmp_path) -> None:
    path = tmp_path / "nested" / "dir" / "journal.sqlite3"
    with Journal(str(path)) as j:
        j.record_equity(1, 1000.0, 100.0)
        j.save_state("k", {"v": 1})
    with Journal(str(path)) as j:  # re-open: schema creation must not fail or wipe data
        assert j.equity_curve() == [(1, 1000.0)]
        assert j.load_state("k") == {"v": 1}


def test_close_is_idempotent_and_blocks_use() -> None:
    j = Journal(":memory:")
    j.close()
    j.close()
    with pytest.raises(RuntimeError):
        j.equity_curve()


def test_parameterized_queries_resist_injection(journal: Journal) -> None:
    evil = "x'); DROP TABLE events; --"
    journal.record_event(1, evil, evil)
    journal.save_state(evil, {"a": 1})
    assert journal.recent_events()[0]["kind"] == evil
    assert journal.load_state(evil) == {"a": 1}
    tables = {r[0] for r in journal.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"decisions", "fills", "trades", "equity", "events", "state"} <= tables
    assert isinstance(journal.conn, sqlite3.Connection)
