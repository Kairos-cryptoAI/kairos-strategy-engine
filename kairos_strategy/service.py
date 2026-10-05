"""Durable closed-bar consumer around the pure strategy generators."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from kairos_core.bus import BusEnvelope, MessageBus, build_bus
from kairos_core.contracts import ClosedBarEventV1, StrategyIntentV1
from kairos_core.contracts.base import utcnow
from kairos_core.contracts.strategy_evaluation import StrategyEvaluationV1
from kairos_core.enums import TradingMode
from kairos_core.logging import configure_logging, get_logger
from kairos_core.topics import Topics
from kairos_persistence import DurableMessageBus

from .config import StrategyEngineSettings
from .runtime import ClosedBarSequenceError, generate_runtime_strategy_intents
from .runtime_evaluation import (
    RuntimeEvaluationBatch,
    evaluate_runtime_bar,
    runtime_evaluation_policy_sha256,
    unavailable_runtime_evaluations,
    validate_runtime_evaluation_provenance,
)
from .runtime_requirements import get_runtime_requirements

log = get_logger("strategy-engine")

_ONE_MINUTE_MS = 60_000


class StrategyEngineService:
    """Generate candidates only after accepting a gap-free final-bar stream."""

    def __init__(
        self,
        settings: StrategyEngineSettings | None = None,
        *,
        bus: MessageBus | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.settings = settings or StrategyEngineSettings()
        if bus is not None:
            self.bus = bus
        else:
            transport = build_bus(self.settings)
            self.bus = (
                transport
                if self.settings.bus_backend == "memory"
                else DurableMessageBus(transport, service_name=self.settings.service_name)
            )
        self._bars: dict[str, deque[ClosedBarEventV1]] = defaultdict(
            lambda: deque(maxlen=self.settings.window_bars)
        )
        self._blocked_symbols: dict[str, str] = {}
        # A durable restart restores the retained strategy window from Postgres
        # before the Redis consumer group starts.  The producer may then
        # replay an older REST-backfill prefix whose deterministic messages
        # were not present in the retained window at restore time.  Those
        # messages are historical duplicates, not a live reorder.
        self._restored_through_ms: dict[str, int] = {}
        self._clock = clock or (lambda: int(utcnow().timestamp() * 1_000))
        self._evaluation_policy_sha256 = runtime_evaluation_policy_sha256(self.settings)
        # One prepared batch per symbol is enough: a failed predecessor cannot
        # be passed by a new bar. Retry all outputs, not a publish cursor, because
        # a durable handler transaction may have rolled back earlier publishes.
        self._prepared: dict[str, tuple[str, RuntimeEvaluationBatch]] = {}
        self._publication_pending: set[str] = set()
        self._evaluation_pending: dict[str, str] = {}
        self._last_evaluations: tuple[StrategyEvaluationV1, ...] = ()

    @property
    def last_evaluations(self) -> tuple[StrategyEvaluationV1, ...]:
        """Latest prepared observations; never an economic or trading permission."""
        return self._last_evaluations

    @property
    def blocked_symbols(self) -> Mapping[str, str]:
        return dict(self._blocked_symbols)

    def _block(self, symbol: str, reason: str) -> None:
        self._blocked_symbols[symbol] = reason

    def _append_or_replay(self, bar: ClosedBarEventV1) -> bool:
        """Append a new contiguous bar; return False for an exact replay."""

        if bar.symbol in self._blocked_symbols:
            raise ClosedBarSequenceError(
                f"{bar.symbol} is blocked after an integrity violation: {self._blocked_symbols[bar.symbol]}"
            )
        history = self._bars[bar.symbol]
        for existing in history:
            if existing.open_time_ms == bar.open_time_ms:
                if existing.bar_sha256 == bar.bar_sha256:
                    return False
                reason = f"conflicting closed bar at {bar.open_time_ms}"
                self._block(bar.symbol, reason)
                raise ClosedBarSequenceError(reason)
        restored_through = self._restored_through_ms.get(bar.symbol)
        if restored_through is not None and bar.open_time_ms <= restored_through:
            # The overlapping portion of the restored window was checked
            # above byte-for-byte.  Anything older is outside the retained
            # strategy state and cannot causally produce a new intent.
            return False
        if history:
            expected = history[-1].open_time_ms + _ONE_MINUTE_MS
            if bar.open_time_ms != expected:
                reason = f"closed-bar gap or reorder: expected {expected}, received {bar.open_time_ms}"
                self._block(bar.symbol, reason)
                raise ClosedBarSequenceError(reason)
        history.append(bar)
        return True

    def _generate_for_bar(self, bar: ClosedBarEventV1) -> tuple[StrategyIntentV1, ...]:
        if not self.settings.enabled_strategy_ids:
            return ()
        emitted: list[StrategyIntentV1] = []
        history = tuple(self._bars[bar.symbol])
        for strategy_id in self.settings.enabled_strategy_ids:
            requirements = get_runtime_requirements(strategy_id)
            if len(history) < requirements.minimum_window_bars:
                continue
            closed_minute = (bar.close_time_ms + 1) // _ONE_MINUTE_MS
            if closed_minute % requirements.decision_interval_bars != requirements.decision_phase_bars:
                continue
            candidates = generate_runtime_strategy_intents(
                strategy_id,
                history,
                for_paper=self.settings.trading_mode is TradingMode.PAPER,
            )
            emitted.extend(
                candidate for candidate in candidates if candidate.decision_ts_ms == bar.close_time_ms
            )
        return tuple(
            sorted(
                emitted,
                key=lambda intent: (
                    intent.decision_ts_ms,
                    intent.symbol,
                    intent.strategy_id,
                    intent.side.value,
                    intent.intent_id or "",
                ),
            )
        )

    async def process_bar(self, bar: ClosedBarEventV1) -> tuple[StrategyIntentV1, ...]:
        if not self.settings.symbol_allowed(bar.symbol):
            raise ValueError(f"closed bar symbol is outside the configured universe: {bar.symbol}")
        if self._clock() < bar.close_time_ms:
            raise ValueError("a future closed bar cannot enter the strategy's causal history")
        if bar.bar_sha256 is None:
            raise ValueError("a runtime bar requires its canonical identity")
        cached = self._prepared.get(bar.symbol)
        if bar.symbol in self._publication_pending:
            if cached is None or cached[0] != bar.bar_sha256:
                raise RuntimeError("an unpublished predecessor must finish before the next symbol bar")
            return await self._publish_prepared(bar.symbol)
        if cached is not None and cached[0] == bar.bar_sha256:
            return ()
        unfinished = self._evaluation_pending.get(bar.symbol)
        if unfinished is not None:
            if unfinished != bar.bar_sha256:
                raise RuntimeError("an unprepared predecessor must finish before the next symbol bar")
            appended = True
        else:
            try:
                appended = self._append_or_replay(bar)
            except ClosedBarSequenceError:
                batch = unavailable_runtime_evaluations(
                    bar,
                    settings=self.settings,
                    policy_sha256=self._evaluation_policy_sha256,
                    clock=self._clock,
                    reason="CLOSED_BAR_SEQUENCE_QUARANTINED",
                )
                self._prepare_publication(bar, batch)
                await self._publish_prepared(bar.symbol)
                raise
            if appended:
                self._evaluation_pending[bar.symbol] = bar.bar_sha256
        if not appended:
            if bar.symbol not in self._restored_through_ms:
                return ()
            # A restored bar is not evidence that this version evaluated it.
            # Reuse independently committed observation bytes when available;
            # otherwise record an explicit unavailable state, never backfill a
            # completed quiet result or a historical trading candidate.
            saved_batch = await self._saved_batch(bar)
            if saved_batch is not None:
                self._prepared[bar.symbol] = (bar.bar_sha256, saved_batch)
                self._last_evaluations = saved_batch.evaluations
                return ()
            batch = unavailable_runtime_evaluations(
                bar,
                settings=self.settings,
                policy_sha256=self._evaluation_policy_sha256,
                clock=self._clock,
                reason="HISTORICAL_REPLAY_NOT_EVALUATED",
            )
        else:
            batch = evaluate_runtime_bar(
                bar,
                tuple(self._bars[bar.symbol]),
                settings=self.settings,
                policy_sha256=self._evaluation_policy_sha256,
                clock=self._clock,
                generator=generate_runtime_strategy_intents,
                requirements_resolver=get_runtime_requirements,
            )
        self._prepare_publication(bar, batch)
        return await self._publish_prepared(bar.symbol)

    def _prepare_publication(self, bar: ClosedBarEventV1, batch: RuntimeEvaluationBatch) -> None:
        if bar.bar_sha256 is None:
            raise ValueError("a prepared publication requires its canonical anchor identity")
        self._prepared[bar.symbol] = (bar.bar_sha256, batch)
        self._evaluation_pending.pop(bar.symbol, None)
        self._publication_pending.add(bar.symbol)
        self._last_evaluations = batch.evaluations

    async def _publish_prepared(self, symbol: str) -> tuple[StrategyIntentV1, ...]:
        batch = self._prepared[symbol][1]
        for evaluation in batch.evaluations:
            await self.bus.publish(Topics.STRATEGY_EVALUATION, evaluation)
        for intent in batch.intents:
            await self.bus.publish(Topics.STRATEGY_INTENT, intent)
        self._publication_pending.discard(symbol)
        return batch.intents

    async def _saved_batch(self, bar: ClosedBarEventV1) -> RuntimeEvaluationBatch | None:
        """Read exact committed outputs for one restored anchor; no DB mutation."""
        if not isinstance(self.bus, DurableMessageBus) or self.bus.repository is None:
            return None
        rows = await self.bus.repository.pool.fetch(
            """SELECT payload FROM event_audit
                 WHERE topic=$1 AND payload->>'symbol'=$2
                   AND payload->>'anchor_bar_sha256'=$3
                   AND payload->>'runtime_policy_sha256'=$4""",
            Topics.STRATEGY_EVALUATION,
            bar.symbol,
            bar.bar_sha256,
            self._evaluation_policy_sha256,
        )
        if not rows:
            return None
        evaluations = tuple(
            StrategyEvaluationV1.model_validate(
                json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
            )
            for row in rows
        )
        expected_roster: set[str | None] = set(self.settings.enabled_strategy_ids)
        if not expected_roster:
            expected_roster.add(None)
        if (
            len(evaluations) != len(expected_roster)
            or {value.strategy_id for value in evaluations} != expected_roster
        ):
            raise RuntimeError("restored strategy observations have an incomplete or duplicate roster")
        if any(
            value.anchor_bar_sha256 != bar.bar_sha256
            or value.decision_ts_ms != bar.close_time_ms
            or value.symbol != bar.symbol
            or value.runtime_policy_sha256 != self._evaluation_policy_sha256
            or value.enabled_strategy_ids != tuple(self.settings.enabled_strategy_ids)
            or value.trading_mode is not self.settings.trading_mode
            for value in evaluations
        ):
            raise RuntimeError("restored strategy observation differs from its exact causal slot")
        if len({value.evaluation_id for value in evaluations}) != len(evaluations):
            raise RuntimeError("restored strategy observations repeat a logical evaluation identity")
        for value in evaluations:
            validate_runtime_evaluation_provenance(value)
        intent_ids = tuple(value for evaluation in evaluations for value in evaluation.intent_ids)
        intents: tuple[StrategyIntentV1, ...] = ()
        if intent_ids:
            intent_rows = await self.bus.repository.pool.fetch(
                "SELECT payload FROM event_audit WHERE topic=$1 AND message_id=ANY($2::TEXT[])",
                Topics.STRATEGY_INTENT,
                list(intent_ids),
            )
            intents = tuple(
                StrategyIntentV1.model_validate(
                    json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
                )
                for row in intent_rows
            )
            if len(intents) != len(intent_ids) or {value.intent_id for value in intents} != set(intent_ids):
                raise RuntimeError("restored directional evaluation is missing its committed intent bytes")
            by_id = {value.intent_id: value for value in intents}
            for evaluation in evaluations:
                for intent_id, side in zip(evaluation.intent_ids, evaluation.intent_sides, strict=True):
                    saved_intent = by_id[intent_id]
                    if (
                        saved_intent.strategy_id != evaluation.strategy_id
                        or saved_intent.strategy_revision != evaluation.strategy_revision
                        or saved_intent.symbol != evaluation.symbol
                        or saved_intent.decision_ts_ms != evaluation.decision_ts_ms
                        or saved_intent.side is not side
                        or saved_intent.provenance.strategy_code_sha256 != evaluation.strategy_code_sha256
                        or saved_intent.provenance.config_sha256 != evaluation.config_sha256
                        or saved_intent.provenance.input_window_sha256 != evaluation.input_window_sha256
                    ):
                        raise RuntimeError(
                            "restored intent bytes differ from their exact observation provenance"
                        )
        return RuntimeEvaluationBatch(
            intents, tuple(sorted(evaluations, key=lambda value: value.strategy_id or ""))
        )

    async def handle_envelope(self, envelope: BusEnvelope) -> None:
        """Request ACK only after every required observation and intent publish."""
        if envelope.topic != Topics.CLOSED_BAR:
            raise ValueError("strategy consumer requires the CLOSED_BAR input topic")
        bar = ClosedBarEventV1.model_validate(envelope.payload)
        try:
            try:
                await self.process_bar(bar)
            except ClosedBarSequenceError as exc:
                log.exception(
                    "strategy.closed_bar_quarantined",
                    envelope_id=envelope.id,
                    symbol=bar.symbol,
                    error_type=type(exc).__name__,
                    sequence_error=str(exc),
                )
            await self.bus.ack(Topics.CLOSED_BAR, envelope, group="strategy-engine")
        except BaseException:
            cached = self._prepared.get(bar.symbol)
            if cached is not None and cached[0] == bar.bar_sha256:
                # This includes failed/cancelled ACK: prior durable publishes
                # may roll back with the input transaction. Keep their exact IDs
                # and bytes; the next delivery republishes, never reevaluates.
                self._publication_pending.add(bar.symbol)
            raise

    def _restore_payloads(self, payloads: Iterable[object]) -> None:
        """Restore every healthy symbol while quarantining a corrupt stream.

        A historical conflict is local to one symbol.  It must prevent that
        symbol from generating candidates, but it must not make the whole
        multi-symbol service unavailable after every restart.
        """

        for raw_payload in payloads:
            payload: Any = raw_payload
            if isinstance(payload, str):
                payload = json.loads(payload)
            if not isinstance(payload, Mapping):
                raise TypeError("event_audit closed-bar payload must be a JSON object")
            bar = ClosedBarEventV1.model_validate(dict(payload))
            if not self.settings.symbol_allowed(bar.symbol):
                continue
            if bar.symbol in self._blocked_symbols:
                continue
            try:
                self._append_or_replay(bar)
            except ClosedBarSequenceError as exc:
                log.error(
                    "strategy.history_symbol_blocked",
                    symbol=bar.symbol,
                    sequence_error=str(exc),
                )

        self._restored_through_ms = {
            symbol: history[-1].open_time_ms
            for symbol, history in self._bars.items()
            if history and symbol not in self._blocked_symbols
        }

    async def _restore_history(self) -> None:
        """Rebuild bounded per-symbol windows from the immutable audit log."""

        if not isinstance(self.bus, DurableMessageBus):
            return
        await self.bus.start()
        if self.bus.repository is None:  # defensive: start() establishes it
            raise RuntimeError("durable strategy bus has no audit repository")
        rows = await self.bus.repository.pool.fetch(
            """SELECT payload
                 FROM (
                     SELECT payload,
                            row_number() OVER (
                                PARTITION BY payload->>'symbol'
                                ORDER BY (payload->>'open_time_ms')::bigint DESC
                            ) AS row_number
                       FROM event_audit
                      WHERE topic=$1
                        AND payload->>'venue'='BINANCE_UM'
                 ) AS ranked
                WHERE row_number <= $2
                ORDER BY payload->>'symbol', (payload->>'open_time_ms')::bigint""",
            Topics.CLOSED_BAR,
            self.settings.window_bars,
        )
        self._restore_payloads(row["payload"] for row in rows)
        log.info(
            "strategy.history_restored",
            symbols=len(self._bars),
            bars=sum(len(history) for history in self._bars.values()),
            blocked_symbols=sorted(self._blocked_symbols),
        )

    async def run(self) -> None:  # pragma: no cover - production consumer is unbounded
        configure_logging(
            self.settings.log_level,
            json_logs=self.settings.log_json,
            service=self.settings.service_name,
        )
        await self._restore_history()
        log.info(
            "strategy.start",
            trading_mode=self.settings.trading_mode.value,
            enabled_strategies=self.settings.enabled_strategy_ids,
        )
        try:
            async for envelope in self.bus.subscribe(
                Topics.CLOSED_BAR,
                group="strategy-engine",
                consumer="closed-bars",
            ):
                try:
                    await self.handle_envelope(envelope)
                except Exception:
                    log.exception(
                        "strategy.closed_bar_failed",
                        envelope_id=envelope.id,
                        symbol=envelope.payload.get("symbol"),
                    )
        finally:
            await self.bus.close()


def main() -> None:  # pragma: no cover
    asyncio.run(StrategyEngineService().run())


if __name__ == "__main__":
    main()
