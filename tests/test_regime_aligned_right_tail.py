from __future__ import annotations

import math
from dataclasses import replace

import pytest
from kairos_core.contracts import ExitPlanV1
from kairos_core.enums import Side

from kairos_strategy.candles import Candle
from kairos_strategy.registry import (
    STRATEGIES,
    PaperStrategyDisabledError,
    StrategyStatus,
    generate_sleeve_intents,
)
from kairos_strategy.runtime import (
    candle_to_closed_bar,
    canonical_intent_batch_bytes,
    generate_research_strategy_intents,
    generate_runtime_strategy_intents,
)
from kairos_strategy.sleeves.regime_aligned_right_tail import (
    RegimeAlignedRightTailConfig,
    _regime_allows,
    generate_regime_aligned_right_tail_intents,
)

HOUR_MS = 3_600_000


def _hourly_path(hourly_returns: list[float]) -> list[Candle]:
    price = 100.0
    rows: list[Candle] = []
    for hour, hourly_return in enumerate(hourly_returns):
        minute_return = (1 + hourly_return) ** (1 / 60) - 1
        for minute in range(60):
            index = hour * 60 + minute
            opened = price
            closed = opened * (1 + minute_return)
            spread = opened * 0.0005
            rows.append(
                Candle(
                    symbol="BTCUSDT",
                    timeframe="1m",
                    open_time_ms=index * 60_000,
                    close_time_ms=(index + 1) * 60_000 - 1,
                    open=opened,
                    high=max(opened, closed) + spread,
                    low=min(opened, closed) - spread,
                    close=closed,
                    volume=100.0,
                    quote_volume=100.0 * closed,
                    taker_buy_volume=55.0 if hourly_return >= 0 else 45.0,
                    taker_buy_quote_volume=(55.0 if hourly_return >= 0 else 45.0) * closed,
                )
            )
            price = closed
    return rows


def _config() -> RegimeAlignedRightTailConfig:
    return RegimeAlignedRightTailConfig(regime_sma_bars=3)


@pytest.mark.parametrize(("hourly_return", "side"), [(0.01, Side.LONG), (-0.01, Side.SHORT)])
def test_matching_slow_regime_preserves_exact_base_lifecycle(hourly_return, side):
    source = _hourly_path([hourly_return] * 73)

    intents = generate_regime_aligned_right_tail_intents(source, _config())

    assert len(intents) == 3
    assert {intent.side for intent in intents} == {side}
    for intent in intents:
        risk = abs(intent.reference_price - intent.exit_plan.stop_price)
        reward = abs(intent.exit_plan.target_price - intent.reference_price)
        metadata = dict(intent.metadata)
        assert intent.sleeve_id == "regime_aligned_right_tail_v1"
        assert intent.entry_eligible_ts_ms == intent.decision_ts_ms + 1
        assert intent.entry_expires_ts_ms == intent.decision_ts_ms + HOUR_MS
        assert intent.exit_plan.max_holding_ms == 72 * HOUR_MS
        assert reward / risk == pytest.approx(4.0)
        assert intent.exit_plan.trailing_activation_price is None
        assert metadata["regime_timeframe"] == "4h"
        assert len(metadata["base_intent_id"]) == 64
        assert len(metadata["feature_hash"]) == 64
        assert math.isfinite(intent.signal_strength)


def test_base_long_is_rejected_when_recent_four_hour_state_is_below_sma():
    returns = [0.0, *([0.02] * 20), *([-0.05] * 4)]

    intents = generate_regime_aligned_right_tail_intents(_hourly_path(returns), _config())

    assert intents == []


def test_equality_and_flat_side_are_never_admitted():
    assert not _regime_allows(Side.LONG, 100.0, 100.0)
    assert not _regime_allows(Side.SHORT, 100.0, 100.0)
    assert not _regime_allows(Side.FLAT, 90.0, 100.0)


def test_gap_resets_both_base_and_regime_warmup():
    source = _hourly_path([0.01] * 97)
    gapped = source[: 48 * 60] + source[49 * 60 :]

    intents = generate_regime_aligned_right_tail_intents(gapped, _config())

    assert not [intent for intent in intents if 49 * HOUR_MS <= intent.decision_ts_ms < 73 * HOUR_MS]
    assert any(intent.decision_ts_ms >= 73 * HOUR_MS - 1 for intent in intents)


def test_future_mutation_cannot_change_prior_intent_bytes():
    source = _hourly_path([0.01] * 73)
    cutoff = 49 * HOUR_MS - 1
    original = [
        intent
        for intent in generate_regime_aligned_right_tail_intents(source, _config())
        if intent.decision_ts_ms <= cutoff
    ]
    changed = list(source)
    for index in range(49 * 60, len(changed)):
        row = changed[index]
        changed[index] = replace(
            row,
            open=row.open * 2,
            high=row.high * 2,
            low=row.low * 2,
            close=row.close * 2,
            quote_volume=row.quote_volume * 2,
            taker_buy_quote_volume=row.taker_buy_quote_volume * 2,
        )
    mutated = [
        intent
        for intent in generate_regime_aligned_right_tail_intents(changed, _config())
        if intent.decision_ts_ms <= cutoff
    ]

    assert original
    assert mutated == original


def test_registry_uses_exact_generator_and_fails_closed_for_paper():
    source = _hourly_path([0.01] * 25)
    config = _config()

    direct = tuple(generate_regime_aligned_right_tail_intents(source, config))
    registered = generate_sleeve_intents("regime_aligned_right_tail_v1", source, config)

    assert direct == registered
    assert STRATEGIES["regime_aligned_right_tail_v1"].status is StrategyStatus.RESEARCH
    with pytest.raises(PaperStrategyDisabledError, match="not PAPER-approved"):
        generate_sleeve_intents("regime_aligned_right_tail_v1", source, config, for_paper=True)


def test_research_and_runtime_adapters_are_byte_identical():
    candles = _hourly_path([0.01] * 25)
    bars = tuple(candle_to_closed_bar(candle) for candle in candles)
    config = _config()

    research = generate_research_strategy_intents("regime_aligned_right_tail_v1", candles, config)
    runtime = generate_runtime_strategy_intents("regime_aligned_right_tail_v1", tuple(reversed(bars)), config)

    assert len(research) == len(runtime) == 1
    assert canonical_intent_batch_bytes(research) == canonical_intent_batch_bytes(runtime)
    assert isinstance(research[0].exit_plan, ExitPlanV1)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"trend_lookback_hours": 0},
        {"minimum_trend_score": float("nan")},
        {"atr_period_hours": True},
        {"stop_atr_multiple": 0},
        {"target_reward_to_risk": -1},
        {"max_hold_hours": 1},
        {"decision_interval_hours": 0},
        {"intent_valid_hours": 25},
        {"regime_sma_bars": True},
        {"regime_sma_bars": 1},
    ],
)
def test_invalid_config_fails_closed(kwargs):
    with pytest.raises(ValueError):
        RegimeAlignedRightTailConfig(**kwargs)


@pytest.mark.parametrize("config", [False, 0, ""])
def test_falsey_invalid_config_does_not_select_defaults(config):
    with pytest.raises(ValueError, match="RegimeAlignedRightTailConfig"):
        generate_regime_aligned_right_tail_intents([], config)  # type: ignore[arg-type]
