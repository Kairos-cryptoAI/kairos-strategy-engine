"""Causal quarter-hour order-flow persistence research sleeve.

The implementation is a deliberately conservative one-minute proxy for the
first-ten-second boundary order imbalance studied by Kim and Hansen (2026).
It is evaluated only after the first full minute of a UTC quarter-hour has
closed and can enter no earlier than the following minute.
"""

from __future__ import annotations

import hashlib
import json
import math
from bisect import bisect_left
from dataclasses import asdict, dataclass
from statistics import median

from kairos_core.enums import Side

from ..candles import Candle
from ..models import ExitPlan, SleeveIntent
from ..timeframes import aggregate
from ..validation import canonical_candles

_ONE_MINUTE_MS = 60_000
_QUARTER_HOUR_MS = 15 * _ONE_MINUTE_MS
_BPS = 10_000.0
_STRATEGY_VERSION = "quarter_hour_flow_v1"


@dataclass(frozen=True, slots=True)
class QuarterHourFlowConfig:
    """Frozen controls for the first quarter-hour proxy experiment."""

    phase_lags: int = 12
    minimum_agreeing_lags: int = 8
    atr_period: int = 96
    minimum_predictable_imbalance: float = 0.025
    minimum_current_imbalance: float = 0.08
    minimum_boundary_volume_ratio: float = 1.0
    stop_atr_multiple: float = 2.0
    target_reward_to_risk: float = 1.5
    max_hold_quarters: int = 32
    intent_valid_minutes: int = 1

    def __post_init__(self) -> None:
        for name in (
            "phase_lags",
            "minimum_agreeing_lags",
            "atr_period",
            "max_hold_quarters",
            "intent_valid_minutes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.minimum_agreeing_lags > self.phase_lags:
            raise ValueError("minimum_agreeing_lags cannot exceed phase_lags")
        for name in (
            "minimum_predictable_imbalance",
            "minimum_current_imbalance",
            "minimum_boundary_volume_ratio",
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
        if self.minimum_predictable_imbalance > 1:
            raise ValueError("minimum_predictable_imbalance must not exceed one")
        if self.minimum_current_imbalance > 1:
            raise ValueError("minimum_current_imbalance must not exceed one")

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
    """Split research input at every missing or malformed one-minute boundary."""

    segments: list[list[Candle]] = []
    current: list[Candle] = []
    for row in rows:
        aligned = (
            row.open_time_ms % _ONE_MINUTE_MS == 0
            and row.close_time_ms == row.open_time_ms + _ONE_MINUTE_MS - 1
        )
        contiguous = not current or row.open_time_ms == current[-1].open_time_ms + _ONE_MINUTE_MS
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
    true_ranges = []
    for index, row in enumerate(rows):
        if index == 0:
            true_ranges.append(row.high - row.low)
        else:
            previous_close = rows[index - 1].close
            true_ranges.append(
                max(row.high - row.low, abs(row.high - previous_close), abs(row.low - previous_close))
            )
    atr = sum(true_ranges[:period]) / period
    values[period - 1] = atr
    for index in range(period, len(rows)):
        atr = (atr * (period - 1) + true_ranges[index]) / period
        values[index] = atr
    return values


def _imbalance(row: Candle) -> float:
    if not math.isfinite(row.quote_volume) or row.quote_volume <= 0:
        return math.nan
    value = (2 * row.taker_buy_quote_volume - row.quote_volume) / row.quote_volume
    return value if math.isfinite(value) and -1 <= value <= 1 else math.nan


def _number(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("quarter-hour intent evidence must be finite")
    return format(0.0 if value == 0 else value, ".17g")


def _feature_hash(payload: dict[str, str | int]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _signal_strength(
    *,
    predictable_imbalance: float,
    current_imbalance: float,
    agreement_ratio: float,
    volume_ratio: float,
    settings: QuarterHourFlowConfig,
) -> float:
    components = (
        min(1.0, abs(predictable_imbalance) / (2 * settings.minimum_predictable_imbalance)),
        min(1.0, abs(current_imbalance) / (2 * settings.minimum_current_imbalance)),
        agreement_ratio,
        min(1.0, volume_ratio / (2 * settings.minimum_boundary_volume_ratio)),
    )
    return sum(components) / len(components)


def _generate_segment(
    rows: list[Candle],
    settings: QuarterHourFlowConfig,
) -> list[SleeveIntent]:
    boundary_rows = [row for row in rows if row.open_time_ms % _QUARTER_HOUR_MS == 0]
    if len(boundary_rows) <= settings.phase_lags:
        return []
    quarters = aggregate(rows, "15m")
    atr_values = _wilder_atr(quarters, settings.atr_period)
    quarter_close_times = [row.close_time_ms for row in quarters]

    intents: list[SleeveIntent] = []
    for boundary_index in range(settings.phase_lags, len(boundary_rows)):
        current = boundary_rows[boundary_index]
        prior = boundary_rows[boundary_index - settings.phase_lags : boundary_index]
        prior_imbalances = [_imbalance(row) for row in prior]
        current_imbalance = _imbalance(current)
        prior_quote_volumes = [row.quote_volume for row in prior]
        if not all(math.isfinite(value) for value in (*prior_imbalances, current_imbalance)):
            continue
        if not all(math.isfinite(value) and value > 0 for value in prior_quote_volumes):
            continue

        predictable_imbalance = sum(prior_imbalances) / settings.phase_lags
        if abs(predictable_imbalance) < settings.minimum_predictable_imbalance:
            continue
        direction = 1 if predictable_imbalance > 0 else -1
        agreeing_lags = sum(direction * value > 0 for value in prior_imbalances)
        directional_current = direction * current_imbalance
        boundary_volume_ratio = current.quote_volume / median(prior_quote_volumes)
        if not (
            agreeing_lags >= settings.minimum_agreeing_lags
            and directional_current >= settings.minimum_current_imbalance
            and math.isfinite(boundary_volume_ratio)
            and boundary_volume_ratio >= settings.minimum_boundary_volume_ratio
        ):
            continue

        # Only fully closed 15-minute bars strictly before the boundary can
        # provide volatility state for the decision.
        quarter_index = bisect_left(quarter_close_times, current.open_time_ms) - 1
        if quarter_index < 0:
            continue
        previous_atr = atr_values[quarter_index]
        if not math.isfinite(previous_atr) or previous_atr <= 0:
            continue

        side = Side.LONG if direction > 0 else Side.SHORT
        reference = current.close
        risk_distance = settings.stop_atr_multiple * previous_atr
        if side is Side.LONG:
            stop = reference - risk_distance
            target = reference + settings.target_reward_to_risk * risk_distance
        else:
            stop = reference + risk_distance
            target = reference - settings.target_reward_to_risk * risk_distance
        if not all(math.isfinite(value) and value > 0 for value in (reference, stop, target)):
            continue

        agreement_ratio = agreeing_lags / settings.phase_lags
        feature_payload: dict[str, str | int] = {
            "agreeing_lags": agreeing_lags,
            "agreement_ratio": _number(agreement_ratio),
            "atr": _number(previous_atr),
            "boundary_volume_ratio": _number(boundary_volume_ratio),
            "config_sha256": settings.fingerprint,
            "current_imbalance": _number(current_imbalance),
            "decision_ts_ms": current.close_time_ms,
            "predictable_imbalance": _number(predictable_imbalance),
            "reference_price": _number(reference),
            "side": side.value,
            "stop_price": _number(stop),
            "strategy_version": _STRATEGY_VERSION,
            "symbol": current.symbol,
            "target_price": _number(target),
        }
        intents.append(
            SleeveIntent(
                sleeve_id=_STRATEGY_VERSION,
                symbol=current.symbol,
                side=side,
                decision_ts_ms=current.close_time_ms,
                entry_eligible_ts_ms=current.close_time_ms + 1,
                entry_expires_ts_ms=(current.close_time_ms + settings.intent_valid_minutes * _ONE_MINUTE_MS),
                reference_price=reference,
                signal_strength=_signal_strength(
                    predictable_imbalance=predictable_imbalance,
                    current_imbalance=current_imbalance,
                    agreement_ratio=agreement_ratio,
                    volume_ratio=boundary_volume_ratio,
                    settings=settings,
                ),
                gross_reward_bps=abs(target - reference) / reference * _BPS,
                exit_plan=ExitPlan(
                    stop_price=stop,
                    target_price=target,
                    max_holding_ms=settings.max_hold_quarters * _QUARTER_HOUR_MS,
                ),
                metadata=(
                    ("agreeing_lags", str(agreeing_lags)),
                    ("agreement_ratio", _number(agreement_ratio)),
                    ("atr", _number(previous_atr)),
                    ("boundary_proxy", "closed_first_1m"),
                    ("boundary_volume_ratio", _number(boundary_volume_ratio)),
                    ("config_sha256", settings.fingerprint),
                    ("current_imbalance", _number(current_imbalance)),
                    ("feature_hash", _feature_hash(feature_payload)),
                    ("predictable_imbalance", _number(predictable_imbalance)),
                    ("strategy_version", _STRATEGY_VERSION),
                ),
            )
        )
    return intents


def generate_quarter_hour_flow_intents(
    candles_1m: list[Candle],
    config: QuarterHourFlowConfig | None = None,
) -> list[SleeveIntent]:
    """Emit deterministic next-minute intents from gap-isolated boundary flow."""

    if config is None:
        settings = QuarterHourFlowConfig()
    elif isinstance(config, QuarterHourFlowConfig):
        settings = config
    else:
        raise ValueError("config must be a QuarterHourFlowConfig or None")
    ordered = canonical_candles(candles_1m, expected_timeframe="1m")
    intents: list[SleeveIntent] = []
    for segment in _segments(ordered):
        intents.extend(_generate_segment(segment, settings))
    return intents
