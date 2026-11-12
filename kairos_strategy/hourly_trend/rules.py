"""Hourly trend candidates decided on every closed quarter hour.

The rules are the ones the live runner (``kairos-autotrader``) has traded on EVEDEX since
2026-11-11: with a four-hour trend, take a pullback to the hourly EMA that closes back
with the trend, or a close beyond the prior 20-hour extreme. They are ported here unchanged
so that research and runtime share one implementation. The namespace is isolated: it is
not in the legacy registry, so no existing source fingerprint changes.

Decisions are made at every closed 15-minute bar. The rules always read one-hour bars: at
each decision the hourly bars are rebuilt so that the newest one ends at that decision, and
the four-hour trend bars are built from those hourly bars. Bars on both sides of a gap are
used as neighbours, exactly as the runner did; only the last two hourly bars must be
consecutive.

No result is claimed. On EVEDEX minute history of October-November 2026 the unfiltered
candidates averaged about -0.08 R after costs.
"""

from __future__ import annotations

import hashlib
import json
import math
from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass

from kairos_core.enums import Side

from ..candles import Candle
from ..models import ExitPlan, SleeveIntent
from ..timeframes import aggregate

_QUARTER_MS = 15 * 60 * 1_000
_HOUR_MS = 60 * 60 * 1_000
_FOUR_HOURS_MS = 4 * _HOUR_MS
STRATEGY_ID = "hourly_trend_candidates_v1"
STRATEGY_REVISION = "1"

SETUP_PULLBACK_RECLAIM = "PULLBACK_RECLAIM"
SETUP_BREAKOUT = "BREAKOUT"


@dataclass(frozen=True, slots=True)
class HourlyTrendCandidatesConfig:
    """Fixed controls of the hourly trend-candidate rules."""

    history_hours: int = 504
    signal_window_bars: int = 300
    minimum_signal_bars: int = 60
    minimum_trend_bars: int = 60
    signal_ema_period: int = 20
    trend_fast_ema_period: int = 20
    trend_slow_ema_period: int = 50
    trend_efficiency_lookback: int = 20
    minimum_trend_efficiency: float = 0.2
    rsi_period: int = 14
    atr_period: int = 14
    pullback_touch_bars: int = 3
    breakout_lookback_bars: int = 20
    pullback_stop_buffer_atr: float = 0.25
    breakout_stop_atr: float = 1.2
    minimum_stop_distance_atr: float = 0.8
    maximum_stop_distance_atr: float = 2.5
    long_pullback_rsi_minimum: float = 35.0
    long_pullback_rsi_maximum: float = 68.0
    long_breakout_rsi_maximum: float = 75.0
    short_pullback_rsi_minimum: float = 32.0
    short_pullback_rsi_maximum: float = 65.0
    short_breakout_rsi_minimum: float = 25.0
    target_reward_to_risk: float = 1.5
    max_hold_minutes: int = 720
    intent_valid_minutes: int = 15

    def __post_init__(self) -> None:
        integer_names = (
            "history_hours",
            "signal_window_bars",
            "minimum_signal_bars",
            "minimum_trend_bars",
            "signal_ema_period",
            "trend_fast_ema_period",
            "trend_slow_ema_period",
            "trend_efficiency_lookback",
            "rsi_period",
            "atr_period",
            "pullback_touch_bars",
            "breakout_lookback_bars",
            "max_hold_minutes",
            "intent_valid_minutes",
        )
        for name in integer_names:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.trend_fast_ema_period >= self.trend_slow_ema_period:
            raise ValueError("trend fast EMA period must be below the slow EMA period")
        if self.minimum_signal_bars > self.signal_window_bars:
            raise ValueError("minimum_signal_bars cannot exceed signal_window_bars")
        if self.signal_window_bars > self.history_hours:
            raise ValueError("signal_window_bars cannot exceed history_hours")
        float_names = (
            "minimum_trend_efficiency",
            "pullback_stop_buffer_atr",
            "breakout_stop_atr",
            "minimum_stop_distance_atr",
            "maximum_stop_distance_atr",
            "long_pullback_rsi_minimum",
            "long_pullback_rsi_maximum",
            "long_breakout_rsi_maximum",
            "short_pullback_rsi_minimum",
            "short_pullback_rsi_maximum",
            "short_breakout_rsi_minimum",
            "target_reward_to_risk",
        )
        for name in float_names:
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be finite and positive")
            object.__setattr__(self, name, float(value))
        if self.minimum_trend_efficiency > 1:
            raise ValueError("minimum_trend_efficiency must not exceed one")
        if self.maximum_stop_distance_atr < self.minimum_stop_distance_atr:
            raise ValueError("maximum stop distance cannot be below the minimum")
        if self.long_pullback_rsi_minimum > self.long_pullback_rsi_maximum:
            raise ValueError("long pullback RSI bounds are inverted")
        if self.short_pullback_rsi_minimum > self.short_pullback_rsi_maximum:
            raise ValueError("short pullback RSI bounds are inverted")

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class HourlyTrendState:
    """What the rules saw at one decision, whether or not a candidate followed.

    ``trend`` is ``UP``, ``DOWN`` or ``FLAT``. The remaining fields describe the newest
    hourly bar and its recent history; they are evidence for a reviewer, not signals.
    """

    symbol: str
    decision_ts_ms: int
    price: float
    trend: str
    trend_ema_fast: float
    trend_ema_slow: float
    trend_efficiency: float
    rsi: float
    atr: float
    atr_pct: float
    change_1h_pct: float
    change_4h_pct: float
    change_24h_pct: float
    change_7d_pct: float
    dist_ema_atr: float
    position_in_7d_range: float | None
    last_bar_volume_vs_20bar_avg: float | None
    consecutive_up_bars: int
    consecutive_down_bars: int


