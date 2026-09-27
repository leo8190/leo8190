"""Self-contained HTML performance report (Spanish UI, inline SVG charts, no external assets).

Design notes:
- Charts use the "emphasis" form: the strategy in the accent hue, buy & hold in a
  de-emphasis gray, both indexed to 100 on ONE axis (never a dual axis).
- SVG draws only geometry (stretched with ``preserveAspectRatio="none"`` and
  ``non-scaling-stroke``); axis labels are HTML so text stays legible at phone width.
- A tiny inline script adds a crosshair + tooltip (keyboard accessible). It is pinned
  by a CSP hash, so nothing else can execute even if a string slipped through escaping.
- Every dynamic string goes through ``html.escape``; chart data travels as JSON inside
  a non-executable ``<script type="application/json">`` with ``<>&`` escaped.
"""

from __future__ import annotations

import base64
import hashlib
import html as html_lib
import json
import logging
import math
import re
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .metrics import PerformanceMetrics, drawdown_curve, format_number, format_pct
from .models import TradeRecord

logger = logging.getLogger(__name__)

MAX_CHART_POINTS = 800
MAX_TRADE_ROWS = 200
DISCLAIMER = (
    "Esto no es asesoramiento financiero. Resultados pasados no garantizan resultados futuros."
)
_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|secret|password|passphrase|private[_-]?key|auth[_-]?token|access[_-]?token)", re.I
)
_VIEW = 1000  # SVG viewBox is 0..1000 on both axes
_NUM_CLASS = ' class="num"'

# -- theme tokens (validated palette: slot-1 blue + de-emphasis gray) ---------

_LIGHT = {
    "page": "#f9f9f7",
    "surface": "#fcfcfb",
    "text-primary": "#0b0b0b",
    "text-secondary": "#52514e",
    "grid": "#e1e0d9",
    "axis": "#c3c2b7",
    "border": "rgba(11, 11, 11, 0.10)",
    "shadow": "rgba(11, 11, 11, 0.12)",
    "series-1": "#2a78d6",
    "context": "#898781",
    "good": "#0ca30c",
    "critical": "#d03b3b",
    "warning": "#fab219",
}
_DARK = {
    **_LIGHT,
    "page": "#0d0d0d",
    "surface": "#1a1a19",
    "text-primary": "#ffffff",
    "text-secondary": "#c3c2b7",
    "grid": "#2c2c2a",
    "axis": "#383835",
    "border": "rgba(255, 255, 255, 0.10)",
    "shadow": "rgba(0, 0, 0, 0.5)",
    "series-1": "#3987e5",
}


def _token_block(tokens: dict[str, str], scheme: str) -> str:
    body = "".join(f"--{name}:{value};" for name, value in tokens.items())
    return f"color-scheme:{scheme};{body}"


