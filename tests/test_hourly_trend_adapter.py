"""The strict adapter of the hourly trend rules: closed bars in, ``StrategyIntentV1`` out."""

from __future__ import annotations

from dataclasses import replace
from functools import cache

import pytest
from kairos_core.contracts import CandidateRouteV1, ClosedBarEventV1, StrategyIntentV1
from kairos_core.enums import CandidateReviewTier, ReasoningEffort

from kairos_strategy.candles import Candle
from kairos_strategy.hourly_trend import (
    STRATEGY_ID,
    STRATEGY_REVISION,
    HourlyTrendCandidatesConfig,
    evaluate_hourly_trend_closed_bars,
    evaluate_hourly_trend_state,
    generate_hourly_trend_candidates_intents,
    hourly_trend_source_identity,
)
from kairos_strategy.hourly_trend.provenance import HOURLY_TREND_SOURCE_FILES
from kairos_strategy.models import SleeveIntent
from kairos_strategy.runtime import ClosedBarSequenceError, candle_to_closed_bar

_MINUTE_MS = 60_000
_QUARTER_MS = 15 * _MINUTE_MS
_HOUR_MS = 60 * _MINUTE_MS
_DAY_MINUTES = 1_440
_START_MS = 1_790_000_000_000 // 86_400_000 * 86_400_000
_COUNT = 16 * _DAY_MINUTES
_SYMBOL = "TESTUSDT"
_DRIFTS = (0.00003, 0.0, -0.00003, 0.0)


