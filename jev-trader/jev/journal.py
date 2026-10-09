"""SQLite journal: decisions, fills, trades, equity, events and small key/value state.

Everything the bot does is appended here so runs can be audited, resumed and reported.
Only stdlib ``sqlite3`` is used; every query is parameterized and every write commits.
Secrets are never persisted: state keys that look like credentials are redacted and
Anthropic-style API keys are scrubbed from free text.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Any, Iterator

from .models import Action, Decision, Fill, MarketSnapshot, RiskVerdict, TradeRecord

logger = logging.getLogger(__name__)

REDACTED = "[REDACTED]"
_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|api[_-]?secret|secret|password|passphrase|private[_-]?key|auth[_-]?token)",
    re.IGNORECASE,
)
_SECRET_TEXT_RE = re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    price REAL NOT NULL,
    action TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0,
    size_pct REAL NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT '',
    model TEXT,
    latency_ms REAL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    reasoning TEXT NOT NULL DEFAULT '',
    approved INTEGER,
    risk_reasons TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    order_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity REAL NOT NULL,
    price REAL NOT NULL,
    fee REAL NOT NULL,
    reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_fills_ts ON fills(ts);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    quantity REAL NOT NULL,
    pnl REAL NOT NULL,
    pnl_pct REAL NOT NULL,
    fees REAL NOT NULL,
    entry_time INTEGER,
    exit_time INTEGER NOT NULL,
    exit_reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_trades_exit ON trades(exit_time);
CREATE TABLE IF NOT EXISTS equity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    equity REAL NOT NULL,
    price REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity(ts);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_ts INTEGER NOT NULL
);
"""

_DECISION_COLUMNS = (
    "id", "ts", "symbol", "price", "action", "confidence", "size_pct", "source", "model",
    "latency_ms", "input_tokens", "output_tokens", "cost_usd", "reasoning", "approved",
    "risk_reasons",
)
_TRADE_COLUMNS = (
    "symbol", "entry_price", "exit_price", "quantity", "pnl", "pnl_pct", "fees",
    "entry_time", "exit_time", "exit_reason",
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def scrub_text(text: str) -> str:
    """Remove anything that looks like an Anthropic API key from free text."""
    return _SECRET_TEXT_RE.sub(REDACTED, text or "")


def redact_secrets(value: Any) -> Any:
    """Recursively replace values whose key looks like a credential."""
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and _SECRET_KEY_RE.search(key):
                out[key] = REDACTED
            else:
                out[key] = redact_secrets(item)
        return out
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, str):
        return scrub_text(value)
    return value