_CSS = (
    f":root{{{_token_block(_LIGHT, 'light')}}}"
    "@media (prefers-color-scheme: dark){"
    f":root:not([data-theme=\"light\"]){{{_token_block(_DARK, 'dark')}}}}}"
    f":root[data-theme=\"dark\"]{{{_token_block(_DARK, 'dark')}}}"
    """
*,*::before,*::after{box-sizing:border-box}
html{-webkit-text-size-adjust:100%;text-size-adjust:100%}
body{margin:0;background:var(--page);color:var(--text-primary);
  font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:1080px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:1.5rem;line-height:1.25;margin:0 0 4px;overflow-wrap:anywhere}
h2{font-size:1.125rem;margin:0 0 12px}
h3{font-size:1rem;margin:0}
section{margin-top:32px}
.period,.note{color:var(--text-secondary);margin:0}
.note{font-size:.8125rem;margin-top:8px}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,11rem),1fr));
  gap:12px;list-style:none;padding:0;margin:0}
.tile,.card,.warnings li,.disclaimer{background:var(--surface);border:1px solid var(--border);border-radius:10px}
.tile{padding:12px 14px;min-width:0}
.tile .label{color:var(--text-secondary);font-size:.8125rem;margin:0}
.tile .value{font-size:1.5rem;font-weight:600;line-height:1.3;margin:2px 0 0;overflow-wrap:anywhere}
.tile .sub{color:var(--text-secondary);font-size:.8125rem;margin:4px 0 0;display:flex;gap:6px;align-items:baseline}
.icon{width:12px;height:12px;flex:none;align-self:center}
.icon-good{fill:var(--good)}.icon-critical{fill:var(--critical)}
.icon-warning{fill:var(--warning)}.icon-ink{fill:#0b0b0b}
.card{padding:16px}
.chart{margin:0}
.chart+.chart{margin-top:28px}
.chart .sub{color:var(--text-secondary);font-size:.8125rem;margin:2px 0 8px}
.legend{display:flex;flex-wrap:wrap;gap:4px 16px;list-style:none;margin:0 0 10px;padding:0;
  font-size:.8125rem;color:var(--text-secondary)}
.legend li{display:flex;align-items:center;gap:6px}
.key{display:inline-block;flex:none;width:16px;height:2px;border-radius:1px;background:var(--c)}
.plot-grid{display:grid;grid-template-columns:3.25rem minmax(0,1fr) 3.75rem;grid-template-rows:220px auto}
.y-axis,.end-labels,.x-axis{position:relative;font-size:.75rem;color:var(--text-secondary);
  font-variant-numeric:tabular-nums}
.y-axis{grid-column:1;grid-row:1}
.y-axis>span{position:absolute;right:8px;transform:translateY(-50%);white-space:nowrap}
.end-labels{grid-column:3;grid-row:1}
.end-labels>span{position:absolute;left:8px;transform:translateY(-50%);white-space:nowrap;
  display:flex;align-items:center;gap:4px;color:var(--text-primary)}
.x-axis{grid-column:2;grid-row:2;height:1.75rem}
.x-axis>span{position:absolute;top:6px;transform:translateX(-50%);white-space:nowrap}
.x-axis>span.first{transform:none}
.x-axis>span.last{transform:translateX(-100%)}
.plot{grid-column:2;grid-row:1;position:relative;min-width:0;border-radius:2px}
.plot:focus{outline:none}
.plot:focus-visible{outline:2px solid var(--series-1);outline-offset:2px}
.plot svg{display:block;width:100%;height:100%;overflow:visible}
.plot .grid line{stroke:var(--grid);stroke-width:1px;vector-effect:non-scaling-stroke}
.plot .baseline{stroke:var(--axis);stroke-width:1px;vector-effect:non-scaling-stroke}
.plot .line{fill:none;stroke-width:2px;stroke-linejoin:round;stroke-linecap:round;
  vector-effect:non-scaling-stroke}
.plot .line-series-1{stroke:var(--series-1)}.plot .line-context{stroke:var(--context)}
.plot .area{fill:var(--series-1);fill-opacity:.1;stroke:none}
.crosshair{position:absolute;top:0;bottom:0;width:1px;margin-left:-.5px;background:var(--axis);pointer-events:none}
.dot{position:absolute;width:8px;height:8px;margin:-4px 0 0 -4px;border-radius:50%;
  background:var(--c);box-shadow:0 0 0 2px var(--surface);pointer-events:none}
.tooltip{position:absolute;top:8px;z-index:2;background:var(--surface);color:var(--text-primary);
  border:1px solid var(--border);border-radius:8px;padding:8px 10px;font-size:.8125rem;
  box-shadow:0 4px 16px var(--shadow);pointer-events:none}
.tooltip .when{color:var(--text-secondary);margin-bottom:4px;white-space:nowrap}
.tooltip .row{display:flex;align-items:center;gap:6px;white-space:nowrap}
.tooltip strong{font-variant-numeric:tabular-nums}
.tooltip .name{color:var(--text-secondary)}
[hidden]{display:none!important}
.table-scroll{overflow-x:auto;max-width:100%;border:1px solid var(--border);border-radius:10px;
  background:var(--surface)}
.table-scroll.tall{max-height:26rem;overflow-y:auto}
table{border-collapse:collapse;width:100%;font-size:.8125rem}
th,td{padding:6px 12px;text-align:left;border-bottom:1px solid var(--grid);white-space:nowrap}
thead th{position:sticky;top:0;background:var(--surface);color:var(--text-secondary);font-weight:600}
tbody tr:last-child td,tbody tr:last-child th{border-bottom:0}
.num{text-align:right;font-variant-numeric:tabular-nums}
.settings th{font-weight:600;color:var(--text-secondary);width:40%}
.settings th,.settings td{white-space:normal;overflow-wrap:anywhere}
details summary{cursor:pointer;color:var(--text-secondary);font-size:.875rem;margin:16px 0 8px}
.warnings{list-style:none;padding:0;margin:0;display:grid;gap:8px}
.warnings li{display:flex;gap:10px;align-items:flex-start;padding:10px 12px;overflow-wrap:anywhere}
.warnings .icon{width:16px;height:16px;margin-top:3px}
.empty{color:var(--text-secondary);padding:24px 12px;text-align:center;margin:0}
.disclaimer{margin:32px 0 0;padding:12px 14px;font-weight:600}
@media (max-width:520px){
  .plot-grid{grid-template-columns:2.75rem minmax(0,1fr) 3.25rem;grid-template-rows:180px auto}
  .x-axis>span.minor{display:none}
}
@media (forced-colors:active){.plot .line{stroke:CanvasText}.dot{background:CanvasText}}
"""
)

