"""Pure adapters from closed bars to the strict runtime StrategyIntent contract."""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence

from kairos_core.contracts import (
    ClosedBarEventV1,
    ExitPlanV1,
    StrategyIntentV1,
    StrategyProvenanceV1,
)
from kairos_core.contracts.base import (
    canonical_json_bytes as contract_json_bytes,
)
from kairos_core.contracts.base import (
    canonical_sha256 as contract_sha256,
)
from kairos_core.contracts.base import (
    datetime_from_unix_ms,
)

from .candles import Candle
from .models import SleeveIntent
from .provenance import canonical_sha256, config_sha256, features_sha256, installed_source_tree_sha256
from .registry import generate_sleeve_intents, get_strategy


class ClosedBarSequenceError(ValueError):
    """The runtime input is not one gap-free, ordered one-minute stream."""


class UnsupportedExitPlanError(ValueError):
    """A research-only lifecycle cannot be represented by ExitPlanV1."""


def candle_to_closed_bar(candle: Candle) -> ClosedBarEventV1:
    """Build the strict full-bar contract without introducing runtime state."""

    if candle.timeframe != "1m":
        raise ValueError("strategy runtime accepts only closed one-minute candles")
    identity = {
        "base_volume": candle.volume,
        "close": candle.close,
        "close_time_ms": candle.close_time_ms,
        "contract_version": "closed-bar.v1",
        "high": candle.high,
        "is_closed": True,
        "low": candle.low,
        "open": candle.open,
        "open_time_ms": candle.open_time_ms,
        "quote_volume": candle.quote_volume,
        "symbol": candle.symbol,
        "taker_buy_base_volume": candle.taker_buy_volume,
        "taker_buy_quote_volume": candle.taker_buy_quote_volume,
        "timeframe": "1m",
        "venue": "BINANCE_UM",
    }
    bar_id = contract_sha256(identity)
    return ClosedBarEventV1(
        source="strategy-engine",
        message_id=bar_id,
        correlation_id=bar_id,
        produced_at=datetime_from_unix_ms(candle.close_time_ms),
        venue="BINANCE_UM",
        symbol=candle.symbol,
        timeframe="1m",
        open_time_ms=candle.open_time_ms,
        close_time_ms=candle.close_time_ms,
        open=candle.open,
        high=candle.high,
        low=candle.low,
        close=candle.close,
        base_volume=candle.volume,
        quote_volume=candle.quote_volume,
        taker_buy_base_volume=candle.taker_buy_volume,
        taker_buy_quote_volume=candle.taker_buy_quote_volume,
        bar_sha256=bar_id,
    )


def closed_bar_to_candle(bar: ClosedBarEventV1) -> Candle:
    return Candle(
        symbol=bar.symbol,
        timeframe=bar.timeframe,
        open_time_ms=bar.open_time_ms,
        close_time_ms=bar.close_time_ms,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.base_volume,
        quote_volume=bar.quote_volume,
        taker_buy_volume=bar.taker_buy_base_volume,
        taker_buy_quote_volume=bar.taker_buy_quote_volume,
    )


def canonical_closed_bars(bars: Sequence[ClosedBarEventV1]) -> tuple[ClosedBarEventV1, ...]:
    if not bars:
        raise ClosedBarSequenceError("strategy generation requires at least one closed bar")
    if any(not isinstance(bar, ClosedBarEventV1) for bar in bars):
        raise TypeError("bars must contain ClosedBarEventV1 values")
    ordered = tuple(sorted(bars, key=lambda bar: bar.open_time_ms))
    first = ordered[0]
    for previous, current in zip(ordered, ordered[1:], strict=False):
        if current.symbol != first.symbol or current.venue != first.venue:
            raise ClosedBarSequenceError("one generation cannot mix symbols or venues")
        if current.open_time_ms == previous.open_time_ms:
            raise ClosedBarSequenceError(f"duplicate closed bar at {current.open_time_ms}")
        if current.open_time_ms != previous.open_time_ms + 60_000:
            raise ClosedBarSequenceError(
                f"closed-bar gap or reorder between {previous.open_time_ms} and {current.open_time_ms}"
            )
    return ordered


def _window_sha256(bars: Sequence[ClosedBarEventV1]) -> str:
    return canonical_sha256({"bars": [bar.identity_payload() for bar in bars]})


def _bar_hashes(bars: Sequence[ClosedBarEventV1]) -> tuple[str, ...]:
    values: list[str] = []
    for bar in bars:
        if bar.bar_sha256 is None:  # impossible after strict contract validation
            raise ValueError("closed bar has no canonical SHA-256")
        values.append(bar.bar_sha256)
    return tuple(values)


