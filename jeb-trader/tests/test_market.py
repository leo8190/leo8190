"""Tests for jeb.market: timeframes, synthetic, CSV and ccxt sources (all offline)."""

from __future__ import annotations

import math
from datetime import datetime, timezone

import ccxt
import pytest

from jeb.market import ccxt_source
from jeb.market.ccxt_source import CcxtMarket
from jeb.market.csv_source import CsvMarket, load_candles, parse_timestamp, save_candles
from jeb.market.synthetic import SyntheticMarket
from jeb.market.timeframes import (
    closed_candles,
    dedupe_candles,
    floor_to_timeframe,
    is_valid_timeframe,
    make_candle,
    timeframe_to_ms,
)
from jeb.models import Candle, MarketDataError

T0 = 1767225600000  # 2026-01-01T00:00:00Z
M5 = 300_000


def candle(ts: int, price: float = 100.0, volume: float = 1.0) -> Candle:
    return Candle(timestamp=ts, open=price, high=price + 1, low=price - 1, close=price, volume=volume)


# ---------------------------------------------------------------- timeframes


@pytest.mark.parametrize(
    ("tf", "ms"),
    [("30s", 30_000), ("1m", 60_000), ("5m", 300_000), ("15m", 900_000), ("1h", 3_600_000),
     ("4h", 14_400_000), ("1d", 86_400_000), ("1w", 604_800_000)],
)
def test_timeframe_to_ms(tf: str, ms: int) -> None:
    assert timeframe_to_ms(tf) == ms
    assert is_valid_timeframe(tf)


@pytest.mark.parametrize("bad", ["", "5", "m", "0m", "-1m", "1M", "1y", "5 m", " 5m", "1.5h", "1mm", "05m", None, 5])
def test_timeframe_to_ms_rejects_bad_input(bad) -> None:
    with pytest.raises(ValueError):
        timeframe_to_ms(bad)
    assert not is_valid_timeframe(bad)


def test_floor_to_timeframe() -> None:
    assert floor_to_timeframe(T0, "5m") == T0
    assert floor_to_timeframe(T0 + M5 - 1, "5m") == T0
    assert floor_to_timeframe(T0 + M5, "5m") == T0 + M5
    assert floor_to_timeframe(T0 + 5_400_000, "1h") == T0 + 3_600_000
    assert floor_to_timeframe(T0 + 1, "1d") == T0


def test_floor_weekly_aligns_to_monday() -> None:
    floored = floor_to_timeframe(T0, "1w")  # 2026-01-01 is a Thursday
    dt = datetime.fromtimestamp(floored / 1000, tz=timezone.utc)
    assert (dt.weekday(), dt.hour, dt.minute) == (0, 0, 0)
    assert dt.date().isoformat() == "2025-12-29"
    assert floor_to_timeframe(floored, "1w") == floored


def test_dedupe_and_closed_candles() -> None:
    raw = [candle(T0 + M5), candle(T0), candle(T0 + M5, price=200.0)]
    unique = dedupe_candles(raw)
    assert [c.timestamp for c in unique] == [T0, T0 + M5]
    assert unique[1].close == 200.0  # last occurrence wins
    # boundary: a candle is closed exactly at open + timeframe
    assert closed_candles(unique, "5m", T0 + 2 * M5) == unique
    assert closed_candles(unique, "5m", T0 + 2 * M5 - 1) == unique[:1]


@pytest.mark.parametrize(
    "values",
    [
        (T0, 100, 99, 98, 100, 1),  # high < open
        (T0, 100, 101, 100.5, 100, 1),  # low > open
        (T0, 0, 1, 0, 1, 1),  # zero price
        (T0, 100, 101, 99, 100, -1),  # negative volume
        (T0, math.nan, 101, 99, 100, 1),  # not finite
        (T0 + 0.5, 100, 101, 99, 100, 1),  # fractional timestamp
        ("x", 100, 101, 99, 100, 1),
        (T0, None, 101, 99, 100, 1),
    ],
)
def test_make_candle_rejects_invalid(values) -> None:
    with pytest.raises(ValueError):
        make_candle(*values)


# ---------------------------------------------------------------- synthetic


