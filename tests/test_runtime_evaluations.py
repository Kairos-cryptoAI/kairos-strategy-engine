"""Offline observation/publication tests; no DB, Redis or external endpoints."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from kairos_core.bus import BusEnvelope
from kairos_core.contracts.strategy_evaluation import StrategyEvaluationV1
from kairos_core.enums import Side
from kairos_core.topics import Topics
from kairos_persistence import DurableMessageBus
from test_quarter_hour_flow import _fixture as quarter_fixture
from test_regime_retest_reclaim import _source as regime_retest_fixture
from test_service import RecordingBus, _candles, _range_config, _settings

from kairos_strategy.config import StrategyEngineSettings
from kairos_strategy.provenance import canonical_json_bytes
from kairos_strategy.runtime import candle_to_closed_bar, generate_runtime_strategy_intents
from kairos_strategy.service import StrategyEngineService
from kairos_strategy.sleeves import RegimeRetestReclaimVariant


class FaultBus(RecordingBus):
    def __init__(self, *, fail_topic=None, fail_ack=False):
        super().__init__()
        self.fail_topic = fail_topic
        self.fail_ack = fail_ack
        self.events = []
        self.attempts = []

    async def publish(self, topic, message):
        payload = self._to_payload(message)
        self.attempts.append((topic, payload))
        self.events.append(("publish", topic))
        if topic == self.fail_topic:
            self.fail_topic = None
            raise RuntimeError("synthetic publish failure")
        return await super().publish(topic, message)

    async def ack(self, topic, envelope, *, group=None):
        self.events.append(("ack", topic))
        if self.fail_ack:
            self.fail_ack = False
            raise RuntimeError("synthetic ACK failure")


def _bars(*, quiet=False, count=None):
    rows = _candles()
    if quiet:
        rows = tuple(replace(value, open=100.0, high=100.2, low=99.8, close=100.0) for value in rows)
    return tuple(candle_to_closed_bar(value) for value in rows[:count])


def _envelope(bar):
    return BusEnvelope("offline-1", Topics.CLOSED_BAR, bar.to_payload())


def _range_service(monkeypatch, *, bus=None, quiet=False, count=None, clock=None):
    bars = _bars(quiet=quiet, count=count)
    bus = FaultBus() if bus is None else bus
    service = StrategyEngineService(
        _settings(enabled_strategy_ids=["range_mean_reversion_v1"]),
        bus=bus,
        clock=clock or (lambda: bars[-1].close_time_ms + 123),
    )
    for value in bars[:-1]:
        service._append_or_replay(value)
    calls = []

    def generator(strategy_id, history, config=None, *, for_paper=False):
        calls.append(history[-1].close_time_ms)
        return generate_runtime_strategy_intents(strategy_id, history, _range_config(), for_paper=for_paper)

    monkeypatch.setattr("kairos_strategy.runtime_evaluation.runtime_configuration", lambda _: _range_config())
    monkeypatch.setattr("kairos_strategy.service.generate_runtime_strategy_intents", generator)
    return service, bus, bars, calls


def test_scheduled_quiet_rejected_research_evaluation_is_explicit_and_causal(monkeypatch):
    service, bus, bars, calls = _range_service(monkeypatch, quiet=True)
    asyncio.run(service.handle_envelope(_envelope(bars[-1])))
    receipt = service.last_evaluations[0]
    assert calls == [bars[-1].close_time_ms]
    assert receipt.status == "NO_INTENT" and receipt.evaluation_complete
    assert receipt.registry_status == "rejected" and not receipt.trading_authority
    assert receipt.anchor_bar_sha256 == bars[-1].bar_sha256
    assert receipt.input_first_open_time_ms == bars[0].open_time_ms
    assert receipt.input_bar_count == len(bars) and receipt.input_window_sha256
    assert receipt.observed_at_ms == bars[-1].close_time_ms + 123
    assert receipt.decision_ts_ms == bars[-1].close_time_ms
    assert receipt.causation_id == bars[-1].message_id
    assert bus.events == [("publish", Topics.STRATEGY_EVALUATION), ("ack", Topics.CLOSED_BAR)]


@pytest.mark.parametrize(
    "count,status,reason,calls_expected",
    [
        (1, "WARMUP", "REGISTERED_MINIMUM_HISTORY_NOT_READY", 0),
        (5, "WARMUP", "COMPLETE_FRAME_LOOKBACK_NOT_READY", 1),
        (199, "NOT_SCHEDULED", "OUTSIDE_REGISTERED_DECISION_CLOCK", 0),
    ],
)
def test_quiet_missing_history_or_cadence_is_not_completed_analysis(
    monkeypatch,
    count,
    status,
    reason,
    calls_expected,
):
    service, _bus, bars, calls = _range_service(monkeypatch, quiet=True, count=count)
    assert asyncio.run(service.process_bar(bars[-1])) == ()
    receipt = service.last_evaluations[0]
    assert receipt.status == status and receipt.reason_code == reason
    assert not receipt.evaluation_complete and not receipt.intent_ids
    assert len(calls) == calls_expected


@pytest.mark.parametrize(
    "strategy_id,count",
    [("quarter_hour_flow_v1", 16), ("regime_veto_retest_reclaim_v1", 5)],
)
def test_unsupported_quiet_readiness_does_not_suppress_the_old_research_generator(
    monkeypatch,
    strategy_id,
    count,
):
    bars = _bars(quiet=True, count=count)
    bus = FaultBus()
    service = StrategyEngineService(_settings(enabled_strategy_ids=[strategy_id]), bus=bus)
    for bar in bars[:-1]:
        service._append_or_replay(bar)
    calls = []

    def generator(strategy_id, history, *, for_paper=False):
        calls.append(strategy_id)
        return generate_runtime_strategy_intents(strategy_id, history, for_paper=for_paper)

    monkeypatch.setattr("kairos_strategy.service.generate_runtime_strategy_intents", generator)
    assert asyncio.run(service.process_bar(bars[-1])) == ()
    assert calls == [strategy_id]
    assert service.last_evaluations[0].status == "UNAVAILABLE"
    assert service.last_evaluations[0].reason_code == "READINESS_POLICY_UNAVAILABLE"
    assert not service.last_evaluations[0].evaluation_complete


def test_quarter_hour_directional_candidate_is_unchanged_without_a_quiet_readiness_claim():
    bars = tuple(candle_to_closed_bar(row) for row in quarter_fixture(minutes=1516))
    expected = tuple(
        value
        for value in generate_runtime_strategy_intents("quarter_hour_flow_v1", bars)
        if value.decision_ts_ms == bars[-1].close_time_ms
    )
    assert expected
    bus = FaultBus()
    service = StrategyEngineService(
        StrategyEngineSettings(
            bus_backend="memory",
            trading_symbols=["BTCUSDT"],
            enabled_strategy_ids=["quarter_hour_flow_v1"],
            window_bars=1600,
        ),
        bus=bus,
    )
    for bar in bars[:-1]:
        service._append_or_replay(bar)
    assert asyncio.run(service.process_bar(bars[-1])) == expected
    assert service.last_evaluations[0].status == "INTENT"
    assert service.last_evaluations[0].registry_status == "research"
    assert [payload for topic, payload in bus.messages if topic == Topics.STRATEGY_INTENT] == [
        value.to_payload() for value in expected
    ]


def test_stateful_regime_retest_candidate_is_unchanged_without_fabricated_quiet_readiness():
    bars = tuple(
        candle_to_closed_bar(row)
        for row in regime_retest_fixture(
            Side.LONG,
            RegimeRetestReclaimVariant.STRUCTURAL_RECLAIM,
        )
    )
    expected = tuple(
        value
        for value in generate_runtime_strategy_intents("regime_veto_retest_reclaim_v1", bars)
        if value.decision_ts_ms == bars[-1].close_time_ms
    )
    assert expected
    service = StrategyEngineService(
        StrategyEngineSettings(
            bus_backend="memory",
            trading_symbols=["BTCUSDT"],
            enabled_strategy_ids=["regime_veto_retest_reclaim_v1"],
            window_bars=5000,
        ),
        bus=FaultBus(),
    )
    for bar in bars[:-1]:
        service._append_or_replay(bar)
    assert asyncio.run(service.process_bar(bars[-1])) == expected
    assert service.last_evaluations[0].status == "INTENT"
    assert service.last_evaluations[0].registry_status == "rejected"


def test_generator_failure_is_sanitized_error_not_no_intent(monkeypatch):
    service, bus, bars, _calls = _range_service(monkeypatch)

    def failed(*args, **kwargs):
        raise RuntimeError("synthetic private exception detail must not be published")

    monkeypatch.setattr("kairos_strategy.service.generate_runtime_strategy_intents", failed)
    asyncio.run(service.handle_envelope(_envelope(bars[-1])))
    receipt = service.last_evaluations[0]
    assert receipt.status == "ERROR" and receipt.error_type == "RuntimeError"
    assert not receipt.evaluation_complete and not receipt.intent_ids
    assert "private exception detail" not in json.dumps(bus.messages)
    assert bus.events[-1] == ("ack", Topics.CLOSED_BAR)


def test_full_window_with_undefined_required_vwap_volume_is_not_completed_analysis(monkeypatch):
    service, _bus, _original_bars, _calls = _range_service(monkeypatch, quiet=True)
    bars = tuple(
        candle_to_closed_bar(
            replace(
                value,
                volume=0.0,
                quote_volume=0.0,
                taker_buy_volume=0.0,
                taker_buy_quote_volume=0.0,
            )
        )
        for value in _candles()
    )
    service._bars.clear()
    for bar in bars[:-1]:
        service._append_or_replay(bar)
    assert asyncio.run(service.process_bar(bars[-1])) == ()
    receipt = service.last_evaluations[0]
    assert receipt.status == "UNAVAILABLE" and not receipt.evaluation_complete
    assert receipt.reason_code == "REQUIRED_VOLUME_CONTEXT_UNAVAILABLE"


@pytest.mark.parametrize("fail_topic", [Topics.STRATEGY_EVALUATION, Topics.STRATEGY_INTENT])
def test_publication_failure_never_acks_and_replays_exact_prepared_bytes(monkeypatch, fail_topic):
    bus = FaultBus(fail_topic=fail_topic)
    service, _bus, bars, calls = _range_service(monkeypatch, bus=bus)
    envelope = _envelope(bars[-1])
    with pytest.raises(RuntimeError, match="publish failure"):
        asyncio.run(service.handle_envelope(envelope))
    original = canonical_json_bytes(service.last_evaluations[0].to_payload())
    assert ("ack", Topics.CLOSED_BAR) not in bus.events
    assert len(service._bars["BTCUSDT"]) == len(bars)
    next_bar = candle_to_closed_bar(
        replace(
            _candles()[-1],
            open_time_ms=bars[-1].open_time_ms + 60_000,
            close_time_ms=bars[-1].close_time_ms + 60_000,
        )
    )
    service._clock = lambda: next_bar.close_time_ms + 123
    with pytest.raises(RuntimeError, match="unpublished predecessor"):
        asyncio.run(service.process_bar(next_bar))
    assert len(service._bars["BTCUSDT"]) == len(bars)
    asyncio.run(service.handle_envelope(envelope))
    assert calls == [bars[-1].close_time_ms]
    evaluation_attempts = [payload for topic, payload in bus.attempts if topic == Topics.STRATEGY_EVALUATION]
    assert len(evaluation_attempts) == 2
    assert all(canonical_json_bytes(payload) == original for payload in evaluation_attempts)
    assert bus.events[-3:] == [
        ("publish", Topics.STRATEGY_EVALUATION),
        ("publish", Topics.STRATEGY_INTENT),
        ("ack", Topics.CLOSED_BAR),
    ]
    before = list(bus.messages)
    assert asyncio.run(service.process_bar(bars[-1])) == ()
    assert bus.messages == before and calls == [bars[-1].close_time_ms]


def test_failed_ack_republishes_same_outputs_without_another_evaluation(monkeypatch):
    bus = FaultBus(fail_ack=True)
    service, _bus, bars, calls = _range_service(monkeypatch, bus=bus)
    envelope = _envelope(bars[-1])
    with pytest.raises(RuntimeError, match="ACK failure"):
        asyncio.run(service.handle_envelope(envelope))
    first = list(bus.messages)
    service._clock = lambda: bars[-1].close_time_ms + 999
    asyncio.run(service.handle_envelope(envelope))
    assert calls == [bars[-1].close_time_ms]
    assert bus.messages == first + first
    assert (
        len({payload["message_id"] for topic, payload in bus.messages if topic == Topics.STRATEGY_EVALUATION})
        == 1
    )


def test_receipt_preparation_failure_does_not_disappear_behind_exact_bar_replay(monkeypatch):
    service, bus, bars, calls = _range_service(monkeypatch, quiet=True)
    anchor = bars[-1]
    # The admission clock succeeds but receipt construction fails validation.
    times = iter((anchor.close_time_ms + 123, anchor.close_time_ms - 1))
    service._clock = lambda: next(times)
    with pytest.raises(ValueError, match="non-backdated observation"):
        asyncio.run(service.handle_envelope(_envelope(anchor)))
    assert not bus.events and service._evaluation_pending == {anchor.symbol: anchor.bar_sha256}
    service._clock = lambda: anchor.close_time_ms + 999
    asyncio.run(service.handle_envelope(_envelope(anchor)))
    assert calls == [anchor.close_time_ms, anchor.close_time_ms]
    assert service.last_evaluations[0].status == "NO_INTENT" and not service._evaluation_pending
    assert bus.events[-1] == ("ack", Topics.CLOSED_BAR)


def test_future_bar_rejected_before_history_publish_or_ack():
    bar = _bars(count=1)[0]
    bus = FaultBus()
    service = StrategyEngineService(_settings(), bus=bus, clock=lambda: bar.close_time_ms - 1)
    with pytest.raises(ValueError, match="future closed bar"):
        asyncio.run(service.handle_envelope(_envelope(bar)))
    assert not service._bars and not bus.events


def test_valid_bar_presented_on_wrong_topic_cannot_be_consumed_or_acked():
    bar = _bars(count=1)[0]
    bus = FaultBus()
    service = StrategyEngineService(_settings(), bus=bus)
    envelope = BusEnvelope("wrong-topic", Topics.STRATEGY_INTENT, bar.to_payload())
    with pytest.raises(ValueError, match="CLOSED_BAR input topic"):
        asyncio.run(service.handle_envelope(envelope))
    assert not service._bars and not bus.events


def test_quarantine_is_observed_before_ack_and_failed_publication_is_retryable():
    bars = _bars(count=3)
    bus = FaultBus(fail_topic=Topics.STRATEGY_EVALUATION)
    service = StrategyEngineService(_settings(), bus=bus)
    service._append_or_replay(bars[0])
    envelope = _envelope(bars[2])
    with pytest.raises(RuntimeError, match="publish failure"):
        asyncio.run(service.handle_envelope(envelope))
    receipt = service.last_evaluations[0]
    assert receipt.status == "UNAVAILABLE" and not receipt.evaluation_complete
    assert receipt.reason_code == "CLOSED_BAR_SEQUENCE_QUARANTINED"
    assert "BTCUSDT" in service.blocked_symbols
    assert ("ack", Topics.CLOSED_BAR) not in bus.events
    asyncio.run(service.handle_envelope(envelope))
    assert bus.events[-2:] == [("publish", Topics.STRATEGY_EVALUATION), ("ack", Topics.CLOSED_BAR)]
    assert bus.attempts[0][1] == bus.attempts[1][1]


def test_restored_history_without_observation_never_backfills_a_completed_evaluation(monkeypatch):
    service, bus, bars, calls = _range_service(monkeypatch)
    service._append_or_replay(bars[-1])
    service._restored_through_ms = {"BTCUSDT": bars[-1].open_time_ms}
    asyncio.run(service.handle_envelope(_envelope(bars[-1])))
    assert not calls
    assert service.last_evaluations[0].status == "UNAVAILABLE"
    assert service.last_evaluations[0].reason_code == "HISTORICAL_REPLAY_NOT_EVALUATED"
    assert not service.last_evaluations[0].evaluation_complete
    assert [topic for topic, _ in bus.messages] == [Topics.STRATEGY_EVALUATION]


class SavedPool:
    def __init__(self, evaluations, intents):
        self.evaluations = evaluations
        self.intents = intents
        self.queries = []

    async def fetch(self, query, *args):
        self.queries.append((query, args))
        assert query.lstrip().startswith("SELECT")
        values = self.evaluations if args[0] == Topics.STRATEGY_EVALUATION else self.intents
        # Both asyncpg text JSON and pre-decoded JSON are supported.
        return [
            {"payload": json.dumps(value) if index % 2 == 0 else value} for index, value in enumerate(values)
        ]


class SavedDurableBus(DurableMessageBus):
    """Type-compatible read-only fake, deliberately never initializes a DB."""

    def __init__(self, pool):
        self.repository = SimpleNamespace(pool=pool)
        self.messages = []
        self.acks = []

    async def publish(self, topic, message):
        payload = self._to_payload(message)
        self.messages.append((topic, payload))
        return payload["message_id"]

    async def ack(self, topic, envelope, *, group=None):
        self.acks.append((topic, envelope.id, group))


def _saved_restart(monkeypatch, *, quiet=False):
    first, bus, bars, calls = _range_service(monkeypatch, quiet=quiet)
    asyncio.run(first.handle_envelope(_envelope(bars[-1])))
    evaluations = [payload for topic, payload in bus.messages if topic == Topics.STRATEGY_EVALUATION]
    intents = [payload for topic, payload in bus.messages if topic == Topics.STRATEGY_INTENT]
    pool = SavedPool(evaluations, intents)
    restored_bus = SavedDurableBus(pool)
    service = StrategyEngineService(
        first.settings, bus=restored_bus, clock=lambda: bars[-1].close_time_ms + 999
    )
    service._restore_payloads(value.to_payload() for value in bars)
    return first, service, restored_bus, pool, bars, calls


@pytest.mark.parametrize("quiet", [True, False])
def test_restored_committed_observation_reuses_exact_identity_without_regeneration(monkeypatch, quiet):
    first, service, bus, pool, bars, calls = _saved_restart(monkeypatch, quiet=quiet)
    asyncio.run(service.handle_envelope(_envelope(bars[-1])))
    assert service.last_evaluations == first.last_evaluations
    assert not bus.messages and len(bus.acks) == 1
    assert calls == [bars[-1].close_time_ms]
    assert len(pool.queries) == (1 if quiet else 2)


@pytest.mark.parametrize("corruption", ["duplicate_roster", "missing_intent", "altered_receipt"])
def test_corrupt_saved_outputs_are_neither_acked_nor_replaced_by_a_new_evaluation(monkeypatch, corruption):
    _first, service, bus, pool, bars, calls = _saved_restart(monkeypatch)
    if corruption == "duplicate_roster":
        pool.evaluations *= 2
    elif corruption == "missing_intent":
        pool.intents.clear()
    else:
        pool.evaluations[0]["observed_at_ms"] += 1
    with pytest.raises(ValueError if corruption == "altered_receipt" else RuntimeError):
        asyncio.run(service.handle_envelope(_envelope(bars[-1])))
    assert not bus.messages and not bus.acks and calls == [bars[-1].close_time_ms]


@pytest.mark.parametrize(
    "field", ["strategy_revision", "registry_status", "strategy_code_sha256", "config_sha256"]
)
def test_self_consistent_saved_receipt_cannot_forge_current_registry_or_source(monkeypatch, field):
    _first, service, bus, pool, bars, calls = _saved_restart(monkeypatch)
    fields = dict(pool.evaluations[0])
    for name in ("evaluation_id", "receipt_sha256", "message_id", "correlation_id"):
        fields.pop(name)
    fields[field] = {
        "strategy_revision": "another-revision",
        "registry_status": "paper_approved",
        "strategy_code_sha256": "e" * 64,
        "config_sha256": "e" * 64,
    }[field]
    # The receipt's own hashes remain valid, and it falsely claims the exact
    # current policy. Independent installed-code/default checks must reject it.
    pool.evaluations[0] = StrategyEvaluationV1.model_validate(fields).to_payload()
    with pytest.raises(ValueError, match="independently installed registry/source/config"):
        asyncio.run(service.handle_envelope(_envelope(bars[-1])))
    assert not bus.messages and not bus.acks and calls == [bars[-1].close_time_ms]


def test_saved_intent_reference_must_match_the_observed_side_and_provenance(monkeypatch):
    _first, service, bus, pool, bars, calls = _saved_restart(monkeypatch)
    fields = dict(pool.evaluations[0])
    for name in ("evaluation_id", "receipt_sha256", "message_id", "correlation_id"):
        fields.pop(name)
    fields["intent_sides"] = ["SHORT" if fields["intent_sides"][0] == "LONG" else "LONG"]
    pool.evaluations[0] = StrategyEvaluationV1.model_validate(fields).to_payload()
    with pytest.raises(RuntimeError, match="exact observation provenance"):
        asyncio.run(service.handle_envelope(_envelope(bars[-1])))
    assert not bus.messages and not bus.acks and calls == [bars[-1].close_time_ms]


def test_incomplete_diagnostic_has_same_slot_as_a_completed_observation(monkeypatch):
    service, _bus, bars, _calls = _range_service(monkeypatch, quiet=True)
    asyncio.run(service.process_bar(bars[-1]))
    complete = service.last_evaluations[0]
    fields = complete.model_dump(
        mode="json",
        exclude={
            "evaluation_id",
            "receipt_sha256",
            "message_id",
            "correlation_id",
            "produced_at",
        },
    )
    fields.update(
        status="UNAVAILABLE",
        reason_code="HISTORICAL_REPLAY_NOT_EVALUATED",
        evaluation_complete=False,
        strategy_code_sha256=None,
        config_sha256=None,
        input_window_sha256=None,
    )
    unavailable = StrategyEvaluationV1.model_validate(fields)
    assert unavailable.evaluation_id == complete.evaluation_id
    assert unavailable.receipt_sha256 != complete.receipt_sha256
