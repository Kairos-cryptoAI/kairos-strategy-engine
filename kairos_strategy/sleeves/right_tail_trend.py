"""Low-dimensional hourly trend sleeve designed to preserve a positive right tail.

The candidate deliberately avoids indicator voting, order-flow filters and
position sizing.  Once per UTC day it measures the same causal 24-hour
return-to-realized-variation score used by ``market_anatomy_v1`` and emits a
fixed stop/target/timeout plan for the next complete hour.  Research and
runtime therefore evaluate one exact generator.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass

from kairos_core.enums import Side

from ..candles import Candle
from ..models import ExitPlan, SleeveIntent
from ..timeframes import aggregate
from ..validation import canonical_candles

_HOUR_MS = 60 * 60 * 1_000
_BPS = 10_000.0
_STRATEGY_VERSION = "right_tail_trend_v1"


@dataclass(frozen=True, slots=True)
class RightTailTrendConfig:
    """Frozen controls for the first right-tail trend candidate.

    The eight fields describe the observation clock, one standardized trend
    score and one symmetric lifecycle.  There are no per-symbol, per-side or
    regime-specific thresholds.
    """

    trend_lookback_hours: int = 24
    minimum_trend_score: float = 1.0
    atr_period_hours: int = 24
    stop_atr_multiple: float = 2.0
    target_reward_to_risk: float = 4.0
    max_hold_hours: int = 72
    decision_interval_hours: int = 24
    intent_valid_hours: int = 1

    def __post_init__(self) -> None:
        for name in (
            "trend_lookback_hours",
            "atr_period_hours",
            "max_hold_hours",
            "decision_interval_hours",
            "intent_valid_hours",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name in (
            "minimum_trend_score",
            "stop_atr_multiple",
            "target_reward_to_risk",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be finite and positive")
            object.__setattr__(self, name, float(value))
        if self.decision_interval_hours < self.intent_valid_hours:
            raise ValueError("decision interval cannot be shorter than intent validity")
        if self.max_hold_hours <= self.intent_valid_hours:
            raise ValueError("maximum hold must exceed intent validity")

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            asdict(self),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
        return hashlib.sha256(encoded).hexdigest()


def _segments(rows: list[Candle]) -> list[list[Candle]]:
    segments: list[list[Candle]] = []
    current: list[Candle] = []
    for row in rows:
        aligned = row.open_time_ms % _HOUR_MS == 0 and row.close_time_ms == row.open_time_ms + _HOUR_MS - 1
        contiguous = not current or row.open_time_ms == current[-1].open_time_ms + _HOUR_MS
        if not aligned or not contiguous:
            if current:
                segments.append(current)
            current = []
        if aligned:
            current.append(row)
    if current:
        segments.append(current)
    return segments


def _wilder_atr(rows: list[Candle], period: int) -> list[float]:
    values = [math.nan] * len(rows)
    if len(rows) < period:
        return values
    true_ranges: list[float] = []
    for index, row in enumerate(rows):
        if index == 0:
            true_ranges.append(row.high - row.low)
        else:
            previous_close = rows[index - 1].close
            true_ranges.append(
                max(
                    row.high - row.low,
                    abs(row.high - previous_close),
                    abs(row.low - previous_close),
                )
            )
    atr = math.fsum(true_ranges[:period]) / period
    values[period - 1] = atr
    for index in range(period, len(rows)):
        atr = (atr * (period - 1) + true_ranges[index]) / period
        values[index] = atr
    return values


def _trend_score(rows: list[Candle], index: int, lookback: int) -> float | None:
    if index < lookback:
        return None
    returns = [
        math.log(rows[position].close / rows[position - 1].close)
        for position in range(index - lookback + 1, index + 1)
    ]
    squared_sum = math.fsum(value * value for value in returns)
    if not math.isfinite(squared_sum) or squared_sum <= 0:
        return None
    score = math.fsum(returns) / math.sqrt(squared_sum)
    return score if math.isfinite(score) else None


def _number(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("right-tail trend evidence must be finite")
    return format(0.0 if value == 0 else value, ".17g")


def _feature_hash(payload: dict[str, str | int]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _generate_segment(
    rows: list[Candle],
    settings: RightTailTrendConfig,
) -> list[SleeveIntent]:
    atr_values = _wilder_atr(rows, settings.atr_period_hours)
    decision_ms = settings.decision_interval_hours * _HOUR_MS
    intents: list[SleeveIntent] = []
    for index, current in enumerate(rows):
        if current.open_time_ms % decision_ms != 0:
            continue
        score = _trend_score(rows, index, settings.trend_lookback_hours)
        atr = atr_values[index]
        if score is None or not math.isfinite(atr) or atr <= 0:
            continue
        if score >= settings.minimum_trend_score:
            side = Side.LONG
        elif score <= -settings.minimum_trend_score:
            side = Side.SHORT
        else:
            continue

        reference = current.close
        risk_distance = settings.stop_atr_multiple * atr
        reward_distance = settings.target_reward_to_risk * risk_distance
        if side is Side.LONG:
            stop = reference - risk_distance
            target = reference + reward_distance
        else:
            stop = reference + risk_distance
            target = reference - reward_distance
        if not all(math.isfinite(value) and value > 0 for value in (reference, stop, target)):
            continue

        feature_payload: dict[str, str | int] = {
            "atr": _number(atr),
            "config_sha256": settings.fingerprint,
            "decision_ts_ms": current.close_time_ms,
            "reference_price": _number(reference),
            "side": side.value,
            "stop_price": _number(stop),
            "strategy_version": _STRATEGY_VERSION,
            "symbol": current.symbol,
            "target_price": _number(target),
            "trend_score": _number(score),
        }
        intents.append(
            SleeveIntent(
                sleeve_id=_STRATEGY_VERSION,
                symbol=current.symbol,
                side=side,
                decision_ts_ms=current.close_time_ms,
                entry_eligible_ts_ms=current.close_time_ms + 1,
                entry_expires_ts_ms=current.close_time_ms + settings.intent_valid_hours * _HOUR_MS,
                reference_price=reference,
                signal_strength=min(1.0, abs(score) / (2 * settings.minimum_trend_score)),
                gross_reward_bps=reward_distance / reference * _BPS,
                exit_plan=ExitPlan(
                    stop_price=stop,
                    target_price=target,
                    max_holding_ms=settings.max_hold_hours * _HOUR_MS,
                ),
                metadata=(
                    ("atr", _number(atr)),
                    ("config_sha256", settings.fingerprint),
                    ("decision_clock", "utc_epoch_aligned"),
                    ("feature_hash", _feature_hash(feature_payload)),
                    ("strategy_version", _STRATEGY_VERSION),
                    ("trend_score", _number(score)),
                ),
            )
        )
    return intents


def generate_right_tail_trend_intents(
    candles_1m: list[Candle],
    config: RightTailTrendConfig | None = None,
) -> list[SleeveIntent]:
    """Emit daily causal intents from gap-isolated closed hourly bars."""

    if config is None:
        settings = RightTailTrendConfig()
    elif isinstance(config, RightTailTrendConfig):
        settings = config
    else:
        raise ValueError("config must be a RightTailTrendConfig or None")
    ordered = canonical_candles(candles_1m, expected_timeframe="1m")
    hourly = aggregate(ordered, "1h")
    intents: list[SleeveIntent] = []
    for segment in _segments(hourly):
        intents.extend(_generate_segment(segment, settings))
    return intents