def assert_consistent(candles: list[Candle], tf_ms: int) -> None:
    for prev, cur in zip(candles, candles[1:]):
        assert cur.timestamp - prev.timestamp == tf_ms
        assert cur.open == prev.close
    for c in candles:
        assert all(math.isfinite(x) and x > 0 for x in (c.open, c.high, c.low, c.close, c.volume))
        assert c.low <= min(c.open, c.close) <= max(c.open, c.close) <= c.high


def test_synthetic_is_deterministic() -> None:
    a = SyntheticMarket(seed=7).generate(500)
    b = SyntheticMarket(seed=7).generate(500)
    assert a == b
    assert SyntheticMarket(seed=8).generate(500) != a


def test_synthetic_generate_is_pure_prefix() -> None:
    m1 = SyntheticMarket(seed=3)
    long_first = m1.generate(400)
    m2 = SyntheticMarket(seed=3)
    short_first = m2.generate(100)
    m2.fetch_candles("BTC/USDT", "5m", 50)
    m2.advance(250)
    assert short_first == long_first[:100]
    assert m2.generate(400) == long_first
    assert m1.generate(100) == long_first[:100]


def test_synthetic_candles_are_consistent() -> None:
    m = SyntheticMarket(timeframe="1h", start_price=2500.0)
    candles = m.generate(5000)
    assert candles[0].timestamp == T0
    assert candles[0].open == 2500.0
    assert_consistent(candles, 3_600_000)


def test_synthetic_volatility_matches_annual_vol() -> None:
    candles = SyntheticMarket(annual_vol=0.6).generate(5000)
    rets = [math.log(b.close / a.close) for a, b in zip(candles, candles[1:])]
    mean = sum(rets) / len(rets)
    stdev = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
    expected = 0.6 * math.sqrt(M5 / (365 * 86_400_000))
    assert 0.7 * expected < stdev < 1.4 * expected


def test_synthetic_has_regimes_and_trends() -> None:
    m = SyntheticMarket()
    candles = m.generate(5000)
    assert {m.regime_at(i) for i in range(5000)} == {"bull", "bear", "sideways"}
    window_returns = [candles[i + 199].close / candles[i].open - 1 for i in range(0, 4800, 200)]
    assert max(window_returns) > 0.02
    assert min(window_returns) < -0.02


def test_synthetic_cursor_semantics() -> None:
    m = SyntheticMarket(initial_history=300)
    full = m.generate(400)
    assert m.cursor == 300
    assert m.now_ms() == T0 + 300 * M5

    got = m.fetch_candles("BTC/USDT", "5m", 50)
    assert got == full[250:300]
    assert all(c.timestamp + M5 <= m.now_ms() for c in got)  # closed only
    assert m.fetch_candles("ETH/USDT", "5m", 1000) == full[:300]
    assert m.fetch_candles("BTC/USDT", "5m", 0) == []
    assert m.last_price() == full[300].open == full[299].close
    assert m.fetch_last_price("BTC/USDT") == m.last_price()

    closed = m.advance()
    assert closed == [full[300]]
    assert m.fetch_candles("BTC/USDT", "5m", 50)[-1] == full[300]
    assert m.advance(3) == full[301:304]
    assert m.cursor == 304
    assert m.fetch_candles("BTC/USDT", "5m", 2) == full[302:304]
    assert m.advance(0) == []


def test_synthetic_no_future_leak_while_replaying() -> None:
    m = SyntheticMarket(seed=11, initial_history=60)
    full = m.generate(200)
    for _ in range(100):
        got = m.fetch_candles("BTC/USDT", "5m", 30)
        assert got == full[max(0, m.cursor - 30):m.cursor]
        assert got[-1].timestamp < m.now_ms()
        m.advance()


def test_synthetic_empty_history() -> None:
    m = SyntheticMarket(initial_history=0, start_price=123.0)
    assert m.fetch_candles("BTC/USDT", "5m", 10) == []
    assert m.last_price() == 123.0


