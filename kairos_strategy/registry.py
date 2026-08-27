"""Closed registry of deterministic strategy generators and their status."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from .allocation import TargetAllocation
from .candles import Candle
from .factors import DerivativeStateObservation
from .models import SleeveIntent
from .sleeves import (
    CrowdedTrendContinuationConfig,
    DonchianEnsembleConfig,
    FourHourSma200Config,
    OrderFlowVolatilityExpansionConfig,
    QuarterHourFlowConfig,
    RangeMeanReversionConfig,
    RegimeVetoRetestReclaimConfig,
    RightTailTrendConfig,
    TrendBreakoutConfig,
    TrendPullbackReclaimConfig,
    generate_crowded_trend_continuation_intents,
    generate_donchian_ensemble_allocations,
    generate_four_hour_sma200_allocations,
    generate_orderflow_volatility_expansion_intents,
    generate_quarter_hour_flow_intents,
    generate_range_mean_reversion_intents,
    generate_regime_veto_retest_reclaim_intents,
    generate_right_tail_trend_intents,
    generate_trend_breakout_intents,
    generate_trend_pullback_reclaim_intents,
)

StrategyGenerator = Callable[[list[Candle], Any], list[SleeveIntent]]
ContextualStrategyGenerator = Callable[
    [list[Candle], list[DerivativeStateObservation], Any], list[SleeveIntent]
]
AllocationStrategyGenerator = Callable[[list[Candle], Any], list[TargetAllocation]]


class StrategyStatus(StrEnum):
    """Promotion status; none of the current research sleeves may trade PAPER."""

    INCONCLUSIVE = "inconclusive"
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


@dataclass(frozen=True, slots=True)
class ContextualStrategyDefinition:
    strategy_id: str
    revision: str
    config_type: type[Any]
    generator: ContextualStrategyGenerator
    source_files: tuple[str, ...]
    status: StrategyStatus = StrategyStatus.RESEARCH

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


@dataclass(frozen=True, slots=True)
class AllocationStrategyDefinition:
    strategy_id: str
    revision: str
    config_type: type[Any]
    generator: AllocationStrategyGenerator
    source_files: tuple[str, ...]
    status: StrategyStatus = StrategyStatus.RESEARCH

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
    *,
    status: StrategyStatus = StrategyStatus.REJECTED,
) -> StrategyDefinition:
    return StrategyDefinition(
        strategy_id=strategy_id,
        revision="1",
        config_type=config_type,
        generator=generator,
        source_files=(*_COMMON_SOURCE_FILES, f"sleeves/{module_name}.py"),
        status=status,
    )


_DEFINITIONS = (
    _definition(
        "right_tail_trend_v1",
        RightTailTrendConfig,
        generate_right_tail_trend_intents,
        "right_tail_trend",
    ),
    _definition(
        "quarter_hour_flow_v1",
        QuarterHourFlowConfig,
        generate_quarter_hour_flow_intents,
        "quarter_hour_flow",
        status=StrategyStatus.RESEARCH,
    ),
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

CONTEXTUAL_STRATEGIES: Mapping[str, ContextualStrategyDefinition] = MappingProxyType(
    {
        "crowded_trend_continuation_v1": ContextualStrategyDefinition(
            strategy_id="crowded_trend_continuation_v1",
            revision="1",
            config_type=CrowdedTrendContinuationConfig,
            generator=generate_crowded_trend_continuation_intents,
            source_files=(
                *_COMMON_SOURCE_FILES,
                "factors.py",
                "sleeves/crowded_trend_continuation.py",
            ),
            status=StrategyStatus.REJECTED,
        )
    }
)

ALLOCATION_STRATEGIES: Mapping[str, AllocationStrategyDefinition] = MappingProxyType(
    {
        "donchian_ensemble_long_v1": AllocationStrategyDefinition(
            strategy_id="donchian_ensemble_long_v1",
            revision="1",
            config_type=DonchianEnsembleConfig,
            generator=generate_donchian_ensemble_allocations,
            source_files=(
                *_COMMON_SOURCE_FILES,
                "allocation.py",
                "sleeves/donchian_ensemble.py",
            ),
            status=StrategyStatus.INCONCLUSIVE,
        ),
        "four_hour_sma200_long_v1": AllocationStrategyDefinition(
            strategy_id="four_hour_sma200_long_v1",
            revision="1",
            config_type=FourHourSma200Config,
            generator=generate_four_hour_sma200_allocations,
            source_files=(
                *_COMMON_SOURCE_FILES,
                "allocation.py",
                "sleeves/four_hour_sma200.py",
            ),
            status=StrategyStatus.RESEARCH,
        ),
    }
)


def get_strategy(strategy_id: str) -> StrategyDefinition:
    try:
        return STRATEGIES[strategy_id]
    except KeyError as exc:
        raise KeyError(f"unknown strategy_id: {strategy_id}") from exc


def get_contextual_strategy(strategy_id: str) -> ContextualStrategyDefinition:
    try:
        return CONTEXTUAL_STRATEGIES[strategy_id]
    except KeyError as exc:
        raise KeyError(f"unknown contextual strategy_id: {strategy_id}") from exc


def get_allocation_strategy(strategy_id: str) -> AllocationStrategyDefinition:
    try:
        return ALLOCATION_STRATEGIES[strategy_id]
    except KeyError as exc:
        raise KeyError(f"unknown allocation strategy_id: {strategy_id}") from exc


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


def generate_contextual_sleeve_intents(
    strategy_id: str,
    candles: Sequence[Candle],
    factor_observations: Sequence[DerivativeStateObservation],
    config: object | None = None,
    *,
    for_paper: bool = False,
) -> tuple[SleeveIntent, ...]:
    """Generate a stable contextual intent batch from explicit immutable inputs."""

    definition = get_contextual_strategy(strategy_id)
    if for_paper and not definition.paper_enabled:
        raise PaperStrategyDisabledError(f"{strategy_id} is not PAPER-approved")
    settings = definition.config_type() if config is None else config
    if not isinstance(settings, definition.config_type):
        raise TypeError(f"config for {strategy_id} must be {definition.config_type.__name__}")
    generated = definition.generator(list(candles), list(factor_observations), settings)
    if any(not isinstance(intent, SleeveIntent) for intent in generated):
        raise TypeError("contextual strategy generators must return SleeveIntent values")
    if any(intent.sleeve_id != strategy_id for intent in generated):
        raise ValueError("contextual strategy generator emitted an intent with the wrong strategy id")
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


def generate_target_allocations(
    strategy_id: str,
    candles: Sequence[Candle],
    config: object | None = None,
    *,
    for_paper: bool = False,
) -> tuple[TargetAllocation, ...]:
    definition = get_allocation_strategy(strategy_id)
    if for_paper and not definition.paper_enabled:
        raise PaperStrategyDisabledError(f"{strategy_id} is not PAPER-approved")
    settings = definition.config_type() if config is None else config
    if not isinstance(settings, definition.config_type):
        raise TypeError(f"config for {strategy_id} must be {definition.config_type.__name__}")
    generated = definition.generator(list(candles), settings)
    if any(not isinstance(allocation, TargetAllocation) for allocation in generated):
        raise TypeError("allocation generators must return TargetAllocation values")
    if any(allocation.strategy_id != strategy_id for allocation in generated):
        raise ValueError("allocation generator emitted a target with the wrong strategy id")
    return tuple(sorted(generated, key=lambda item: (item.decision_ts_ms, item.symbol, item.allocation_id)))
