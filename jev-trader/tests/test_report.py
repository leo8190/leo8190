from __future__ import annotations

import base64
import hashlib
import json
import re

from jev.metrics import compute_metrics
from jev.models import TradeRecord
from jev.report import (
    DISCLAIMER,
    MAX_CHART_POINTS,
    downsample_indices,
    render_html_report,
    write_report,
)

HOUR = 3_600_000
T0 = 1_704_067_200_000  # 2024-01-01T00:00:00Z


def _curves(n: int = 50) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    equity = [(T0 + i * HOUR, 1000.0 + 10 * (i % 7) - (30 if i == n // 2 else 0)) for i in range(n)]
    prices = [(T0 + i * HOUR, 100.0 + i * 0.5) for i in range(n)]
    return equity, prices


def _trade(i: int, reason: str = "take_profit") -> TradeRecord:
    return TradeRecord(
        symbol="BTC/USDT", entry_price=100.0, exit_price=102.0, quantity=0.0015, pnl=1.5 - (i % 3),
        pnl_pct=1.5 - (i % 3), fees=0.1, entry_time=T0 + i * HOUR, exit_time=T0 + i * HOUR + 30 * 60_000,
        exit_reason=reason,
    )


def _render(equity=None, prices=None, trades=None, title="Backtest BTC/USDT 1h", settings=None, warnings=None) -> str:
    equity = _curves()[0] if equity is None else equity
    prices = _curves()[1] if prices is None else prices
    trades = [_trade(i) for i in range(5)] if trades is None else trades
    first = prices[0][1] if prices else None
    last = prices[-1][1] if prices else None
    m = compute_metrics(equity, trades, 1000.0, HOUR, first, last, llm_calls=3, llm_cost_usd=0.0042, fees_paid=1.25)
    settings = {"symbol": "BTC/USDT", "engine": "hybrid"} if settings is None else settings
    return render_html_report(m, equity, prices, trades, title, settings, warnings)


def _polyline_point_counts(doc: str) -> list[int]:
    return [len(pts.split()) for pts in re.findall(r'<polyline[^>]*points="([^"]*)"', doc)]


def test_report_contains_key_sections() -> None:
    doc = _render(warnings=["Backtest sin slippage real."])
    assert doc.startswith("<!DOCTYPE html>") and '<html lang="es">' in doc
    for text in ("Retorno total", "Máx. drawdown", "Sharpe", "Tasa de acierto", "Operaciones",
                 "Comisiones", "Coste LLM", "Buy &amp; hold", "Configuración", "Advertencias",
                 "Backtest sin slippage real.", "Periodo: 2024-01-01 00:00 UTC"):
        assert text in doc, text
    assert DISCLAIMER in doc
    assert "Esto no es asesoramiento financiero. Resultados pasados no garantizan resultados futuros." in doc
    assert doc.count('role="img"') >= 2 and "<title id=" in doc and "<desc id=" in doc
    assert "prefers-color-scheme: dark" in doc and "--series-1" in doc
    assert "2024-01-01T00:30:00Z" in doc  # trade exit time as UTC ISO
    assert "take_profit" in doc
    assert 'name="viewport"' in doc


def test_self_contained_no_external_resources() -> None:
    doc = _render()
    assert "<link" not in doc
    assert not re.search(r"<script[^>]+src=", doc)
    assert "http://" not in doc and "https://" not in doc
    assert "@import" not in doc


def test_csp_hash_matches_inline_script() -> None:
    doc = _render()
    scripts = re.findall(r"<script>(.*?)</script>", doc, flags=re.S)
    assert len(scripts) == 1
    digest = base64.b64encode(hashlib.sha256(scripts[0].encode("utf-8")).digest()).decode()
    assert f"'sha256-{digest}'" in doc


def test_chart_data_json_is_valid_and_normalized() -> None:
    doc = _render()
    raw = re.search(r'<script type="application/json" id="jev-chart-data">(.*?)</script>', doc, re.S).group(1)
    data = json.loads(raw)
    equity = data["charts"]["equity"]["series"]
    assert [s["name"] for s in equity] == ["Estrategia", "Buy & hold"]
    assert equity[0]["v"][0] == 100.0 and equity[1]["v"][0] == 100.0  # both indexed to 100
    dd = data["charts"]["drawdown"]["series"][0]["v"]
    assert max(dd) <= 0.0


def test_escapes_malicious_strings() -> None:
    evil = "<script>alert('x')</script>"
    doc = _render(
        title=evil,
        trades=[_trade(0, reason=evil)],
        settings={evil: evil, "note": '"><img src=x onerror=alert(1)>'},
        warnings=[evil],
    )
    assert "<script>alert" not in doc
    assert "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;" in doc
    assert "<img src=x" not in doc
    assert "onerror=alert(1)&gt;" in doc  # present only as inert escaped text


def test_secret_settings_are_masked() -> None:
    doc = _render(settings={"api_key": "sk-live-123", "llm_max_tokens": "400"})
    assert "sk-live-123" not in doc and "••••" in doc
    assert "400" in doc


def test_empty_trades_and_curves_do_not_crash() -> None:
    doc = _render(equity=[], prices=[], trades=[], settings={}, warnings=None)
    assert "Sin operaciones cerradas" in doc
    assert "Sin datos suficientes para graficar" in doc
    assert "Periodo: sin datos" in doc
    assert "Advertencias" not in doc
    assert "<script" not in doc  # no chart, no script
    assert DISCLAIMER in doc


def test_single_point_curve_and_missing_prices() -> None:
    doc = _render(equity=[(T0, 1000.0)], prices=[])
    assert "Sin datos suficientes para graficar" in doc
    doc = _render(prices=[])
    assert "Sin referencia de buy &amp; hold" in doc
    assert len(_polyline_point_counts(doc)) == 2  # strategy line + drawdown line only


def test_downsamples_to_max_points() -> None:
    n = 5000
    equity = [(T0 + i * HOUR, 1000.0 + (i % 97) - (400 if i == 3001 else 0)) for i in range(n)]
    prices = [(T0 + i * HOUR, 100.0 + (i % 53)) for i in range(n)]
    doc = _render(equity=equity, prices=prices)
    counts = _polyline_point_counts(doc)
    assert counts and all(c <= MAX_CHART_POINTS for c in counts)
    assert "Curvas reducidas a" in doc
    idx = downsample_indices(n, [[v for _, v in equity]])
    assert idx[0] == 0 and idx[-1] == n - 1 and 3001 in idx  # extreme preserved
    assert len(idx) <= MAX_CHART_POINTS


def test_trades_truncated_to_last_200() -> None:
    trades = [_trade(i) for i in range(250)]
    doc = _render(trades=trades)
    assert "Mostrando las últimas 200 de 250 operaciones." in doc
    trade_table = doc.split('id="h-trades"')[1].split("</table>")[0]
    assert trade_table.count("<tr>") == 201  # header + 200 rows
    assert '<td class="num">51</td>' in trade_table and '<td class="num">250</td>' in trade_table


def test_write_report_creates_parent_dirs(tmp_path) -> None:
    target = tmp_path / "a" / "b" / "report.html"
    out = write_report(target, _render())
    assert out == target and target.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")