def test_synthetic_errors() -> None:
    m = SyntheticMarket(timeframe="5m")
    assert m.fetch_candles("BTC/USDT", "5m", 1)
    with pytest.raises(MarketDataError):
        m.fetch_candles("BTC/USDT", "1h", 10)
    with pytest.raises(MarketDataError):
        m.fetch_candles("BTC/USDT", "bogus", 10)
    with pytest.raises(ValueError):
        m.advance(-1)
    with pytest.raises(ValueError):
        m.generate(-1)
    with pytest.raises(ValueError):
        SyntheticMarket(timeframe="5x")
    with pytest.raises(ValueError):
        SyntheticMarket(start_price=0)
    with pytest.raises(ValueError):
        SyntheticMarket(annual_vol=-0.1)


def test_synthetic_start_ts_is_aligned() -> None:
    m = SyntheticMarket(timeframe="1h", start_ts=T0 + 123_456)
    assert m.generate(1)[0].timestamp == T0


# ---------------------------------------------------------------- CSV


def test_csv_round_trip(tmp_path) -> None:
    candles = SyntheticMarket(seed=5).generate(120)
    path = tmp_path / "sub" / "btc.csv"
    save_candles(path, candles)
    assert path.read_text(encoding="utf-8").splitlines()[0] == "timestamp,open,high,low,close,volume"
    assert load_candles(path) == candles  # exact float round trip
    assert list(tmp_path.joinpath("sub").iterdir()) == [path]  # no temp files left


def test_csv_iso_timestamps_sort_and_dedup(tmp_path) -> None:
    path = tmp_path / "iso.csv"
    path.write_text(
        "Timestamp,Open,High,Low,Close,Volume,extra\n"
        "2026-01-01T00:10:00Z,102,103,101,102,3,x\n"
        "2026-01-01T00:00:00+00:00,100,101,99,100,1,x\n"
        "\n"
        "2026-01-01 00:05:00,101,102,100,101,2,x\n"
        f"{T0 + M5},111,112,110,111,9,x\n",
        encoding="utf-8",
    )
    candles = load_candles(path)
    assert [c.timestamp for c in candles] == [T0, T0 + M5, T0 + 2 * M5]
    assert candles[1].close == 111.0  # duplicate timestamp: last row wins
    assert candles[2].volume == 3.0


def test_parse_timestamp_variants() -> None:
    assert parse_timestamp(str(T0)) == T0
    assert parse_timestamp(f"{T0}.0") == T0
    assert parse_timestamp("2026-01-01T01:00:00+01:00") == T0
    for bad in ("", "abc", "12.5", "-5", "2026-13-01"):
        with pytest.raises(ValueError):
            parse_timestamp(bad)


def test_csv_malformed_row_reports_line(tmp_path) -> None:
    path = tmp_path / "bad.csv"
    path.write_text(
        "timestamp,open,high,low,close,volume\n"
        f"{T0},100,101,99,100,1\n"
        f"{T0 + M5},100,101,99,100,1\n"
        f"{T0 + 2 * M5},100,101,99,abc,1\n",
        encoding="utf-8",
    )
    with pytest.raises(MarketDataError, match="line 4"):
        load_candles(path)


@pytest.mark.parametrize(
    "body",
    [
        f"timestamp,open,high,low,close,volume\n{T0},100,99,98,100,1\n",  # high < open
        f"timestamp,open,high,low,close,volume\n{T0},100,101,99\n",  # short row
        f"timestamp,open,high,low,close,volume\nyesterday,100,101,99,100,1\n",
    ],
)
def test_csv_rejects_bad_rows(tmp_path, body: str) -> None:
    path = tmp_path / "bad.csv"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(MarketDataError, match="line 2"):
        load_candles(path)


def test_csv_missing_file_header_or_columns(tmp_path) -> None:
    with pytest.raises(MarketDataError):
        load_candles(tmp_path / "missing.csv")
    empty = tmp_path / "empty.csv"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(MarketDataError, match="empty"):
        load_candles(empty)
    no_volume = tmp_path / "novol.csv"
    no_volume.write_text(f"timestamp,open,high,low,close\n{T0},1,1,1,1\n", encoding="utf-8")
    with pytest.raises(MarketDataError, match="volume"):
        load_candles(no_volume)
    header_only = tmp_path / "header.csv"
    header_only.write_text("timestamp,open,high,low,close,volume\n", encoding="utf-8")
    assert load_candles(header_only) == []


