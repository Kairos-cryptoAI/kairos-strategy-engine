"""The live runner's rules, isolated; legacy registries and frozen source files are untouched."""

from .adapter import HourlyTrendEvaluation, evaluate_hourly_trend_closed_bars
from .provenance import HourlyTrendSourceIdentity, hourly_trend_source_identity
from .rules import (
    SETUP_BREAKOUT,
    SETUP_PULLBACK_RECLAIM,
    STRATEGY_ID,
    STRATEGY_REVISION,
    HourlyTrendCandidatesConfig,
    HourlyTrendState,
    evaluate_hourly_trend_state,
    generate_hourly_trend_candidates_intents,
)

__all__ = [
    "SETUP_BREAKOUT",
    "SETUP_PULLBACK_RECLAIM",
    "STRATEGY_ID",
    "STRATEGY_REVISION",
    "HourlyTrendCandidatesConfig",
    "HourlyTrendEvaluation",
    "HourlyTrendSourceIdentity",
    "HourlyTrendState",
    "evaluate_hourly_trend_closed_bars",
    "evaluate_hourly_trend_state",
    "generate_hourly_trend_candidates_intents",
    "hourly_trend_source_identity",
]
