"""Engineering adapter tests, not a selected or qualified trading campaign.

The OHLC history and configuration below reuse the pre-existing pure-runtime
parity fixture. Positive and quiet outcomes execute the real registered code.
Only explicit corruption/ambiguity tests replace the generator or resolver.
There are no services, durable runtime connections, provider or exchange calls.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from kairos_core.contracts import ClosedBarEventV1, MarketSnapshot, ResearchObservationWindowV1
from kairos_core.contracts.base import canonical_sha256, datetime_from_unix_ms
from kairos_core.contracts.market import DerivativesMetrics, OrderBookSummary, TechnicalIndicators
from kairos_persistence.causal_campaign import CampaignMarketContextV1
from kairos_persistence.research_evidence import ResearchSourceReceiptV1
from test_runtime_parity import frozen_closed_bar_stream, frozen_range_config

from kairos_strategy.campaign import (
    CausalStrategyEvaluationError,
    CausalStrategyEvaluator,
    CausalStrategyUnsupportedError,
)
from kairos_strategy.provenance import config_sha256
from kairos_strategy.runtime import (
    ClosedBarSequenceError,
    UnsupportedExitPlanError,
    candle_to_closed_bar,
    canonical_intent_batch_bytes,
    closed_bar_to_candle,
    generate_runtime_strategy_intents,
)
from kairos_strategy.runtime_requirements import RuntimeRequirements
from kairos_strategy.sleeves import (
    OrderFlowVolatilityExpansionConfig,
    QuarterHourFlowConfig,
    RangeMeanReversionConfig,
    RegimeAlignedRightTailConfig,
    RegimeVetoRetestReclaimConfig,
    RightTailTrendConfig,
    TrendBreakoutConfig,
    TrendPullbackReclaimConfig,
)

CAMPAIGN = "engineering-only-fixture"
SCHEDULE = "1" * 64
PROTOCOL = "2" * 64
REFERENCE = "sealed-engineering-fixture-window"


class FixtureOnlyMemoryResolver:
    """Test-only resolver, deliberately not advertised as durable history."""

    def __init__(self, bars):
        self.bars = bars
        self.calls = []

    async def load(self, reference):
        self.calls.append(reference)
        if reference != REFERENCE:
            raise KeyError("fixture reference is unknown")
        return self.bars


def bars_fixture(*, quiet=False):
    candles = frozen_closed_bar_stream()
    if quiet:
        candles = tuple(
            replace(
                candle,
                open=100.0,
                high=100.2,
                low=99.8,
                close=100.0,
                quote_volume=1000.0,
                taker_buy_quote_volume=500.0,
            )
            for candle in candles
        )
    return tuple(candle_to_closed_bar(candle) for candle in candles)


def make_inputs(bars=None, *, context_changes=None, source_changes=None, window_changes=None):
    bars = bars_fixture() if bars is None else bars
    anchor = bars[-1]
    snapshot = MarketSnapshot(
        source="fixture-quant-scout",
        message_id="fixed-fixture-snapshot",
        produced_at=datetime_from_unix_ms(anchor.close_time_ms + 10),
        symbol=anchor.symbol,
        timeframe="1m",
        mid_price=anchor.close,
        volume_usd=1000,
        order_book=OrderBookSummary(best_bid=97.9, best_ask=98.1, spread_bps=20, imbalance=0, depth_usd=1000),
        derivatives=DerivativesMetrics(funding_rate=0, open_interest=1000),
        indicators=TechnicalIndicators(rsi_14=50, macd=0, macd_signal=0, macd_hist=0),
    )
    context_payload = {
        "anchor_bar": anchor,
        "market_snapshot": snapshot,
        "bar_window_reference": REFERENCE,
        "bar_window_sha256": canonical_sha256({"bars": [bar.identity_payload() for bar in bars]}),
        "bar_count": len(bars),
        "first_open_time_ms": bars[0].open_time_ms,
        **(context_changes or {}),
    }
    context = CampaignMarketContextV1(**context_payload)
    source_payload = {
        "campaign_id": CAMPAIGN,
        "sample_id": "sample-1",
        "schedule_digest": SCHEDULE,
        "candidate_protocol_digest": PROTOCOL,
        "source_kind": "MARKET_SNAPSHOT",
        "source_name": "quant-context",
        "reference": "context-1",
        "source_as_of_ts_ms": anchor.close_time_ms + 10,
        "observed_at_ts_ms": anchor.close_time_ms + 20,
        "content": context.model_dump(mode="json"),
        **(source_changes or {}),
    }
    source = ResearchSourceReceiptV1(**source_payload)
    window = ResearchObservationWindowV1(
        **{
            "sample_id": "sample-1",
            "symbol": "BTCUSDT",
            "timeframe": "1m",
            "market_as_of_ts_ms": anchor.close_time_ms + 100,
            "market_snapshot_sha256": source.content_sha256,
            "paired_at_ts_ms": anchor.close_time_ms + 200,
            "sample_deadline_ts_ms": anchor.close_time_ms + 1000,
            **(window_changes or {}),
        }
    )
    return window, (source,), context


def evaluator(resolver=None, **changes):
    return CausalStrategyEvaluator(
        **{
            "strategy_id": "range_mean_reversion_v1",
            "strategy_revision": "1",
            "config": frozen_range_config(),
            "minimum_window_bars": 200,
            "bar_window_resolver": resolver or FixtureOnlyMemoryResolver(bars_fixture()),
            "campaign_id": CAMPAIGN,
            "schedule_digest": SCHEDULE,
            "candidate_protocol_digest": PROTOCOL,
            **changes,
        }
    )


def run(subject, inputs):
    window, sources, _ = inputs
    return asyncio.run(subject.evaluate(window=window, sources=sources))


def test_actual_generator_positive_preserves_exact_bytes_anchor_clock_and_all_hashes():
    bars = bars_fixture()
    inputs = make_inputs(bars)
    resolver = FixtureOnlyMemoryResolver(bars)
    result = run(evaluator(resolver), inputs)
    expected = generate_runtime_strategy_intents("range_mean_reversion_v1", bars, frozen_range_config())
    assert len(expected) == 1
    assert result.intent is not None
    assert canonical_intent_batch_bytes((result.intent,)) == canonical_intent_batch_bytes(expected)
    assert result.intent.decision_ts_ms == bars[-1].close_time_ms < inputs[0].market_as_of_ts_ms
    assert result.context_source_receipt_sha256 == inputs[1][0].receipt_sha256
    assert result.bar_window_sha256 == result.intent.provenance.input_window_sha256
    assert result.bar_count == 200
    assert result.intent.provenance.input_bar_sha256s[-1] == bars[-1].bar_sha256
    assert bars[-1].bar_sha256 != inputs[1][0].content_sha256
    assert resolver.calls == [REFERENCE]


def test_actual_generator_quiet_history_is_honest_no_intent_not_an_error():
    bars = bars_fixture(quiet=True)
    assert generate_runtime_strategy_intents("range_mean_reversion_v1", bars, frozen_range_config()) == ()
    result = run(evaluator(FixtureOnlyMemoryResolver(bars)), make_inputs(bars))
    assert result.intent is None
    assert result.bar_count == len(bars)


def test_prior_intent_is_not_reused_when_the_real_generator_has_no_current_signal():
    bars = bars_fixture()
    tail = tuple(
        candle_to_closed_bar(
            replace(
                frozen_closed_bar_stream()[-1],
                open_time_ms=(200 + offset) * 60_000,
                close_time_ms=(201 + offset) * 60_000 - 1,
            )
        )
        for offset in range(5)
    )
    extended = bars + tail
    intents = generate_runtime_strategy_intents("range_mean_reversion_v1", extended, frozen_range_config())
    assert intents and all(intent.decision_ts_ms < extended[-1].close_time_ms for intent in intents)
    result = run(evaluator(FixtureOnlyMemoryResolver(extended)), make_inputs(extended))
    assert result.intent is None


@pytest.mark.parametrize(
    "changes,exception",
    [
        ({"strategy_id": "not-registered"}, KeyError),
        ({"strategy_revision": "2"}, CausalStrategyEvaluationError),
        ({"config": None}, TypeError),
        ({"config": False}, TypeError),
        ({"minimum_window_bars": True}, CausalStrategyEvaluationError),
        ({"minimum_window_bars": 1}, CausalStrategyEvaluationError),
        ({"minimum_window_bars": 50_001}, CausalStrategyEvaluationError),
        ({"bar_window_resolver": None}, TypeError),
        ({"campaign_id": "../primary"}, ValueError),
        ({"schedule_digest": "G" * 64}, ValueError),
    ],
)
def test_explicit_constructor_fails_closed_without_defaults(changes, exception):
    with pytest.raises(exception):
        evaluator(**changes)


def test_trial15_registered_minimum_is_not_reduced_or_implicitly_promoted():
    with pytest.raises(CausalStrategyEvaluationError, match="warmup"):
        evaluator(
            strategy_id="regime_aligned_right_tail_v1",
            config=RegimeAlignedRightTailConfig(),
            minimum_window_bars=48_059,
        )


@pytest.mark.parametrize(
    "strategy_id,config,frames,minimum",
    [
        ("range_mean_reversion_v1", RangeMeanReversionConfig(), (("5m", 26), ("1h", 13)), 780),
        ("trend_breakout_v1", TrendBreakoutConfig(), (("5m", 21), ("1h", 13)), 780),
        ("trend_pullback_reclaim_v1", TrendPullbackReclaimConfig(), (("5m", 51), ("1h", 72)), 4320),
        ("orderflow_volatility_expansion_v1", OrderFlowVolatilityExpansionConfig(), (("5m", 73),), 365),
        ("right_tail_trend_v1", RightTailTrendConfig(), (("1h", 25),), 1500),
        (
            "regime_aligned_right_tail_v1",
            RegimeAlignedRightTailConfig(),
            (("1h", 25), ("4h", 200)),
            48_060,
        ),
    ],
)
def test_configured_complete_frame_floor_covers_all_supported_sleeves(strategy_id, config, frames, minimum):
    subject = evaluator(strategy_id=strategy_id, config=config, minimum_window_bars=minimum)
    assert subject._frame_requirements == frames
    with pytest.raises(CausalStrategyEvaluationError, match="configured.*registered"):
        evaluator(strategy_id=strategy_id, config=config, minimum_window_bars=minimum - 1)


@pytest.mark.parametrize(
    "strategy_id,config",
    [
        ("quarter_hour_flow_v1", QuarterHourFlowConfig()),
        ("regime_veto_retest_reclaim_v1", RegimeVetoRetestReclaimConfig()),
    ],
)
def test_stateful_or_phase_specific_sleeve_is_typed_unsupported_not_no_intent(strategy_id, config):
    resolver = FixtureOnlyMemoryResolver(bars_fixture())
    with pytest.raises(CausalStrategyUnsupportedError, match="readiness policy"):
        evaluator(resolver, strategy_id=strategy_id, config=config, minimum_window_bars=50_000)
    assert resolver.calls == []


def test_default_range_two_bar_history_is_rejected_before_resolver_or_generator(monkeypatch):
    resolver = FixtureOnlyMemoryResolver(bars_fixture()[-2:])

    def should_not_generate(*args, **kwargs):
        pytest.fail("unready history must not be classified as NO_INTENT")

    monkeypatch.setattr("kairos_strategy.campaign.generate_runtime_strategy_intents", should_not_generate)
    with pytest.raises(CausalStrategyEvaluationError, match="warmup"):
        evaluator(resolver, config=RangeMeanReversionConfig(), minimum_window_bars=2)
    assert resolver.calls == []


def test_raw_count_enough_but_missing_complete_hour_is_not_no_intent(monkeypatch):
    bars = bars_fixture(quiet=True)[20:]
    assert len(bars) == 180

    def should_not_generate(*args, **kwargs):
        pytest.fail("partial UTC hour must not count toward configured regime readiness")

    monkeypatch.setattr("kairos_strategy.campaign.generate_runtime_strategy_intents", should_not_generate)
    subject = evaluator(FixtureOnlyMemoryResolver(bars), minimum_window_bars=180)
    with pytest.raises(CausalStrategyEvaluationError, match="1h warmup requires 3.*only 2"):
        run(subject, make_inputs(bars))


def test_actual_complete_hour_and_five_minute_floor_quiet_history_is_no_intent():
    bars = bars_fixture(quiet=True)[:180]
    subject = evaluator(FixtureOnlyMemoryResolver(bars), minimum_window_bars=180)
    result = run(subject, make_inputs(bars))
    assert result.intent is None
    assert result.bar_count == 180


def test_raw_count_enough_but_missing_complete_five_minute_frame_is_not_no_intent(monkeypatch):
    bars = tuple(
        candle_to_closed_bar(
            replace(
                closed_bar_to_candle(bar),
                open_time_ms=bar.open_time_ms + 60_000,
                close_time_ms=bar.close_time_ms + 60_000,
            )
        )
        for bar in bars_fixture(quiet=True)
    )
    config = replace(frozen_range_config(), vwap_lookback_bars=38, regime_lookback_hours=1)

    def should_not_generate(*args, **kwargs):
        pytest.fail("partial first and last 5m frames must not seed a prior VWAP pair")

    monkeypatch.setattr("kairos_strategy.campaign.generate_runtime_strategy_intents", should_not_generate)
    subject = evaluator(FixtureOnlyMemoryResolver(bars), config=config)
    with pytest.raises(CausalStrategyEvaluationError, match="5m warmup requires 40.*only 39"):
        run(subject, make_inputs(bars))


@pytest.mark.parametrize(
    "changes",
    [{"regime_lookback_hours": 3}, {"vwap_lookback_bars": 100}, {"atr_period": 41}],
)
def test_selected_config_cannot_hide_larger_indicator_dependency_behind_raw_minimum(changes):
    with pytest.raises(CausalStrategyEvaluationError, match="warmup"):
        evaluator(config=replace(frozen_range_config(), **changes), minimum_window_bars=200)


@pytest.mark.parametrize("filter_name", ["minimum_volume_surprise", "minimum_directional_taker_share"])
def test_breakout_flow_warmup_applies_only_when_existing_filter_is_enabled(filter_name):
    config = TrendBreakoutConfig(regime_lookback_hours=1, volume_lookback=100)
    disabled = evaluator(strategy_id="trend_breakout_v1", config=config, minimum_window_bars=120)
    assert disabled._frame_requirements == (("5m", 21), ("1h", 2))
    enabled_config = replace(
        config, **{filter_name: 1.0 if filter_name == "minimum_volume_surprise" else 0.5}
    )
    with pytest.raises(CausalStrategyEvaluationError, match="warmup"):
        evaluator(strategy_id="trend_breakout_v1", config=enabled_config, minimum_window_bars=120)
    enabled = evaluator(strategy_id="trend_breakout_v1", config=enabled_config, minimum_window_bars=505)
    assert enabled._frame_requirements == (("5m", 101), ("1h", 2))


def test_orderflow_zero_volume_reset_cannot_borrow_seed_from_an_older_segment(monkeypatch):
    bars = tuple(
        candle_to_closed_bar(
            replace(
                closed_bar_to_candle(bar),
                volume=0.0,
                quote_volume=0.0,
                taker_buy_volume=0.0,
                taker_buy_quote_volume=0.0,
            )
        )
        if 190 <= index < 195
        else bar
        for index, bar in enumerate(bars_fixture(quiet=True))
    )
    config = OrderFlowVolatilityExpansionConfig(
        baseline_lookback=2,
        compression_short_lookback=2,
        compression_long_lookback=3,
        atr_period=2,
        persistence_lookback=2,
        flip_lookback=2,
    )

    def should_not_generate(*args, **kwargs):
        pytest.fail("zero-volume reset must not become a warmup-derived NO_INTENT")

    monkeypatch.setattr("kairos_strategy.campaign.generate_runtime_strategy_intents", should_not_generate)
    subject = evaluator(
        FixtureOnlyMemoryResolver(bars), strategy_id="orderflow_volatility_expansion_v1", config=config
    )
    with pytest.raises(CausalStrategyEvaluationError, match="5m warmup requires 4.*only 1"):
        run(subject, make_inputs(bars))


def test_changed_complete_frame_policy_is_rejected_before_history_load():
    resolver = FixtureOnlyMemoryResolver(bars_fixture())
    subject = evaluator(resolver)
    subject._frame_requirements = (("5m", 2), ("1h", 1))
    with pytest.raises(CausalStrategyEvaluationError, match="changed"):
        run(subject, make_inputs())
    assert resolver.calls == []


def test_artifact_binds_config_operational_minimum_and_real_source_not_campaign_scope():
    base = evaluator()
    assert base.artifact_sha256 == evaluator(campaign_id="other-campaign").artifact_sha256
    assert base.artifact_sha256 != evaluator(minimum_window_bars=201).artifact_sha256
    assert (
        base.artifact_sha256
        != evaluator(config=replace(frozen_range_config(), max_hold_bars=7)).artifact_sha256
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"campaign_id": "another-campaign"},
        {"sample_id": "unknown-sample"},
        {"schedule_digest": "3" * 64},
        {"candidate_protocol_digest": "4" * 64},
        {"observed_at_ts_ms": bars_fixture()[-1].close_time_ms + 101},
    ],
)
def test_source_scope_or_late_actual_capture_is_rejected_before_resolver(changes):
    resolver = FixtureOnlyMemoryResolver(bars_fixture())
    with pytest.raises(CausalStrategyEvaluationError, match="scope or actual capture"):
        run(evaluator(resolver), make_inputs(source_changes=changes))
    assert resolver.calls == []


def test_non_market_source_scope_is_also_checked():
    window, sources, context = make_inputs()
    news = ResearchSourceReceiptV1(
        campaign_id="wrong-campaign",
        sample_id=window.sample_id,
        schedule_digest=SCHEDULE,
        candidate_protocol_digest=PROTOCOL,
        source_kind="NEWS",
        source_name="news",
        reference="news-1",
        source_as_of_ts_ms=window.market_as_of_ts_ms - 10,
        observed_at_ts_ms=window.market_as_of_ts_ms - 5,
        content={"headline": "fixture only"},
    )
    with pytest.raises(CausalStrategyEvaluationError, match="scope"):
        run(evaluator(), (window, (*sources, news), context))


def test_repeated_market_slot_and_missing_market_are_rejected():
    window, sources, context = make_inputs()
    with pytest.raises(CausalStrategyEvaluationError, match="repeats"):
        run(evaluator(), (window, sources * 2, context))
    other = ResearchSourceReceiptV1(
        **{
            **sources[0].model_dump(exclude={"receipt_sha256", "content_sha256"}),
            "source_kind": "NEWS",
        }
    )
    with pytest.raises(CausalStrategyEvaluationError, match="exactly one"):
        run(evaluator(), (window, (other,), context))


def test_changed_source_content_and_wrong_preregistered_digest_are_rejected():
    inputs = make_inputs()
    inputs[1][0].content["bar_count"] += 1
    with pytest.raises(ValueError, match="hash"):
        run(evaluator(), inputs)
    with pytest.raises(CausalStrategyEvaluationError, match="hash differs"):
        run(evaluator(), make_inputs(window_changes={"market_snapshot_sha256": "5" * 64}))


@pytest.mark.parametrize("window_changes", [{"symbol": "ETHUSDT"}, {"timeframe": "5m"}])
def test_context_geometry_cannot_change_scheduled_symbol_or_timeframe(window_changes):
    with pytest.raises(CausalStrategyEvaluationError, match="context differs"):
        run(evaluator(), make_inputs(window_changes=window_changes))


@pytest.mark.parametrize(
    "transform,exception",
    [
        (lambda bars: tuple(reversed(bars)), CausalStrategyEvaluationError),
        (lambda bars: bars[:10] + bars[11:], ClosedBarSequenceError),
        (lambda bars: bars[:10] + (bars[9],) + bars[10:], ClosedBarSequenceError),
        (lambda bars: bars[:-1], CausalStrategyEvaluationError),
        (lambda bars: bars[1:], CausalStrategyEvaluationError),
        (lambda bars: list(bars), CausalStrategyEvaluationError),
        (lambda bars: (), CausalStrategyEvaluationError),
        (lambda bars: (*bars[:-1], bars[-1].model_dump()), TypeError),
    ],
)
def test_resolver_must_return_exact_full_ordered_typed_window(transform, exception):
    with pytest.raises(exception):
        run(evaluator(FixtureOnlyMemoryResolver(transform(bars_fixture()))), make_inputs())


@pytest.mark.parametrize(
    "changes", [{"bar_window_sha256": "6" * 64}, {"bar_count": 199, "first_open_time_ms": 60_000}]
)
def test_descriptor_hash_and_count_are_independently_verified(changes):
    with pytest.raises(CausalStrategyEvaluationError, match="history differs"):
        run(evaluator(), make_inputs(context_changes=changes))


def test_adequate_declared_context_below_explicit_warmup_is_not_no_intent():
    with pytest.raises(CausalStrategyEvaluationError, match="below.*warmup"):
        run(evaluator(minimum_window_bars=201), make_inputs())


def test_wrong_registered_evaluation_cadence_is_not_no_intent(monkeypatch):
    monkeypatch.setattr(
        "kairos_strategy.campaign.get_runtime_requirements",
        lambda _: RuntimeRequirements(minimum_window_bars=2, decision_interval_bars=2, decision_phase_bars=1),
    )
    with pytest.raises(CausalStrategyEvaluationError, match="evaluation clock"):
        run(evaluator(), make_inputs())


def test_caller_config_is_copied_but_mutated_frozen_config_is_rejected():
    config = frozen_range_config()
    subject = evaluator(config=config)
    original_hash = config_sha256(config)
    object.__setattr__(config, "max_hold_bars", 7)
    result = run(subject, make_inputs())
    assert result.intent.provenance.config_sha256 == original_hash
    object.__setattr__(subject._config, "max_hold_bars", 8)
    with pytest.raises(CausalStrategyEvaluationError, match="changed"):
        run(subject, make_inputs())


def test_changed_installed_source_and_policy_are_rejected_before_loading(monkeypatch):
    subject = evaluator()
    monkeypatch.setattr("kairos_strategy.campaign.installed_source_tree_sha256", lambda _: "7" * 64)
    with pytest.raises(CausalStrategyEvaluationError, match="changed"):
        run(subject, make_inputs())


def test_resolver_await_cannot_admit_changed_config():
    subject = evaluator()

    class MutatingResolver:
        async def load(self, reference):
            object.__setattr__(subject._config, "max_hold_bars", 9)
            return bars_fixture()

    subject._resolver = MutatingResolver()
    with pytest.raises(CausalStrategyEvaluationError, match="changed"):
        run(subject, make_inputs())


def test_multiple_actual_anchor_candidates_are_rejected_not_ranked(monkeypatch):
    expected = generate_runtime_strategy_intents(
        "range_mean_reversion_v1", bars_fixture(), frozen_range_config()
    )
    monkeypatch.setattr(
        "kairos_strategy.campaign.generate_runtime_strategy_intents", lambda *a, **k: expected * 2
    )
    with pytest.raises(CausalStrategyEvaluationError, match="multiple anchor intents"):
        run(evaluator(), make_inputs())


def test_generator_failure_is_not_converted_into_no_intent(monkeypatch):
    def unsupported(*args, **kwargs):
        raise UnsupportedExitPlanError("fixture unsupported exit")

    monkeypatch.setattr("kairos_strategy.campaign.generate_runtime_strategy_intents", unsupported)
    with pytest.raises(UnsupportedExitPlanError):
        run(evaluator(), make_inputs())


def test_tampered_typed_bar_cannot_bypass_canonical_revalidation():
    bars = list(bars_fixture())
    bars[-1] = bars[-1].model_copy(update={"bar_sha256": "8" * 64})
    with pytest.raises(ValueError, match="bar_sha256"):
        run(evaluator(FixtureOnlyMemoryResolver(tuple(bars))), make_inputs())


def test_expired_real_intent_is_rejected_without_adjusting_its_expiry():
    anchor = bars_fixture()[-1]
    with pytest.raises(CausalStrategyEvaluationError, match="expiry"):
        run(
            evaluator(),
            make_inputs(
                window_changes={
                    "paired_at_ts_ms": anchor.close_time_ms + 300_000,
                    "sample_deadline_ts_ms": anchor.close_time_ms + 300_001,
                }
            ),
        )


def test_future_anchor_or_unsupported_snapshot_clock_never_becomes_no_intent():
    bars = bars_fixture()
    with pytest.raises(CausalStrategyEvaluationError, match="context differs"):
        run(
            evaluator(),
            make_inputs(
                source_changes={
                    "source_as_of_ts_ms": bars[-1].close_time_ms,
                    "observed_at_ts_ms": bars[-1].close_time_ms,
                },
                window_changes={"market_as_of_ts_ms": bars[-1].close_time_ms},
            ),
        )
    window, sources, context = make_inputs()
    submillisecond = context.market_snapshot.model_copy(
        update={"produced_at": context.market_snapshot.produced_at.replace(microsecond=10_001)}
    )
    # The causal context can now reject the unsupported clock even earlier
    # than the evaluator; neither boundary may floor it into admissible time.
    with pytest.raises(ValueError, match="sub-millisecond|exact milliseconds"):
        run(evaluator(), make_inputs(context_changes={"market_snapshot": submillisecond}))
    assert sources[0].content == context.model_dump(mode="json")


def test_changed_registered_revision_after_construction_is_rejected(monkeypatch):
    subject = evaluator()
    monkeypatch.setattr(
        "kairos_strategy.campaign.get_strategy", lambda _: replace(subject._definition, revision="2")
    )
    with pytest.raises(CausalStrategyEvaluationError, match="changed"):
        run(subject, make_inputs())


def test_wrong_instrument_in_resolved_history_is_not_a_quiet_strategy():
    bars = list(bars_fixture())
    payload = bars[10].model_dump(
        exclude={"bar_sha256", "message_id", "correlation_id", "produced_at", "symbol"}
    )
    bars[10] = ClosedBarEventV1(**payload, symbol="ETHUSDT")
    with pytest.raises(ClosedBarSequenceError, match="mix symbols"):
        run(evaluator(FixtureOnlyMemoryResolver(tuple(bars))), make_inputs())


def test_canonical_but_different_bar_cannot_replace_saved_history():
    bars = list(bars_fixture())
    payload = bars[10].model_dump(exclude={"bar_sha256", "message_id", "correlation_id", "produced_at"})
    payload["base_volume"] += 1
    bars[10] = ClosedBarEventV1(**payload)
    with pytest.raises(CausalStrategyEvaluationError, match="history differs"):
        run(evaluator(FixtureOnlyMemoryResolver(tuple(bars))), make_inputs())


def test_validly_rehashed_but_wrong_generated_provenance_is_rejected(monkeypatch):
    expected = generate_runtime_strategy_intents(
        "range_mean_reversion_v1", bars_fixture(), frozen_range_config()
    )
    payload = expected[0].model_dump(exclude={"intent_id", "message_id", "correlation_id", "produced_at"})
    payload["provenance"]["config_sha256"] = "9" * 64
    altered = type(expected[0]).model_validate(payload)
    monkeypatch.setattr(
        "kairos_strategy.campaign.generate_runtime_strategy_intents", lambda *a, **k: (altered,)
    )
    with pytest.raises(CausalStrategyEvaluationError, match="lineage"):
        run(evaluator(), make_inputs())


def test_actual_evaluation_does_not_mutate_shared_source_content():
    inputs = make_inputs()
    original = canonical_sha256(inputs[1][0].model_dump(mode="json"))
    result = run(evaluator(), inputs)
    assert result.intent is not None
    assert canonical_sha256(inputs[1][0].model_dump(mode="json")) == original
