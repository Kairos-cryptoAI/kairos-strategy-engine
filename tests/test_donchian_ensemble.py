from __future__ import annotations

from dataclasses import replace

import pytest

from kairos_strategy.allocation import AllocationReason, TargetAllocation
from kairos_strategy.candles import Candle
from kairos_strategy.registry import (
    ALLOCATION_STRATEGIES,
    PaperStrategyDisabledError,
    StrategyStatus,
    generate_target_allocations,
)
from kairos_strategy.sleeves import (
    DonchianEnsembleConfig,
    generate_donchian_ensemble_allocations,
)

DAY_MS = 86_400_000


def _config() -> DonchianEnsembleConfig:
    return DonchianEnsembleConfig(
        horizons_days=(2, 3, 5),
        volatility_lookback_days=3,
        annualization_days=365,
        target_annualized_volatility=0.25,
        maximum_weight=2.0,
        volatility_rebalance_threshold=0.20,
    )


def _daily_path(daily_returns: list[float]) -> list[Candle]:
    rows: list[Candle] = []
    price = 100.0
    for day, daily_return in enumerate(daily_returns):
        minute_return = (1 + daily_return) ** (1 / 1_440) - 1
        for minute in range(1_440):
            index = day * 1_440 + minute
            opened = price
            closed = opened * (1 + minute_return)
            rows.append(
                Candle(
                    symbol="BTCUSDT",
                    timeframe="1m",
                    open_time_ms=index * 60_000,
                    close_time_ms=(index + 1) * 60_000 - 1,
                    open=opened,
                    high=max(opened, closed),
                    low=min(opened, closed),
                    close=closed,
                    volume=100.0,
                    quote_volume=100.0 * closed,
                    taker_buy_volume=55.0 if daily_return > 0 else 45.0,
                    taker_buy_quote_volume=(55.0 if daily_return > 0 else 45.0) * closed,
                )
            )
            price = closed
    return rows


def test_published_defaults_are_frozen_exactly():
    config = DonchianEnsembleConfig()

    assert config.horizons_days == (5, 10, 20, 30, 60, 90, 150, 250, 360)
    assert config.volatility_lookback_days == 90
    assert config.annualization_days == 365
    assert config.target_annualized_volatility == 0.25
    assert config.maximum_weight == 2.0
    assert config.volatility_rebalance_threshold == 0.20


def test_rising_daily_closes_activate_all_models_with_monotonic_stops():
    source = _daily_path([0.01, 0.015, 0.012, 0.018, 0.011, 0.014, 0.013, 0.019])

    allocations = generate_donchian_ensemble_allocations(source, _config())

    assert allocations
    assert all(item.active_horizons == (2, 3, 5) for item in allocations)
    assert all(0 < item.target_weight <= 2 for item in allocations)
    assert allocations[0].decision_ts_ms == 5 * DAY_MS - 1
    assert all(item.effective_ts_ms == item.decision_ts_ms + 1 for item in allocations)
    for horizon in (2, 3, 5):
        stops = [dict(item.trailing_stops)[horizon] for item in allocations]
        assert stops == sorted(stops)


def test_close_below_trailing_stops_exits_without_shorting():
    source = _daily_path([0.01, 0.015, 0.012, 0.018, 0.011, -0.20, -0.10])

    allocations = generate_donchian_ensemble_allocations(source, _config())

    assert allocations[-1].target_weight == 0.0
    assert allocations[-1].active_horizons == ()
    assert allocations[-1].trailing_stops == ()
    assert any(item.target_weight == 0 and item.reason is AllocationReason.SIGNAL for item in allocations)


def test_future_mutation_cannot_change_prior_allocation_bytes():
    source = _daily_path([0.01, 0.015, 0.012, 0.018, 0.011, 0.014, 0.013, 0.019])
    cutoff = 6 * DAY_MS - 1
    original = [
        item
        for item in generate_donchian_ensemble_allocations(source, _config())
        if item.decision_ts_ms <= cutoff
    ]
    changed = [
        replace(row, open=row.open * 2, high=row.high * 2, low=row.low * 2, close=row.close * 2)
        if row.open_time_ms >= 6 * DAY_MS
        else row
        for row in source
    ]
    mutated = [
        item
        for item in generate_donchian_ensemble_allocations(changed, _config())
        if item.decision_ts_ms <= cutoff
    ]

    assert original
    assert mutated == original


def test_allocation_registry_uses_exact_generator_and_fails_closed_for_paper():
    source = _daily_path([0.01, 0.015, 0.012, 0.018, 0.011, 0.014])
    direct = tuple(generate_donchian_ensemble_allocations(source, _config()))

    registered = generate_target_allocations("donchian_ensemble_long_v1", source, _config())

    assert registered == direct
    assert ALLOCATION_STRATEGIES["donchian_ensemble_long_v1"].status is StrategyStatus.RESEARCH
    with pytest.raises(PaperStrategyDisabledError, match="not PAPER-approved"):
        generate_target_allocations("donchian_ensemble_long_v1", source, _config(), for_paper=True)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"horizons_days": (5, 5)},
        {"horizons_days": (1, 5)},
        {"volatility_lookback_days": True},
        {"annualization_days": 1},
        {"target_annualized_volatility": 0},
        {"maximum_weight": 3},
        {"volatility_rebalance_threshold": 1},
    ],
)
def test_invalid_config_fails_closed(kwargs):
    with pytest.raises(ValueError):
        DonchianEnsembleConfig(**kwargs)


def test_target_allocation_rejects_mismatched_stop_lineage():
    with pytest.raises(ValueError, match="exactly cover"):
        TargetAllocation(
            strategy_id="x",
            symbol="BTCUSDT",
            decision_ts_ms=1,
            effective_ts_ms=2,
            target_weight=1.0,
            annualized_volatility=0.5,
            active_horizons=(5,),
            trailing_stops=(),
            reason=AllocationReason.SIGNAL,
        )