_JS = """(function () {
  "use strict";
  var el = document.getElementById("jeb-chart-data");
  if (!el) return;
  var data = JSON.parse(el.textContent);
  var ts = data.ts, n = ts.length;
  if (n < 2) return;
  var span = ts[n - 1] - ts[0];
  var xs = ts.map(function (t, i) { return span > 0 ? (t - ts[0]) / span : i / (n - 1); });
  function fmt(v, d) {
    var parts = Math.abs(v).toFixed(d).split(".");
    parts[0] = parts[0].replace(/\\B(?=(\\d{3})+(?!\\d))/g, ".");
    var neg = v < 0 && Number(Math.abs(v).toFixed(d)) !== 0;
    return (neg ? "-" : "") + parts.join(",");
  }
  function when(t) { return new Date(t).toISOString().slice(0, 16).replace("T", " ") + " UTC"; }
  function nearest(fx) {
    var lo = 0, hi = n - 1;
    while (hi - lo > 1) { var mid = (lo + hi) >> 1; if (xs[mid] < fx) lo = mid; else hi = mid; }
    return Math.abs(xs[lo] - fx) <= Math.abs(xs[hi] - fx) ? lo : hi;
  }
  Array.prototype.forEach.call(document.querySelectorAll(".plot[data-chart]"), function (plot) {
    var spec = data.charts[plot.getAttribute("data-chart")];
    if (!spec) return;
    var cross = plot.querySelector(".crosshair"), tip = plot.querySelector(".tooltip");
    var dots = spec.series.map(function (s) {
      var d = document.createElement("span");
      d.className = "dot"; d.hidden = true; d.style.setProperty("--c", "var(" + s.color + ")");
      plot.appendChild(d);
      return d;
    });
    var idx = n - 1;
    function yPct(v) { return (1 - (v - spec.lo) / (spec.hi - spec.lo)) * 100; }
    function show(i) {
      idx = i;
      var left = xs[i] * 100;
      cross.style.left = left + "%"; cross.hidden = false;
      spec.series.forEach(function (s, k) {
        dots[k].style.left = left + "%"; dots[k].style.top = yPct(s.v[i]) + "%"; dots[k].hidden = false;
      });
      tip.textContent = "";
      var w = document.createElement("div"); w.className = "when"; w.textContent = when(ts[i]);
      tip.appendChild(w);
      spec.series.forEach(function (s) {
        var row = document.createElement("div"); row.className = "row";
        var key = document.createElement("span"); key.className = "key";
        key.style.setProperty("--c", "var(" + s.color + ")");
        var val = document.createElement("strong"); val.textContent = fmt(s.v[i], spec.decimals) + spec.unit;
        var name = document.createElement("span"); name.className = "name"; name.textContent = s.name;
        row.appendChild(key); row.appendChild(val); row.appendChild(name); tip.appendChild(row);
      });
      tip.hidden = false;
      if (xs[i] > 0.55) { tip.style.left = "auto"; tip.style.right = "calc(" + (100 - left) + "% + 12px)"; }
      else { tip.style.right = "auto"; tip.style.left = "calc(" + left + "% + 12px)"; }
    }
    function hide() {
      cross.hidden = true; tip.hidden = true;
      dots.forEach(function (d) { d.hidden = true; });
    }
    plot.addEventListener("pointermove", function (e) {
      var r = plot.getBoundingClientRect();
      if (r.width > 0) show(nearest((e.clientX - r.left) / r.width));
    });
    plot.addEventListener("pointerleave", hide);
    plot.addEventListener("focus", function () { show(idx); });
    plot.addEventListener("blur", hide);
    plot.addEventListener("keydown", function (e) {
      var step = e.shiftKey ? Math.max(1, Math.round(n / 20)) : 1, i = idx;
      if (e.key === "ArrowLeft") i = Math.max(0, idx - step);
      else if (e.key === "ArrowRight") i = Math.min(n - 1, idx + step);
      else if (e.key === "Home") i = 0;
      else if (e.key === "End") i = n - 1;
      else if (e.key === "Escape") { hide(); return; }
      else return;
      e.preventDefault();
      show(i);
    });
  });
})();"""

_JS_HASH = base64.b64encode(hashlib.sha256(_JS.encode("utf-8")).digest()).decode("ascii")
_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; "
    f"script-src 'sha256-{_JS_HASH}'; img-src data:; base-uri 'none'; form-action 'none'"
)

_ICON_UP = '<path class="icon-good" d="M6 1.5 11 10.5H1Z"/>'
_ICON_DOWN = '<path class="icon-critical" d="M6 10.5 11 1.5H1Z"/>'
_ICON_WARN = (
    '<path class="icon-warning" d="M8 1 15.5 14.5H.5Z"/>'
    '<path class="icon-ink" d="M7.2 5.5h1.6v4.5H7.2zM7.2 11h1.6v1.6H7.2z"/>'
)