@cache
def _candles() -> tuple[Candle, ...]:
    """A gap-free deterministic series: an integer generator and plain arithmetic only."""

    state = 20261113

    def uniform() -> float:
        nonlocal state
        state = (state * 6364136223846793005 + 1442695040888963407) % 2**64
        return (state >> 11) / 2**53

    price = 100.0
    rows: list[Candle] = []
    for index in range(_COUNT):
        drift = _DRIFTS[(index // (3 * _DAY_MINUTES)) % len(_DRIFTS)]
        opened = price
        closed = opened * (1.0 + drift + 0.0006 * (uniform() + uniform() + uniform() + uniform() - 2.0))
        high = max(opened, closed) * (1.0 + 0.0003 * uniform())
        low = min(opened, closed) * (1.0 - 0.0003 * uniform())
        base = 10.0 + 5.0 * uniform()
        price = closed
        open_time_ms = _START_MS + index * _MINUTE_MS
        rows.append(
            Candle(
                symbol=_SYMBOL,
                timeframe="1m",
                open_time_ms=open_time_ms,
                close_time_ms=open_time_ms + _MINUTE_MS - 1,
                open=opened,
                high=high,
                low=low,
                close=closed,
                volume=base,
                quote_volume=base * closed,
                taker_buy_volume=base / 2,
                taker_buy_quote_volume=base * closed / 2,
            )
        )
    return tuple(rows)


@cache
def _bars() -> tuple[ClosedBarEventV1, ...]:
    return tuple(candle_to_closed_bar(row) for row in _candles())


@cache
def _rule_intents() -> tuple[SleeveIntent, ...]:
    return tuple(generate_hourly_trend_candidates_intents(list(_candles())))


def _bars_until(decision_ts_ms: int) -> tuple[ClosedBarEventV1, ...]:
    return tuple(bar for bar in _bars() if bar.close_time_ms <= decision_ts_ms)


def _assert_strict_form(intent: StrategyIntentV1, raw: SleeveIntent) -> None:
    assert (intent.strategy_id, intent.strategy_revision) == (STRATEGY_ID, STRATEGY_REVISION)
    assert (intent.symbol, intent.venue, intent.timeframe) == (_SYMBOL, "BINANCE_UM", "1m")
    assert intent.side is raw.side
    assert intent.decision_ts_ms == raw.decision_ts_ms
    assert intent.entry_eligible_ts_ms == raw.entry_eligible_ts_ms == raw.decision_ts_ms + 1
    assert intent.entry_expires_ts_ms == raw.entry_expires_ts_ms
    assert intent.reference_price == raw.reference_price
    assert intent.signal_strength == raw.signal_strength
    assert intent.gross_reward_bps == raw.gross_reward_bps
    assert intent.exit_plan.stop_price == raw.exit_plan.stop_price
    assert intent.exit_plan.target_price == raw.exit_plan.target_price
    assert intent.exit_plan.max_holding_ms == raw.exit_plan.max_holding_ms
    assert intent.metadata == raw.metadata


def test_strict_intent_is_the_rule_candidate_with_exact_provenance():
    raw_intents = _rule_intents()
    assert len(raw_intents) >= 6, "the synthetic series must exercise the rules"
    identity = hourly_trend_source_identity()
    for raw in raw_intents[-6:]:
        bars = _bars_until(raw.decision_ts_ms)
        evaluation = evaluate_hourly_trend_closed_bars(bars)
        intent = evaluation.intent
        assert intent is not None and evaluation.state is not None
        _assert_strict_form(intent, raw)
        assert evaluation.identity == identity
        assert evaluation.decision_ts_ms == raw.decision_ts_ms == evaluation.state.decision_ts_ms
        assert evaluation.input_bar_count == len(bars)
        assert evaluation.input_first_open_time_ms == _START_MS
        assert intent.provenance.strategy_code_sha256 == identity.strategy_code_sha256
        assert intent.provenance.config_sha256 == identity.config_sha256
        assert intent.provenance.input_bar_sha256s == tuple(bar.bar_sha256 for bar in bars)
        # The strict contract recomputes the identity from the full payload.
        assert StrategyIntentV1.model_validate(intent.model_dump(mode="json")) == intent


def test_strict_intent_can_be_routed_by_the_candidate_contract():
    raw = _rule_intents()[-1]
    intent = evaluate_hourly_trend_closed_bars(_bars_until(raw.decision_ts_ms)).intent
    assert intent is not None
    route = CandidateRouteV1(
        source="test-router",
        intent=intent,
        review_tier=CandidateReviewTier.NORMAL,
        requested_reasoning_effort=ReasoningEffort.MEDIUM,
        routed_at_ms=intent.decision_ts_ms,
        review_deadline_ms=intent.decision_ts_ms + 20_000,
    )
    assert route.intent_sha256 == intent.intent_id


def test_a_decision_without_a_candidate_still_reports_the_state():
    emitted = {raw.decision_ts_ms for raw in _rule_intents()}
    last_close = _candles()[-1].close_time_ms
    quiet = next(
        close
        for close in range(last_close, last_close - 200 * _QUARTER_MS, -_QUARTER_MS)
        if close not in emitted
    )
    evaluation = evaluate_hourly_trend_closed_bars(_bars_until(quiet))
    state, raw = evaluate_hourly_trend_state([row for row in _candles() if row.close_time_ms <= quiet])
    assert raw is None and evaluation.intent is None
    assert evaluation.state == state and state is not None
    assert evaluation.decision_ts_ms == quiet


def test_only_the_history_window_is_read_and_declared():
    config = HourlyTrendCandidatesConfig(history_hours=336)
    candidates = generate_hourly_trend_candidates_intents(list(_candles()), config)
    raw = candidates[-1]
    assert raw.decision_ts_ms + 1 - _START_MS > 336 * _HOUR_MS, "the series must exceed the window"
    bars = _bars_until(raw.decision_ts_ms)
    evaluation = evaluate_hourly_trend_closed_bars(bars, config)
    first_open_ms = raw.decision_ts_ms + 1 - 336 * _HOUR_MS
    assert evaluation.input_bar_count == 336 * 60
    assert evaluation.input_first_open_time_ms == first_open_ms
    assert evaluation.intent is not None
    _assert_strict_form(evaluation.intent, raw)
    assert evaluation.intent.provenance.input_bar_sha256s == tuple(
        bar.bar_sha256 for bar in bars if bar.open_time_ms >= first_open_ms
    )
    exact = evaluate_hourly_trend_closed_bars(
        tuple(bar for bar in bars if bar.open_time_ms >= first_open_ms), config
    )
    assert exact.intent == evaluation.intent
    assert evaluation.identity.config_sha256 != hourly_trend_source_identity().config_sha256


def test_input_order_does_not_change_the_result():
    raw = _rule_intents()[-1]
    bars = _bars_until(raw.decision_ts_ms)
    assert evaluate_hourly_trend_closed_bars(tuple(reversed(bars))) == evaluate_hourly_trend_closed_bars(bars)


def test_short_history_gives_no_state_and_no_intent():
    evaluation = evaluate_hourly_trend_closed_bars(_bars()[: 3 * _DAY_MINUTES])
    assert evaluation.state is None and evaluation.intent is None
    assert evaluation.input_bar_count == 3 * _DAY_MINUTES


def test_ambiguous_input_raises_instead_of_deciding():
    bars = _bars()[: 12 * _DAY_MINUTES]
    with pytest.raises(ClosedBarSequenceError):
        evaluate_hourly_trend_closed_bars(())
    with pytest.raises(ClosedBarSequenceError, match="gap"):
        evaluate_hourly_trend_closed_bars(bars[:5_000] + bars[5_001:])
    with pytest.raises(ClosedBarSequenceError, match="duplicate"):
        evaluate_hourly_trend_closed_bars(bars + bars[-1:])
    mixed = list(bars)
    mixed[100] = candle_to_closed_bar(replace(_candles()[100], symbol="ETHUSDT"))
    with pytest.raises(ClosedBarSequenceError, match="mix"):
        evaluate_hourly_trend_closed_bars(mixed)
    with pytest.raises(ValueError, match="quarter hour"):
        evaluate_hourly_trend_closed_bars(bars[:-1])
    with pytest.raises(TypeError):
        evaluate_hourly_trend_closed_bars(list(_candles()[:30]))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="HourlyTrendCandidatesConfig"):
        evaluate_hourly_trend_closed_bars(bars, config=object())  # type: ignore[arg-type]


def test_source_closure_covers_the_adapter_and_the_shared_conversion():
    assert {"hourly_trend/adapter.py", "runtime.py", "registry.py"} <= set(HOURLY_TREND_SOURCE_FILES)
    assert tuple(sorted(HOURLY_TREND_SOURCE_FILES)) == HOURLY_TREND_SOURCE_FILES


@pytest.mark.parametrize("tick", ["0.01", "0.5", "7"])
def test_venue_tick_puts_both_barriers_on_the_grid_toward_the_reference(tick):
    from decimal import Decimal

    from kairos_core.enums import Side

    increment = Decimal(tick)
    raw = _rule_intents()[-1]
    evaluation = evaluate_hourly_trend_closed_bars(_bars_until(raw.decision_ts_ms), price_increment=increment)
    intent = evaluation.intent
    if intent is None:  # a coarse grid may leave no room between the barriers
        return
    stop, target = Decimal(repr(intent.exit_plan.stop_price)), Decimal(repr(intent.exit_plan.target_price))
    reference = Decimal(repr(intent.reference_price))
    assert stop % increment == 0 and target % increment == 0
    raw_stop, raw_target = Decimal(repr(raw.exit_plan.stop_price)), Decimal(repr(raw.exit_plan.target_price))
    if intent.side is Side.LONG:
        assert raw_stop <= stop < reference < target <= raw_target
    else:
        assert raw_target <= target < reference < stop <= raw_stop
    assert abs(stop - raw_stop) < increment and abs(target - raw_target) < increment
    assert dict(intent.metadata)["venue_price_increment"] == tick
    assert intent.gross_reward_bps == float(abs(target - reference) / reference * 10_000)
    assert StrategyIntentV1.model_validate(intent.model_dump(mode="json")) == intent


def test_venue_tick_drops_a_candidate_whose_barrier_reaches_the_reference():
    from decimal import Decimal

    from kairos_strategy.hourly_trend import on_venue_tick

    raw = _rule_intents()[-1]
    huge = Decimal(int(raw.reference_price * 10))
    assert on_venue_tick(raw, huge) is None
    with pytest.raises(ValueError, match="positive"):
        on_venue_tick(raw, Decimal(0))
