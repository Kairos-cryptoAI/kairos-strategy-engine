"""Immutable target-allocation contracts for slow portfolio strategies."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from enum import StrEnum


class AllocationReason(StrEnum):
    SIGNAL = "signal"
    VOLATILITY = "volatility"
    DEADBAND_HOLD = "deadband_hold"


@dataclass(frozen=True, slots=True)
class TargetAllocation:
    """One causal end-of-day target weight, effective on the next day."""

    strategy_id: str
    symbol: str
    decision_ts_ms: int
    effective_ts_ms: int
    target_weight: float
    annualized_volatility: float
    active_horizons: tuple[int, ...]
    trailing_stops: tuple[tuple[int, float], ...]
    reason: AllocationReason
    metadata: tuple[tuple[str, str], ...] = ()
    allocation_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.strategy_id or self.strategy_id != self.strategy_id.strip():
            raise ValueError("strategy_id must be normalized")
        if not self.symbol or self.symbol != self.symbol.strip().upper():
            raise ValueError("symbol must be normalized uppercase")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (self.decision_ts_ms, self.effective_ts_ms)
        ):
            raise ValueError("allocation timestamps must be non-negative integers")
        if self.effective_ts_ms <= self.decision_ts_ms:
            raise ValueError("allocation must become effective after its decision")
        if (
            isinstance(self.target_weight, bool)
            or not isinstance(self.target_weight, (int, float))
            or not math.isfinite(self.target_weight)
            or not 0 <= self.target_weight <= 2
        ):
            raise ValueError("target_weight must be finite within [0, 2]")
        if (
            isinstance(self.annualized_volatility, bool)
            or not isinstance(self.annualized_volatility, (int, float))
            or not math.isfinite(self.annualized_volatility)
            or self.annualized_volatility <= 0
        ):
            raise ValueError("annualized_volatility must be finite and positive")
        if tuple(sorted(set(self.active_horizons))) != self.active_horizons:
            raise ValueError("active_horizons must be sorted and unique")
        stop_horizons = tuple(horizon for horizon, _ in self.trailing_stops)
        if stop_horizons != self.active_horizons:
            raise ValueError("trailing stops must exactly cover active horizons")
        if any(not math.isfinite(stop) or stop <= 0 for _, stop in self.trailing_stops):
            raise ValueError("trailing stops must be finite and positive")
        if not isinstance(self.reason, AllocationReason):
            raise ValueError("reason must be an AllocationReason")
        canonical_metadata = tuple(sorted(self.metadata))
        if (
            not isinstance(self.metadata, tuple)
            or len({key for key, _ in canonical_metadata}) != len(canonical_metadata)
            or any(not key or key != key.strip() for key, _ in canonical_metadata)
            or any(
                not isinstance(key, str) or not isinstance(value, str) for key, value in canonical_metadata
            )
        ):
            raise ValueError("metadata must contain unique normalized string pairs")
        object.__setattr__(self, "target_weight", float(self.target_weight))
        object.__setattr__(self, "annualized_volatility", float(self.annualized_volatility))
        object.__setattr__(self, "metadata", canonical_metadata)
        payload = {
            "active_horizons": self.active_horizons,
            "annualized_volatility": self.annualized_volatility,
            "decision_ts_ms": self.decision_ts_ms,
            "effective_ts_ms": self.effective_ts_ms,
            "metadata": canonical_metadata,
            "reason": self.reason.value,
            "strategy_id": self.strategy_id,
            "symbol": self.symbol,
            "target_weight": self.target_weight,
            "trailing_stops": self.trailing_stops,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        object.__setattr__(self, "allocation_id", hashlib.sha256(encoded).hexdigest())
