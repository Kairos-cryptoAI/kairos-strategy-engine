import math
from dataclasses import replace

import pytest
from kairos_core.contracts import ExitPlanV1
from kairos_core.enums import Side

from kairos_strategy.candles import Candle
from kairos_strategy.runtime import (
    candle_to_closed_bar,
    canonical_intent_batch_bytes,
    generate_research_strategy_intents,
    generate_runtime_strategy_intents,
)
from kairos_strategy.sleeves.right_tail_trend import (
    RightTailTrendConfig,
    generate_right_tail_trend_intents,
)


def _hourly_path(*, hours: int = 73, hourly_return: float = 0.01) -> list[Candle]:
    price = 100.0
    rows: list[Candle] = []
    minute_return = (1 + hourly_return) ** (1 / 60) - 1
    for index in range(hours * 60):
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
                taker_buy_volume=55.0 if hourly_return > 0 else 45.0,
                taker_buy_quote_volume=(55.0 if hourly_return > 0 else 45.0) * closed,
            )
        )
        price = closed
    return rows


@pytest.mark.parametrize(("hourly_return", "side"), [(0.01, Side.LONG), (-0.01, Side.SHORT)])
def test_daily_closed_hour_trend_emits_symmetric_fixed_lifecycle(hourly_return, side):
    intents = generate_right_tail_trend_intents(_hourly_path(hourly_return=hourly_return))

    assert len(intents) == 3
    assert {intent.side for intent in intents} == {side}
    assert [intent.decision_ts_ms for intent in intents] == [
        25 * 3_600_000 - 1,
        49 * 3_600_000 - 1,
        73 * 3_600_000 - 1,
    ]
    for intent in intents:
        risk = abs(intent.reference_price - intent.exit_plan.stop_price)
        reward = abs(intent.exit_plan.target_price - intent.reference_price)
        assert intent.sleeve_id == "right_tail_trend_v1"
        assert intent.entry_eligible_ts_ms == intent.decision_ts_ms + 1
        assert intent.entry_expires_ts_ms == intent.decision_ts_ms + 3_600_000
        assert intent.exit_plan.max_holding_ms == 72 * 3_600_000
        assert reward / risk == pytest.approx(4.0)
        assert intent.exit_plan.trailing_activation_price is None
        assert len(dict(intent.metadata)["feature_hash"]) == 64
        assert math.isfinite(intent.signal_strength)


def test_alternating_hourly_returns_do_not_form_a_trend():
    rows: list[Candle] = []
    price = 100.0
    for hour in range(73):
        factor = 1.01 if hour % 2 == 0 else 1 / 1.01
        minute_return = factor ** (1 / 60) - 1
        for minute in range(60):
            index = hour * 60 + minute
            closed = price * (1 + minute_return)
            spread = price * 0.0005
            rows.append(
                Candle(
                    symbol="BTCUSDT",
                    timeframe="1m",
                    open_time_ms=index * 60_000,
                    close_time_ms=(index + 1) * 60_000 - 1,
                    open=price,
                    high=max(price, closed) + spread,
                    low=min(price, closed) - spread,
                    close=closed,
                    volume=100.0,
                    quote_volume=100 * closed,
                    taker_buy_volume=50.0,
                    taker_buy_quote_volume=50.0 * closed,
                )
            )
            price = closed

    assert generate_right_tail_trend_intents(rows) == []


def test_future_mutation_does_not_change_prior_intent_bytes():
    source = _hourly_path()
    cutoff = 49 * 3_600_000 - 1
    original = [
        intent for intent in generate_right_tail_trend_intents(source) if intent.decision_ts_ms <= cutoff
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
        intent for intent in generate_right_tail_trend_intents(changed) if intent.decision_ts_ms <= cutoff
    ]

    assert original
    assert mutated == original


def test_hourly_gap_resets_score_and_atr_history():
    source = _hourly_path(hours=121)
    gapped = source[: 48 * 60] + source[49 * 60 :]
    intents = generate_right_tail_trend_intents(gapped)

    assert intents
    assert not [intent for intent in intents if 49 * 3_600_000 <= intent.decision_ts_ms < 73 * 3_600_000 - 1]
    assert any(intent.decision_ts_ms >= 73 * 3_600_000 - 1 for intent in intents)


def test_research_and_runtime_adapters_are_byte_identical():
    candles = _hourly_path(hours=25)
    bars = tuple(candle_to_closed_bar(candle) for candle in candles)

    research = generate_research_strategy_intents("right_tail_trend_v1", candles)
    runtime = generate_runtime_strategy_intents("right_tail_trend_v1", tuple(reversed(bars)))

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
    ],
)
def test_invalid_config_fails_closed(kwargs):
    with pytest.raises(ValueError):
        RightTailTrendConfig(**kwargs)


@pytest.mark.parametrize("config", [False, 0, ""])
def test_falsey_invalid_config_does_not_select_defaults(config):
    with pytest.raises(ValueError, match="RightTailTrendConfig"):
        generate_right_tail_trend_intents([], config)  # type: ignore[arg-type]
