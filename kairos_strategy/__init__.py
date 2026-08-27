"""Kairos deterministic strategy source of truth.

The package is intentionally pure: no exchange clients, LLMs, environment
secrets, wall-clock reads, or randomness are permitted in strategy generation.
"""

from .candles import Candle
from .config import StrategyEngineSettings
from .factors import DerivativeStateObservation, canonical_derivative_observations
from .models import ExitPlan, ExitReason, SleeveIntent, TradeRecord
from .provenance import (
    candle_payload,
    canonical_json_bytes,
    canonical_sha256,
    config_sha256,
    features_sha256,
    input_window_sha256,
    installed_source_tree_sha256,
    source_tree_sha256,
)
from .registry import (
    CONTEXTUAL_STRATEGIES,
    STRATEGIES,
    ContextualStrategyDefinition,
    PaperStrategyDisabledError,
    StrategyDefinition,
    StrategyStatus,
    generate_contextual_sleeve_intents,
    generate_sleeve_intents,
    get_contextual_strategy,
    get_strategy,
)
from .runtime import (
    ClosedBarSequenceError,
    UnsupportedExitPlanError,
    candle_to_closed_bar,
    canonical_closed_bars,
    canonical_intent_batch_bytes,
    closed_bar_to_candle,
    generate_research_strategy_intents,
    generate_runtime_strategy_intents,
)
from .sleeves import *  # noqa: F403
from .sleeves import __all__ as _sleeve_exports

__all__ = [
    "CONTEXTUAL_STRATEGIES",
    "STRATEGIES",
    "Candle",
    "ContextualStrategyDefinition",
    "DerivativeStateObservation",
    "ClosedBarSequenceError",
    "ExitPlan",
    "ExitReason",
    "PaperStrategyDisabledError",
    "SleeveIntent",
    "StrategyDefinition",
    "StrategyEngineSettings",
    "StrategyStatus",
    "TradeRecord",
    "UnsupportedExitPlanError",
    "candle_to_closed_bar",
    "candle_payload",
    "canonical_json_bytes",
    "canonical_derivative_observations",
    "canonical_closed_bars",
    "canonical_intent_batch_bytes",
    "canonical_sha256",
    "config_sha256",
    "features_sha256",
    "generate_sleeve_intents",
    "generate_contextual_sleeve_intents",
    "generate_research_strategy_intents",
    "generate_runtime_strategy_intents",
    "get_strategy",
    "get_contextual_strategy",
    "input_window_sha256",
    "installed_source_tree_sha256",
    "source_tree_sha256",
    "closed_bar_to_candle",
    *_sleeve_exports,
]
