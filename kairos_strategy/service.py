"""Durable closed-bar consumer around the pure strategy generators."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping
from typing import Any

from kairos_core.bus import MessageBus, build_bus
from kairos_core.contracts import ClosedBarEventV1, StrategyIntentV1
from kairos_core.enums import TradingMode
from kairos_core.logging import configure_logging, get_logger
from kairos_core.topics import Topics
from kairos_persistence import DurableMessageBus

from .config import StrategyEngineSettings
from .runtime import ClosedBarSequenceError, generate_runtime_strategy_intents
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
        if not self._append_or_replay(bar):
            return ()
        intents = self._generate_for_bar(bar)
        for intent in intents:
            await self.bus.publish(Topics.STRATEGY_INTENT, intent)
        return intents

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
                    bar = ClosedBarEventV1.model_validate(envelope.payload)
                    await self.process_bar(bar)
                    await self.bus.ack(Topics.CLOSED_BAR, envelope, group="strategy-engine")
                except ClosedBarSequenceError as exc:
                    log.exception(
                        "strategy.closed_bar_quarantined",
                        envelope_id=envelope.id,
                        symbol=envelope.payload.get("symbol"),
                        error_type=type(exc).__name__,
                        sequence_error=str(exc),
                    )
                    # The immutable event already exists in the durable audit
                    # log.  Acknowledge this deterministic poison message so a
                    # quarantined symbol cannot starve the healthy universe.
                    await self.bus.ack(Topics.CLOSED_BAR, envelope, group="strategy-engine")
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
