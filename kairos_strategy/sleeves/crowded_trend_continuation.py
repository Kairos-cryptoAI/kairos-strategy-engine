"""Trend continuation conditioned on causal derivatives crowding state.

The factor thresholds are copied unchanged from the descriptive
``derivatives_state_v1`` study.  They are deliberately global across symbols
and sides.  This module contains no data access and can only operate on factor
observations supplied explicitly by its caller.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass

from kairos_core.enums import Side

from ..candles import Candle
from ..factors import DerivativeStateObservation, canonical_derivative_observations
from ..models import ExitPlan, SleeveIntent
from ..timeframes import aggregate
from ..validation import canonical_candles

_HOUR_MS = 60 * 60 * 1_000
_BPS = 10_000.0
_STRATEGY_VERSION = "crowded_trend_continuation_v1"


@dataclass(frozen=True, slots=True)
class CrowdedTrendContinuationConfig:
    """Frozen, non-optimized definition of the contextual candidate."""

    trend_lookback_hours: int = 24
    minimum_trend_score: float = 1.0
    minimum_open_interest_change: float = 0.05
    minimum_aligned_premium: float = 0.0005
    minimum_aligned_funding: float = 0.0001
    atr_period_hours: int = 24
    stop_atr_multiple: float = 2.0
    target_reward_to_risk: float = 4.0
    max_hold_hours: int = 24
    intent_valid_hours: int = 1

    def __post_init__(self) -> None:
        for name in (
            "trend_lookback_hours",
            "atr_period_hours",
            "max_hold_hours",
            "intent_valid_hours",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name in (
            "minimum_trend_score",
            "minimum_open_interest_change",
            "minimum_aligned_premium",
            "minimum_aligned_funding",
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
        if self.max_hold_hours <= self.intent_valid_hours:
            raise ValueError("maximum hold must exceed intent validity")

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


def _hourly_segments(rows: list[Candle]) -> list[list[Candle]]:
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
        previous_close = rows[index - 1].close if index else row.close
        true_ranges.append(
            max(row.high - row.low, abs(row.high - previous_close), abs(row.low - previous_close))
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
    if squared_sum <= 0 or not math.isfinite(squared_sum):
        return None
    score = math.fsum(returns) / math.sqrt(squared_sum)
    return score if math.isfinite(score) else None


def _number(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("crowded-trend evidence must be finite")
    return format(0.0 if value == 0 else value, ".17g")


def _feature_hash(payload: dict[str, str | int]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _generate_segment(
    rows: list[Candle],
    factors_by_hour: dict[int, DerivativeStateObservation],
    settings: CrowdedTrendContinuationConfig,
) -> list[SleeveIntent]:
    atr_values = _wilder_atr(rows, settings.atr_period_hours)
    intents: list[SleeveIntent] = []
    for index, current in enumerate(rows):
        if index < settings.trend_lookback_hours:
            continue
        factor = factors_by_hour.get(current.open_time_ms)
        prior_factor = factors_by_hour.get(current.open_time_ms - settings.trend_lookback_hours * _HOUR_MS)
        if factor is None or prior_factor is None:
            continue
        score = _trend_score(rows, index, settings.trend_lookback_hours)
        atr = atr_values[index]
        if score is None or not math.isfinite(atr) or atr <= 0:
            continue
        if score >= settings.minimum_trend_score:
            side = Side.LONG
            direction = 1.0
        elif score <= -settings.minimum_trend_score:
            side = Side.SHORT
            direction = -1.0
        else:
            continue
        oi_change = factor.open_interest_value / prior_factor.open_interest_value - 1
        aligned_premium = direction * factor.premium_close
        aligned_funding = direction * factor.funding_rate
        if oi_change < settings.minimum_open_interest_change or not (
            aligned_premium >= settings.minimum_aligned_premium
            or aligned_funding >= settings.minimum_aligned_funding
        ):
            continue

        reference = current.close
        risk_distance = settings.stop_atr_multiple * atr
        reward_distance = settings.target_reward_to_risk * risk_distance
        stop = reference - direction * risk_distance
        target = reference + direction * reward_distance
        if not all(math.isfinite(value) and value > 0 for value in (reference, stop, target)):
            continue
        payload: dict[str, str | int] = {
            "aligned_funding": _number(aligned_funding),
            "aligned_premium": _number(aligned_premium),
            "atr": _number(atr),
            "config_sha256": settings.fingerprint,
            "decision_ts_ms": current.close_time_ms,
            "funding_timestamp_ms": factor.funding_timestamp_ms,
            "oi_change": _number(oi_change),
            "open_interest_timestamp_ms": factor.open_interest_timestamp_ms,
            "reference_price": _number(reference),
            "side": side.value,
            "strategy_version": _STRATEGY_VERSION,
            "symbol": current.symbol,
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
                    ("aligned_funding", _number(aligned_funding)),
                    ("aligned_premium", _number(aligned_premium)),
                    ("atr", _number(atr)),
                    ("config_sha256", settings.fingerprint),
                    ("feature_hash", _feature_hash(payload)),
                    ("oi_change", _number(oi_change)),
                    ("strategy_version", _STRATEGY_VERSION),
                    ("trend_score", _number(score)),
                ),
            )
        )
    return intents


def generate_crowded_trend_continuation_intents(
    candles_1m: list[Candle],
    factor_observations: list[DerivativeStateObservation],
    config: CrowdedTrendContinuationConfig | None = None,
) -> list[SleeveIntent]:
    """Emit causal hourly intents from explicit price and factor inputs."""

    if config is None:
        settings = CrowdedTrendContinuationConfig()
    elif isinstance(config, CrowdedTrendContinuationConfig):
        settings = config
    else:
        raise ValueError("config must be a CrowdedTrendContinuationConfig or None")
    ordered = canonical_candles(candles_1m, expected_timeframe="1m")
    factors = canonical_derivative_observations(factor_observations)
    if not ordered or not factors:
        return []
    if ordered[0].symbol != factors[0].symbol:
        raise ValueError("price and factor symbols must match")
    factors_by_hour = {factor.open_time_ms: factor for factor in factors}
    hourly = aggregate(ordered, "1h")
    intents: list[SleeveIntent] = []
    for segment in _hourly_segments(hourly):
        intents.extend(_generate_segment(segment, factors_by_hour, settings))
    return intents