def test_csv_market(tmp_path) -> None:
    candles = SyntheticMarket(seed=9).generate(100)
    path = tmp_path / "data.csv"
    save_candles(path, candles)
    market = CsvMarket(path, "5m")
    assert market.fetch_candles("BTC/USDT", "5m", 10) == candles[-10:]
    assert market.fetch_candles("BTC/USDT", "5m", 1000) == candles
    assert market.fetch_candles("BTC/USDT", "5m", 0) == []
    assert market.fetch_last_price("BTC/USDT") == candles[-1].close
    assert market.candles == candles
    with pytest.raises(MarketDataError):
        market.fetch_candles("BTC/USDT", "1h", 10)

    save_candles(path, candles[:50])
    market.reload()
    assert market.fetch_candles("BTC/USDT", "5m", 1000) == candles[:50]


def test_csv_market_rejects_wrong_spacing(tmp_path) -> None:
    path = tmp_path / "data.csv"
    save_candles(path, SyntheticMarket(timeframe="5m").generate(20))
    with pytest.raises(MarketDataError, match="apart"):
        CsvMarket(path, "1h")
    empty = tmp_path / "empty.csv"
    save_candles(empty, [])
    with pytest.raises(MarketDataError):
        CsvMarket(empty, "5m").fetch_last_price("BTC/USDT")


# ---------------------------------------------------------------- ccxt


def ohlcv(n: int, start: int = T0, tf_ms: int = M5) -> list[list[float]]:
    return [[start + i * tf_ms, 100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i, 10.0] for i in range(n)]


class FakeExchange:
    """Minimal ccxt-like exchange with fetch_ohlcv(since, limit) semantics."""

    def __init__(self, rows=None, ticker=None, error: Exception | None = None, ignore_since: bool = False):
        self.rows = rows or []
        self.ticker = ticker
        self.error = error
        self.ignore_since = ignore_since
        self.calls: list[dict] = []
        self.has = {"fetchOHLCV": True}

    def fetch_ohlcv(self, symbol, timeframe="1m", since=None, limit=None, params=None):
        self.calls.append({"symbol": symbol, "timeframe": timeframe, "since": since, "limit": limit})
        if self.error is not None:
            raise self.error
        if since is not None and not self.ignore_since:
            rows = sorted((r for r in self.rows if r[0] >= since), key=lambda r: r[0])
            return rows[:limit]
        return self.rows[-limit:] if limit else self.rows  # raw order, like a quirky exchange

    def fetch_ticker(self, symbol):
        if self.error is not None:
            raise self.error
        return self.ticker


def make_market(exchange: FakeExchange, now: int, **kwargs) -> CcxtMarket:
    return CcxtMarket(exchange=exchange, now_ms=lambda: now, **kwargs)


def test_ccxt_fetch_candles_drops_forming_candle() -> None:
    rows = ohlcv(11)  # candles 0..10; candle 10 is still forming
    fake = FakeExchange(rows)
    market = make_market(fake, now=T0 + 10 * M5 + 1_000)
    got = market.fetch_candles("BTC/USDT", "5m", 5)
    assert [c.timestamp for c in got] == [T0 + i * M5 for i in range(5, 10)]
    assert fake.calls[-1] == {"symbol": "BTC/USDT", "timeframe": "5m", "since": None, "limit": 6}
    assert got[-1].close == 109.5


def test_ccxt_fetch_candles_boundary_and_dedup() -> None:
    rows = ohlcv(4)
    rows = [rows[2], rows[0], rows[1], list(rows[1]), rows[3]]  # unsorted + duplicate
    market = make_market(FakeExchange(rows), now=T0 + 4 * M5)  # candle 3 closes exactly now
    got = market.fetch_candles("BTC/USDT", "5m", 10)
    assert [c.timestamp for c in got] == [T0 + i * M5 for i in range(4)]
    assert market.fetch_candles("BTC/USDT", "5m", 0) == []


def test_ccxt_missing_volume_defaults_to_zero() -> None:
    rows = [[T0, 100.0, 101.0, 99.0, 100.0, None], [T0 + M5, 100.0, 101.0, 99.0, 100.0]]
    got = make_market(FakeExchange(rows), now=T0 + 10 * M5).fetch_candles("BTC/USDT", "5m", 5)
    assert [c.volume for c in got] == [0.0, 0.0]