class Journal:
    """Append-only trading journal backed by SQLite (``":memory:"`` supported)."""

    def __init__(self, path: str = "jev_journal.sqlite3", *, read_only: bool = False) -> None:
        """``read_only=True`` opens an existing journal without ever writing to it (safe while
        a bot is running on it): no schema creation, no WAL switch, ``query_only`` on."""
        self.path = str(path)
        self.read_only = read_only
        self._lock = threading.RLock()
        if read_only:
            if self.path == ":memory:" or not Path(self.path).is_file():
                raise FileNotFoundError(f"no existe el journal {self.path}")
            uri = f"{Path(self.path).resolve().as_uri()}?mode=ro"
            if not Path(self.path + "-wal").exists():
                # No live writer: immutable creates no -wal/-shm side files, which another user's
                # files could otherwise leave behind and block the bot's next writes.
                uri += "&immutable=1"
            self._conn: sqlite3.Connection | None = sqlite3.connect(uri, uri=True, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA query_only=ON")
            return
        if self.path != ":memory:" and not self.path.startswith("file:"):
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self._try_pragma("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- lifecycle -----------------------------------------------------------

    @contextmanager
    def snapshot(self) -> Iterator[None]:
        """Run several reads against one consistent snapshot, even while a bot is writing."""
        with self._lock:
            self.conn.execute("BEGIN")
            try:
                yield
            finally:
                self.conn.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> "Journal":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Journal is closed")
        return self._conn

    def _try_pragma(self, sql: str) -> None:
        try:
            self.conn.execute(sql)
        except sqlite3.DatabaseError as exc:  # pragma: no cover - platform dependent
            logger.debug("pragma %s failed: %s", sql, exc)

    def _write(self, sql: str, params: tuple[Any, ...]) -> int:
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return int(cur.lastrowid or 0)

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    # -- writes --------------------------------------------------------------

    def record_decision(
        self, snapshot: MarketSnapshot, decision: Decision, verdict: RiskVerdict | None
    ) -> int:
        """Store one engine decision and (optionally) the risk verdict. Returns the row id."""
        approved = None if verdict is None else int(verdict.approved)
        reasons = [] if verdict is None else [scrub_text(r) for r in verdict.reasons]
        return self._write(
            "INSERT INTO decisions (ts, symbol, price, action, confidence, size_pct, source, "
            "model, latency_ms, input_tokens, output_tokens, cost_usd, reasoning, approved, "
            "risk_reasons) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                snapshot.timestamp,
                snapshot.symbol,
                snapshot.price,
                decision.action.value,
                decision.confidence,
                decision.size_pct,
                decision.source,
                decision.model,
                decision.latency_ms,
                decision.input_tokens,
                decision.output_tokens,
                decision.cost_usd,
                scrub_text(decision.reasoning),
                approved,
                json.dumps(reasons, ensure_ascii=False),
            ),
        )

    def record_fill(self, fill: Fill, reason: str = "") -> int:
        return self._write(
            "INSERT INTO fills (ts, order_id, symbol, side, quantity, price, fee, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                fill.timestamp,
                fill.order_id,
                fill.symbol,
                fill.side.value,
                fill.quantity,
                fill.price,
                fill.fee,
                scrub_text(reason),
            ),
        )

    def record_trade(self, trade: TradeRecord) -> int:
        placeholders = ", ".join("?" for _ in _TRADE_COLUMNS)
        values = tuple(getattr(trade, col) for col in _TRADE_COLUMNS)
        values = values[:-1] + (scrub_text(trade.exit_reason),)
        return self._write(
            f"INSERT INTO trades ({', '.join(_TRADE_COLUMNS)}) VALUES ({placeholders})", values
        )

    def record_equity(self, ts: int, equity: float, price: float) -> int:
        return self._write(
            "INSERT INTO equity (ts, equity, price) VALUES (?, ?, ?)",
            (int(ts), float(equity), float(price)),
        )

    def record_event(self, ts: int, kind: str, message: str) -> int:
        return self._write(
            "INSERT INTO events (ts, kind, message) VALUES (?, ?, ?)",
            (int(ts), kind, scrub_text(message)),
        )

    def save_state(self, key: str, value: dict, ts: int | None = None) -> None:
        """Upsert a JSON-serializable dict. Credential-like keys are redacted."""
        if not isinstance(value, dict):
            raise TypeError("state value must be a dict")
        clean = redact_secrets(value)
        if clean != value:
            logger.warning("state %r contained secret-looking fields; they were redacted", key)
        payload = json.dumps(clean, ensure_ascii=False, sort_keys=True)
        self._write(
            "INSERT INTO state (key, value, updated_ts) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_ts = excluded.updated_ts",
            (key, payload, _now_ms() if ts is None else int(ts)),
        )

    # -- reads ---------------------------------------------------------------

    def load_state(self, key: str) -> dict | None:
        rows = self._query("SELECT value FROM state WHERE key = ?", (key,))
        if not rows:
            return None
        value = json.loads(rows[0]["value"])
        return value if isinstance(value, dict) else None

    def state_keys(self, prefix: str = "") -> list[str]:
        """Saved state keys starting with ``prefix``, sorted."""
        rows = self._query("SELECT key FROM state ORDER BY key")
        return [row["key"] for row in rows if row["key"].startswith(prefix)]

    def recent_decisions(self, limit: int = 20) -> list[dict]:
        """Newest first. ``approved`` is None when no risk verdict was recorded."""
        rows = self._query(
            f"SELECT {', '.join(_DECISION_COLUMNS)} FROM decisions ORDER BY ts DESC, id DESC LIMIT ?",
            (max(0, int(limit)),),
        )
        return [self._decision_row(row) for row in rows]

    @staticmethod
    def _decision_row(row: sqlite3.Row) -> dict:
        out = dict(row)
        out["approved"] = None if out["approved"] is None else bool(out["approved"])
        out["risk_reasons"] = json.loads(out["risk_reasons"] or "[]")
        return out

    def trades(self) -> list[TradeRecord]:
        rows = self._query(
            f"SELECT {', '.join(_TRADE_COLUMNS)} FROM trades ORDER BY exit_time, id"
        )
        return [TradeRecord(**dict(row)) for row in rows]

    def fills(self) -> list[dict]:
        """All fills, oldest first, as dicts (includes ``reason``)."""
        rows = self._query(
            "SELECT ts, order_id, symbol, side, quantity, price, fee, reason FROM fills ORDER BY ts, id"
        )
        return [dict(row) for row in rows]

    def events(self, kind: str | None = None) -> list[dict]:
        """All events (optionally of one kind), oldest first."""
        if kind is None:
            rows = self._query("SELECT ts, kind, message FROM events ORDER BY ts, id")
        else:
            rows = self._query("SELECT ts, kind, message FROM events WHERE kind = ? ORDER BY ts, id", (kind,))
        return [dict(row) for row in rows]

    def recent_events(self, limit: int = 20) -> list[dict]:
        rows = self._query(
            "SELECT ts, kind, message FROM events ORDER BY ts DESC, id DESC LIMIT ?",
            (max(0, int(limit)),),
        )
        return [dict(row) for row in rows]

    def equity_curve(self) -> list[tuple[int, float]]:
        rows = self._query("SELECT ts, equity FROM equity ORDER BY ts, id")
        return [(int(row["ts"]), float(row["equity"])) for row in rows]

    def price_curve(self) -> list[tuple[int, float]]:
        rows = self._query("SELECT ts, price FROM equity ORDER BY ts, id")
        return [(int(row["ts"]), float(row["price"])) for row in rows]

    def symbols(self) -> list[str]:
        """Symbols seen in decisions, most recent first."""
        rows = self._query("SELECT symbol, MAX(ts) AS last FROM decisions GROUP BY symbol ORDER BY last DESC")
        return [str(row["symbol"]) for row in rows]

    def llm_cost_usd(self, since_ts: int | None = None) -> float:
        """Sum of decision ``cost_usd`` (optionally for decisions with ``ts >= since_ts``)."""
        if since_ts is None:
            rows = self._query("SELECT COALESCE(SUM(cost_usd), 0) AS c FROM decisions")
        else:
            rows = self._query(
                "SELECT COALESCE(SUM(cost_usd), 0) AS c FROM decisions WHERE ts >= ?",
                (int(since_ts),),
            )
        return float(rows[0]["c"])

    def total_fees(self) -> float:
        rows = self._query("SELECT COALESCE(SUM(fee), 0) AS f FROM fills")
        return float(rows[0]["f"])

    def summary(self) -> dict:
        """Aggregate counts and totals for status screens and reports."""
        actions = {a.value: 0 for a in Action}
        for row in self._query("SELECT action, COUNT(*) AS n FROM decisions GROUP BY action"):
            actions[row["action"]] = int(row["n"])
        sources = {
            row["source"]: int(row["n"])
            for row in self._query(
                "SELECT source, COUNT(*) AS n FROM decisions GROUP BY source ORDER BY source"
            )
        }
        agg = self._query(
            "SELECT COUNT(*) AS n, COALESCE(SUM(approved = 1), 0) AS approved, "
            "COALESCE(SUM(cost_usd), 0) AS cost, AVG(latency_ms) AS latency, "
            "COALESCE(SUM(input_tokens + output_tokens > 0), 0) AS llm_calls FROM decisions"
        )[0]
        fills = self._query("SELECT COUNT(*) AS n, COALESCE(SUM(fee), 0) AS fees FROM fills")[0]
        trades = self._query(
            "SELECT COUNT(*) AS n, COALESCE(SUM(pnl), 0) AS pnl, "
            "COALESCE(SUM(pnl > 0), 0) AS wins FROM trades"
        )[0]
        events = self._query("SELECT COUNT(*) AS n FROM events")[0]
        return {
            "decisions": int(agg["n"]),
            "actions": actions,
            "sources": sources,
            "approved": int(agg["approved"]),
            "llm_calls": int(agg["llm_calls"]),
            "llm_cost_usd": float(agg["cost"]),
            "avg_latency_ms": None if agg["latency"] is None else float(agg["latency"]),
            "fills": int(fills["n"]),
            "fees_paid": float(fills["fees"]),
            "trades": int(trades["n"]),
            "winning_trades": int(trades["wins"]),
            "realized_pnl": float(trades["pnl"]),
            "events": int(events["n"]),
        }