@dataclass(frozen=True, slots=True)
class _Bar:
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    quote_volume: float


def _ema(values: list[float], period: int) -> list[float]:
    """EMA seeded with the first value of the window."""

    if not values:
        return []
    alpha = 2.0 / (period + 1)
    out = [values[0]]
    for value in values[1:]:
        out.append(out[-1] + alpha * (value - out[-1]))
    return out


def _rsi(closes: list[float], period: int) -> float:
    if len(closes) <= period:
        return 50.0
    gains = losses = 0.0
    for previous, current in zip(closes[:period], closes[1 : period + 1], strict=False):
        change = current - previous
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    average_gain, average_loss = gains / period, losses / period
    for previous, current in zip(closes[period:-1], closes[period + 1 :], strict=False):
        change = current - previous
        average_gain = (average_gain * (period - 1) + max(change, 0.0)) / period
        average_loss = (average_loss * (period - 1) + max(-change, 0.0)) / period
    if average_loss == 0:
        return 100.0 if average_gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + average_gain / average_loss)


def _atr(bars: list[_Bar], period: int) -> float:
    """Mean true range of the last ``period`` steps."""

    if len(bars) < 2:
        return 0.0
    ranges = [
        max(current.high - current.low, abs(current.high - previous.close), abs(current.low - previous.close))
        for previous, current in zip(bars[:-1], bars[1:], strict=False)
    ]
    window = ranges[-period:]
    return sum(window) / len(window)


def _efficiency_ratio(closes: list[float], period: int) -> float:
    """Net move divided by the path travelled: near one is a clean trend, near zero is chop."""

    if len(closes) <= period:
        return 0.0
    window = closes[-period - 1 :]
    path = sum(abs(b - a) for a, b in zip(window[:-1], window[1:], strict=False))
    return abs(window[-1] - window[0]) / path if path else 0.0


def _merge(rows: list[_Bar], opened: int) -> _Bar:
    return _Bar(
        open_time_ms=opened,
        open=rows[0].open,
        high=max(row.high for row in rows),
        low=min(row.low for row in rows),
        close=rows[-1].close,
        quote_volume=sum(row.quote_volume for row in rows),
    )