def test_ccxt_download_history_paginates() -> None:
    rows = ohlcv(2501)  # 2500 closed + 1 forming
    fake = FakeExchange(rows)
    market = make_market(fake, now=T0 + 2500 * M5 + 5)
    got = market.download_history("BTC/USDT", "5m", T0, T0 + 10_000 * M5, page_limit=1000)
    assert len(got) == 2500
    assert [c.timestamp for c in got] == [T0 + i * M5 for i in range(2500)]
    assert [call["since"] for call in fake.calls] == [T0, T0 + 1000 * M5, T0 + 2000 * M5]
    assert all(call["limit"] == 1000 for call in fake.calls)


def test_ccxt_download_history_respects_until() -> None:
    fake = FakeExchange(ohlcv(3000))
    market = make_market(fake, now=T0 + 3000 * M5)
    got = market.download_history("BTC/USDT", "5m", T0 + 100 * M5, T0 + 1500 * M5, page_limit=1000)
    assert got[0].timestamp == T0 + 100 * M5
    assert got[-1].timestamp == T0 + 1499 * M5
    assert len(got) == 1400
    assert len(fake.calls) == 2
    assert market.download_history("BTC/USDT", "5m", T0, T0) == []
    with pytest.raises(ValueError):
        market.download_history("BTC/USDT", "5m", T0, T0 + M5, page_limit=0)


def test_ccxt_download_history_stops_without_progress() -> None:
    # Exchange ignores `since` and always returns the same latest page.
    fake = FakeExchange(ohlcv(50), ignore_since=True)
    market = make_market(fake, now=T0 + 1000 * M5)
    got = market.download_history("BTC/USDT", "5m", T0, T0 + 1000 * M5, page_limit=100)
    assert len(got) == 50
    assert len(fake.calls) == 2  # second page made no progress


def test_ccxt_download_history_stops_on_empty_page() -> None:
    fake = FakeExchange(ohlcv(10))
    market = make_market(fake, now=T0 + 1000 * M5)
    got = market.download_history("BTC/USDT", "5m", T0, T0 + 1000 * M5, page_limit=100)
    assert len(got) == 10
    assert len(fake.calls) == 2


@pytest.mark.parametrize(
    "error",
    [ccxt.NetworkError("timeout"), ccxt.RequestTimeout("slow"), ccxt.ExchangeError("boom"),
     ccxt.BadSymbol("no such market"), ccxt.RateLimitExceeded("429")],
)
def test_ccxt_errors_are_wrapped(error: Exception) -> None:
    market = make_market(FakeExchange(error=error), now=T0)
    with pytest.raises(MarketDataError) as info:
        market.fetch_candles("BTC/USDT", "5m", 10)
    assert info.value.__cause__ is error
    with pytest.raises(MarketDataError) as info:
        market.fetch_last_price("BTC/USDT")
    assert info.value.__cause__ is error
    with pytest.raises(MarketDataError):
        market.download_history("BTC/USDT", "5m", T0 - 100 * M5, T0)


@pytest.mark.parametrize(
    "rows",
    [
        [["x", 1, 1, 1, 1, 1]],
        [[T0, 100.0, 99.0, 98.0, 100.0, 1.0]],  # inconsistent OHLC
        [[T0, 1.0]],
        ["garbage"],
    ],
)
def test_ccxt_malformed_rows(rows) -> None:
    market = make_market(FakeExchange(rows), now=T0 + 10 * M5)
    with pytest.raises(MarketDataError, match="malformed"):
        market.fetch_candles("BTC/USDT", "5m", 10)


def test_ccxt_malformed_response_and_bad_timeframe() -> None:
    fake = FakeExchange()
    fake.fetch_ohlcv = lambda *a, **k: {"not": "a list"}
    market = make_market(fake, now=T0)
    with pytest.raises(MarketDataError, match="malformed"):
        market.fetch_candles("BTC/USDT", "5m", 10)
    with pytest.raises(MarketDataError):
        market.fetch_candles("BTC/USDT", "5x", 10)


def test_ccxt_unexpected_exception_is_wrapped() -> None:
    error = KeyError("result")
    market = make_market(FakeExchange(error=error), now=T0)
    with pytest.raises(MarketDataError) as info:
        market.fetch_candles("BTC/USDT", "5m", 10)
    assert info.value.__cause__ is error


