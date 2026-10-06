"""Engineering cases only: no empirical profitability or scientific promotion."""

from __future__ import annotations

from dataclasses import replace

import pytest
from kairos_core.contracts.regime_capability import CapabilityRegime
from kairos_core.enums import Side

from kairos_strategy.adaptive import (
    DEFAULT_CONFIG,
    STRATEGY_ID,
    AdaptiveConfig,
    build_adaptive_capability_policy,
    evaluate_adaptive,
    evaluate_adaptive_closed_bars,
    generate_adaptive_intents,
)
from kairos_strategy.adaptive.config import UNIVERSE
from kairos_strategy.adaptive.logic import _atr, _base_regime, _planning_economics, _regime_at, _trend_setup
from kairos_strategy.candles import Candle
from kairos_strategy.provenance import canonical_sha256
from kairos_strategy.runtime import candle_to_closed_bar
from kairos_strategy.timeframes import aggregate

SOURCE_SET = "a" * 64
BarGeometry = tuple[float, float, float, float]


def _minutes(geometries: list[BarGeometry], *, offset_minutes: int = 0) -> list[Candle]:
    """Expand controlled 5m geometry into genuine, complete closed 1m OHLC."""
    rows = []
    for index, (opened, high, low, closed) in enumerate(geometries):
        for minute in range(5):
            left = opened + (closed - opened) * minute / 5
            right = opened + (closed - opened) * (minute + 1) / 5
            timestamp = (offset_minutes + index * 5 + minute) * 60_000
            rows.append(
                Candle(
                    symbol="BTCUSDT",
                    timeframe="1m",
                    open_time_ms=timestamp,
                    close_time_ms=timestamp + 59_999,
                    open=left,
                    close=right,
                    high=max(left, right, high if minute == 2 else right),
                    low=min(left, right, low if minute == 2 else right),
                    volume=1.0,
                )
            )
    return rows


def _trend_case(*, short: bool = False, offset: int = 0, cooldown_shock: bool = False) -> list[Candle]:
    geometries = []
    for index in range(660):
        closed = 100.0 + 0.05 * index
        opened = 100.0 + 0.05 * max(0, index - 1)
        geometries.append((opened, closed + 0.4, opened - 0.4, closed))
    if cooldown_shock:
        opened = geometries[636][0]
        geometries[636] = (opened, opened + 0.4, opened - 3.8, opened - 3.4)
    prefix = _minutes(geometries, offset_minutes=offset)
    first_close = prefix[-11].close_time_ms
    rows15 = [row for row in aggregate(prefix, "15m") if row.close_time_ms <= first_close]
    mean = sum(row.close for row in rows15[-20:]) / 20
    atr = _atr(rows15)
    first = geometries[-3][3]
    pullback, reclaim = mean + 0.05 * atr, mean + 0.65 * atr
    geometries[-2] = (first, first, mean - 0.4 * atr, pullback)
    geometries[-1] = (pullback, reclaim + 0.1 * atr, mean - 0.1 * atr, reclaim)
    if short:
        # Exact mirrored geometry, not a separately tuned bearish parameter set.
        geometries = [
            (280 - opened, 280 - low, 280 - high, 280 - closed) for opened, high, low, closed in geometries
        ]
    return _minutes(geometries, offset_minutes=offset)


def _range_geometries() -> list[BarGeometry]:
    rows: list[BarGeometry] = [(100.0, 100.5, 99.5, 100.0) for _ in range(648)]
    # Two separated touches on each edge; not every 5m candle sweeps both edges.
    for index in (600, 624):
        rows[index] = (100.0, 100.5, 98.0, 100.0)
    for index in (612, 636):
        rows[index] = (100.0, 102.0, 99.5, 100.0)
    return rows


def _range_case(*, short: bool = False) -> list[Candle]:
    rows = _range_geometries()
    rows.append((100.0, 102.1, 99.5, 101.6) if short else (100.0, 100.5, 97.9, 98.4))
    return _minutes(rows)


def _crash_case(*, reject_first_cost: bool = False, repeated: bool = False) -> list[Candle]:
    rows = _range_geometries()
    rejected_close = 97.89 if reject_first_cost else 97.7
    rows.extend(
        [
            (100.0, 100.5, 97.6, 97.7),  # Shock, with ATR fixed strictly before it.
            (97.7, 98.0, 97.5, 97.7),
            (97.7, 98.1, 97.5, 98.0),  # Controlled retest, not a falling-price chase.
            (98.0, 98.1, 97.5, rejected_close),
        ]
    )
    if repeated:
        rows.extend([(rejected_close, 98.0, 97.5, 97.7), (97.7, 98.1, 97.5, 98.0), (98.0, 98.1, 97.5, 97.6)])
    return _minutes(rows)


