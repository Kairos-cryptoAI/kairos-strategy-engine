"""Explicit observation of the unchanged runtime generator's current decision.

This operational adapter does not choose alpha, promote registry entries or
alter intent bytes. Reviewed finite readiness policies are reused from the
causal adapter; unsupported quiet policies are UNAVAILABLE, never a fabricated
analysis. They do not suppress unchanged DRY_RUN research candidates. Receipt
clocks are local observations, not independent DB attestations.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from kairos_core.contracts import ClosedBarEventV1, StrategyIntentV1
from kairos_core.contracts.strategy_evaluation import StrategyEvaluationStatus, StrategyEvaluationV1
from kairos_core.enums import TradingMode

from .campaign import CausalStrategyUnsupportedError, _complete_frame_requirements
from .config import StrategyEngineSettings
from .provenance import canonical_sha256, config_sha256, installed_source_tree_sha256
from .registry import StrategyDefinition, get_strategy
from .runtime import canonical_closed_bars, closed_bar_to_candle
from .runtime_requirements import RuntimeRequirements, get_runtime_requirements
from .sleeves import RangeMeanReversionConfig, RightTailTrendConfig, TrendBreakoutConfig
from .timeframes import TIMEFRAME_MS, aggregate


@dataclass(frozen=True, slots=True)
class RuntimeEvaluationBatch:
    """Prepared outputs retained unchanged through publication/ACK retries."""

    intents: tuple[StrategyIntentV1, ...]
    evaluations: tuple[StrategyEvaluationV1, ...]


def runtime_evaluation_policy_sha256(settings: StrategyEngineSettings) -> str:
    return canonical_sha256(
        {
            "contract_version": "runtime-strategy-observation-policy.v1",
            "source_sha256": installed_source_tree_sha256(
                ("runtime_evaluation.py", "service.py", "campaign.py", "runtime_requirements.py", "config.py")
            ),
            "enabled_strategy_ids": settings.enabled_strategy_ids,
            "registered_sources": {
                strategy_id: {
                    "revision": get_strategy(strategy_id).revision,
                    "status": get_strategy(strategy_id).status.value,
                    "source_sha256": installed_source_tree_sha256(get_strategy(strategy_id).source_files),
                    "default_config_sha256": config_sha256(get_strategy(strategy_id).config_type()),
                }
                for strategy_id in settings.enabled_strategy_ids
            },
            "trading_mode": settings.trading_mode.value,
            "trading_symbols": settings.trading_symbols,
            "window_bars": settings.window_bars,
            "readiness": "reviewed-finite-complete-frames-not-indicator-convergence",
            "authority": "OBSERVATION_ONLY",
        }
    )


def runtime_configuration(definition: StrategyDefinition) -> object:
    """Use the very same registered default as generate_runtime_strategy_intents."""
    return definition.config_type()


def _scheduled(
    strategy_id: str,
    config: object,
    bar: ClosedBarEventV1,
    requirements: RuntimeRequirements,
    frames: tuple[tuple[str, int], ...] | None,
) -> bool:
    closed_minute = (bar.close_time_ms + 1) // 60_000
    if closed_minute % requirements.decision_interval_bars != requirements.decision_phase_bars:
        return False
    if strategy_id == "right_tail_trend_v1" and isinstance(config, RightTailTrendConfig):
        hour_open = bar.close_time_ms + 1 - TIMEFRAME_MS["1h"]
        return hour_open >= 0 and hour_open % (config.decision_interval_hours * TIMEFRAME_MS["1h"]) == 0
    if strategy_id == "quarter_hour_flow_v1":
        return bar.open_time_ms % TIMEFRAME_MS["15m"] == 0
    if strategy_id == "regime_veto_retest_reclaim_v1":
        return (bar.close_time_ms + 1) % TIMEFRAME_MS["5m"] == 0
    # Each supported generator decides only at its complete primary frame close.
    if frames is None:
        raise CausalStrategyUnsupportedError("selected strategy has no reviewed decision clock")
    return (bar.close_time_ms + 1) % TIMEFRAME_MS[frames[0][0]] == 0


def _history_ready(
    strategy_id: str,
    bars: tuple[ClosedBarEventV1, ...],
    frames: tuple[tuple[str, int], ...],
) -> bool:
    candles = [closed_bar_to_candle(bar) for bar in bars]
    for frame, required in frames:
        rows = aggregate(candles, frame)
        available = len(rows)
        if strategy_id == "orderflow_volatility_expansion_v1":
            available = 0
            for row in reversed(rows):
                if row.volume <= 0:
                    break
                available += 1
        if available < required:
            return False
    return True


def _volume_context_ready(strategy_id: str, config: object, bars: tuple[ClosedBarEventV1, ...]) -> bool:
    """Do not label an undefined required volume denominator a quiet analysis."""
    if strategy_id == "range_mean_reversion_v1" and isinstance(config, RangeMeanReversionConfig):
        rows = aggregate([closed_bar_to_candle(value) for value in bars], "5m")
        # The generator uses strictly prior VWAP at both current and previous
        # complete 5m frames. Frame-count readiness is checked before this.
        return all(
            sum(value.volume for value in rows[index - config.vwap_lookback_bars : index]) > 0
            for index in (len(rows) - 2, len(rows) - 1)
        )
    if (
        strategy_id == "trend_breakout_v1"
        and isinstance(config, TrendBreakoutConfig)
        and (config.minimum_volume_surprise is not None or config.minimum_directional_taker_share is not None)
    ):
        rows = aggregate([closed_bar_to_candle(value) for value in bars], "5m")
        return (
            rows[-1].volume > 0 and sum(value.volume for value in rows[-config.volume_lookback - 1 : -1]) > 0
        )
    return True


def _receipt(
    *,
    settings: StrategyEngineSettings,
    policy_sha256: str,
    bar: ClosedBarEventV1,
    history: Sequence[ClosedBarEventV1],
    clock: Callable[[], int],
    definition: StrategyDefinition | None = None,
    code_sha256: str | None = None,
    config_fingerprint: str | None = None,
    status: StrategyEvaluationStatus,
    reason: str,
    intents: tuple[StrategyIntentV1, ...] = (),
    window_sha256: str | None = None,
    error_type: str | None = None,
) -> StrategyEvaluationV1:
    references = tuple(sorted(intents, key=lambda intent: intent.intent_id or ""))
    if bar.bar_sha256 is None:
        raise ValueError("an observed closed bar requires its canonical identity")
    intent_ids: list[str] = []
    for intent in references:
        if intent.intent_id is None:
            raise ValueError("an observed directional intent requires its canonical identity")
        intent_ids.append(intent.intent_id)
    return StrategyEvaluationV1(
        causation_id=bar.message_id,
        scope="ENGINE" if definition is None else "STRATEGY",
        strategy_id=None if definition is None else definition.strategy_id,
        strategy_revision=None if definition is None else definition.revision,
        registry_status=None if definition is None else definition.status.value,
        trading_mode=settings.trading_mode,
        enabled_strategy_ids=tuple(settings.enabled_strategy_ids),
        runtime_policy_sha256=policy_sha256,
        strategy_code_sha256=code_sha256,
        config_sha256=config_fingerprint,
        symbol=bar.symbol,
        bar_open_time_ms=bar.open_time_ms,
        decision_ts_ms=bar.close_time_ms,
        anchor_bar_sha256=bar.bar_sha256,
        input_first_open_time_ms=history[0].open_time_ms,
        input_bar_count=len(history),
        input_window_sha256=window_sha256,
        observed_at_ms=clock(),
        status=status,
        evaluation_complete=status in {"INTENT", "NO_INTENT"},
        reason_code=reason,
        error_type=error_type,
        intent_ids=tuple(intent_ids),
        intent_sides=tuple(intent.side for intent in references),
    )


def unavailable_runtime_evaluations(
    bar: ClosedBarEventV1,
    *,
    settings: StrategyEngineSettings,
    policy_sha256: str,
    clock: Callable[[], int],
    reason: str,
) -> RuntimeEvaluationBatch:
    """Observe one rejected/unrecoverable input without inventing a causal window."""
    definitions = tuple(get_strategy(value) for value in settings.enabled_strategy_ids)
    evaluations = tuple(
        _receipt(
            settings=settings,
            policy_sha256=policy_sha256,
            bar=bar,
            history=(bar,),
            clock=clock,
            definition=definition,
            code_sha256=None if definition is None else installed_source_tree_sha256(definition.source_files),
            config_fingerprint=None
            if definition is None
            else config_sha256(runtime_configuration(definition)),
            status="UNAVAILABLE",
            reason=reason,
        )
        for definition in definitions or (None,)
    )
    return RuntimeEvaluationBatch((), evaluations)


def evaluate_runtime_bar(
    bar: ClosedBarEventV1,
    history: Sequence[ClosedBarEventV1],
    *,
    settings: StrategyEngineSettings,
    policy_sha256: str,
    clock: Callable[[], int],
    generator: Callable[..., tuple[StrategyIntentV1, ...]],
    requirements_resolver: Callable[[str], RuntimeRequirements] = get_runtime_requirements,
) -> RuntimeEvaluationBatch:
    """Emit explicit state for each enabled strategy, preserving candidate bytes."""
    if not settings.enabled_strategy_ids:
        receipt = _receipt(
            settings=settings,
            policy_sha256=policy_sha256,
            bar=bar,
            history=(bar,),
            clock=clock,
            status="DISABLED",
            reason="EMPTY_STRATEGY_ALLOW_LIST",
        )
        return RuntimeEvaluationBatch((), (receipt,))
    ordered = canonical_closed_bars(history)
    if ordered[-1].bar_sha256 != bar.bar_sha256:
        raise ValueError("runtime observation anchor differs from its causal history")
    evaluations: list[StrategyEvaluationV1] = []
    emitted: list[StrategyIntentV1] = []
    for strategy_id in settings.enabled_strategy_ids:
        definition = get_strategy(strategy_id)
        code_sha256: str | None = None
        config_fingerprint: str | None = None
        window_sha256: str | None = None
        error_type: str | None = None
        status: StrategyEvaluationStatus
        candidates: tuple[StrategyIntentV1, ...] = ()
        try:
            config = runtime_configuration(definition)
            config_fingerprint = config_sha256(config)
            code_sha256 = installed_source_tree_sha256(definition.source_files)
            if settings.trading_mode is TradingMode.PAPER and not definition.paper_enabled:
                status, reason = "DISABLED", "STRATEGY_NOT_PAPER_APPROVED"
            else:
                requirements = requirements_resolver(strategy_id)
                try:
                    frames = _complete_frame_requirements(strategy_id, config)
                except CausalStrategyUnsupportedError:
                    frames = None
                if len(ordered) < requirements.minimum_window_bars:
                    status, reason = "WARMUP", "REGISTERED_MINIMUM_HISTORY_NOT_READY"
                elif not _scheduled(strategy_id, config, bar, requirements, frames):
                    status, reason = "NOT_SCHEDULED", "OUTSIDE_REGISTERED_DECISION_CLOCK"
                else:
                    window_sha256 = canonical_sha256(
                        {"bars": [value.identity_payload() for value in ordered]}
                    )
                    generated = generator(
                        strategy_id,
                        ordered,
                        for_paper=settings.trading_mode is TradingMode.PAPER,
                    )
                    candidates = tuple(
                        value for value in generated if value.decision_ts_ms == bar.close_time_ms
                    )
                    for value in candidates:
                        StrategyIntentV1.model_validate(value.model_dump(mode="json"))
                        if (
                            value.strategy_id != definition.strategy_id
                            or value.strategy_revision != definition.revision
                            or value.symbol != bar.symbol
                            or value.provenance.strategy_code_sha256 != code_sha256
                            or value.provenance.config_sha256 != config_fingerprint
                            or value.provenance.input_window_sha256 != window_sha256
                        ):
                            raise ValueError(
                                "current runtime intent differs from the observed frozen input/source"
                            )
                    # A real directional result proves its analysis path ran;
                    # observation readiness must never silently narrow the old
                    # DRY_RUN research allow-list or discard original bytes.
                    if candidates:
                        status, reason = "INTENT", "DIRECTIONAL_INTENT_GENERATED"
                    elif frames is None:
                        status, reason = "UNAVAILABLE", "READINESS_POLICY_UNAVAILABLE"
                    elif not _history_ready(strategy_id, ordered, frames):
                        status, reason = "WARMUP", "COMPLETE_FRAME_LOOKBACK_NOT_READY"
                    elif not _volume_context_ready(strategy_id, config, ordered):
                        status, reason = "UNAVAILABLE", "REQUIRED_VOLUME_CONTEXT_UNAVAILABLE"
                    else:
                        status, reason = "NO_INTENT", "COMPLETE_EVALUATION_NO_INTENT"
        except CausalStrategyUnsupportedError:
            status, reason = "UNAVAILABLE", "READINESS_POLICY_UNAVAILABLE"
        except Exception as exc:
            status, reason, error_type = "ERROR", "STRATEGY_EVALUATION_FAILED", type(exc).__name__
            candidates = ()
        evaluations.append(
            _receipt(
                settings=settings,
                policy_sha256=policy_sha256,
                bar=bar,
                history=ordered,
                clock=clock,
                definition=definition,
                code_sha256=code_sha256,
                config_fingerprint=config_fingerprint,
                status=status,
                reason=reason,
                intents=candidates,
                window_sha256=window_sha256,
                error_type=error_type,
            )
        )
        emitted.extend(candidates)
    return RuntimeEvaluationBatch(
        tuple(
            sorted(
                emitted,
                key=lambda value: (
                    value.decision_ts_ms,
                    value.symbol,
                    value.strategy_id,
                    value.side.value,
                    value.intent_id or "",
                ),
            )
        ),
        tuple(evaluations),
    )


def validate_runtime_evaluation_provenance(value: StrategyEvaluationV1) -> None:
    """Recheck independently installed registry/source/defaults on durable replay."""
    if value.scope == "ENGINE":
        return  # The strict Core contract forbids fictitious strategy fingerprints.
    if value.strategy_id is None:
        raise ValueError("a restored strategy observation requires its registered identity")
    definition = get_strategy(value.strategy_id)
    if (
        value.strategy_revision != definition.revision
        or value.registry_status != definition.status.value
        or value.strategy_code_sha256 != installed_source_tree_sha256(definition.source_files)
        or value.config_sha256 != config_sha256(runtime_configuration(definition))
    ):
        raise ValueError(
            "restored strategy observation differs from independently installed registry/source/config"
        )


__all__ = ["RuntimeEvaluationBatch", "evaluate_runtime_bar", "runtime_evaluation_policy_sha256"]