def test_ccxt_capability_check() -> None:
    fake = FakeExchange(ohlcv(3))
    fake.has = {"fetchOHLCV": False}
    with pytest.raises(MarketDataError, match="fetchOHLCV"):
        make_market(fake, now=T0 + 10 * M5).fetch_candles("BTC/USDT", "5m", 2)
    assert fake.calls == []


@pytest.mark.parametrize(
    ("ticker", "expected"),
    [
        ({"last": 101.5, "close": 100.0, "bid": 1.0, "ask": 2.0}, 101.5),
        ({"last": None, "close": 100.0}, 100.0),
        ({"last": None, "close": None, "bid": 99.0, "ask": 101.0}, 100.0),
        ({"last": "102.5"}, 102.5),
    ],
)
def test_ccxt_fetch_last_price(ticker: dict, expected: float) -> None:
    assert make_market(FakeExchange(ticker=ticker), now=T0).fetch_last_price("BTC/USDT") == expected


@pytest.mark.parametrize("ticker", [{"last": None, "bid": 99.0}, {}, {"last": 0.0}, None, [1, 2]])
def test_ccxt_fetch_last_price_unusable(ticker) -> None:
    with pytest.raises(MarketDataError):
        make_market(FakeExchange(ticker=ticker), now=T0).fetch_last_price("BTC/USDT")


def test_ccxt_redacts_api_keys() -> None:
    key, secret = "KEY-abc123", "SECRET-xyz789"
    error = ccxt.AuthenticationError(f"binance invalid key {key} sig {secret}")
    market = CcxtMarket(exchange=FakeExchange(error=error), api_key=key, api_secret=secret, now_ms=lambda: T0)
    with pytest.raises(MarketDataError) as info:
        market.fetch_last_price("BTC/USDT")
    assert key not in str(info.value) and secret not in str(info.value)
    assert "***" in str(info.value)
    assert key not in repr(market) and secret not in repr(market)


class RecordingExchange:
    """Stands in for a ccxt exchange class to observe construction and sandbox calls."""

    instances: list["RecordingExchange"] = []
    sandbox_error: Exception | None = None

    def __init__(self, config: dict) -> None:
        self.config = config
        self.sandbox: list[bool] = []
        RecordingExchange.instances.append(self)

    def set_sandbox_mode(self, enabled: bool) -> None:
        if RecordingExchange.sandbox_error is not None:
            raise RecordingExchange.sandbox_error
        self.sandbox.append(enabled)


@pytest.fixture
def recording_ccxt(monkeypatch):
    RecordingExchange.instances = []
    RecordingExchange.sandbox_error = None
    monkeypatch.setattr(ccxt_source.ccxt, "binance", RecordingExchange)
    return RecordingExchange


def test_ccxt_creates_exchange_with_sandbox(recording_ccxt) -> None:
    market = CcxtMarket("binance", use_testnet=True, api_key="k", api_secret="s")
    ex = market.exchange
    assert isinstance(ex, recording_ccxt)
    assert ex.config == {"enableRateLimit": True, "apiKey": "k", "secret": "s"}
    assert ex.sandbox == [True]


def test_ccxt_no_sandbox_when_testnet_disabled(recording_ccxt) -> None:
    market = CcxtMarket("binance", use_testnet=False)
    assert market.exchange.config == {"enableRateLimit": True}
    assert market.exchange.sandbox == []


def test_ccxt_sandbox_unsupported_raises(recording_ccxt) -> None:
    recording_ccxt.sandbox_error = ccxt.NotSupported("no sandbox")
    with pytest.raises(MarketDataError, match="testnet") as info:
        CcxtMarket("binance", use_testnet=True)
    assert isinstance(info.value.__cause__, ccxt.NotSupported)


def test_ccxt_unknown_exchange() -> None:
    with pytest.raises(MarketDataError, match="unknown"):
        CcxtMarket("not_a_real_exchange")


def test_ccxt_injected_exchange_is_used_as_is() -> None:
    fake = FakeExchange()
    market = CcxtMarket(exchange=fake, use_testnet=True)
    assert market.exchange is fake
