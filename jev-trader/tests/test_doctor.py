"""Tests for `jev doctor`: read-only pre-flight checks, all offline with fakes."""

from __future__ import annotations

from types import SimpleNamespace

import httpx2
import typesafe_sdk as ts

from jev.cli import main
from jev.config import Settings
from jev.doctor import FAIL, OK, SKIP, WARN, run_checks
from jev.models import Candle, MarketDataError

NOW = 1_767_225_600_000  # 2026-01-01T00:00Z
FIVE_MIN = 300_000
KEY = {"TYPESAFE_API_KEY": "test-key"}


def jev_client(status: int = 200, models: tuple[str, ...] = ("jev-latest", "jev-1.13")):
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/v1/models"  # never /v1/systemone: no tokens spent
        if status != 200:
            return httpx2.Response(status, json={"error": "nope"})
        return httpx2.Response(200, json={"models": [
            {"name": m, "description": "System one model", "release_date": "2026-09-15"} for m in models]})

    return lambda timeout: ts.TypeSafeClient(api_key="test-key", transport=httpx2.MockTransport(handler),
                                             retry=ts.RetryPolicy(max_retries=0), timeout=timeout)


def market(last_open: int = NOW - 2 * FIVE_MIN, error: Exception | None = None):
    def fetch_candles(symbol, timeframe, limit):
        if error:
            raise error
        return [Candle(timestamp=last_open - i * FIVE_MIN, open=1, high=1, low=1, close=60000.5, volume=1)
                for i in reversed(range(limit))]

    return lambda settings: SimpleNamespace(fetch_candles=fetch_candles)


def by_name(checks):
    return {c.name: c for c in checks}


def test_ready_machine_passes_every_check():
    checks = by_name(run_checks(Settings(), KEY, jev_client_factory=jev_client(),
                                market_factory=market(), now_ms=lambda: NOW))
    assert checks["Modo"].status == OK
    assert checks["Clave de Jev"].status == OK
    assert checks["API de Jev"].status == OK and "jev-latest" in checks["API de Jev"].detail
    assert checks["Datos del exchange"].status == OK
    assert checks["Clave de Claude"].status == SKIP
    assert checks["Claves del exchange"].status == SKIP
    assert not any(c.status == FAIL for c in checks.values())


def test_missing_jev_key_fails_for_the_default_engine_and_skips_the_api():
    checks = by_name(run_checks(Settings(), {}, jev_client_factory=jev_client(), market_factory=market(),
                                now_ms=lambda: NOW))
    assert checks["Clave de Jev"].status == FAIL
    assert checks["API de Jev"].status == SKIP


def test_rules_engine_does_not_need_jev():
    checks = by_name(run_checks(Settings(engine="rules"), {}, online=False))
    assert checks["Clave de Jev"].status == SKIP
    assert checks["Red"].status == SKIP


def test_rejected_key_and_unknown_model_are_reported():
    checks = by_name(run_checks(Settings(), KEY, jev_client_factory=jev_client(status=401),
                                market_factory=market(), now_ms=lambda: NOW))
    assert checks["API de Jev"].status == FAIL and "rechazada" in checks["API de Jev"].detail
    checks = by_name(run_checks(Settings(jev_model="jev-9"), KEY, jev_client_factory=jev_client(),
                                market_factory=market(), now_ms=lambda: NOW))
    assert checks["API de Jev"].status == WARN


def test_unreachable_jev_and_exchange_fail_without_crashing():
    def unreachable(request):
        raise httpx2.ConnectError("blocked")

    factory = lambda timeout: ts.TypeSafeClient(api_key="k", transport=httpx2.MockTransport(unreachable),
                                                retry=ts.RetryPolicy(max_retries=0), timeout=timeout)
    checks = by_name(run_checks(Settings(), KEY, jev_client_factory=factory,
                                market_factory=market(error=MarketDataError("403")), now_ms=lambda: NOW))
    assert checks["API de Jev"].status == FAIL and "api.typesafe.ai" in checks["API de Jev"].detail
    assert checks["Datos del exchange"].status == FAIL


def test_stale_candles_warn():
    checks = by_name(run_checks(Settings(), KEY, jev_client_factory=jev_client(),
                                market_factory=market(last_open=NOW - 60 * FIVE_MIN), now_ms=lambda: NOW))
    assert checks["Datos del exchange"].status == WARN


def test_live_mode_reports_network_and_keys_without_printing_them():
    s = Settings(mode="live", api_key="AKIAVERYSECRET", api_secret="SECRETVALUE")
    checks = by_name(run_checks(s, KEY, online=False))
    assert checks["Modo"].status == OK and "TESTNET" in checks["Modo"].detail
    assert checks["Claves del exchange"].status == OK
    assert all("SECRET" not in c.detail for c in checks.values())
    mainnet = Settings(mode="live", use_testnet=False, live_confirm="YES_I_ACCEPT_REAL_MONEY_RISK",
                       api_key="a", api_secret="b")
    assert by_name(run_checks(mainnet, KEY, online=False))["Modo"].status == WARN


def test_cli_doctor_offline(tmp_path, monkeypatch, capsys):
    # The CLI loads .env from its working directory; never use the developer's real key.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert main(["doctor", "--offline", "--engine", "rules"]) == 0
    out = capsys.readouterr().out
    assert "Listo para paper trading." in out
    monkeypatch.setenv("TYPESAFE_API_KEY", "doctor-test-key")
    assert main(["doctor", "--offline"]) == 0
    out = capsys.readouterr().out
    assert "doctor-test-key" not in out
    monkeypatch.delenv("TYPESAFE_API_KEY")
    assert main(["doctor", "--offline"]) == 1  # default hybrid engine needs Jev
    assert "problema" in capsys.readouterr().out
