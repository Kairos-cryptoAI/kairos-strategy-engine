"""One engineering hypothesis, not a tunable grid or a trading admission."""

from __future__ import annotations

from dataclasses import dataclass, field

STRATEGY_ID = "adaptive_pullback_range_v1"
STRATEGY_REVISION = "1"
DETECTOR_SOURCE = "adaptive-finite-detector-v1"
UNIVERSE = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT")


@dataclass(frozen=True, slots=True)
class AdaptiveConfig:
    """Fixed interpretable defaults chosen before economic evaluation.

    No constructor tuning knobs: changing a rule requires a new reviewed
    source/config identity. Costs are planning assumptions, NOT venue facts.
    Model/feed costs and actual funding still belong to the economic evaluator.
    """

    policy_version: str = field(default="adaptive-pullback-range.v1", init=False)
    history_bars: int = field(default=3_240, init=False)
    decision_minutes: int = field(default=5, init=False)
    fast_hours: int = field(default=20, init=False)
    slow_hours: int = field(default=50, init=False)
    efficiency_hours: int = field(default=24, init=False)
    slope_hours: int = field(default=3, init=False)
    atr_period: int = field(default=14, init=False)
    setup_mean_bars: int = field(default=20, init=False)
    range_bars: int = field(default=16, init=False)
    trend_efficiency: float = field(default=0.30, init=False)
    range_efficiency: float = field(default=0.20, init=False)
    trend_slope_atr: float = field(default=0.25, init=False)
    range_slope_atr: float = field(default=0.10, init=False)
    range_ma_gap_atr: float = field(default=0.50, init=False)
    shock_atr: float = field(default=2.0, init=False)
    crash_bars: int = field(default=12, init=False)
    minimum_stop_atr: float = field(default=0.50, init=False)
    maximum_stop_atr: float = field(default=2.0, init=False)
    maximum_stop_bps: float = field(default=300.0, init=False)
    target_r: float = field(default=2.0, init=False)
    minimum_net_reward_risk: float = field(default=1.25, init=False)
    maximum_cost_stop_fraction: float = field(default=0.50, init=False)
    fee_bps_per_side: float = field(default=4.5, init=False)
    spread_bps: float = field(default=2.0, init=False)
    slippage_bps_per_side: float = field(default=1.0, init=False)
    uncertainty_buffer_bps: float = field(default=2.0, init=False)
    adverse_carry_bps: float = field(default=3.0, init=False)
    latency_bps: float = field(default=2.0, init=False)
    entry_lifetime_ms: int = field(default=60_000, init=False)
    trend_holding_ms: int = field(default=120 * 60_000, init=False)
    range_holding_ms: int = field(default=60 * 60_000, init=False)
    crash_holding_ms: int = field(default=60 * 60_000, init=False)


DEFAULT_CONFIG = AdaptiveConfig()


def require_fixed_config(config: AdaptiveConfig) -> None:
    if type(config) is not AdaptiveConfig or config != DEFAULT_CONFIG:
        raise ValueError("adaptive v1 requires its exact fixed configuration; no implicit tuning")
