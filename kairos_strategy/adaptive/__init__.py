"""Isolated new hypothesis; legacy registries and frozen source files are untouched."""

from .adapter import AdaptiveEvaluation, build_adaptive_capability_policy, evaluate_adaptive_closed_bars
from .config import DEFAULT_CONFIG, STRATEGY_ID, AdaptiveConfig
from .logic import AdaptiveDecision, evaluate_adaptive, generate_adaptive_intents
from .provenance import adaptive_source_identity

__all__ = [
    "DEFAULT_CONFIG",
    "STRATEGY_ID",
    "AdaptiveConfig",
    "AdaptiveDecision",
    "evaluate_adaptive",
    "generate_adaptive_intents",
    "AdaptiveEvaluation",
    "build_adaptive_capability_policy",
    "evaluate_adaptive_closed_bars",
    "adaptive_source_identity",
]
