"""Finite causal trend/range/retest hypothesis; no clocks, orders or promotion."""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass

from kairos_core.contracts.regime_capability import CapabilityRegime
from kairos_core.contracts.strategy_evaluation import StrategyEvaluationStatus
from kairos_core.enums import Side

from ..candles import Candle
from ..models import ExitPlan, SleeveIntent
from ..provenance import candle_payload, canonical_sha256
from ..registry import PaperStrategyDisabledError
from ..timeframes import aggregate
from ..validation import canonical_candles
from .config import DEFAULT_CONFIG, STRATEGY_ID, UNIVERSE, AdaptiveConfig, require_fixed_config
from .provenance import adaptive_window_sha256


@dataclass(frozen=True, slots=True)
class AdaptiveDecision:
    """An algorithm outcome, not an attestation of real-time source availability."""

    status: StrategyEvaluationStatus
    reason: str
    regime: CapabilityRegime
    decision_ts_ms: int | None
    input_window_sha256: str | None
    features: tuple[tuple[str, str], ...] = ()
    intent: SleeveIntent | None = None


def _mean(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("a finite indicator requires a complete window")
    scale = max(abs(value) for value in values)
    return 0.0 if scale == 0 else math.fsum(value / scale for value in values) / len(values) * scale


def _atr(rows: Sequence[Candle], period: int = 14) -> float:
    if len(rows) < period + 1:
        raise ValueError("ATR requires prior closes and its complete finite window")
    tail = rows[-period - 1 :]
    return _mean(
        [
            max(row.high - row.low, abs(row.high - prior.close), abs(row.low - prior.close))
            for prior, row in zip(tail, tail[1:], strict=False)
        ]
    )


def _through(rows: Sequence[Candle], timestamp: int) -> list[Candle]:
    end = bisect_right([row.close_time_ms for row in rows], timestamp)
    return list(rows[:end])


def _raw_regime(hours: Sequence[Candle], config: AdaptiveConfig) -> CapabilityRegime:
    if len(hours) < max(config.slow_hours, config.fast_hours + config.slope_hours):
        return CapabilityRegime.UNCERTAIN
    closes = [row.close for row in hours]
    fast = _mean(closes[-config.fast_hours :])
    slow = _mean(closes[-config.slow_hours :])
    prior_fast = _mean(closes[-config.fast_hours - config.slope_hours : -config.slope_hours])
    atr = _atr(hours, config.atr_period)
    if atr <= 0:
        return CapabilityRegime.UNCERTAIN
    er_closes = closes[-config.efficiency_hours - 1 :]
    scale = max(er_closes)
    scaled = [value / scale for value in er_closes]
    variation = math.fsum(abs(right - left) for left, right in zip(scaled, scaled[1:], strict=False))
    efficiency = 0.0 if variation == 0 else abs(scaled[-1] - scaled[0]) / variation
    slope = (fast - prior_fast) / atr
    gap = abs(fast - slow) / atr
    if not all(math.isfinite(value) for value in (efficiency, slope, gap)):
        raise ValueError("regime arithmetic is outside the finite numeric range")
    if closes[-1] > fast > slow and efficiency >= config.trend_efficiency and slope >= config.trend_slope_atr:
        return CapabilityRegime.BULL
    if (
        closes[-1] < fast < slow
        and efficiency >= config.trend_efficiency
        and slope <= -config.trend_slope_atr
    ):
        return CapabilityRegime.BEAR
    if (
        efficiency <= config.range_efficiency
        and gap <= config.range_ma_gap_atr
        and abs(slope) <= config.range_slope_atr
    ):
        return CapabilityRegime.RANGE
    return CapabilityRegime.UNCERTAIN


def _base_regime(hours: Sequence[Candle], timestamp: int, config: AdaptiveConfig) -> CapabilityRegime:
    prefix = _through(hours, timestamp)
    if len(prefix) < config.slow_hours + 1:
        return CapabilityRegime.UNCERTAIN
    current, previous = _raw_regime(prefix, config), _raw_regime(prefix[:-1], config)
    return current if current is previous else CapabilityRegime.UNCERTAIN


def _regime_at(
    rows5: Sequence[Candle], hours: Sequence[Candle], timestamp: int, config: AdaptiveConfig
) -> tuple[CapabilityRegime, int | None]:
    """Apply the SAME shock/cooldown rule at each causal pattern timestamp."""
    prefix5, prefix_hours = _through(rows5, timestamp), _through(hours, timestamp)
    regime = _base_regime(prefix_hours, timestamp, config)
    shocks = _shocks(prefix5, config)
    last_shock = shocks[-1] if shocks else None
    if last_shock is not None:
        if len(prefix5) - 1 - last_shock < config.crash_bars:
            regime = CapabilityRegime.CRASH
        elif prefix_hours[-2].close_time_ms <= prefix5[last_shock].close_time_ms:
            regime = CapabilityRegime.UNCERTAIN
    return regime, last_shock


def _shocks(rows: Sequence[Candle], config: AdaptiveConfig) -> list[int]:
    # Only the last three hours can affect current CRASH/cooldown. Padding for
    # each strictly-prior finite ATR is already inside the complete 54h input.
    indices: list[int] = []
    for index in range(max(config.atr_period + 1, len(rows) - 36), len(rows)):
        prior_atr = _atr(rows[:index], config.atr_period)
        if prior_atr > 0 and rows[index].close - rows[index - 1].close <= -config.shock_atr * prior_atr:
            indices.append(index)
    return indices


def _frozen_range(
    rows15: Sequence[Candle], before_ms: int, config: AdaptiveConfig
) -> tuple[float, float, float]:
    prefix = _through(rows15, before_ms - 1)
    range_rows = prefix[-config.range_bars :]
    # ATR is fixed before the range itself, not enlarged by its subsequent sweep.
    atr = _atr(prefix[: -config.range_bars], config.atr_period)
    return min(row.low for row in range_rows), max(row.high for row in range_rows), atr


def _has_separated_touches(rows: Sequence[Candle], level: float, width: float, *, lower: bool) -> bool:
    touches = [
        index
        for index, row in enumerate(rows)
        if (row.low <= level + width if lower else row.high >= level - width)
    ]
    return bool(touches) and touches[-1] - touches[0] >= 4


def _trend_setup(
    rows5: Sequence[Candle],
    rows15: Sequence[Candle],
    hours: Sequence[Candle],
    regime: CapabilityRegime,
    config: AdaptiveConfig,
) -> tuple[Side, float, float, float, str] | None:
    first, pullback, reclaim = rows5[-3:]
    prefix15 = _through(rows15, first.close_time_ms)
    mean = _mean([row.close for row in prefix15[-config.setup_mean_bars :]])
    atr15, atr5 = _atr(prefix15, config.atr_period), _atr(rows5[:-2], config.atr_period)
    if atr15 <= 0 or atr5 <= 0 or _regime_at(rows5, hours, first.close_time_ms, config)[0] is not regime:
        return None
    if regime is CapabilityRegime.BULL:
        matched = (
            first.close >= mean + 0.25 * atr15
            and mean - 0.25 * atr15 <= pullback.close <= mean + 0.10 * atr15
            and pullback.low <= mean + 0.10 * atr15
            and mean + 0.25 * atr15 <= reclaim.close <= mean + 0.75 * atr15
            and min(pullback.low, reclaim.low) >= mean - 0.50 * atr15
        )
        side, stop = Side.LONG, min(pullback.low, reclaim.low) - 0.25 * atr5
    else:
        matched = (
            first.close <= mean - 0.25 * atr15
            and mean - 0.10 * atr15 <= pullback.close <= mean + 0.25 * atr15
            and pullback.high >= mean - 0.10 * atr15
            and mean - 0.75 * atr15 <= reclaim.close <= mean - 0.25 * atr15
            and max(pullback.high, reclaim.high) <= mean + 0.50 * atr15
        )
        side, stop = Side.SHORT, max(pullback.high, reclaim.high) + 0.25 * atr5
    if not matched:
        return None
    risk = abs(reclaim.close - stop)
    target = reclaim.close + (1 if side is Side.LONG else -1) * config.target_r * risk
    return side, stop, target, atr15, "TREND_PULLBACK_RECLAIM"


def _range_setup(
    rows5: Sequence[Candle], rows15: Sequence[Candle], config: AdaptiveConfig
) -> tuple[Side, float, float, float, str] | None:
    previous, current = rows5[-2:]
    lower, upper, atr15 = _frozen_range(rows15, current.open_time_ms, config)
    width = upper - lower
    prefix15 = _through(rows15, current.open_time_ms - 1)
    range_rows = prefix15[-config.range_bars :]
    if (
        atr15 <= 0
        or not 2 * atr15 <= width <= 6 * atr15
        or not lower + 0.25 * width <= previous.close <= upper - 0.25 * width
        or not _has_separated_touches(range_rows, lower, 0.25 * atr15, lower=True)
        or not _has_separated_touches(range_rows, upper, 0.25 * atr15, lower=False)
    ):
        return None
    atr5 = _atr(rows5[:-1], config.atr_period)
    if (
        lower - 0.25 * atr15 <= current.low <= lower
        and lower + 0.10 * atr15 <= current.close <= lower + 0.50 * atr15
    ):
        return Side.LONG, current.low - 0.25 * atr5, (lower + upper) / 2, atr15, "RANGE_EDGE_RECLAIM"
    if (
        upper <= current.high <= upper + 0.25 * atr15
        and upper - 0.50 * atr15 <= current.close <= upper - 0.10 * atr15
    ):
        return Side.SHORT, current.high + 0.25 * atr5, (lower + upper) / 2, atr15, "RANGE_EDGE_RECLAIM"
    return None


def _crash_setup(
    rows5: Sequence[Candle], rows15: Sequence[Candle], shock: int, config: AdaptiveConfig
) -> tuple[tuple[Side, float, float, float, str] | None, str]:
    lower, _, atr15 = _frozen_range(rows15, rows5[shock].open_time_ms, config)
    if atr15 <= 0 or rows5[shock].close >= lower - 0.10 * atr15:
        return None, "CRASH_PROTECTION_ONLY"
    last = len(rows5) - 1
    for end in range(shock + 3, min(shock + 6, last) + 1):
        retest, rejection = rows5[end - 1 : end + 1]
        if any(row.close > lower + 0.25 * atr15 for row in rows5[shock + 1 : end + 1]):
            return None, "CRASH_LEVEL_INVALIDATED"
        matched = (
            lower - 0.10 * atr15 <= retest.high <= lower + 0.25 * atr15
            and lower - 0.10 * atr15 <= retest.close <= lower + 0.10 * atr15
            and lower - 0.50 * atr15 <= rejection.close < lower - 0.10 * atr15
        )
        if not matched:
            continue
        # Geometry consumes the first setup before economics/review/no-fill.
        # No per-arm or position state can create a second attempt for this shock.
        if end != last:
            return None, "CRASH_SETUP_ALREADY_CONSUMED"
        atr5 = _atr(rows5[: end - 1], config.atr_period)
        stop = max(retest.high, rejection.high) + 0.25 * atr5
        target = rejection.close - config.target_r * (stop - rejection.close)
        return (Side.SHORT, stop, target, atr15, "CRASH_SUPPORT_RETEST"), "PATTERN_READY"
    return None, "CRASH_WAITING_FOR_CONTROLLED_RETEST"


def _planning_cost_bps(config: AdaptiveConfig) -> float:
    # Same aggregate planning components as the existing AllInCostModel, with
    # explicit adverse carry/latency assumptions. This is NOT a venue receipt.
    return (
        2 * config.fee_bps_per_side
        + config.spread_bps
        + 2 * config.slippage_bps_per_side
        + config.uncertainty_buffer_bps
        + config.adverse_carry_bps
        + config.latency_bps
    )


def _planning_economics(
    reference: float, stop: float, target: float, config: AdaptiveConfig
) -> tuple[float, float, float, float]:
    risk_bps = abs(reference - stop) / reference * 10_000
    reward_bps = abs(target - reference) / reference * 10_000
    cost = _planning_cost_bps(config)
    # Same exact stop-loss/reward arithmetic as size_and_admit, expressed in
    # entry-relative bps to avoid overflowing price * cost on extreme inputs.
    stop_cost_bps = cost * max(1.0, stop / reference)
    reward_cost_bps = cost * max(1.0, target / reference)
    net_rr = max(0.0, reward_bps - reward_cost_bps) / (risk_bps + stop_cost_bps)
    return risk_bps, reward_bps, cost, net_rr


def evaluate_adaptive(
    candles: Sequence[Candle], config: AdaptiveConfig = DEFAULT_CONFIG, *, for_paper: bool = False
) -> AdaptiveDecision:
    """Evaluate ONLY the latest causal slot using a fixed 54h window.

    Historical input may be sorted for reproducible offline evaluation. This
    does not claim live delivery order, arrival time or venue qualification.
    """
    if for_paper:
        raise PaperStrategyDisabledError(f"{STRATEGY_ID} is RESEARCH only, not PAPER-approved")
    require_fixed_config(config)
    ordered = canonical_candles(candles, expected_timeframe="1m")
    if not ordered:
        return AdaptiveDecision("WARMUP", "NO_CLOSED_HISTORY", CapabilityRegime.UNCERTAIN, None, None)
    latest = ordered[-1]
    window = ordered[-config.history_bars :]
    if latest.symbol not in UNIVERSE:
        raise ValueError("adaptive input is outside the fixed five-symbol universe")
    if any(row.open_time_ms % 60_000 or row.close_time_ms != row.open_time_ms + 59_999 for row in window):
        raise ValueError("adaptive requires exactly closed UTC-aligned one-minute candles")
    if any(
        right.open_time_ms != left.open_time_ms + 60_000
        for left, right in zip(window, window[1:], strict=False)
    ):
        return AdaptiveDecision(
            "UNAVAILABLE", "CLOSED_HISTORY_GAP", CapabilityRegime.UNCERTAIN, latest.close_time_ms, None
        )
    window_sha = adaptive_window_sha256(window)
    if len(window) < config.history_bars:
        return AdaptiveDecision(
            "WARMUP",
            "COMPLETE_54H_HISTORY_REQUIRED",
            CapabilityRegime.UNCERTAIN,
            latest.close_time_ms,
            window_sha,
        )
    if (latest.close_time_ms + 1) % (config.decision_minutes * 60_000):
        return AdaptiveDecision(
            "NOT_SCHEDULED",
            "OUTSIDE_CLOSED_5M_CLOCK",
            CapabilityRegime.UNCERTAIN,
            latest.close_time_ms,
            window_sha,
        )
    rows5, rows15, hours = (aggregate(window, frame) for frame in ("5m", "15m", "1h"))
    if any(
        right.open_time_ms - left.open_time_ms != interval
        for rows, interval in ((rows5, 300_000), (rows15, 900_000), (hours, 3_600_000))
        for left, right in zip(rows, rows[1:], strict=False)
    ):
        return AdaptiveDecision(
            "UNAVAILABLE",
            "COMPLETE_FRAME_CONTINUITY_REQUIRED",
            CapabilityRegime.UNCERTAIN,
            latest.close_time_ms,
            window_sha,
        )
    if any(_atr(rows, config.atr_period) <= 0 for rows in (rows5, rows15, hours)):
        return AdaptiveDecision(
            "UNAVAILABLE",
            "UNDEFINED_VOLATILITY_CONTEXT",
            CapabilityRegime.UNCERTAIN,
            latest.close_time_ms,
            window_sha,
        )
    regime, last_shock = _regime_at(rows5, hours, latest.close_time_ms, config)
    features = {
        "policy_version": config.policy_version,
        "regime": regime.value,
        "last_complete_hour_ms": str(hours[-1].close_time_ms),
        "last_shock_ms": "NONE" if last_shock is None else str(rows5[last_shock].close_time_ms),
        "setup_bars_sha256": canonical_sha256([candle_payload(row) for row in rows5[-3:]]),
    }
    outcome_reason = "NO_COMPLETED_SETUP"
    setup = None
    if regime in (CapabilityRegime.BULL, CapabilityRegime.BEAR):
        setup = _trend_setup(rows5, rows15, hours, regime, config)
    elif regime is CapabilityRegime.RANGE:
        setup = _range_setup(rows5, rows15, config)
    elif regime is CapabilityRegime.CRASH and last_shock is not None:
        setup, outcome_reason = _crash_setup(rows5, rows15, last_shock, config)
    else:
        outcome_reason = "REGIME_UNCERTAIN_OR_POST_SHOCK_COOLDOWN"
    if setup is None:
        return AdaptiveDecision(
            "NO_INTENT",
            outcome_reason,
            regime,
            latest.close_time_ms,
            window_sha,
            tuple(sorted(features.items())),
        )
    side, stop, target, atr15, pattern = setup
    reference = latest.close
    risk = abs(reference - stop)
    if (
        not all(math.isfinite(value) for value in (stop, target, risk, atr15))
        or min(stop, target, risk, atr15) <= 0
        or not (stop < reference < target if side is Side.LONG else target < reference < stop)
    ):
        return AdaptiveDecision(
            "NO_INTENT",
            "NON_EXECUTABLE_BARRIERS",
            regime,
            latest.close_time_ms,
            window_sha,
            tuple(sorted(features.items())),
        )
    risk_bps, reward_bps, cost, net_rr = _planning_economics(reference, stop, target, config)
    features.update(
        {
            "pattern": pattern,
            "frozen_atr15": format(atr15, ".17g"),
            "planning_round_trip_bps": format(cost, ".17g"),
            "planning_net_reward_risk": format(net_rr, ".17g"),
            "planning_cost_authority": "ASSUMPTION_NOT_VENUE_MEASUREMENT",
        }
    )
    if (
        not all(math.isfinite(value) for value in (stop, target, risk, risk_bps, reward_bps, cost, net_rr))
        or min(stop, target, risk) <= 0
    ):
        reason = "NON_EXECUTABLE_BARRIERS"
    elif not config.minimum_stop_atr * atr15 <= risk <= config.maximum_stop_atr * atr15:
        reason = "STOP_OUTSIDE_STRUCTURAL_ATR_BOUNDS"
    elif risk_bps > config.maximum_stop_bps:
        reason = "STOP_DISTANCE_TOO_WIDE"
    elif cost > config.maximum_cost_stop_fraction * risk_bps or net_rr < config.minimum_net_reward_risk:
        reason = "INSUFFICIENT_PLANNING_COST_HEADROOM"
    else:
        holding = (
            config.crash_holding_ms
            if regime is CapabilityRegime.CRASH
            else config.range_holding_ms
            if regime is CapabilityRegime.RANGE
            else config.trend_holding_ms
        )
        intent = SleeveIntent(
            sleeve_id=STRATEGY_ID,
            symbol=latest.symbol,
            side=side,
            decision_ts_ms=latest.close_time_ms,
            entry_eligible_ts_ms=latest.close_time_ms + 1,
            entry_expires_ts_ms=latest.close_time_ms + config.entry_lifetime_ms,
            reference_price=reference,
            signal_strength=1.0,
            gross_reward_bps=reward_bps,
            exit_plan=ExitPlan(stop_price=stop, target_price=target, max_holding_ms=holding),
            metadata=tuple(sorted(features.items())),
        )
        return AdaptiveDecision(
            "INTENT",
            "COMPLETE_CAUSAL_PATTERN",
            regime,
            latest.close_time_ms,
            window_sha,
            tuple(sorted(features.items())),
            intent,
        )
    return AdaptiveDecision(
        "NO_INTENT", reason, regime, latest.close_time_ms, window_sha, tuple(sorted(features.items()))
    )


def generate_adaptive_intents(
    candles: Sequence[Candle], config: AdaptiveConfig = DEFAULT_CONFIG, *, for_paper: bool = False
) -> list[SleeveIntent]:
    """Reference replay of the SAME finite per-slot decision, without arm state.

    This correctness reference is bounded by the supplied dataset. It is not a
    tuned multi-year screening runner; each slot uses a constant-size window.
    """
    if for_paper:
        raise PaperStrategyDisabledError(f"{STRATEGY_ID} is RESEARCH only, not PAPER-approved")
    require_fixed_config(config)
    ordered = canonical_candles(candles, expected_timeframe="1m")
    intents: list[SleeveIntent] = []
    for end in range(config.history_bars, len(ordered) + 1):
        if (ordered[end - 1].close_time_ms + 1) % (config.decision_minutes * 60_000):
            continue
        decision = evaluate_adaptive(ordered[end - config.history_bars : end], config)
        if decision.intent is not None:
            intents.append(decision.intent)
    return intents
