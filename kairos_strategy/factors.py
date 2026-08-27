"""Immutable derivatives-state observations for contextual strategies."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

_HOUR_MS = 60 * 60 * 1_000


@dataclass(frozen=True, slots=True)
class DerivativeStateObservation:
    """Causal factor values known by the end of one complete UTC hour."""

    symbol: str
    open_time_ms: int
    close_time_ms: int
    premium_close: float
    funding_rate: float
    funding_timestamp_ms: int
    open_interest_value: float
    open_interest_timestamp_ms: int

    def __post_init__(self) -> None:
        if not self.symbol or self.symbol != self.symbol.strip().upper():
            raise ValueError("factor symbol must be a normalized uppercase string")
        timestamps = (
            self.open_time_ms,
            self.close_time_ms,
            self.funding_timestamp_ms,
            self.open_interest_timestamp_ms,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in timestamps):
            raise ValueError("factor timestamps must be non-negative integers")
        if self.open_time_ms % _HOUR_MS or self.close_time_ms != self.open_time_ms + _HOUR_MS - 1:
            raise ValueError("factor observation must cover one complete UTC hour")
        if not self.open_time_ms <= self.open_interest_timestamp_ms <= self.close_time_ms:
            raise ValueError("open-interest timestamp must lie inside the observed hour")
        if not self.open_time_ms - 8 * _HOUR_MS <= self.funding_timestamp_ms <= self.close_time_ms:
            raise ValueError("funding observation must be known and no older than eight hours")
        values = (self.premium_close, self.funding_rate, self.open_interest_value)
        if not all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in values):
            raise ValueError("factor values must be numeric")
        if not all(math.isfinite(value) for value in values):
            raise ValueError("factor values must be finite")
        if self.open_interest_value <= 0:
            raise ValueError("open interest must be positive")


def canonical_derivative_observations(
    observations: Iterable[DerivativeStateObservation],
) -> list[DerivativeStateObservation]:
    """Return a reproducible, unique single-symbol factor sequence."""

    ordered = sorted(observations, key=lambda item: (item.open_time_ms, item.symbol))
    if not ordered:
        return []
    symbol = ordered[0].symbol
    previous_open: int | None = None
    for observation in ordered:
        if not isinstance(observation, DerivativeStateObservation):
            raise TypeError("context observations must be DerivativeStateObservation values")
        if observation.symbol != symbol:
            raise ValueError("one contextual evaluation cannot mix factor symbols")
        if observation.open_time_ms == previous_open:
            raise ValueError(f"duplicate factor open timestamp {observation.open_time_ms}")
        previous_open = observation.open_time_ms
    return ordered
