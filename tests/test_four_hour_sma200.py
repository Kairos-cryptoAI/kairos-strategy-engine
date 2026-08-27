from __future__ import annotations

from dataclasses import replace

import pytest

from kairos_strategy.candles import Candle
from kairos_strategy.registry import (
    ALLOCATION_STRATEGIES,
    PaperStrategyDisabledError,
    StrategyStatus,
    generate_target_allocations,
)
from kairos_strategy.sleeves import (
    FourHourSma200Config,
    generate_four_hour_sma200_allocations,
)

MINUTE_MS = 60_000
FOUR_HOURS_MS = 14_400_000


def _four_hour_path(closes: list[float]) -> list[Candle]:
    rows: list[Candle] = []
    prior = closes[0]
    for period, close in enumerate(closes):
        for minute in range(240):
            index = period * 240 + minute
            opened = prior
            fraction = (minute + 1) / 240
            closed = opened + (close - opened) * fraction
            rows.append(
                Candle(
                    symbol="BTCUSDT",
                    timeframe="1m",
                    open_time_ms=index * MINUTE_MS,
                    close_time_ms=(index + 1) * MINUTE_MS - 1,
                    open=opened,
                    high=max(opened, closed),
                    low=min(opened, closed),
                    close=closed,
                    volume=0,
                )
            )
            prior = closed
    return rows


def test_published_defaults_are_frozen_exactly():
    config = FourHourSma200Config()

    assert config.sma_bars == 200
    assert config.target_weight == 1.0


def test_rule_enters_only_after_closed_bar_and_exits_on_equality():
    source = _four_hour_path([100, 101, 102, 103, 104, 100])

    allocations = generate_four_hour_sma200_allocations(source, FourHourSma200Config(sma_bars=3))

    assert [item.target_weight for item in allocations] == [1.0, 1.0, 1.0, 0.0]
    assert allocations[0].decision_ts_ms == 3 * FOUR_HOURS_MS - 1
    assert allocations[0].effective_ts_ms == 3 * FOUR_HOURS_MS
    assert allocations[-1].active_horizons == ()
    assert allocations[-1].trailing_stops == ()
    assert allocations[-1].annualized_volatility is None


def test_gap_resets_the_full_sma_warmup():
    first = _four_hour_path([100, 101, 102])
    second = [
        replace(
            row,
            open_time_ms=row.open_time_ms + 4 * FOUR_HOURS_MS,
            close_time_ms=row.close_time_ms + 4 * FOUR_HOURS_MS,
        )
        for row in _four_hour_path([103, 104])
    ]

    allocations = generate_four_hour_sma200_allocations(first + second, FourHourSma200Config(sma_bars=3))

    assert len(allocations) == 1
    assert allocations[0].decision_ts_ms == 3 * FOUR_HOURS_MS - 1


def test_future_mutation_cannot_change_prior_allocation_bytes():
    source = _four_hour_path([100, 101, 102, 103, 104])
    cutoff = 4 * FOUR_HOURS_MS - 1
    config = FourHourSma200Config(sma_bars=3)
    original = [
        item
        for item in generate_four_hour_sma200_allocations(source, config)
        if item.decision_ts_ms <= cutoff
    ]
    changed = [
        replace(row, open=row.open * 2, high=row.high * 2, low=row.low * 2, close=row.close * 2)
        if row.open_time_ms >= 4 * FOUR_HOURS_MS
        else row
        for row in source
    ]
    mutated = [
        item
        for item in generate_four_hour_sma200_allocations(changed, config)
        if item.decision_ts_ms <= cutoff
    ]

    assert original
    assert mutated == original


def test_registry_uses_exact_generator_and_fails_closed_for_paper():
    source = _four_hour_path([100, 101, 102, 103])
    config = FourHourSma200Config(sma_bars=3)

    direct = tuple(generate_four_hour_sma200_allocations(source, config))
    registered = generate_target_allocations("four_hour_sma200_long_v1", source, config)

    assert registered == direct
    assert ALLOCATION_STRATEGIES["four_hour_sma200_long_v1"].status is StrategyStatus.RESEARCH
    with pytest.raises(PaperStrategyDisabledError, match="not PAPER-approved"):
        generate_target_allocations("four_hour_sma200_long_v1", source, config, for_paper=True)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sma_bars": True},
        {"sma_bars": 1},
        {"target_weight": 0},
        {"target_weight": 1.1},
        {"target_weight": float("nan")},
    ],
)
def test_invalid_config_fails_closed(kwargs):
    with pytest.raises(ValueError):
        FourHourSma200Config(**kwargs)
