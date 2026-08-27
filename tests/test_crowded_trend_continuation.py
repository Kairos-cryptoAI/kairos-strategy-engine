from __future__ import annotations

from dataclasses import replace

import pytest
from kairos_core.enums import Side

from kairos_strategy.candles import Candle
from kairos_strategy.factors import (
    DerivativeStateObservation,
    canonical_derivative_observations,
)
from kairos_strategy.registry import (
    CONTEXTUAL_STRATEGIES,
    PaperStrategyDisabledError,
    StrategyStatus,
    generate_contextual_sleeve_intents,
)
from kairos_strategy.sleeves import (
    CrowdedTrendContinuationConfig,
    generate_crowded_trend_continuation_intents,
)

HOUR_MS = 3_600_000


def _source(*, hours: int = 50, hourly_return: float = 0.01):
    candles: list[Candle] = []
    factors: list[DerivativeStateObservation] = []
    price = 100.0
    minute_return = (1 + hourly_return) ** (1 / 60) - 1
    for hour in range(hours):
        for minute in range(60):
            index = hour * 60 + minute
            opened = price
            closed = opened * (1 + minute_return)
            candles.append(
                Candle(
                    symbol="BTCUSDT",
                    timeframe="1m",
                    open_time_ms=index * 60_000,
                    close_time_ms=(index + 1) * 60_000 - 1,
                    open=opened,
                    high=max(opened, closed) * 1.0005,
                    low=min(opened, closed) * 0.9995,
                    close=closed,
                    volume=100.0,
                    quote_volume=100.0 * closed,
                    taker_buy_volume=55.0,
                    taker_buy_quote_volume=55.0 * closed,
                )
            )
            price = closed
        opened_ms = hour * HOUR_MS
        factors.append(
            DerivativeStateObservation(
                symbol="BTCUSDT",
                open_time_ms=opened_ms,
                close_time_ms=(hour + 1) * HOUR_MS - 1,
                premium_close=0.0006 if hourly_return > 0 else -0.0006,
                funding_rate=0.0,
                funding_timestamp_ms=opened_ms,
                open_interest_value=100.0 if hour < 24 else 106.0,
                open_interest_timestamp_ms=(hour + 1) * HOUR_MS - 1,
            )
        )
    return candles, factors


@pytest.mark.parametrize(("hourly_return", "side"), [(0.01, Side.LONG), (-0.01, Side.SHORT)])
def test_exact_crowding_state_emits_symmetric_fixed_lifecycle(hourly_return, side):
    candles, factors = _source(hourly_return=hourly_return)

    intents = generate_crowded_trend_continuation_intents(candles, factors)

    assert len(intents) == 24
    assert {intent.side for intent in intents} == {side}
    assert intents[0].decision_ts_ms == 25 * HOUR_MS - 1
    assert intents[-1].decision_ts_ms == 48 * HOUR_MS - 1
    for intent in intents:
        risk = abs(intent.reference_price - intent.exit_plan.stop_price)
        reward = abs(intent.exit_plan.target_price - intent.reference_price)
        assert intent.sleeve_id == "crowded_trend_continuation_v1"
        assert intent.entry_eligible_ts_ms == intent.decision_ts_ms + 1
        assert intent.entry_expires_ts_ms == intent.decision_ts_ms + HOUR_MS
        assert intent.exit_plan.max_holding_ms == 24 * HOUR_MS
        assert reward / risk == pytest.approx(4.0)
        assert len(dict(intent.metadata)["feature_hash"]) == 64


def test_open_interest_and_aligned_carry_are_both_required():
    candles, factors = _source()
    no_oi_growth = [replace(item, open_interest_value=100.0) for item in factors]
    no_aligned_carry = [replace(item, premium_close=-0.0006, funding_rate=0.0) for item in factors]

    assert generate_crowded_trend_continuation_intents(candles, no_oi_growth) == []
    assert generate_crowded_trend_continuation_intents(candles, no_aligned_carry) == []


def test_future_price_or_factor_mutation_cannot_change_prior_intents():
    candles, factors = _source()
    cutoff = 30 * HOUR_MS - 1
    original = [
        intent
        for intent in generate_crowded_trend_continuation_intents(candles, factors)
        if intent.decision_ts_ms <= cutoff
    ]
    changed_candles = [
        replace(row, open=row.open * 2, high=row.high * 2, low=row.low * 2, close=row.close * 2)
        if row.open_time_ms >= 30 * HOUR_MS
        else row
        for row in candles
    ]
    changed_factors = [
        replace(row, premium_close=0.01, open_interest_value=500.0)
        if row.open_time_ms >= 30 * HOUR_MS
        else row
        for row in factors
    ]
    mutated = [
        intent
        for intent in generate_crowded_trend_continuation_intents(changed_candles, changed_factors)
        if intent.decision_ts_ms <= cutoff
    ]

    assert original
    assert mutated == original


def test_contextual_registry_is_stable_and_fails_closed_for_paper():
    candles, factors = _source()
    direct = tuple(generate_crowded_trend_continuation_intents(candles, factors))
    registered = generate_contextual_sleeve_intents(
        "crowded_trend_continuation_v1", candles, tuple(reversed(factors))
    )

    assert registered == direct
    assert CONTEXTUAL_STRATEGIES["crowded_trend_continuation_v1"].status is StrategyStatus.RESEARCH
    with pytest.raises(PaperStrategyDisabledError, match="not PAPER-approved"):
        generate_contextual_sleeve_intents("crowded_trend_continuation_v1", candles, factors, for_paper=True)


def test_duplicate_or_stale_factor_context_fails_closed():
    _, factors = _source(hours=25)
    with pytest.raises(ValueError, match="duplicate factor"):
        canonical_derivative_observations([factors[0], factors[0]])
    with pytest.raises(ValueError, match="no older than eight hours"):
        replace(factors[10], funding_timestamp_ms=factors[10].open_time_ms - 9 * HOUR_MS)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"trend_lookback_hours": 0},
        {"minimum_open_interest_change": 0},
        {"minimum_aligned_premium": float("nan")},
        {"minimum_aligned_funding": False},
        {"target_reward_to_risk": -1},
        {"max_hold_hours": 1},
    ],
)
def test_invalid_config_fails_closed(kwargs):
    with pytest.raises(ValueError):
        CrowdedTrendContinuationConfig(**kwargs)
