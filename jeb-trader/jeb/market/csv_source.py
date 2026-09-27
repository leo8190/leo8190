"""CSV candle files: load, save and replay as a ``MarketDataSource``.

Format: header ``timestamp,open,high,low,close,volume`` (case-insensitive, extra
columns ignored). ``timestamp`` is the candle OPEN time as integer ms UTC or an
ISO-8601 string (naive strings are taken as UTC). Files are written with int ms.
"""

from __future__ import annotations

import csv
import logging
import os
import tempfile
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TextIO

from ..models import Candle, MarketDataError
from .timeframes import dedupe_candles, make_candle, timeframe_to_ms

logger = logging.getLogger(__name__)

COLUMNS = ("timestamp", "open", "high", "low", "close", "volume")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def parse_timestamp(value: str) -> int:
    """Parse an int-ms or ISO-8601 timestamp string into int ms UTC (ValueError if bad)."""
    text = value.strip()
    if not text:
        raise ValueError("empty timestamp")
    try:
        ms = int(text)
    except ValueError:
        try:
            number = float(text)
        except ValueError:
            return _iso_to_ms(text)
        if not number.is_integer():
            raise ValueError(f"timestamp must be an integer of ms, got {value!r}") from None
        ms = int(number)
    if ms < 0:
        raise ValueError(f"timestamp must be >= 0, got {value!r}")
    return ms


def load_candles(path: str | os.PathLike[str]) -> list[Candle]:
    """Load candles from a CSV file: sorted by time, duplicate timestamps dropped (last wins).

    Raises MarketDataError for unreadable files, missing columns or malformed rows
    (the message names the offending line).
    """
    p = Path(path)
    try:
        with p.open("r", encoding="utf-8-sig", newline="") as fh:
            candles = _read_rows(fh, p)
    except OSError as exc:
        raise MarketDataError(f"cannot read candle file {p}: {exc}") from exc
    unique = dedupe_candles(candles)
    if len(unique) != len(candles):
        logger.warning("%s: dropped %d duplicate timestamps", p, len(candles) - len(unique))
    return unique


def save_candles(path: str | os.PathLike[str], candles: Iterable[Candle]) -> None:
    """Write candles (as given) to ``path`` atomically, creating parent directories."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(COLUMNS)
            for c in candles:
                writer.writerow([c.timestamp, repr(c.open), repr(c.high), repr(c.low),
                                 repr(c.close), repr(c.volume)])
        os.replace(tmp_name, p)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


class CsvMarket:
    """Replays a CSV candle file as a ``MarketDataSource`` (loaded once; see ``reload``)."""

    def __init__(self, path: str | os.PathLike[str], timeframe: str) -> None:
        self.path = Path(path)
        self.timeframe = timeframe
        self.tf_ms = timeframe_to_ms(timeframe)  # ValueError on a bad timeframe
        self._candles: list[Candle] = []
        self.reload()

    @property
    def candles(self) -> list[Candle]:
        """All loaded candles, oldest first (a copy)."""
        return list(self._candles)

    def reload(self) -> None:
        """Re-read the file and check that its spacing matches the timeframe."""
        candles = load_candles(self.path)
        _check_spacing(candles, self.tf_ms, self.timeframe, self.path)
        self._candles = candles
        logger.debug("loaded %d candles from %s", len(candles), self.path)

    def fetch_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        """Last ``limit`` candles of the file (``symbol`` is ignored)."""
        try:
            requested = timeframe_to_ms(timeframe)
        except ValueError as exc:
            raise MarketDataError(str(exc)) from exc
        if requested != self.tf_ms:
            raise MarketDataError(f"{self.path} holds {self.timeframe!r} candles, not {timeframe!r}")
        if limit <= 0:
            return []
        return self._candles[-limit:]

    def fetch_last_price(self, symbol: str) -> float:
        """Close of the last candle in the file."""
        if not self._candles:
            raise MarketDataError(f"{self.path} has no candles")
        return self._candles[-1].close


def _read_rows(fh: TextIO, path: Path) -> list[Candle]:
    reader = csv.reader(fh)
    header = next(reader, None)
    if header is None:
        raise MarketDataError(f"{path}: empty file (expected header {','.join(COLUMNS)})")
    index = _column_index(header, path)
    candles: list[Candle] = []
    for row in reader:
        if not any(cell.strip() for cell in row):
            continue  # blank line
        try:
            candles.append(_row_to_candle(row, index))
        except ValueError as exc:
            raise MarketDataError(
                f"{path}: malformed row at line {reader.line_num}: {exc}"
            ) from exc
    return candles


def _column_index(header: list[str], path: Path) -> dict[str, int]:
    names = [h.strip().lower() for h in header]
    missing = [c for c in COLUMNS if c not in names]
    if missing:
        raise MarketDataError(f"{path}: missing column(s) {', '.join(missing)} in header {header}")
    return {c: names.index(c) for c in COLUMNS}


def _row_to_candle(row: list[str], index: dict[str, int]) -> Candle:
    if len(row) <= max(index.values()):
        raise ValueError(f"expected at least {max(index.values()) + 1} fields, got {len(row)}")
    values = {c: row[i].strip() for c, i in index.items()}
    return make_candle(
        parse_timestamp(values["timestamp"]),
        values["open"], values["high"], values["low"], values["close"], values["volume"],
    )


def _iso_to_ms(text: str) -> int:
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"timestamp {text!r} is neither int ms nor ISO-8601") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - _EPOCH) // timedelta(milliseconds=1)


def _check_spacing(candles: list[Candle], tf_ms: int, timeframe: str, path: Path) -> None:
    if len(candles) < 2:
        return
    min_gap = min(b.timestamp - a.timestamp for a, b in zip(candles, candles[1:]))
    if min_gap != tf_ms:
        raise MarketDataError(
            f"{path}: candles are {min_gap} ms apart but timeframe {timeframe!r} is {tf_ms} ms"
        )