def _runtime_exit_plan(intent: SleeveIntent) -> ExitPlanV1:
    plan = intent.exit_plan
    if plan.trailing_activation_price is not None or plan.trailing_distance is not None:
        raise UnsupportedExitPlanError(
            f"{intent.sleeve_id} emitted a trailing plan that is not supported by stop-target-timeout.v1"
        )
    return ExitPlanV1(
        stop_price=plan.stop_price,
        target_price=plan.target_price,
        max_holding_ms=plan.max_holding_ms,
    )


def _strict_intent(
    raw: SleeveIntent,
    *,
    strategy_revision: str,
    code_sha256: str,
    config_fingerprint: str,
    input_bars: Sequence[ClosedBarEventV1],
) -> StrategyIntentV1:
    exit_plan = _runtime_exit_plan(raw)
    provenance = StrategyProvenanceV1(
        strategy_code_sha256=code_sha256,
        config_sha256=config_fingerprint,
        input_window_sha256=_window_sha256(input_bars),
        features_sha256=features_sha256(dict(raw.metadata)),
        input_bar_sha256s=_bar_hashes(input_bars),
    )
    identity = {
        "contract_version": "strategy-intent.v1",
        "decision_ts_ms": raw.decision_ts_ms,
        "entry_eligible_ts_ms": raw.entry_eligible_ts_ms,
        "entry_expires_ts_ms": raw.entry_expires_ts_ms,
        "evidence": [],
        "exit_plan": exit_plan.model_dump(mode="json"),
        "gross_reward_bps": raw.gross_reward_bps,
        "metadata": raw.metadata,
        "provenance": provenance.model_dump(mode="json"),
        "reference_price": raw.reference_price,
        "side": raw.side.value,
        "signal_strength": raw.signal_strength,
        "strategy_id": raw.sleeve_id,
        "strategy_revision": strategy_revision,
        "symbol": raw.symbol,
        "timeframe": "1m",
        "venue": "BINANCE_UM",
    }
    intent_id = contract_sha256(identity)
    return StrategyIntentV1(
        source="strategy-engine",
        message_id=intent_id,
        correlation_id=intent_id,
        produced_at=datetime_from_unix_ms(raw.decision_ts_ms),
        intent_id=intent_id,
        strategy_id=raw.sleeve_id,
        strategy_revision=strategy_revision,
        symbol=raw.symbol,
        timeframe="1m",
        venue="BINANCE_UM",
        side=raw.side,
        decision_ts_ms=raw.decision_ts_ms,
        entry_eligible_ts_ms=raw.entry_eligible_ts_ms,
        entry_expires_ts_ms=raw.entry_expires_ts_ms,
        reference_price=raw.reference_price,
        signal_strength=raw.signal_strength,
        gross_reward_bps=raw.gross_reward_bps,
        exit_plan=exit_plan,
        provenance=provenance,
        metadata=raw.metadata,
    )


def generate_runtime_strategy_intents(
    strategy_id: str,
    bars: Sequence[ClosedBarEventV1],
    config: object | None = None,
    *,
    for_paper: bool = False,
) -> tuple[StrategyIntentV1, ...]:
    """Generate strict runtime intents from one complete closed-bar stream."""

    ordered = canonical_closed_bars(bars)
    definition = get_strategy(strategy_id)
    settings = definition.config_type() if config is None else config
    raw = generate_sleeve_intents(
        strategy_id, [closed_bar_to_candle(bar) for bar in ordered], settings, for_paper=for_paper
    )
    code_fingerprint = installed_source_tree_sha256(definition.source_files)
    config_fingerprint = config_sha256(settings)
    close_times = [bar.close_time_ms for bar in ordered]
    strict: list[StrategyIntentV1] = []
    for candidate in raw:
        prefix_end = bisect_right(close_times, candidate.decision_ts_ms)
        if prefix_end == 0:
            raise ValueError("strategy intent predates its input bar window")
        strict.append(
            _strict_intent(
                candidate,
                strategy_revision=definition.revision,
                code_sha256=code_fingerprint,
                config_fingerprint=config_fingerprint,
                input_bars=ordered[:prefix_end],
            )
        )
    return tuple(
        sorted(
            strict,
            key=lambda intent: (
                intent.decision_ts_ms,
                intent.symbol,
                intent.side.value,
                intent.intent_id or "",
            ),
        )
    )


def generate_research_strategy_intents(
    strategy_id: str,
    candles: Sequence[Candle],
    config: object | None = None,
) -> tuple[StrategyIntentV1, ...]:
    """Research adapter that deliberately enters the same runtime conversion."""

    return generate_runtime_strategy_intents(
        strategy_id,
        [candle_to_closed_bar(candle) for candle in candles],
        config,
    )


def canonical_intent_batch_bytes(intents: Sequence[StrategyIntentV1]) -> bytes:
    """Canonical full payload bytes, including deterministic message envelopes."""

    return contract_json_bytes({"intents": [intent.model_dump(mode="json") for intent in intents]})
