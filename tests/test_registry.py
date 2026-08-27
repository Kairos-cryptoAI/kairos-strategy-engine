from __future__ import annotations

import pytest

from kairos_strategy.candles import Candle
from kairos_strategy.registry import (
    STRATEGIES,
    PaperStrategyDisabledError,
    StrategyStatus,
    generate_sleeve_intents,
)
from kairos_strategy.sleeves import (
    RangeMeanReversionConfig,
    generate_range_mean_reversion_intents,
)


def _range_source(last_two_closes: tuple[float, float]) -> list[Candle]:
    closes = [100 + (index % 2) * 0.2 for index in range(40)]
    closes[-2:] = last_two_closes
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
                    volume=10,
                    quote_volume=10 * close,
                    taker_buy_volume=5,
                )
            )
    return rows


def _config() -> RangeMeanReversionConfig:
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


def test_registry_uses_the_exact_owned_generator_and_stable_order():
    source = _range_source((96.0, 98.0))
    direct = tuple(generate_range_mean_reversion_intents(source, _config()))
    registered = generate_sleeve_intents("range_mean_reversion_v1", source, _config())

    assert registered == direct
    assert [intent.intent_id for intent in registered] == [intent.intent_id for intent in direct]


def test_every_existing_sleeve_is_rejected_for_paper():
    assert STRATEGIES
    assert {definition.status for definition in STRATEGIES.values()} == {
        StrategyStatus.REJECTED,
        StrategyStatus.RESEARCH,
    }
    assert STRATEGIES["quarter_hour_flow_v1"].status is StrategyStatus.RESEARCH
    assert STRATEGIES["right_tail_trend_v1"].status is StrategyStatus.REJECTED
    assert not any(definition.paper_enabled for definition in STRATEGIES.values())

    with pytest.raises(PaperStrategyDisabledError, match="not PAPER-approved"):
        generate_sleeve_intents(
            "range_mean_reversion_v1",
            _range_source((96.0, 98.0)),
            _config(),
            for_paper=True,
        )
