"""Closed registry of deterministic strategy generators and their status."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from .candles import Candle
from .models import SleeveIntent
from .sleeves import (
    OrderFlowVolatilityExpansionConfig,
    RangeMeanReversionConfig,
    RegimeVetoRetestReclaimConfig,
    TrendBreakoutConfig,
    TrendPullbackReclaimConfig,
    generate_orderflow_volatility_expansion_intents,
    generate_range_mean_reversion_intents,
    generate_regime_veto_retest_reclaim_intents,
    generate_trend_breakout_intents,
    generate_trend_pullback_reclaim_intents,
)

StrategyGenerator = Callable[[list[Candle], Any], list[SleeveIntent]]


class StrategyStatus(StrEnum):
    """Promotion status; none of the current research sleeves may trade PAPER."""

    REJECTED = "rejected"
    RESEARCH = "research"
    PAPER_APPROVED = "paper_approved"


class PaperStrategyDisabledError(RuntimeError):
    """Raised when a non-approved research sleeve is requested for PAPER."""


@dataclass(frozen=True, slots=True)
class StrategyDefinition:
    strategy_id: str
    revision: str
    config_type: type[Any]
    generator: StrategyGenerator
    source_files: tuple[str, ...]
    status: StrategyStatus = StrategyStatus.REJECTED

    def __post_init__(self) -> None:
        if not self.strategy_id or self.strategy_id != self.strategy_id.strip():
            raise ValueError("strategy_id must be a normalized non-empty string")
        if not self.revision or self.revision != self.revision.strip():
            raise ValueError("revision must be a normalized non-empty string")
        if not self.source_files or len(set(self.source_files)) != len(self.source_files):
            raise ValueError("source_files must be non-empty and unique")
        if not isinstance(self.status, StrategyStatus):
            raise ValueError("status must be a StrategyStatus")

    @property
    def paper_enabled(self) -> bool:
        return self.status is StrategyStatus.PAPER_APPROVED


_COMMON_SOURCE_FILES = (
    "candles.py",
    "indicators.py",
    "models.py",
    "provenance.py",
    "registry.py",
    "runtime.py",
    "timeframes.py",
    "validation.py",
)


def _definition(
    strategy_id: str,
    config_type: type[Any],
    generator: StrategyGenerator,
    module_name: str,
) -> StrategyDefinition:
    return StrategyDefinition(
        strategy_id=strategy_id,
        revision="1",
        config_type=config_type,
        generator=generator,
        source_files=(*_COMMON_SOURCE_FILES, f"sleeves/{module_name}.py"),
    )


_DEFINITIONS = (
    _definition(
        "orderflow_volatility_expansion_v1",
        OrderFlowVolatilityExpansionConfig,
        generate_orderflow_volatility_expansion_intents,
        "orderflow_volatility_expansion",
    ),
    _definition(
        "range_mean_reversion_v1",
        RangeMeanReversionConfig,
        generate_range_mean_reversion_intents,
        "range_mean_reversion",
    ),
    _definition(
        "regime_veto_retest_reclaim_v1",
        RegimeVetoRetestReclaimConfig,
        generate_regime_veto_retest_reclaim_intents,
        "regime_retest_reclaim",
    ),
    _definition(
        "trend_breakout_v1",
        TrendBreakoutConfig,
        generate_trend_breakout_intents,
        "trend_breakout",
    ),
    _definition(
        "trend_pullback_reclaim_v1",
        TrendPullbackReclaimConfig,
        generate_trend_pullback_reclaim_intents,
        "trend_pullback_reclaim",
    ),
)

STRATEGIES: Mapping[str, StrategyDefinition] = MappingProxyType(
    {definition.strategy_id: definition for definition in _DEFINITIONS}
)


def get_strategy(strategy_id: str) -> StrategyDefinition:
    try:
        return STRATEGIES[strategy_id]
    except KeyError as exc:
        raise KeyError(f"unknown strategy_id: {strategy_id}") from exc


def generate_sleeve_intents(
    strategy_id: str,
    candles: Sequence[Candle],
    config: object | None = None,
    *,
    for_paper: bool = False,
) -> tuple[SleeveIntent, ...]:
    """Generate a stable ordered set from the registered source implementation."""

    definition = get_strategy(strategy_id)
    if for_paper and not definition.paper_enabled:
        raise PaperStrategyDisabledError(f"{strategy_id} is not PAPER-approved")
    settings = definition.config_type() if config is None else config
    if not isinstance(settings, definition.config_type):
        raise TypeError(f"config for {strategy_id} must be {definition.config_type.__name__}")
    generated = definition.generator(list(candles), settings)
    if any(not isinstance(intent, SleeveIntent) for intent in generated):
        raise TypeError("strategy generators must return SleeveIntent values")
    if any(intent.sleeve_id != strategy_id for intent in generated):
        raise ValueError("strategy generator emitted an intent with the wrong strategy id")
    return tuple(
        sorted(
            generated,
            key=lambda intent: (
                intent.decision_ts_ms,
                intent.symbol,
                intent.side.value,
                intent.intent_id,
            ),
        )
    )
