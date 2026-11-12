"""The isolated hourly trend-candidate rules and their parity with the live runner.

The fixture holds what the runner's original implementation produced for the synthetic
series built below. The series uses an integer generator and plain arithmetic only, so it
is bit-identical on every platform.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from functools import cache
from pathlib import Path

import pytest
from kairos_core.enums import Side

from kairos_strategy.candles import Candle
from kairos_strategy.hourly_trend import (
    SETUP_BREAKOUT,
    SETUP_PULLBACK_RECLAIM,
    STRATEGY_ID,
    HourlyTrendCandidatesConfig,
    evaluate_hourly_trend_state,
    generate_hourly_trend_candidates_intents,
    hourly_trend_source_identity,
)
from kairos_strategy.hourly_trend.provenance import HOURLY_TREND_SOURCE_FILES
from kairos_strategy.hourly_trend.rules import _decide, _phases
from kairos_strategy.registry import ALLOCATION_STRATEGIES, CONTEXTUAL_STRATEGIES, STRATEGIES, get_strategy
from kairos_strategy.timeframes import build_timeframes

_FIXTURE = Path(__file__).parent / "fixtures" / "hourly_trend_candidates_runner_parity.json"
_MINUTE_MS = 60_000
_QUARTER_MS = 15 * _MINUTE_MS
_HOUR_MS = 60 * _MINUTE_MS
_DAY_MINUTES = 1_440
_START_MS = 1_790_000_000_000 // 86_400_000 * 86_400_000 + 7 * _MINUTE_MS
_COUNT = 26 * _DAY_MINUTES
_SEED = 20261112
_REGIME_MINUTES = 3 * _DAY_MINUTES
_DRIFTS = (0.00003, 0.0, -0.00003, 0.0)
_MISSING = frozenset(range(17 * _DAY_MINUTES + 200, 17 * _DAY_MINUTES + 203))
_SYMBOL = "TESTUSDT"


def _synthetic_minutes() -> list[list[float]]:
    """Rows of open time, open, high, low, close, base volume and quote volume."""

    state = _SEED

    def uniform() -> float:
        nonlocal state
        state = (state * 6364136223846793005 + 1442695040888963407) % 2**64
        return (state >> 11) / 2**53

    price = 100.0
    rows: list[list[float]] = []
    for index in range(_COUNT):
        drift = _DRIFTS[(index // _REGIME_MINUTES) % len(_DRIFTS)]
        noise = uniform() + uniform() + uniform() + uniform() - 2.0
        opened = price
        closed = opened * (1.0 + drift + 0.0006 * noise)
        high = max(opened, closed) * (1.0 + 0.0003 * uniform())
        low = min(opened, closed) * (1.0 - 0.0003 * uniform())
        base = 10.0 + 5.0 * uniform()
        price = closed
        if index not in _MISSING:
            rows.append([_START_MS + index * _MINUTE_MS, opened, high, low, closed, base, base * closed])
    return rows


@cache
def _expected() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


@cache
def _source() -> tuple[Candle, ...]:
    rows = _synthetic_minutes()
    digest = hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()
    assert digest == _expected()["input"]["sha256"], "the synthetic series differs from the recorded one"
    return tuple(
        Candle(
            symbol=_SYMBOL,
            timeframe="1m",
            open_time_ms=int(row[0]),
            close_time_ms=int(row[0]) + _MINUTE_MS - 1,
            open=row[1],
            high=row[2],
            low=row[3],
            close=row[4],
            volume=row[5],
            quote_volume=row[6],
        )
        for row in rows
    )


@cache
def _intents() -> tuple:
    return tuple(generate_hourly_trend_candidates_intents(list(_source())))


def _until(decision_ts_ms: int) -> list[Candle]:
    return [row for row in _source() if row.close_time_ms <= decision_ts_ms]


def test_candidates_equal_the_runner_implementation_bar_for_bar():
    expected = _expected()["candidates"]
    intents = _intents()

    assert {(row["side"], row["setup"]) for row in expected} == {
        (side, setup) for side in ("LONG", "SHORT") for setup in (SETUP_PULLBACK_RECLAIM, SETUP_BREAKOUT)
    }, "the fixture must exercise every rule"
    assert [intent.decision_ts_ms for intent in intents] == [row["decision_ts_ms"] for row in expected]
    for intent, row in zip(intents, expected, strict=True):
        metadata = dict(intent.metadata)
        assert intent.side.value == row["side"]
        assert metadata["setup"] == row["setup"]
        assert intent.reference_price == pytest.approx(row["reference_price"], rel=1e-12)
        assert intent.exit_plan.stop_price == pytest.approx(row["stop_price"], rel=1e-12)
        assert intent.exit_plan.target_price == pytest.approx(row["target_price"], rel=1e-12)
        assert float(metadata["atr"]) == pytest.approx(row["atr"], rel=1e-12)
        assert intent.exit_plan.max_holding_ms == row["max_holding_minutes"] * _MINUTE_MS


def test_state_equals_the_runner_features_on_every_sampled_decision():
    expected = _expected()
    quarters = build_timeframes(list(_source()))["15m"]
    phases = _phases(quarters)
    settings = HourlyTrendCandidatesConfig()

    evaluated = 0
    trends: dict[str, int] = {}
    states = {}
    for quarter in quarters:
        offset = (quarter.close_time_ms + 1) % _HOUR_MS
        state, _ = _decide(_SYMBOL, quarter.close_time_ms, phases[offset], settings)
        if state is None:
            continue
        evaluated += 1
        trends[state.trend] = trends.get(state.trend, 0) + 1
        states[state.decision_ts_ms] = state
    assert evaluated == expected["evaluated_decisions"]
    assert dict(sorted(trends.items())) == expected["trends"]

    assert len(expected["states"]) >= 30
    for row in expected["states"]:
        state = states[row["decision_ts_ms"]]
        assert state.trend == row["trend"]
        for name in (
            "price",
            "trend_ema_fast",
            "trend_ema_slow",
            "trend_efficiency",
            "rsi",
            "atr",
            "atr_pct",
            "change_4h_pct",
            "change_24h_pct",
            "dist_ema_atr",
        ):
            assert getattr(state, name) == pytest.approx(row[name], rel=1e-12, abs=1e-12), name
        context = row["rounded_context"]
        assert round(state.change_1h_pct, 3) == context["change_1h_pct"]
        assert round(state.change_7d_pct, 2) == context["change_7d_pct"]
        assert state.position_in_7d_range is not None
        assert round(state.position_in_7d_range, 3) == context["position_in_7d_range"]
        assert state.last_bar_volume_vs_20bar_avg is not None
        assert round(state.last_bar_volume_vs_20bar_avg, 2) == context["last_bar_volume_vs_20bar_avg"]
        assert state.consecutive_up_bars == context["consecutive_up_bars"]
        assert state.consecutive_down_bars == context["consecutive_down_bars"]


def test_intent_shape_follows_the_sleeve_conventions():
    intents = _intents()
    assert len({intent.intent_id for intent in intents}) == len(intents)
    for intent in intents:
        metadata = dict(intent.metadata)
        risk = abs(intent.reference_price - intent.exit_plan.stop_price)
        reward = abs(intent.exit_plan.target_price - intent.reference_price)
        atr = float(metadata["atr"])
        assert intent.sleeve_id == STRATEGY_ID == "hourly_trend_candidates_v1"
        assert intent.symbol == _SYMBOL
        assert (intent.decision_ts_ms + 1) % _QUARTER_MS == 0
        assert intent.entry_eligible_ts_ms == intent.decision_ts_ms + 1
        assert intent.entry_expires_ts_ms == intent.decision_ts_ms + _QUARTER_MS
        assert intent.exit_plan.max_holding_ms == 720 * _MINUTE_MS
        assert intent.exit_plan.trailing_activation_price is None
        assert reward / risk == pytest.approx(1.5)
        assert 0.8 * atr * (1 - 1e-12) <= risk <= 2.5 * atr * (1 + 1e-12)
        assert metadata["trend"] == ("UP" if intent.side is Side.LONG else "DOWN")
        assert metadata["setup"] in (SETUP_PULLBACK_RECLAIM, SETUP_BREAKOUT)
        assert metadata["strategy_version"] == STRATEGY_ID
        assert metadata["config_sha256"] == HourlyTrendCandidatesConfig().fingerprint
        assert len(metadata["feature_hash"]) == 64
        assert len(intent.intent_id) == 64


def test_newest_decision_matches_the_generator_and_needs_only_the_history_window():
    intents = _intents()
    for intent in (intents[0], intents[len(intents) // 2], intents[-1]):
        source = _until(intent.decision_ts_ms)
        state, single = evaluate_hourly_trend_state(source)
        assert single == intent
        assert state is not None and state.decision_ts_ms == intent.decision_ts_ms

    last = intents[-1]
    tail_start = last.decision_ts_ms + 1 - 504 * _HOUR_MS + _QUARTER_MS
    tail = [row for row in _until(last.decision_ts_ms) if row.open_time_ms >= tail_start]
    assert len(tail) < len(_until(last.decision_ts_ms)), "the fixture must be longer than the history window"
    assert evaluate_hourly_trend_state(tail)[1] == last, "older bars must not change a decision"

    decided = {intent.decision_ts_ms for intent in intents}
    quiet = next(
        row.close_time_ms
        for row in reversed(build_timeframes(list(_source()))["15m"])
        if row.close_time_ms not in decided
    )
    state, none = evaluate_hourly_trend_state(_until(quiet))
    assert none is None and state is not None, "the state is reported also without a candidate"


def test_input_order_does_not_change_the_result():
    last = _intents()[-1]
    source = _until(last.decision_ts_ms)[-505 * 60 :]
    assert evaluate_hourly_trend_state(list(reversed(source))) == evaluate_hourly_trend_state(source)


def test_a_gap_silences_only_the_decisions_whose_last_two_hours_it_touches():
    quarters = build_timeframes(list(_source()))["15m"]
    phases = _phases(quarters)
    settings = HourlyTrendCandidatesConfig()
    missing_quarter = (_START_MS + min(_MISSING) * _MINUTE_MS) // _QUARTER_MS * _QUARTER_MS
    assert missing_quarter not in {row.open_time_ms for row in quarters}

    def state_at(boundary_ms: int):
        return _decide(_SYMBOL, boundary_ms - 1, phases[boundary_ms % _HOUR_MS], settings)[0]

    assert state_at(missing_quarter) is not None
    for step in range(1, 9):
        assert state_at(missing_quarter + step * _QUARTER_MS) is None, step
    assert state_at(missing_quarter + 9 * _QUARTER_MS) is not None, "later decisions bridge the gap"


def test_short_history_gives_no_decision():
    source = list(_source())[: 9 * _DAY_MINUTES]
    assert generate_hourly_trend_candidates_intents(source) == []
    assert evaluate_hourly_trend_state(source) == (None, None)
    assert generate_hourly_trend_candidates_intents([]) == []
    assert evaluate_hourly_trend_state([]) == (None, None)


def test_a_stricter_configuration_only_removes_candidates():
    baseline = {intent.decision_ts_ms for intent in _intents()}
    strict = generate_hourly_trend_candidates_intents(
        list(_source()), HourlyTrendCandidatesConfig(minimum_trend_efficiency=0.5)
    )
    assert strict and {intent.decision_ts_ms for intent in strict} < baseline
    assert all(float(dict(intent.metadata)["trend_efficiency"]) >= 0.5 for intent in strict)


@pytest.mark.parametrize(
    "change",
    [
        {"history_hours": 0},
        {"history_hours": True},
        {"atr_period": 1.5},
        {"trend_fast_ema_period": 50},
        {"minimum_signal_bars": 301},
        {"signal_window_bars": 505},
        {"minimum_trend_efficiency": 1.1},
        {"minimum_trend_efficiency": float("nan")},
        {"breakout_stop_atr": 0},
        {"maximum_stop_distance_atr": 0.5},
        {"long_pullback_rsi_minimum": 70.0},
        {"short_pullback_rsi_maximum": 30.0},
        {"target_reward_to_risk": float("inf")},
    ],
)
def test_invalid_configuration_is_rejected(change):
    with pytest.raises(ValueError):
        replace(HourlyTrendCandidatesConfig(), **change)


def test_configuration_is_typed_and_fingerprinted():
    default = HourlyTrendCandidatesConfig()
    assert default.fingerprint == HourlyTrendCandidatesConfig().fingerprint
    assert replace(default, target_reward_to_risk=2).fingerprint != default.fingerprint
    assert isinstance(replace(default, target_reward_to_risk=2).target_reward_to_risk, float)
    with pytest.raises(ValueError, match="HourlyTrendCandidatesConfig"):
        generate_hourly_trend_candidates_intents([], {"history_hours": 504})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="HourlyTrendCandidatesConfig"):
        hourly_trend_source_identity({"history_hours": 504})  # type: ignore[arg-type]


def test_identity_is_separate_from_the_legacy_registry():
    identity = hourly_trend_source_identity()
    assert identity.strategy_id == STRATEGY_ID and identity.revision == "1"
    assert len(identity.strategy_code_sha256) == 64 and len(identity.config_sha256) == 64
    assert (
        hourly_trend_source_identity(replace(HourlyTrendCandidatesConfig(), max_hold_minutes=60)) != identity
    )
    assert "registry.py" not in HOURLY_TREND_SOURCE_FILES

    with pytest.raises(KeyError, match="unknown strategy_id: hourly_trend_candidates_v1"):
        get_strategy(STRATEGY_ID)
    assert STRATEGY_ID not in {**STRATEGIES, **CONTEXTUAL_STRATEGIES, **ALLOCATION_STRATEGIES}
