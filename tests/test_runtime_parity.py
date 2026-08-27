from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest
from kairos_core.contracts import ClosedBarEventV1

from kairos_strategy.candles import Candle
from kairos_strategy.registry import PaperStrategyDisabledError
from kairos_strategy.runtime import (
    ClosedBarSequenceError,
    UnsupportedExitPlanError,
    candle_to_closed_bar,
    canonical_intent_batch_bytes,
    generate_research_strategy_intents,
    generate_runtime_strategy_intents,
)
from kairos_strategy.sleeves import RangeMeanReversionConfig


def frozen_closed_bar_stream() -> tuple[Candle, ...]:
    """A complete immutable stream that emits exactly one range intent."""

    closes = [100 + (index % 2) * 0.2 for index in range(40)]
    closes[-2:] = [96.0, 98.0]
    rows: list[Candle] = []
    for bar, close in enumerate(closes):
        for minute in range(5):
            index = bar * 5 + minute
            rows.append(
                Candle(
                    symbol="BTCUSDT",
                    timeframe="1m",
                    open_time_ms=index * 60_000,
                    close_time_ms=(index + 1) * 60_000 - 1,
                    open=close,
                    high=close + 0.2,
                    low=close - 0.2,
                    close=close,
                    volume=10.0,
                    quote_volume=10.0 * close,
                    taker_buy_volume=5.0,
                    taker_buy_quote_volume=5.0 * close,
                )
            )
    return tuple(rows)


def frozen_range_config() -> RangeMeanReversionConfig:
    return RangeMeanReversionConfig(
        vwap_lookback_bars=3,
        atr_period=2,
        regime_lookback_hours=2,
        maximum_regime_efficiency=1,
        maximum_abs_hourly_slope=1,
        band_atr_multiple=0.5,
        stop_atr_multiple=1,
        max_hold_bars=6,
    )


def test_research_and_runtime_emit_byte_identical_ordered_contracts_and_ids():
    candles = frozen_closed_bar_stream()
    bars = tuple(candle_to_closed_bar(candle) for candle in candles)

    research = generate_research_strategy_intents("range_mean_reversion_v1", candles, frozen_range_config())
    runtime = generate_runtime_strategy_intents(
        "range_mean_reversion_v1", tuple(reversed(bars)), frozen_range_config()
    )

    assert len(research) == len(runtime) == 1
    assert {"message_id", "correlation_id", "produced_at"} <= bars[0].model_fields_set
    assert {"message_id", "correlation_id", "produced_at"} <= research[0].model_fields_set
    assert [intent.intent_id for intent in research] == [intent.intent_id for intent in runtime]
    assert canonical_intent_batch_bytes(research) == canonical_intent_batch_bytes(runtime)
    assert (
        hashlib.sha256(canonical_intent_batch_bytes(research)).hexdigest()
        == "dd33d84595306558ce1a147021ad0ff1df6bf96e855e8f862296c205fd8921c6"
    )


def test_runtime_blocks_gaps_duplicates_and_mixed_symbols():
    bars = tuple(candle_to_closed_bar(candle) for candle in frozen_closed_bar_stream())
    with pytest.raises(ClosedBarSequenceError, match="gap or reorder"):
        generate_runtime_strategy_intents(
            "range_mean_reversion_v1", bars[:10] + bars[11:], frozen_range_config()
        )
    with pytest.raises(ClosedBarSequenceError, match="duplicate"):
        generate_runtime_strategy_intents(
            "range_mean_reversion_v1", bars[:10] + (bars[9],) + bars[10:], frozen_range_config()
        )
    mixed = list(bars)
    mixed[-1] = ClosedBarEventV1(
        **{
            **bars[-1].model_dump(
                exclude={
                    "bar_sha256",
                    "correlation_id",
                    "message_id",
                    "produced_at",
                    "symbol",
                }
            ),
            "symbol": "ETHUSDT",
        }
    )
    with pytest.raises(ClosedBarSequenceError, match="mix symbols"):
        generate_runtime_strategy_intents("range_mean_reversion_v1", mixed, frozen_range_config())


def test_paper_rejects_every_current_sleeve_before_candidate_conversion():
    bars = tuple(candle_to_closed_bar(candle) for candle in frozen_closed_bar_stream())
    with pytest.raises(PaperStrategyDisabledError, match="not PAPER-approved"):
        generate_runtime_strategy_intents(
            "range_mean_reversion_v1", bars, frozen_range_config(), for_paper=True
        )


def test_v1_runtime_refuses_a_research_trailing_plan():
    candles = list(frozen_closed_bar_stream())
    price = 100.0
    trend: list[Candle] = []
    for index in range(900):
        opened = price
        closed = opened * 1.0002
        trend.append(
            replace(
                candles[index % len(candles)],
                open_time_ms=index * 60_000,
                close_time_ms=(index + 1) * 60_000 - 1,
                open=opened,
                high=closed,
                low=opened,
                close=closed,
                quote_volume=10 * closed,
                taker_buy_quote_volume=5.5 * closed,
                taker_buy_volume=5.5,
            )
        )
        price = closed

    with pytest.raises(UnsupportedExitPlanError, match="trailing plan"):
        generate_research_strategy_intents("trend_breakout_v1", trend)
