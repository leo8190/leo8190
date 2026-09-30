"""Risk manager: the final word on every order.

It sizes entries from the stop distance (fixed fractional risk), enforces position,
confidence, trade-count and cooldown limits, runs the protective stop/take-profit
checks and the kill switch (daily loss, drawdown). Exits that reduce risk are always
allowed, even while trading is halted.
"""

from __future__ import annotations

import logging
import math
import re

from .config import RiskConfig
from .models import Action, ConfigError, Decision, MarketSnapshot, OrderRequest, PortfolioView, RiskVerdict, Side
from .portfolio import Portfolio

logger = logging.getLogger(__name__)

DAILY_LOSS = "daily_loss"
MAX_DRAWDOWN = "max_drawdown"
STOP_LOSS = "stop_loss"
TAKE_PROFIT = "take_profit"

CASH_BUFFER = 0.995  # keep 0.5 % of the cash free for price moves and rounding
# A partial SELL must leave at least min notional x this (slippage, fees and the move
# between the decision close and the fill); otherwise the whole position is sold.
PARTIAL_SELL_MARGIN = 1.05

_TF_RE = re.compile(r"([1-9][0-9]*)([smhdw])")
_TF_UNIT_MS = {"s": 1_000, "m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}


def timeframe_ms(timeframe: str) -> int:
    """Timeframe length in ms (shared helper when available, small local parser otherwise)."""
    try:
        from .market.timeframes import timeframe_to_ms
    except ImportError:  # pragma: no cover - the shared helper exists in the full package
        return _parse_timeframe(timeframe)
    return timeframe_to_ms(timeframe)


def _parse_timeframe(timeframe: str) -> int:
    match = _TF_RE.fullmatch(timeframe or "")
    if match is None:
        raise ValueError(f"invalid timeframe {timeframe!r}")
    amount, unit = match.groups()
    return int(amount) * _TF_UNIT_MS[unit]


def _finite(value: float | None) -> bool:
    return value is None or (isinstance(value, (int, float)) and math.isfinite(value))


def _short(text: str, limit: int = 120) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _reject(*reasons: str) -> RiskVerdict:
    return RiskVerdict(approved=False, reasons=list(reasons))


class RiskManager:
    """Validates decisions and turns approved ones into sized OrderRequests."""

    def __init__(self, config: RiskConfig, fee_pct: float, slippage_pct: float = 0.0) -> None:
        for name, value in (("fee_pct", fee_pct), ("slippage_pct", slippage_pct)):
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ConfigError(f"{name} must be a finite number >= 0, got {value!r}")
        if not 0 < config.min_stop_pct <= config.max_stop_pct:
            raise ConfigError("risk config needs 0 < min_stop_pct <= max_stop_pct (sizing divides by the stop)")
        self.config = config
        self.fee_pct = float(fee_pct)
        self.slippage_pct = float(slippage_pct)

    # ------------------------------------------------------------------ protective exits

    def check_protective_exit(
        self, portfolio: Portfolio, low: float, high: float, open_price: float | None = None
    ) -> tuple[str, float] | None:
        """(reason, exit price) if the candle touched the stop or the take-profit.

        Both touched in the same candle -> stop_loss (we cannot know which came first).
        With ``open_price`` a gap through a level exits at the open: below the stop that
        is worse than the stop, above the take-profit it is better than the target.
        """
        if not portfolio.in_position:
            return None
        if not all(_finite(v) for v in (low, high, open_price)):
            logger.warning("protective check skipped: non-finite candle values low=%r high=%r", low, high)
            return None
        stop, target = portfolio.stop_loss, portfolio.take_profit
        gapped = open_price is not None and open_price > 0
        if stop is not None and low <= stop:
            return STOP_LOSS, min(stop, open_price) if gapped else stop
        if target is not None and high >= target:
            return TAKE_PROFIT, max(target, open_price) if gapped else target
        return None

    # ------------------------------------------------------------------ kill switch

    def update_kill_switch(self, portfolio: Portfolio, equity: float) -> str | None:
        """Trip the kill switch when limits are breached; return the active halt reason.

        max_drawdown (loss from the equity peak) halts until ``reset_kill_switch``;
        daily_loss (loss since the UTC day start) halts until the next UTC day.
        The halt is stored on the portfolio so it survives restarts.
        """
        if not math.isfinite(equity):
            raise ValueError(f"equity must be finite, got {equity!r}")
        cfg = self.config
        if portfolio.halted_reason == MAX_DRAWDOWN:
            return MAX_DRAWDOWN
        drawdown = portfolio.drawdown_pct(equity)
        if drawdown >= cfg.max_drawdown_pct:
            portfolio.halt(MAX_DRAWDOWN)
            logger.warning(
                "KILL SWITCH max_drawdown: %.2f%% from peak %.6g >= %.2f%%; halted until reset",
                drawdown, portfolio.equity_peak or 0.0, cfg.max_drawdown_pct,
            )
            return MAX_DRAWDOWN
        if portfolio.halted_reason is not None:
            return portfolio.halted_reason
        daily_loss = portfolio.daily_pnl_pct(equity)
        if daily_loss >= cfg.max_daily_loss_pct:
            portfolio.halt(DAILY_LOSS)
            logger.warning(
                "KILL SWITCH daily_loss: %.2f%% since UTC day start >= %.2f%%; halted for the day",
                daily_loss, cfg.max_daily_loss_pct,
            )
            return DAILY_LOSS
        return None

    def reset_kill_switch(self, portfolio: Portfolio, equity: float | None = None) -> None:
        """Clear any halt and re-baseline the peak and day start so it does not re-trip at once."""
        baseline = equity if equity is not None else portfolio.last_equity
        previous = portfolio.halted_reason
        portfolio.clear_halt()
        if baseline is not None and math.isfinite(baseline):
            portfolio.equity_peak = float(baseline)
            portfolio.day_start_equity = float(baseline)
        logger.warning("kill switch reset (was %s), baseline equity %s", previous, baseline)

    # ------------------------------------------------------------------ decisions

    def evaluate(
        self,
        decision: Decision,
        view: PortfolioView,
        snapshot: MarketSnapshot,
        portfolio: Portfolio,
    ) -> RiskVerdict:
        """Approve (with a sized order) or reject a decision, with human-readable reasons."""
        if decision.action is Action.HOLD:
            return _reject("HOLD: nothing to do")
        problem = self._invalid_inputs(decision, view, snapshot, portfolio)
        if problem:
            logger.warning("risk rejected %s: %s", decision.action.value, problem)
            return _reject(problem)
        if decision.action is Action.BUY:
            return self._evaluate_buy(decision, view, snapshot, portfolio)
        return self._evaluate_sell(decision, view, snapshot, portfolio)

    def _invalid_inputs(
        self, decision: Decision, view: PortfolioView, snapshot: MarketSnapshot, portfolio: Portfolio
    ) -> str | None:
        numbers = {
            "confidence": decision.confidence,
            "size_pct": decision.size_pct,
            "stop_loss_pct": decision.stop_loss_pct,
            "take_profit_pct": decision.take_profit_pct,
            "price": snapshot.price,
            "cash": view.cash,
            "base_qty": view.base_qty,
            "equity": view.equity,
        }
        bad = [name for name, value in numbers.items() if not _finite(value)]
        if bad:
            return f"non-finite value(s): {', '.join(bad)}"
        if snapshot.price <= 0:
            return f"invalid price {snapshot.price}"
        if snapshot.symbol != portfolio.symbol:
            return f"snapshot symbol {snapshot.symbol} does not match portfolio symbol {portfolio.symbol}"
        return None

    # ------------------------------------------------------------------ BUY

    def _evaluate_buy(
        self, decision: Decision, view: PortfolioView, snapshot: MarketSnapshot, portfolio: Portfolio
    ) -> RiskVerdict:
        reasons = self._buy_gate_reasons(decision, snapshot, portfolio)
        if reasons:
            return _reject(*reasons)
        cfg = self.config
        price = snapshot.price
        equity = view.equity
        if equity <= 0:
            return _reject(f"equity {equity:.2f} {view.quote_currency} is not positive")

        stop_pct, tp_pct = self.stop_and_target_pct(decision)
        value, binding, risk_amount = self.buy_value(decision, view, stop_pct)
        if value < cfg.min_order_notional:
            return _reject(
                f"order value {value:.2f} {view.quote_currency} below min notional "
                f"{cfg.min_order_notional:.2f} (limited by {binding})"
            )

        quantity = value / price
        stop_loss = price * (1.0 - stop_pct / 100.0)
        take_profit = price * (1.0 + tp_pct / 100.0)
        reason = (
            f"BUY {decision.source} conf {decision.confidence:.2f}: {value:.2f} {view.quote_currency} "
            f"({value / equity * 100:.1f}% of equity, limited by {binding}); risk {risk_amount:.2f} "
            f"over stop {stop_pct:.2f}% @ {stop_loss:.8g}, tp {tp_pct:.2f}% @ {take_profit:.8g}"
        )
        if decision.reasoning:
            reason += f" | {_short(decision.reasoning)}"
        order = OrderRequest(
            symbol=portfolio.symbol, side=Side.BUY, quantity=quantity, reference_price=price, reason=reason
        )
        return RiskVerdict(
            approved=True,
            order=order,
            reasons=[f"size limited by {binding}"],
            stop_loss=stop_loss,
            take_profit=take_profit,
        )

    def _buy_gate_reasons(self, decision: Decision, snapshot: MarketSnapshot, portfolio: Portfolio) -> list[str]:
        cfg = self.config
        reasons: list[str] = []
        halt = portfolio.active_halt(snapshot.timestamp)
        if halt:
            reasons.append(f"trading halted by kill switch ({halt})")
        if portfolio.in_position:
            reasons.append(
                f"already holding {portfolio.qty:.8g} {portfolio.base_currency} (no pyramiding in v1)"
            )
        if decision.confidence < cfg.min_confidence:
            reasons.append(f"confidence {decision.confidence:.2f} below minimum {cfg.min_confidence:.2f}")
        trades = portfolio.trades_on(snapshot.timestamp)
        if trades >= cfg.max_trades_per_day:
            reasons.append(f"daily trade limit reached ({trades}/{cfg.max_trades_per_day} fills today)")
        cooldown = self._cooldown_reason(snapshot, portfolio)
        if cooldown:
            reasons.append(cooldown)
        return reasons

    def _cooldown_reason(self, snapshot: MarketSnapshot, portfolio: Portfolio) -> str | None:
        need = self.config.cooldown_candles
        if need <= 0 or portfolio.last_exit_ts is None:
            return None
        try:
            step = timeframe_ms(snapshot.timeframe)
        except ValueError:
            return f"cannot check cooldown: invalid timeframe {snapshot.timeframe!r}"
        # Both times on the candle grid: the snapshot is a candle OPEN time, while a live
        # exit is stamped with the wall clock (close + grace) and a backtest exit with the
        # open of its candle. Counting candles from the open of the exit candle makes both
        # block exactly ``need`` decisions on the candles that close after the exit.
        from .market.timeframes import floor_to_timeframe

        exit_candle = floor_to_timeframe(portfolio.last_exit_ts, snapshot.timeframe)
        elapsed = (snapshot.timestamp - exit_candle) // step
        if elapsed < need:
            return f"cooldown: {max(elapsed, 0)} of {need} candles since the last exit"
        return None

    def stop_and_target_pct(self, decision: Decision) -> tuple[float, float]:
        """(stop_pct, take_profit_pct) after defaults, clamping and the minimum reward rule."""
        cfg = self.config
        stop_pct = decision.stop_loss_pct or cfg.default_stop_pct
        stop_pct = min(max(stop_pct, cfg.min_stop_pct), cfg.max_stop_pct)
        tp_pct = decision.take_profit_pct or cfg.default_take_profit_pct
        if tp_pct < stop_pct:
            tp_pct = max(cfg.default_take_profit_pct, 1.5 * stop_pct)
        return stop_pct, tp_pct

    def buy_value(self, decision: Decision, view: PortfolioView, stop_pct: float) -> tuple[float, str, float]:
        """(order value in quote, binding limit name, risk amount) for a BUY."""
        cfg = self.config
        equity = view.equity
        risk_amount = equity * cfg.risk_per_trade_pct / 100.0
        round_trip_cost = 2.0 * (self.fee_pct + self.slippage_pct) / 100.0
        caps: list[tuple[str, float]] = [
            (f"risk {cfg.risk_per_trade_pct:g}% of equity", risk_amount / (stop_pct / 100.0 + round_trip_cost)),
            (f"max position {cfg.max_position_pct:g}% of equity", equity * cfg.max_position_pct / 100.0),
        ]
        if 0 < decision.size_pct < 1:
            caps.append((f"engine size {decision.size_pct:.2f} of equity", decision.size_pct * equity))
        cash_cap = max(view.cash, 0.0) / (1.0 + self.fee_pct / 100.0 + self.slippage_pct / 100.0) * CASH_BUFFER
        caps.append(("available cash", cash_cap))
        binding, value = min(caps, key=lambda cap: cap[1])
        return max(value, 0.0), binding, risk_amount

    # ------------------------------------------------------------------ SELL

    def _evaluate_sell(
        self, decision: Decision, view: PortfolioView, snapshot: MarketSnapshot, portfolio: Portfolio
    ) -> RiskVerdict:
        cfg = self.config
        held = portfolio.managed_qty(view.base_qty)
        if held <= 0:
            if portfolio.in_position:
                return _reject(
                    f"broker reports {view.base_qty:.8g} {view.base_currency}: nothing of the tracked "
                    f"position is available to sell"
                )
            return _reject("no position to sell (spot long-only)")
        min_conf = cfg.min_confidence / 2.0
        if decision.confidence < min_conf:
            return _reject(f"exit confidence {decision.confidence:.2f} below minimum {min_conf:.2f}")

        price = snapshot.price
        fraction = decision.size_pct if 0 < decision.size_pct <= 1 else 1.0
        quantity = held * fraction
        note = f"{fraction * 100:.0f}% of position"
        if quantity < held:
            remaining_value = (held - quantity) * price
            order_value = quantity * price
            # The remainder must stay sellable at the fill price too, not only at this close.
            too_small = cfg.min_order_notional * PARTIAL_SELL_MARGIN
            if remaining_value < too_small or order_value < cfg.min_order_notional:
                quantity = held
                note = f"whole position (partial {fraction * 100:.0f}% would leave or trade < min notional)"
        reason = (
            f"SELL {decision.source} conf {decision.confidence:.2f}: {note}, "
            f"{quantity:.8g} {view.base_currency} ~{quantity * price:.2f} {view.quote_currency}"
        )
        if decision.reasoning:
            reason += f" | {_short(decision.reasoning)}"
        order = OrderRequest(
            symbol=portfolio.symbol, side=Side.SELL, quantity=quantity, reference_price=price, reason=reason
        )
        return RiskVerdict(approved=True, order=order, reasons=[note])

    def forced_exit_order(
        self, view: PortfolioView, reason: str, price: float | None = None
    ) -> OrderRequest | None:
        """Order that flattens the managed position (kill switch / protective exit).

        Returns None when there is nothing to sell. ``price`` defaults to the view's
        mark price (position_value / base_qty).
        """
        if not view.in_position or not math.isfinite(view.base_qty):
            return None
        reference = price if price is not None else view.position_value / view.base_qty
        return OrderRequest(
            symbol=f"{view.base_currency}/{view.quote_currency}",
            side=Side.SELL,
            quantity=view.base_qty,
            reference_price=reference,
            reason=f"forced exit ({reason}): {view.base_qty:.8g} {view.base_currency}",
        )
