"""Operational history and decision-clock requirements for runtime sleeves.

These values affect only how the service schedules an unchanged pure generator.
They deliberately live outside the strategy source fingerprint: changing a
buffer bound or avoiding redundant calls must not pretend to change alpha.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class RuntimeRequirements:
    """Minimum complete 1m history and generator evaluation cadence."""

    minimum_window_bars: int = 2
    decision_interval_bars: int = 1

    def __post_init__(self) -> None:
        for name, value in (
            ("minimum_window_bars", self.minimum_window_bars),
            ("decision_interval_bars", self.decision_interval_bars),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


_DEFAULT = RuntimeRequirements()
_REQUIREMENTS = MappingProxyType(
    {
        # 200 complete 4h bars.  The daily clock is UTC epoch-aligned.
        "regime_aligned_right_tail_v1": RuntimeRequirements(
            minimum_window_bars=200 * 4 * 60,
            decision_interval_bars=24 * 60,
        ),
    }
)


def get_runtime_requirements(strategy_id: str) -> RuntimeRequirements:
    """Return operational requirements for a registered strategy."""

    return _REQUIREMENTS.get(strategy_id, _DEFAULT)


__all__ = ["RuntimeRequirements", "get_runtime_requirements"]