@pytest.mark.parametrize("symbol", UNIVERSE)
def test_real_generator_to_strict_observation_bull_path(symbol):
    rows = [replace(row, symbol=symbol) for row in _trend_case()]
    decision = evaluate_adaptive(rows)
    assert decision.status == "INTENT", (decision.reason, decision.features)
    assert decision.regime is CapabilityRegime.BULL
    assert decision.intent is not None and decision.intent.side is Side.LONG
    bars = [candle_to_closed_bar(row) for row in rows]
    result = evaluate_adaptive_closed_bars(
        bars, observed_at_ms=rows[-1].close_time_ms + 2, source_set_sha256=SOURCE_SET
    )
    assert result.intent is not None and result.regime_observation is not None
    assert result.intent.strategy_id == STRATEGY_ID
    assert result.evaluation.evaluation_complete and not result.evaluation.trading_authority
    assert result.intent.provenance.input_window_sha256 == decision.input_window_sha256
    assert result.intent.provenance.input_bar_sha256s == tuple(bar.bar_sha256 for bar in bars[-3240:])
    assert result.evaluation.input_window_sha256 == result.intent.provenance.input_window_sha256
    assert result.evaluation.intent_ids == (result.intent.intent_id,)
    build_adaptive_capability_policy(SOURCE_SET).validate_observation(result.regime_observation)


def test_bear_is_mirrored_rules_and_short_only():
    decision = evaluate_adaptive(_trend_case(short=True))
    assert decision.status == "INTENT", (decision.reason, decision.features)
    assert decision.regime is CapabilityRegime.BEAR
    assert decision.intent is not None and decision.intent.side is Side.SHORT
    assert decision.intent.exit_plan.max_holding_ms == 120 * 60_000