# -- small helpers ---------------------------------------------------------------


def _esc(value: object) -> str:
    return html_lib.escape(str(value), quote=True)


def _icon(paths: str, size: int = 12) -> str:
    return f'<svg class="icon" viewBox="0 0 {size} {size}" aria-hidden="true" focusable="false">{paths}</svg>'


def _iso(ts: int | None) -> str:
    if ts is None:
        return "—"
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _human_time(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _fmt_amount(value: float) -> str:
    """Prices/quantities: more decimals for small magnitudes, trailing zeros trimmed."""
    if not math.isfinite(value):
        return "n/d"
    magnitude = abs(value)
    decimals = 2 if magnitude >= 1000 else 4 if magnitude >= 1 else 8
    text = format_number(value, decimals)
    if decimals > 2 and "," in text:
        head, tail = text.split(",")
        tail = tail.rstrip("0").ljust(2, "0")
        text = f"{head},{tail}"
    return text


def _decimals_for(step: float) -> int:
    for d in range(6):
        if abs(round(step, d) - step) < 1e-9 * max(1.0, abs(step)):
            return d
    return 6


def _nice_domain(lo: float, hi: float, max_intervals: int = 6) -> tuple[float, float, list[float]]:
    """Expand [lo, hi] to the tightest clean bounds (1/2/2.5/5 x 10^k steps, <= 6 intervals)."""
    if not (math.isfinite(lo) and math.isfinite(hi)):
        lo, hi = 0.0, 1.0
    if hi - lo < 1e-9:
        pad = max(abs(hi) * 0.01, 1.0)
        lo, hi = lo - pad, hi + pad
    mag = 10 ** math.floor(math.log10((hi - lo) / max_intervals))
    best: tuple[float, float, float] | None = None
    for step in (m * mag for m in (1, 2, 2.5, 5, 10, 20)):
        nlo = math.floor(lo / step + 1e-9) * step
        nhi = math.ceil(hi / step - 1e-9) * step
        if round((nhi - nlo) / step) <= max_intervals and (best is None or nhi - nlo < best[1] - best[0]):
            best = (nlo, nhi, step)
    nlo, nhi, step = best  # type: ignore[misc]  # the x20 step always fits
    count = int(round((nhi - nlo) / step))
    ticks = [round(nlo + i * step, 10) for i in range(count + 1)]
    return ticks[0], ticks[-1], ticks


def downsample_indices(n: int, keys: list[list[float]], max_points: int = MAX_CHART_POINTS) -> list[int]:
    """Indices to plot: first, last, and each bucket's min/max of every key series."""
    if n <= max_points:
        return list(range(n))
    per_bucket = 2 * max(1, len(keys))
    buckets = max(1, (max_points - 2) // per_bucket)
    chosen = {0, n - 1}
    inner = n - 2
    for b in range(buckets):
        lo, hi = 1 + b * inner // buckets, 1 + (b + 1) * inner // buckets
        if hi <= lo:
            continue
        seg = range(lo, hi)
        for series in keys:
            chosen.add(min(seg, key=series.__getitem__))
            chosen.add(max(seg, key=series.__getitem__))
    return sorted(chosen)


# -- chart data ----------------------------------------------------------------


@dataclass(frozen=True)
class _Series:
    name: str
    color: str  # CSS custom property name, e.g. "--series-1"
    values: list[float]


@dataclass(frozen=True)
class _Chart:
    key: str
    title: str
    subtitle: str
    desc: str
    series: list[_Series]
    lo: float
    hi: float
    ticks: list[float]
    unit: str
    baseline: float | None
    area: bool = False


def _clean_curve(curve: list[tuple[int, float]]) -> list[tuple[int, float]]:
    clean = [(int(t), float(v)) for t, v in curve if v is not None and math.isfinite(v)]
    if len(clean) != len(curve):
        logger.warning("dropped %d non-finite points from a curve", len(curve) - len(clean))
    return sorted(clean, key=lambda p: p[0])


def _buy_and_hold_index(ts: list[int], price_curve: list[tuple[int, float]]) -> list[float] | None:
    """Price as-of each equity timestamp, indexed to 100 at the first one."""
    prices = [(t, p) for t, p in _clean_curve(price_curve) if p > 0]
    if not prices or not ts:
        return None
    price_ts = [t for t, _ in prices]
    aligned = [prices[max(0, bisect_right(price_ts, t) - 1)][1] for t in ts]
    return [p / aligned[0] * 100.0 for p in aligned]


def _prepare(
    metrics: PerformanceMetrics,
    equity_curve: list[tuple[int, float]],
    price_curve: list[tuple[int, float]],
) -> tuple[list[int], list[float], list[float] | None, list[float]]:
    curve = _clean_curve(equity_curve)
    ts = [t for t, _ in curve]
    base = metrics.start_equity if metrics.start_equity > 0 else (curve[0][1] if curve else 1.0)
    base = base or 1.0
    strategy = [v / base * 100.0 for _, v in curve]
    bh = _buy_and_hold_index(ts, price_curve)
    dd = [d for _, d in drawdown_curve(curve, metrics.start_equity)]
    return ts, strategy, bh, dd


def _equity_chart(ts: list[int], strategy: list[float], bh: list[float] | None) -> _Chart:
    series = [_Series("Estrategia", "--series-1", strategy)]
    if bh is not None:
        series.append(_Series("Buy & hold", "--context", bh))
    all_values = [v for s in series for v in s.values] + [100.0]
    lo, hi = min(all_values), max(all_values)
    pad = (hi - lo) * 0.04
    lo, hi, ticks = _nice_domain(lo - pad, hi + pad)
    ends = "; ".join(f"{s.name} termina en {format_number(s.values[-1], 1)}" for s in series)
    return _Chart(
        key="equity",
        title="Equity: estrategia vs buy & hold" if bh is not None else "Equity de la estrategia",
        subtitle="Índice base 100 al inicio del periodo",
        desc=f"Líneas indexadas a 100 entre {_human_time(ts[0])} y {_human_time(ts[-1])}. {ends}.",
        series=series,
        lo=lo,
        hi=hi,
        ticks=ticks,
        unit="",
        baseline=100.0,
    )


def _drawdown_chart(ts: list[int], dd: list[float]) -> _Chart:
    worst = min(dd) if dd else 0.0
    lo, _, ticks = _nice_domain(min(worst * 1.05, -1.0), 0.0)  # never zoom below 1 %
    return _Chart(
        key="drawdown",
        title="Drawdown de la estrategia",
        subtitle="Caída (%) desde el máximo previo de equity",
        desc=(
            f"Área bajo cero entre {_human_time(ts[0])} y {_human_time(ts[-1])}. "
            f"Peor caída {format_pct(-worst)}; valor final {format_pct(dd[-1])}."
        ),
        series=[_Series("Drawdown", "--series-1", dd)],
        lo=lo,
        hi=0.0,
        ticks=[t for t in ticks if t <= 0],
        unit=" %",
        baseline=0.0,
        area=True,
    )


# -- chart rendering -----------------------------------------------------------


def _x_fracs(ts: list[int]) -> list[float]:
    span = ts[-1] - ts[0]
    if span <= 0:
        return [i / max(1, len(ts) - 1) for i in range(len(ts))]
    return [(t - ts[0]) / span for t in ts]


def _y_frac(chart: _Chart, value: float) -> float:
    return 1.0 - (value - chart.lo) / (chart.hi - chart.lo)


def _points(chart: _Chart, xs: list[float], values: list[float]) -> str:
    return " ".join(
        f"{x * _VIEW:.1f},{_y_frac(chart, v) * _VIEW:.1f}" for x, v in zip(xs, values)
    )


def _svg(chart: _Chart, xs: list[float]) -> str:
    grid = "".join(
        f'<line x1="0" x2="{_VIEW}" y1="{_y_frac(chart, t) * _VIEW:.1f}" y2="{_y_frac(chart, t) * _VIEW:.1f}"/>'
        for t in chart.ticks
    )
    parts = [f'<g class="grid">{grid}</g>']
    if chart.baseline is not None and chart.lo <= chart.baseline <= chart.hi:
        y = _y_frac(chart, chart.baseline) * _VIEW
        parts.append(f'<line class="baseline" x1="0" x2="{_VIEW}" y1="{y:.1f}" y2="{y:.1f}"/>')
    if chart.area:
        values = chart.series[0].values
        y0 = _y_frac(chart, 0.0) * _VIEW
        path = f"M0,{y0:.1f} L{_points(chart, xs, values).replace(' ', ' L')} L{_VIEW},{y0:.1f} Z"
        parts.append(f'<path class="area" d="{path}"/>')
    for series in reversed(chart.series):  # emphasized series drawn last (on top)
        cls = "line-series-1" if series.color == "--series-1" else "line-context"
        parts.append(
            f'<polyline class="line {cls}" points="{_points(chart, xs, series.values)}"/>'
        )
    tid, did = f"{chart.key}-title", f"{chart.key}-desc"
    return (
        f'<svg viewBox="0 0 {_VIEW} {_VIEW}" preserveAspectRatio="none" role="img" '
        f'aria-labelledby="{tid} {did}" focusable="false">'
        f'<title id="{tid}">{_esc(chart.title)}</title><desc id="{did}">{_esc(chart.desc)}</desc>'
        f'{"".join(parts)}</svg>'
    )


def _y_axis(chart: _Chart) -> str:
    step = chart.ticks[1] - chart.ticks[0] if len(chart.ticks) > 1 else 1.0
    decimals = _decimals_for(abs(step))
    spans = "".join(
        f'<span style="top:{_y_frac(chart, t) * 100:.2f}%">{_esc(format_number(t, decimals))}</span>'
        for t in chart.ticks
    )
    return f'<div class="y-axis" aria-hidden="true">{spans}</div>'


def _x_axis(ts: list[int]) -> str:
    fmt = "%Y-%m-%d" if ts[-1] - ts[0] >= 2 * 86_400_000 else "%m-%d %H:%M"
    fracs = (0.0, 0.25, 0.5, 0.75, 1.0)
    spans = []
    for i, frac in enumerate(fracs):
        t = int(ts[0] + (ts[-1] - ts[0]) * frac)
        label = datetime.fromtimestamp(t / 1000, tz=timezone.utc).strftime(fmt)
        cls = "first" if i == 0 else "last" if i == len(fracs) - 1 else "minor" if i % 2 else ""
        spans.append(f'<span class="{cls}" style="left:{frac * 100:.0f}%">{_esc(label)}</span>')
    return f'<div class="x-axis" aria-hidden="true">{"".join(spans)}</div>'


def _end_labels(chart: _Chart) -> str:
    """Direct end labels; the context label is dropped when it would collide."""
    placed: list[float] = []
    spans = []
    for series in chart.series:
        y = _y_frac(chart, series.values[-1])
        if any(abs(y - other) < 0.09 for other in placed):
            continue
        placed.append(y)
        spans.append(
            f'<span style="top:{y * 100:.2f}%"><span class="key" style="--c:var({series.color})"></span>'
            f"{_esc(format_number(series.values[-1], 1))}</span>"
        )
    return f'<div class="end-labels" aria-hidden="true">{"".join(spans)}</div>'


def _legend(chart: _Chart) -> str:
    if len(chart.series) < 2:
        return ""
    items = "".join(
        f'<li><span class="key" style="--c:var({s.color})"></span>{_esc(s.name)}</li>'
        for s in chart.series
    )
    return f'<ul class="legend" aria-label="Leyenda">{items}</ul>'


def _chart_figure(chart: _Chart, ts: list[int], xs: list[float]) -> str:
    return (
        f'<figure class="chart" id="chart-{chart.key}">'
        f"<h3>{_esc(chart.title)}</h3><p class=\"sub\">{_esc(chart.subtitle)}</p>"
        f"{_legend(chart)}"
        '<div class="plot-grid">'
        f"{_y_axis(chart)}"
        f'<div class="plot" data-chart="{chart.key}" tabindex="0" role="group" '
        f'aria-label="{_esc(chart.title)}. Use las flechas para recorrer los valores.">'
        f"{_svg(chart, xs)}"
        '<div class="crosshair" hidden></div><div class="tooltip" aria-live="polite" hidden></div>'
        "</div>"
        f"{_end_labels(chart)}{_x_axis(ts)}"
        "</div></figure>"
    )


def _chart_json(ts: list[int], charts: list[_Chart]) -> str:
    payload = {
        "ts": ts,
        "charts": {
            c.key: {
                "lo": c.lo,
                "hi": c.hi,
                "unit": c.unit,
                "decimals": 2,
                "series": [
                    {"name": s.name, "color": s.color, "v": [round(v, 4) for v in s.values]}
                    for s in c.series
                ],
            }
            for c in charts
        },
    }
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _data_table(ts: list[int], strategy: list[float], bh: list[float] | None, dd: list[float]) -> str:
    head = '<th scope="col">Fecha (UTC)</th><th scope="col" class="num">Estrategia (base 100)</th>'
    if bh is not None:
        head += '<th scope="col" class="num">Buy &amp; hold (base 100)</th>'
    head += '<th scope="col" class="num">Drawdown (%)</th>'
    rows = []
    for i, t in enumerate(ts):
        cells = f"<td>{_iso(t)}</td><td class=\"num\">{format_number(strategy[i])}</td>"
        if bh is not None:
            cells += f'<td class="num">{format_number(bh[i])}</td>'
        cells += f'<td class="num">{format_number(dd[i])}</td>'
        rows.append(f"<tr>{cells}</tr>")
    return (
        "<details><summary>Ver los datos de los gráficos como tabla</summary>"
        '<div class="table-scroll tall"><table><thead><tr>'
        f"{head}</tr></thead><tbody>{''.join(rows)}</tbody></table></div></details>"
    )


def _charts_section(
    metrics: PerformanceMetrics,
    equity_curve: list[tuple[int, float]],
    price_curve: list[tuple[int, float]],
) -> tuple[str, str]:
    """Return (section html, chart JSON or "")."""
    ts, strategy, bh, dd = _prepare(metrics, equity_curve, price_curve)
    total_points = len(ts)
    if total_points < 2:
        body = '<p class="empty">Sin datos suficientes para graficar la equity.</p>'
        return f'<section aria-labelledby="h-charts"><h2 id="h-charts">Gráficos</h2><div class="card">{body}</div></section>', ""
    keys = [strategy, dd] + ([bh] if bh is not None else [])
    idx = downsample_indices(len(ts), keys)
    ts = [ts[i] for i in idx]
    strategy = [strategy[i] for i in idx]
    dd = [dd[i] for i in idx]
    bh = None if bh is None else [bh[i] for i in idx]
    charts = [_equity_chart(ts, strategy, bh), _drawdown_chart(ts, dd)]
    xs = _x_fracs(ts)
    figures = "".join(_chart_figure(c, ts, xs) for c in charts)
    note = ""
    if len(idx) < total_points:
        note = (
            f'<p class="note">Curvas reducidas a {format_number(len(idx), 0)} puntos '
            f"(de {format_number(total_points, 0)}) conservando máximos y mínimos.</p>"
        )
    section = (
        '<section aria-labelledby="h-charts"><h2 id="h-charts">Gráficos</h2>'
        f'<div class="card">{figures}{note}{_data_table(ts, strategy, bh, dd)}</div></section>'
    )
    return section, _chart_json(ts, charts)


# -- other sections ------------------------------------------------------------


def _tile(label: str, value: str, sub: str = "") -> str:
    sub_html = f'<p class="sub">{sub}</p>' if sub else ""
    return (
        f'<li class="tile"><p class="label">{_esc(label)}</p>'
        f'<p class="value">{_esc(value)}</p>{sub_html}</li>'
    )


def _return_delta(m: PerformanceMetrics) -> str:
    if m.buy_and_hold_return_pct is None:
        return "<span>Sin referencia de buy &amp; hold</span>"
    diff = m.total_return_pct - m.buy_and_hold_return_pct
    bh = _esc(format_pct(m.buy_and_hold_return_pct, signed=True))
    if abs(diff) < 0.005:
        return f"<span>Igual que buy &amp; hold ({bh})</span>"
    icon, word = (_ICON_UP, "sobre") if diff > 0 else (_ICON_DOWN, "bajo")
    text = f"{format_number(diff, 2, signed=True)} pp {word} buy & hold ({format_pct(m.buy_and_hold_return_pct, signed=True)})"
    return f"{_icon(icon)}<span>{_esc(text)}</span>"


def _tiles(m: PerformanceMetrics) -> str:
    has_trades = m.num_trades > 0
    pf = "n/d (sin pérdidas)" if m.profit_factor is None else format_number(m.profit_factor)
    trade_sub = f"Media {format_pct(m.avg_trade_pct, signed=True)}" if has_trades else "Sin operaciones cerradas"
    if m.exposure_pct is not None:
        trade_sub += f" · exposición {format_pct(m.exposure_pct, 1)}"
    tiles = [
        _tile("Retorno total", format_pct(m.total_return_pct, signed=True), _return_delta(m)),
        _tile("Máx. drawdown", format_pct(m.max_drawdown_pct), "<span>Caída desde el máximo</span>"),
        _tile("Sharpe", format_number(m.sharpe), f"<span>{_esc('Sortino ' + format_number(m.sortino))} · anualizados</span>"),
        _tile(
            "Tasa de acierto",
            format_pct(m.win_rate_pct, 1) if has_trades else "n/d",
            f"<span>{_esc('Profit factor ' + pf)}</span>" if has_trades else "",
        ),
        _tile("Operaciones", format_number(m.num_trades, 0), f"<span>{_esc(trade_sub)}</span>"),
        _tile("Comisiones", format_number(m.fees_paid), "<span>En moneda de cotización</span>"),
        _tile(
            "Coste LLM",
            f"US$ {format_number(m.llm_cost_usd, 4)}",
            f"<span>{_esc(format_number(m.llm_calls, 0))} llamadas</span>",
        ),
    ]
    return (
        '<section aria-labelledby="h-metrics"><h2 id="h-metrics">Métricas</h2>'
        f'<ul class="tiles">{"".join(tiles)}</ul></section>'
    )


def _trades_section(trades: list[TradeRecord]) -> str:
    head = '<section aria-labelledby="h-trades"><h2 id="h-trades">Operaciones</h2>'
    if not trades:
        return head + '<div class="card"><p class="empty">Sin operaciones cerradas en este periodo.</p></div></section>'
    ordered = sorted(trades, key=lambda t: t.exit_time)
    shown = ordered[-MAX_TRADE_ROWS:]
    offset = len(ordered) - len(shown)
    cols = [
        ("#", True), ("Entrada (UTC)", False), ("Salida (UTC)", False), ("Precio entrada", True),
        ("Precio salida", True), ("Cantidad", True), ("PnL", True), ("PnL %", True),
        ("Motivo de salida", False),
    ]
    header = "".join(
        f'<th scope="col"{_NUM_CLASS if num else ""}>{_esc(name)}</th>' for name, num in cols
    )
    rows = []
    for i, t in enumerate(shown, start=offset + 1):
        rows.append(
            "<tr>"
            f'<td class="num">{i}</td><td>{_esc(_iso(t.entry_time))}</td><td>{_esc(_iso(t.exit_time))}</td>'
            f'<td class="num">{_esc(_fmt_amount(t.entry_price))}</td>'
            f'<td class="num">{_esc(_fmt_amount(t.exit_price))}</td>'
            f'<td class="num">{_esc(_fmt_amount(t.quantity))}</td>'
            f'<td class="num">{_esc(format_number(t.pnl, 2, signed=True))}</td>'
            f'<td class="num">{_esc(format_pct(t.pnl_pct, 2, signed=True))}</td>'
            f"<td>{_esc(t.exit_reason or '—')}</td>"
            "</tr>"
        )
    note = ""
    if offset:
        note = (
            f'<p class="note">Mostrando las últimas {format_number(len(shown), 0)} de '
            f"{format_number(len(ordered), 0)} operaciones.</p>"
        )
    return (
        head + '<div class="table-scroll tall"><table><thead><tr>'
        f"{header}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>{note}</section>"
    )


def _settings_section(settings_summary: dict[str, str]) -> str:
    head = '<section aria-labelledby="h-settings"><h2 id="h-settings">Configuración</h2>'
    if not settings_summary:
        return head + '<div class="card"><p class="empty">Sin configuración registrada.</p></div></section>'
    rows = []
    for key, value in settings_summary.items():
        shown = "••••" if _SECRET_KEY_RE.search(str(key)) and value else value
        rows.append(f'<tr><th scope="row">{_esc(key)}</th><td>{_esc(shown)}</td></tr>')
    return (
        head + '<div class="table-scroll"><table class="settings"><tbody>'
        f"{''.join(rows)}</tbody></table></div></section>"
    )


def _warnings_section(warnings: list[str] | None) -> str:
    items = [w for w in (warnings or []) if str(w).strip()]
    if not items:
        return ""
    lis = "".join(
        f"<li>{_icon(_ICON_WARN, 16)}<span><strong>Aviso:</strong> {_esc(w)}</span></li>" for w in items
    )
    return (
        '<section aria-labelledby="h-warnings"><h2 id="h-warnings">Advertencias</h2>'
        f'<ul class="warnings">{lis}</ul></section>'
    )


def _period(equity_curve: list[tuple[int, float]], price_curve: list[tuple[int, float]], bars: int) -> str:
    stamps = [t for t, _ in equity_curve] or [t for t, _ in price_curve]
    if not stamps:
        return "Periodo: sin datos"
    return (
        f"Periodo: {_human_time(min(stamps))} → {_human_time(max(stamps))} · "
        f"{format_number(bars, 0)} velas"
    )


# -- public API ----------------------------------------------------------------


def render_html_report(
    metrics: PerformanceMetrics,
    equity_curve: list[tuple[int, float]],
    price_curve: list[tuple[int, float]],
    trades: list[TradeRecord],
    title: str,
    settings_summary: dict[str, str],
    warnings: list[str] | None = None,
) -> str:
    """Render one self-contained HTML report (inline CSS/SVG/JS, no external requests)."""
    charts_html, chart_json = _charts_section(metrics, equity_curve, price_curve)
    scripts = ""
    if chart_json:
        scripts = (
            f'<script type="application/json" id="jeb-chart-data">{chart_json}</script>'
            f"<script>{_JS}</script>"
        )
    return (
        "<!DOCTYPE html>\n"
        '<html lang="es"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<meta http-equiv="Content-Security-Policy" content="{_CSP}">'
        '<meta name="color-scheme" content="light dark">'
        f"<title>{_esc(title)}</title><style>{_CSS}</style></head><body><main>"
        f'<header><h1>{_esc(title)}</h1><p class="period">'
        f"{_esc(_period(equity_curve, price_curve, metrics.bars))}</p></header>"
        f"{_tiles(metrics)}{charts_html}{_trades_section(trades)}"
        f"{_settings_section(settings_summary)}{_warnings_section(warnings)}"
        f'<p class="disclaimer" role="note">{_esc(DISCLAIMER)}</p>'
        f"</main>{scripts}</body></html>\n"
    )


def write_report(path: str | Path, html: str) -> Path:
    """Write the report as UTF-8, creating parent directories. Returns the path."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html, encoding="utf-8")
    logger.info("report written to %s", target)
    return target
