"""Published long-only daily Donchian ensemble allocation model."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from statistics import stdev

from ..allocation import AllocationReason, TargetAllocation
from ..candles import Candle
from ..timeframes import aggregate
from ..validation import canonical_candles

_DAY_MS = 24 * 60 * 60 * 1_000
_STRATEGY_ID = "donchian_ensemble_long_v1"


@dataclass(frozen=True, slots=True)
class DonchianEnsembleConfig:
    """Frozen transcription of Zarattini, Pagani and Barbon (2025)."""

    horizons_days: tuple[int, ...] = (5, 10, 20, 30, 60, 90, 150, 250, 360)
    volatility_lookback_days: int = 90
    annualization_days: int = 365
    target_annualized_volatility: float = 0.25
    maximum_weight: float = 2.0
    volatility_rebalance_threshold: float = 0.20

    def __post_init__(self) -> None:
        if (
            not isinstance(self.horizons_days, tuple)
            or not self.horizons_days
            or tuple(sorted(set(self.horizons_days))) != self.horizons_days
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 2
                for value in self.horizons_days
            )
        ):
            raise ValueError("horizons_days must be sorted unique integers of at least two days")
        for name in ("volatility_lookback_days", "annualization_days"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 2:
                raise ValueError(f"{name} must be an integer of at least two")
        for name in (
            "target_annualized_volatility",
            "maximum_weight",
            "volatility_rebalance_threshold",
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
        if self.maximum_weight > 2:
            raise ValueError("maximum_weight cannot exceed the published 2x cap")
        if self.volatility_rebalance_threshold >= 1:
            raise ValueError("volatility rebalance threshold must be below one")

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


def _segments(rows: list[Candle]) -> list[list[Candle]]:
    result: list[list[Candle]] = []
    current: list[Candle] = []
    for row in rows:
        aligned = row.open_time_ms % _DAY_MS == 0 and row.close_time_ms == row.open_time_ms + _DAY_MS - 1
        contiguous = not current or row.open_time_ms == current[-1].open_time_ms + _DAY_MS
        if not aligned or not contiguous:
            if current:
                result.append(current)
            current = []
        if aligned:
            current.append(row)
    if current:
        result.append(current)
    return result


def _annualized_volatility(rows: list[Candle], index: int, settings: DonchianEnsembleConfig) -> float | None:
    if index < settings.volatility_lookback_days:
        return None
    returns = [
        rows[position].close / rows[position - 1].close - 1
        for position in range(index - settings.volatility_lookback_days + 1, index + 1)
    ]
    value = stdev(returns) * math.sqrt(settings.annualization_days)
    return value if math.isfinite(value) and value > 0 else None


def _generate_segment(rows: list[Candle], settings: DonchianEnsembleConfig) -> list[TargetAllocation]:
    active = {horizon: False for horizon in settings.horizons_days}
    trailing: dict[int, float] = {}
    executed_weight: float | None = None
    allocations: list[TargetAllocation] = []
    full_warmup = max(max(settings.horizons_days), settings.volatility_lookback_days + 1)
    for index, row in enumerate(rows):
        signal_changed = False
        for horizon in settings.horizons_days:
            if index < horizon - 1:
                continue
            window = rows[index - horizon + 1 : index + 1]
            upper = max(item.close for item in window)
            lower = min(item.close for item in window)
            midpoint = 0.5 * (upper + lower)
            was_active = active[horizon]
            if row.close == upper:
                active[horizon] = True
                trailing[horizon] = max(trailing.get(horizon, midpoint), midpoint)
            elif was_active and row.close <= trailing[horizon]:
                active[horizon] = False
                trailing.pop(horizon, None)
            elif was_active:
                trailing[horizon] = max(trailing[horizon], midpoint)
            signal_changed |= active[horizon] != was_active

        volatility = _annualized_volatility(rows, index, settings)
        if index < full_warmup - 1 or volatility is None:
            continue
        active_horizons = tuple(horizon for horizon in settings.horizons_days if active[horizon])
        model_weight = min(
            settings.target_annualized_volatility / volatility,
            settings.maximum_weight,
        )
        desired_weight = model_weight * len(active_horizons) / len(settings.horizons_days)
        if executed_weight is None or signal_changed:
            target_weight = desired_weight
            reason = AllocationReason.SIGNAL
        else:
            relative_difference = (
                abs(desired_weight - executed_weight) / executed_weight
                if executed_weight > 0
                else (0.0 if desired_weight == 0 else math.inf)
            )
            if relative_difference > settings.volatility_rebalance_threshold:
                target_weight = desired_weight
                reason = AllocationReason.VOLATILITY
            else:
                target_weight = executed_weight
                reason = AllocationReason.DEADBAND_HOLD
        executed_weight = target_weight
        stops = tuple((horizon, trailing[horizon]) for horizon in active_horizons)
        allocations.append(
            TargetAllocation(
                strategy_id=_STRATEGY_ID,
                symbol=row.symbol,
                decision_ts_ms=row.close_time_ms,
                effective_ts_ms=row.close_time_ms + 1,
                target_weight=target_weight,
                annualized_volatility=volatility,
                active_horizons=active_horizons,
                trailing_stops=stops,
                reason=reason,
                metadata=(
                    ("annualization", "simple_returns_sqrt_365"),
                    ("config_sha256", settings.fingerprint),
                    ("source", "zarattini_pagani_barbon_2025"),
                    ("volatility_deadband", "relative_to_last_executed_weight"),
                ),
            )
        )
    return allocations


def generate_donchian_ensemble_allocations(
    candles_1m: list[Candle],
    config: DonchianEnsembleConfig | None = None,
) -> list[TargetAllocation]:
    """Generate causal next-day targets from complete UTC daily closes."""

    if config is None:
        settings = DonchianEnsembleConfig()
    elif isinstance(config, DonchianEnsembleConfig):
        settings = config
    else:
        raise ValueError("config must be a DonchianEnsembleConfig or None")
    ordered = canonical_candles(candles_1m, expected_timeframe="1m")
    daily = aggregate(ordered, "1d")
    allocations: list[TargetAllocation] = []
    for segment in _segments(daily):
        allocations.extend(_generate_segment(segment, settings))
    return allocations