@pytest.mark.parametrize("short", (False, True))
def test_range_reclaim_is_executable_but_needs_a_fresh_center_to_edge_event(short):
    rows = _range_case(short=short)
    decision = evaluate_adaptive(rows)
    assert decision.status == "INTENT", (decision.reason, decision.features)
    assert decision.regime is CapabilityRegime.RANGE
    assert decision.intent is not None and decision.intent.side is (Side.SHORT if short else Side.LONG)
    assert decision.intent.exit_plan.target_price == 100.0
    assert decision.intent.exit_plan.max_holding_ms == 60 * 60_000
    final = rows[-1].close
    followup = [(final, max(final, 100.5 if short else 98.5), min(final, 99.5 if short else 97.9), final)]
    extra = _minutes(followup, offset_minutes=(rows[-1].close_time_ms + 1) // 60_000)
    assert evaluate_adaptive([*rows, *extra]).intent is None


def test_crash_retest_is_short_not_an_unconditional_shock_order():
    rows = _crash_case()
    shock_only = evaluate_adaptive(rows[:-15])
    assert shock_only.regime is CapabilityRegime.CRASH and shock_only.intent is None
    decision = evaluate_adaptive(rows)
    assert decision.status == "INTENT", (decision.reason, decision.features)
    assert decision.regime is CapabilityRegime.CRASH
    assert decision.intent is not None and decision.intent.side is Side.SHORT
    assert decision.intent.exit_plan.max_holding_ms == 60 * 60_000
    repeated = evaluate_adaptive(_crash_case(repeated=True))
    assert repeated.status == "NO_INTENT" and repeated.reason == "CRASH_SETUP_ALREADY_CONSUMED"


def test_first_crash_geometry_is_consumed_even_if_economics_reject_it():
    first = evaluate_adaptive(_crash_case(reject_first_cost=True))
    assert first.intent is None and first.reason == "INSUFFICIENT_PLANNING_COST_HEADROOM"
    later = evaluate_adaptive(_crash_case(reject_first_cost=True, repeated=True))
    assert later.intent is None and later.reason == "CRASH_SETUP_ALREADY_CONSUMED"


@pytest.mark.parametrize("offset", (0, 5, 10))
def test_finite_window_full_prefix_and_rolling_tail_have_identical_candidates(offset):
    rows = _trend_case(offset=offset)
    assert evaluate_adaptive(rows) == evaluate_adaptive(rows[-3240:])
    bars = [candle_to_closed_bar(row) for row in rows]
    clock = rows[-1].close_time_ms + 1
    full = evaluate_adaptive_closed_bars(bars, observed_at_ms=clock, source_set_sha256=SOURCE_SET)
    tail = evaluate_adaptive_closed_bars(bars[-3240:], observed_at_ms=clock, source_set_sha256=SOURCE_SET)
    assert full == tail


def test_future_append_cannot_change_an_earlier_replay_candidate():
    rows = _trend_case()
    prefix = generate_adaptive_intents(rows)
    assert prefix and prefix[-1].decision_ts_ms == rows[-1].close_time_ms
    future = _minutes(
        [(rows[-1].close, 200.0, 80.0, 90.0)] * 3, offset_minutes=(rows[-1].close_time_ms + 1) // 60_000
    )
    extended = generate_adaptive_intents([*rows, *future])
    assert [item for item in extended if item.decision_ts_ms <= rows[-1].close_time_ms] == prefix


def test_p1_cannot_escape_post_shock_cooldown_at_first_eligible_hour():
    rows = _trend_case(cooldown_shock=True)
    rows5, rows15, hours = [aggregate(rows[-3240:], frame) for frame in ("5m", "15m", "1h")]
    first, last = rows5[-3].close_time_ms, rows5[-1].close_time_ms
    assert _base_regime(hours, first, DEFAULT_CONFIG) is CapabilityRegime.BULL
    assert _regime_at(rows5, hours, first, DEFAULT_CONFIG)[0] is CapabilityRegime.UNCERTAIN
    assert _regime_at(rows5, hours, last, DEFAULT_CONFIG)[0] is CapabilityRegime.BULL
    assert _trend_setup(rows5, rows15, hours, CapabilityRegime.BULL, DEFAULT_CONFIG) is None
    assert evaluate_adaptive(rows).intent is None


def test_planning_economics_match_existing_loss_and_reward_formula():
    decision = evaluate_adaptive(_trend_case())
    assert decision.intent is not None
    intent, features = decision.intent, dict(decision.features)
    entry, stop, target = intent.reference_price, intent.exit_plan.stop_price, intent.exit_plan.target_price
    loss = abs(entry - stop) + max(entry, stop) * 20 / 10_000
    reward = max(0, abs(target - entry) - max(entry, target) * 20 / 10_000)
    assert float(features["planning_round_trip_bps"]) == 20.0
    assert float(features["planning_net_reward_risk"]) == pytest.approx(reward / loss, rel=1e-12)
    assert features["planning_cost_authority"] == "ASSUMPTION_NOT_VENUE_MEASUREMENT"
    assert intent.signal_strength == 1.0  # Rule diagnostic, NOT a confidence/size multiplier.
    assert intent.entry_eligible_ts_ms == intent.decision_ts_ms + 1
    assert intent.entry_expires_ts_ms == intent.decision_ts_ms + 60_000


def test_one_fixed_config_has_no_implicit_parameter_search_or_per_asset_tuning():
    with pytest.raises(TypeError):
        AdaptiveConfig(fast_hours=10)
    tampered = AdaptiveConfig()
    object.__setattr__(tampered, "fast_hours", 10)
    with pytest.raises(ValueError, match="exact fixed configuration"):
        evaluate_adaptive(_trend_case(), tampered)
    assert canonical_sha256(DEFAULT_CONFIG) != canonical_sha256(tampered)


@pytest.mark.parametrize("short", (False, True))
@pytest.mark.parametrize("risk_bps", (59.5, 60.0, 60.5, 61.0))
def test_cost_boundary_uses_exact_side_specific_arithmetic_not_rounded_minimum(short, risk_bps):
    entry, direction = 100.0, -1 if short else 1
    distance = entry * risk_bps / 10_000
    stop, target = entry - direction * distance, entry + direction * 2 * distance
    actual_risk, reward_bps, cost, net_rr = _planning_economics(entry, stop, target, DEFAULT_CONFIG)
    loss = distance + max(entry, stop) * 0.002
    reward = max(0, 2 * distance - max(entry, target) * 0.002)
    assert actual_risk == pytest.approx(risk_bps)
    assert reward_bps == pytest.approx(2 * risk_bps)
    assert cost == 20
    assert net_rr == pytest.approx(reward / loss, rel=1e-12)
    if risk_bps <= 60:
        assert net_rr < DEFAULT_CONFIG.minimum_net_reward_risk
    else:
        assert net_rr > DEFAULT_CONFIG.minimum_net_reward_risk


def test_backfilled_valid_pattern_never_becomes_a_timely_live_candidate():
    rows = _trend_case()
    assert evaluate_adaptive(rows).intent is not None
    result = evaluate_adaptive_closed_bars(
        [candle_to_closed_bar(row) for row in rows],
        observed_at_ms=rows[-1].close_time_ms + 60_001,
        source_set_sha256=SOURCE_SET,
    )
    assert result.intent is None and result.regime_observation is None
    assert result.evaluation.status == "UNAVAILABLE" and not result.evaluation.evaluation_complete


def test_numeric_failure_is_error_not_a_successful_quiet_evaluation():
    rows = [replace(row, volume=1e308) for row in _trend_case()]
    result = evaluate_adaptive_closed_bars(
        [candle_to_closed_bar(row) for row in rows],
        observed_at_ms=rows[-1].close_time_ms + 1,
        source_set_sha256=SOURCE_SET,
    )
    assert result.intent is None and result.regime_observation is None
    assert result.evaluation.status == "ERROR" and not result.evaluation.evaluation_complete
    assert result.evaluation.error_type == "ValueError"
    assert result.evaluation.reason_code == "FINITE_EVALUATION_ERROR"