@dataclass(frozen=True, slots=True)
class _Phase:
    """Hourly bars that close on one quarter-hour phase, and the 4h bars built from them."""

    hours: list[_Bar]
    hour_opens: list[int]
    trend_bars: list[_Bar]
    trend_first_hour_opens: list[int]
    trend_last_hour_opens: list[int]


def _phases(quarters: list[Candle]) -> dict[int, _Phase]:
    """Group closed 15-minute bars into hourly bars for each of the four phases."""

    phases: dict[int, _Phase] = {}
    for offset in (0, _QUARTER_MS, 2 * _QUARTER_MS, 3 * _QUARTER_MS):
        buckets: dict[int, list[_Bar]] = {}
        for row in quarters:
            key = (row.open_time_ms - offset) // _HOUR_MS * _HOUR_MS + offset
            buckets.setdefault(key, []).append(
                _Bar(row.open_time_ms, row.open, row.high, row.low, row.close, row.quote_volume)
            )
        hours = [_merge(rows, opened) for opened, rows in sorted(buckets.items()) if len(rows) == 4]
        trend_buckets: dict[int, list[_Bar]] = {}
        for hour in hours:
            trend_buckets.setdefault(hour.open_time_ms // _FOUR_HOURS_MS * _FOUR_HOURS_MS, []).append(hour)
        complete = [(opened, rows) for opened, rows in sorted(trend_buckets.items()) if len(rows) == 4]
        phases[offset] = _Phase(
            hours=hours,
            hour_opens=[hour.open_time_ms for hour in hours],
            trend_bars=[_merge(rows, opened) for opened, rows in complete],
            trend_first_hour_opens=[rows[0].open_time_ms for _, rows in complete],
            trend_last_hour_opens=[rows[-1].open_time_ms for _, rows in complete],
        )
    return phases


def _number(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("hourly trend evidence must be finite")
    normalized = 0.0 if value == 0 else value
    return format(normalized, ".17g")


def _feature_hash(payload: dict[str, str | int]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _decide(
    symbol: str,
    decision_close_ms: int,
    phase: _Phase,
    settings: HourlyTrendCandidatesConfig,
) -> tuple[HourlyTrendState | None, SleeveIntent | None]:
    """Apply the rules at one closed quarter hour."""

    boundary = decision_close_ms + 1
    newest_open = boundary - _HOUR_MS
    end = bisect_right(phase.hour_opens, newest_open)
    if end < 2 or phase.hour_opens[end - 1] != newest_open:
        return None, None
    if phase.hour_opens[end - 2] != newest_open - _HOUR_MS:
        # A missing bar would stretch the last step and misdate the signal.
        return None, None
    oldest_open = boundary - settings.history_hours * _HOUR_MS + _HOUR_MS
    start = bisect_left(phase.hour_opens, oldest_open)
    history = phase.hours[start:end]
    bars = history[-settings.signal_window_bars :]
    trend_start = bisect_left(phase.trend_first_hour_opens, oldest_open)
    trend_end = bisect_right(phase.trend_last_hour_opens, newest_open)
    trend_bars = phase.trend_bars[trend_start:trend_end]
    if len(bars) < settings.minimum_signal_bars or len(trend_bars) < settings.minimum_trend_bars:
        return None, None

    trend_closes = [bar.close for bar in trend_bars]
    trend_fast = _ema(trend_closes, settings.trend_fast_ema_period)[-1]
    trend_slow = _ema(trend_closes, settings.trend_slow_ema_period)[-1]
    efficiency = _efficiency_ratio(trend_closes, settings.trend_efficiency_lookback)
    if (
        trend_fast > trend_slow
        and trend_closes[-1] > trend_slow
        and efficiency >= settings.minimum_trend_efficiency
    ):
        trend = "UP"
    elif (
        trend_fast < trend_slow
        and trend_closes[-1] < trend_slow
        and efficiency >= settings.minimum_trend_efficiency
    ):
        trend = "DOWN"
    else:
        trend = "FLAT"

    closes = [bar.close for bar in bars]
    signal_ema = _ema(closes, settings.signal_ema_period)
    last = bars[-1]
    atr = _atr(bars, settings.atr_period)
    if atr <= 0 or last.close <= 0:
        return None, None
    strength = _rsi(closes, settings.rsi_period)

    back_4h = min(len(bars) - 1, 4)
    back_24h = min(len(bars) - 1, 24)
    week = history[-168:]
    week_high, week_low = max(bar.high for bar in week), min(bar.low for bar in week)
    volumes = [bar.quote_volume for bar in history[-21:-1]]
    average_volume = sum(volumes) / len(volumes) if volumes else 0.0
    ups = 0
    for bar in reversed(history[-12:]):
        if bar.close <= bar.open:
            break
        ups += 1
    downs = 0
    for bar in reversed(history[-12:]):
        if bar.close >= bar.open:
            break
        downs += 1
    state = HourlyTrendState(
        symbol=symbol,
        decision_ts_ms=decision_close_ms,
        price=last.close,
        trend=trend,
        trend_ema_fast=trend_fast,
        trend_ema_slow=trend_slow,
        trend_efficiency=efficiency,
        rsi=strength,
        atr=atr,
        atr_pct=100.0 * atr / last.close,
        change_1h_pct=(last.close / history[-2].close - 1) * 100,
        change_4h_pct=100.0 * (last.close / bars[-1 - back_4h].close - 1.0),
        change_24h_pct=100.0 * (last.close / bars[-1 - back_24h].close - 1.0),
        change_7d_pct=(last.close / week[0].open - 1) * 100,
        dist_ema_atr=(last.close - signal_ema[-1]) / atr,
        position_in_7d_range=(
            (last.close - week_low) / (week_high - week_low) if week_high > week_low else None
        ),
        last_bar_volume_vs_20bar_avg=last.quote_volume / average_volume if average_volume > 0 else None,
        consecutive_up_bars=ups,
        consecutive_down_bars=downs,
    )

    recent = bars[-settings.pullback_touch_bars :]
    recent_ema = signal_ema[-settings.pullback_touch_bars :]
    prior = bars[-settings.breakout_lookback_bars - 1 : -1]
    prior_high, prior_low = max(bar.high for bar in prior), min(bar.low for bar in prior)

    side: Side | None = None
    setup = ""
    raw_stop = 0.0
    if trend == "UP":
        touched = any(bar.low <= level for bar, level in zip(recent, recent_ema, strict=False))
        if (
            touched
            and last.close > signal_ema[-1]
            and last.close > last.open
            and settings.long_pullback_rsi_minimum <= strength <= settings.long_pullback_rsi_maximum
        ):
            side, setup = Side.LONG, SETUP_PULLBACK_RECLAIM
            raw_stop = min(bar.low for bar in recent) - settings.pullback_stop_buffer_atr * atr
        elif last.close > prior_high and strength < settings.long_breakout_rsi_maximum:
            side, setup = Side.LONG, SETUP_BREAKOUT
            raw_stop = last.close - settings.breakout_stop_atr * atr
    elif trend == "DOWN":
        touched = any(bar.high >= level for bar, level in zip(recent, recent_ema, strict=False))
        if (
            touched
            and last.close < signal_ema[-1]
            and last.close < last.open
            and settings.short_pullback_rsi_minimum <= strength <= settings.short_pullback_rsi_maximum
        ):
            side, setup = Side.SHORT, SETUP_PULLBACK_RECLAIM
            raw_stop = max(bar.high for bar in recent) + settings.pullback_stop_buffer_atr * atr
        elif last.close < prior_low and strength > settings.short_breakout_rsi_minimum:
            side, setup = Side.SHORT, SETUP_BREAKOUT
            raw_stop = last.close + settings.breakout_stop_atr * atr
    if side is None:
        return state, None

    distance = max(abs(last.close - raw_stop), settings.minimum_stop_distance_atr * atr)
    if distance > settings.maximum_stop_distance_atr * atr:
        return state, None
    sign = 1.0 if side is Side.LONG else -1.0
    reference = last.close
    stop = reference - sign * distance
    target = reference + sign * settings.target_reward_to_risk * distance
    if stop <= 0 or target <= 0:
        return state, None

    feature_payload: dict[str, str | int] = {
        "atr": _number(atr),
        "config_sha256": settings.fingerprint,
        "decision_ts_ms": decision_close_ms,
        "reference_price": _number(reference),
        "rsi": _number(strength),
        "setup": setup,
        "side": side.value,
        "signal_ema": _number(signal_ema[-1]),
        "stop_price": _number(stop),
        "strategy_version": STRATEGY_ID,
        "symbol": symbol,
        "target_price": _number(target),
        "trend_efficiency": _number(efficiency),
        "trend_ema_fast": _number(trend_fast),
        "trend_ema_slow": _number(trend_slow),
    }
    intent = SleeveIntent(
        sleeve_id=STRATEGY_ID,
        symbol=symbol,
        side=side,
        decision_ts_ms=decision_close_ms,
        entry_eligible_ts_ms=decision_close_ms + 1,
        entry_expires_ts_ms=decision_close_ms + settings.intent_valid_minutes * 60_000,
        reference_price=reference,
        signal_strength=min(1.0, max(0.0, efficiency)),
        gross_reward_bps=abs(target - reference) / reference * 10_000,
        exit_plan=ExitPlan(
            stop_price=stop,
            target_price=target,
            max_holding_ms=settings.max_hold_minutes * 60_000,
        ),
        metadata=(
            ("atr", _number(atr)),
            ("config_sha256", settings.fingerprint),
            ("feature_hash", _feature_hash(feature_payload)),
            ("rsi", _number(strength)),
            ("setup", setup),
            ("signal_ema", _number(signal_ema[-1])),
            ("strategy_version", STRATEGY_ID),
            ("trend", trend),
            ("trend_efficiency", _number(efficiency)),
            ("trend_ema_fast", _number(trend_fast)),
            ("trend_ema_slow", _number(trend_slow)),
        ),
    )
    return state, intent


def _settings(config: HourlyTrendCandidatesConfig | None) -> HourlyTrendCandidatesConfig:
    if config is None:
        return HourlyTrendCandidatesConfig()
    if isinstance(config, HourlyTrendCandidatesConfig):
        return config
    raise ValueError("config must be a HourlyTrendCandidatesConfig or None")


def generate_hourly_trend_candidates_intents(
    candles_1m: list[Candle],
    config: HourlyTrendCandidatesConfig | None = None,
) -> list[SleeveIntent]:
    """Emit an intent at every closed quarter hour whose hourly bar meets the rules."""

    settings = _settings(config)
    quarters = aggregate(candles_1m, "15m")
    if not quarters:
        return []
    phases = _phases(quarters)
    intents: list[SleeveIntent] = []
    for quarter in quarters:
        offset = (quarter.close_time_ms + 1) % _HOUR_MS
        _, intent = _decide(quarter.symbol, quarter.close_time_ms, phases[offset], settings)
        if intent is not None:
            intents.append(intent)
    return intents


def evaluate_hourly_trend_state(
    candles_1m: list[Candle],
    config: HourlyTrendCandidatesConfig | None = None,
) -> tuple[HourlyTrendState | None, SleeveIntent | None]:
    """State and intent at the newest closed quarter hour of the input.

    The intent equals the one :func:`generate_hourly_trend_candidates_intents` emits for
    that decision. The state is returned whenever the rules could be evaluated, so a
    reviewer can see the market also when no candidate was produced.
    """

    settings = _settings(config)
    quarters = aggregate(candles_1m, "15m")
    if not quarters:
        return None, None
    newest = quarters[-1]
    offset = (newest.close_time_ms + 1) % _HOUR_MS
    return _decide(newest.symbol, newest.close_time_ms, _phases(quarters)[offset], settings)


__all__ = [
    "SETUP_BREAKOUT",
    "SETUP_PULLBACK_RECLAIM",
    "STRATEGY_ID",
    "STRATEGY_REVISION",
    "HourlyTrendCandidatesConfig",
    "HourlyTrendState",
    "evaluate_hourly_trend_state",
    "generate_hourly_trend_candidates_intents",
]
