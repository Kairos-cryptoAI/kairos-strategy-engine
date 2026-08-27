"""Deterministic, causal strategy sleeves shared by research and runtime."""

from .crowded_trend_continuation import (
    CrowdedTrendContinuationConfig,
    generate_crowded_trend_continuation_intents,
)
from .donchian_ensemble import DonchianEnsembleConfig, generate_donchian_ensemble_allocations
from .orderflow_volatility_expansion import (
    OrderFlowExpansionVariant,
    OrderFlowVolatilityExpansionConfig,
    generate_orderflow_volatility_expansion_intents,
)
from .quarter_hour_flow import QuarterHourFlowConfig, generate_quarter_hour_flow_intents
from .range_mean_reversion import (
    RangeMeanReversionConfig,
    generate_range_mean_reversion_intents,
)
from .regime_retest_reclaim import (
    RegimeRetestGenerationCounters,
    RegimeRetestGenerationEvidence,
    RegimeRetestReclaimVariant,
    RegimeRetestSetupEvent,
    RegimeRetestSetupEventType,
    RegimeVetoRetestReclaimConfig,
    generate_regime_veto_retest_reclaim_evidence,
    generate_regime_veto_retest_reclaim_intents,
)
from .right_tail_trend import RightTailTrendConfig, generate_right_tail_trend_intents
from .trend_breakout import TrendBreakoutConfig, generate_trend_breakout_intents
from .trend_pullback_reclaim import (
    PullbackDepthVariant,
    TrendPullbackReclaimConfig,
    generate_trend_pullback_reclaim_intents,
)

__all__ = [
    "CrowdedTrendContinuationConfig",
    "DonchianEnsembleConfig",
    "OrderFlowExpansionVariant",
    "OrderFlowVolatilityExpansionConfig",
    "PullbackDepthVariant",
    "QuarterHourFlowConfig",
    "RangeMeanReversionConfig",
    "RightTailTrendConfig",
    "RegimeRetestGenerationCounters",
    "RegimeRetestGenerationEvidence",
    "RegimeRetestReclaimVariant",
    "RegimeRetestSetupEvent",
    "RegimeRetestSetupEventType",
    "RegimeVetoRetestReclaimConfig",
    "TrendBreakoutConfig",
    "TrendPullbackReclaimConfig",
    "generate_orderflow_volatility_expansion_intents",
    "generate_crowded_trend_continuation_intents",
    "generate_donchian_ensemble_allocations",
    "generate_quarter_hour_flow_intents",
    "generate_range_mean_reversion_intents",
    "generate_right_tail_trend_intents",
    "generate_regime_veto_retest_reclaim_evidence",
    "generate_regime_veto_retest_reclaim_intents",
    "generate_trend_breakout_intents",
    "generate_trend_pullback_reclaim_intents",
]
