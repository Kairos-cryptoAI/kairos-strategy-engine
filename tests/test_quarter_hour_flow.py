from __future__ import annotations

from dataclasses import replace

import pytest
from kairos_core.enums import Side

from kairos_strategy.candles import Candle
from kairos_strategy.registry import PaperStrategyDisabledError, generate_sleeve_intents
from kairos_strategy.runtime import (
    candle_to_closed_bar,
    canonical_intent_batch_bytes,
    generate_research_strategy_intents,
    generate_runtime_strategy_intents,
)
from kairos_strategy.sleeves import QuarterHourFlowConfig, generate_quarter_hour_flow_intents


def _fixture(*, direction: int = 1, minutes: int = 1_530) -> list[Candle]:
    rows: list[Candle] = []
    price = 100.0
    for index in range(minutes):
        opened = price
        closed = opened + (0.02 if index % 2 == 0 else -0.01)
        boundary = index % 15 == 0
        buyer_share = 0.60 if direction > 0 else 0.40
        if not boundary:
            buyer_share = 0.50
        quote_volume = 1_200.0 if boundary else 1_000.0
        rows.append(
            Candle(
                symbol="BTCUSDT",
                timeframe="1m",
                open_time_ms=index * 60_000,
                close_time_ms=(index + 1) * 60_000 - 1,
                open=opened,
                high=max(opened, closed) + 0.08,
                low=min(opened, closed) - 0.08,
                close=closed,
                volume=10.0,
                quote_volume=quote_volume,
                taker_buy_volume=10.0 * buyer_share,
                taker_buy_quote_volume=quote_volume * buyer_share,
            )
        )
        price = closed
    return rows


@pytest.mark.parametrize(
    "overrides,match",
    [
        ({"phase_lags": 0}, "positive integer"),
        ({"minimum_agreeing_lags": 13}, "cannot exceed"),
        ({"minimum_current_imbalance": float("nan")}, "finite and positive"),
        ({"minimum_predictable_imbalance": 1.1}, "must not exceed one"),
    ],
)
def test_config_rejects_invalid_controls(overrides, match):
    with pytest.raises(ValueError, match=match):
        QuarterHourFlowConfig(**overrides)


@pytest.mark.parametrize("direction,side", [(1, Side.LONG), (-1, Side.SHORT)])
def test_boundary_flow_emits_causal_fixed_lifecycle(direction, side):
    intents = generate_quarter_hour_flow_intents(_fixture(direction=direction))
    assert intents
    intent = intents[-1]
    assert intent.side is side
    assert intent.decision_ts_ms % (15 * 60_000) == 60_000 - 1
    assert intent.entry_eligible_ts_ms == intent.decision_ts_ms + 1
    assert intent.entry_expires_ts_ms == intent.decision_ts_ms + 60_000
    assert intent.exit_plan.max_holding_ms == 8 * 60 * 60_000
    if side is Side.LONG:
        assert intent.exit_plan.stop_price < intent.reference_price < intent.exit_plan.target_price
    else:
        assert intent.exit_plan.target_price < intent.reference_price < intent.exit_plan.stop_price
    assert dict(intent.metadata)["boundary_proxy"] == "closed_first_1m"


def test_signal_requires_current_boundary_agreement_and_volume():
    rows = _fixture()
    last_boundary = max(index for index in range(len(rows)) if index % 15 == 0)
    disagrees = replace(
        rows[last_boundary],
        taker_buy_volume=4.0,
        taker_buy_quote_volume=rows[last_boundary].quote_volume * 0.4,
    )
    rows[last_boundary] = disagrees
    assert all(
        intent.decision_ts_ms != disagrees.close_time_ms
        for intent in generate_quarter_hour_flow_intents(rows)
    )

    rows = _fixture()
    low_volume = replace(
        rows[last_boundary],
        quote_volume=100.0,
        taker_buy_quote_volume=60.0,
    )
    rows[last_boundary] = low_volume
    assert all(
        intent.decision_ts_ms != low_volume.close_time_ms
        for intent in generate_quarter_hour_flow_intents(rows)
    )


def test_gap_resets_all_phase_and_atr_state():
    rows = _fixture()
    removed = rows[:1_450] + rows[1_451:]
    intents = generate_quarter_hour_flow_intents(removed)
    assert all(intent.decision_ts_ms < rows[1_450].open_time_ms for intent in intents)


def test_research_runtime_parity_is_byte_identical_and_paper_is_disabled():
    rows = _fixture()
    config = QuarterHourFlowConfig()
    bars = tuple(candle_to_closed_bar(row) for row in rows)
    research = generate_research_strategy_intents("quarter_hour_flow_v1", rows, config)
    runtime = generate_runtime_strategy_intents("quarter_hour_flow_v1", bars, config)
    assert research
    assert canonical_intent_batch_bytes(research) == canonical_intent_batch_bytes(runtime)

    with pytest.raises(PaperStrategyDisabledError, match="not PAPER-approved"):
        generate_sleeve_intents("quarter_hour_flow_v1", rows, config, for_paper=True)
