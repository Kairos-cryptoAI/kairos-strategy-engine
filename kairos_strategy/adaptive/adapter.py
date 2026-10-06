"""Opt-in observation adapter, not a registered PAPER/LIVE service or admission."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from kairos_core.contracts import ClosedBarEventV1, StrategyIntentV1
from kairos_core.contracts.base import datetime_from_unix_ms
from kairos_core.contracts.regime_capability import (
    CapabilityRegime,
    RegimeCapabilityPolicyV1,
    RegimeObservationV1,
    StrategyRegimeCapabilityV1,
)
from kairos_core.contracts.strategy_evaluation import StrategyEvaluationV1
from kairos_core.enums import Side, TradingMode

from ..registry import PaperStrategyDisabledError
from ..runtime import _strict_intent, closed_bar_to_candle
from ..validation import canonical_candles
from .config import DEFAULT_CONFIG, DETECTOR_SOURCE, STRATEGY_ID, STRATEGY_REVISION, UNIVERSE
from .logic import AdaptiveDecision, evaluate_adaptive
from .provenance import AdaptiveSourceIdentity, adaptive_source_identity, adaptive_window_sha256


@dataclass(frozen=True, slots=True)
class AdaptiveEvaluation:
    decision: AdaptiveDecision
    evaluation: StrategyEvaluationV1
    intent: StrategyIntentV1 | None = None
    regime_observation: RegimeObservationV1 | None = None


def build_adaptive_capability_policy(source_set_sha256: str) -> RegimeCapabilityPolicyV1:
    """Return a research mapping; consumers still need independent frozen pins.

    Merely constructing this object does not enroll a campaign, attest economics,
    grant a RiskTradeDecision or change any trading/service allow-list.
    """
    return _capability_policy(source_set_sha256, adaptive_source_identity())


def _capability_policy(source_set_sha256: str, identity: AdaptiveSourceIdentity) -> RegimeCapabilityPolicyV1:
    if not isinstance(source_set_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", source_set_sha256) is None:
        raise ValueError("an explicit canonical source-set SHA-256 is required")
    return RegimeCapabilityPolicyV1(
        source_set_sha256=source_set_sha256,
        observation_source=DETECTOR_SOURCE,
        detector_code_sha256=identity.detector_code_sha256,
        detector_config_sha256=identity.detector_config_sha256,
        capabilities=tuple(
            StrategyRegimeCapabilityV1(
                strategy_id=STRATEGY_ID,
                strategy_revision=STRATEGY_REVISION,
                strategy_code_sha256=identity.strategy_code_sha256,
                config_sha256=identity.config_sha256,
                regime=regime,
                sides=sides,
            )
            for regime, sides in (
                (CapabilityRegime.BULL, (Side.LONG,)),
                (CapabilityRegime.BEAR, (Side.SHORT,)),
                (CapabilityRegime.RANGE, (Side.LONG, Side.SHORT)),
                (CapabilityRegime.CRASH, (Side.SHORT,)),
            )
        ),
    )


def evaluate_adaptive_closed_bars(
    bars: Sequence[ClosedBarEventV1],
    *,
    observed_at_ms: int,
    source_set_sha256: str,
    trading_mode: TradingMode = TradingMode.DRY_RUN,
) -> AdaptiveEvaluation:
    """One causal candidate/evaluation, with a real caller clock and no I/O.

    Backfilled bars cannot masquerade as timely observations. Empty, ambiguous,
    future or unsupported input raises rather than inventing a successful slot.
    Gaps, warmup, unscheduled clocks and numerical errors never mean NO_INTENT.
    """
    if trading_mode is not TradingMode.DRY_RUN:
        raise PaperStrategyDisabledError(
            "adaptive observation supports DRY_RUN only; PAPER/LIVE are disabled"
        )
    if isinstance(observed_at_ms, bool) or not isinstance(observed_at_ms, int) or observed_at_ms < 0:
        raise ValueError("actual observed_at_ms must be a non-negative integer")
    if not bars or any(not isinstance(bar, ClosedBarEventV1) for bar in bars):
        raise ValueError("a non-empty strict closed-bar stream is required")
    ordered = tuple(sorted(bars, key=lambda bar: bar.open_time_ms))
    candles = canonical_candles([closed_bar_to_candle(bar) for bar in ordered], expected_timeframe="1m")
    latest = ordered[-1]
    if latest.bar_sha256 is None:
        raise ValueError("anchor closed bar has no canonical SHA-256")
    if latest.symbol not in UNIVERSE or observed_at_ms < latest.close_time_ms:
        raise ValueError("unsupported universe or an observation clock earlier than its closed input")
    observation_time = datetime_from_unix_ms(observed_at_ms)
    if any(bar.produced_at.utcoffset() is None or bar.produced_at > observation_time for bar in ordered):
        raise ValueError("future or naive producer envelope cannot precede the actual local observation")
    window, window_candles = ordered[-DEFAULT_CONFIG.history_bars :], candles[-DEFAULT_CONFIG.history_bars :]
    identity = adaptive_source_identity()
    policy = _capability_policy(source_set_sha256, identity)
    error_type: str | None = None
    if observed_at_ms > latest.close_time_ms + DEFAULT_CONFIG.entry_lifetime_ms:
        decision = AdaptiveDecision(
            "UNAVAILABLE",
            "LATE_OBSERVATION",
            CapabilityRegime.UNCERTAIN,
            latest.close_time_ms,
            adaptive_window_sha256(window_candles),
        )
    else:
        try:
            decision = evaluate_adaptive(window_candles)
        except (ArithmeticError, ValueError) as exc:
            # No exception text, provider payloads or source values in receipts.
            error_type = type(exc).__name__
            decision = AdaptiveDecision(
                "ERROR",
                "FINITE_EVALUATION_ERROR",
                CapabilityRegime.UNCERTAIN,
                latest.close_time_ms,
                adaptive_window_sha256(window_candles),
            )
    intent, observation = None, None
    if decision.intent is not None:
        intent = _strict_intent(
            decision.intent,
            strategy_revision=STRATEGY_REVISION,
            code_sha256=identity.strategy_code_sha256,
            config_fingerprint=identity.config_sha256,
            input_bars=window,
        )
        if intent.provenance.input_window_sha256 != decision.input_window_sha256:
            raise ValueError("adaptive input window differs across generator and strict adapter")
        observation = RegimeObservationV1(
            source=DETECTOR_SOURCE,
            source_set_sha256=source_set_sha256,
            detector_code_sha256=identity.detector_code_sha256,
            detector_config_sha256=identity.detector_config_sha256,
            intent=intent,
            regime=decision.regime,
            event_as_of_ms=latest.close_time_ms,
            observed_at_ms=observed_at_ms,
            expires_at_ms=intent.entry_expires_ts_ms,
        )
        policy.validate_observation(observation)
    evaluation = StrategyEvaluationV1(
        scope="STRATEGY",
        strategy_id=STRATEGY_ID,
        strategy_revision=STRATEGY_REVISION,
        registry_status="research",
        trading_mode=TradingMode.DRY_RUN,
        enabled_strategy_ids=(STRATEGY_ID,),
        runtime_policy_sha256=policy.policy_sha256,
        strategy_code_sha256=identity.strategy_code_sha256,
        config_sha256=identity.config_sha256,
        symbol=latest.symbol,
        bar_open_time_ms=latest.open_time_ms,
        decision_ts_ms=latest.close_time_ms,
        anchor_bar_sha256=latest.bar_sha256,
        input_first_open_time_ms=window[0].open_time_ms,
        input_bar_count=len(window),
        input_window_sha256=decision.input_window_sha256,
        observed_at_ms=observed_at_ms,
        status=decision.status,
        evaluation_complete=decision.status in {"INTENT", "NO_INTENT"},
        reason_code=decision.reason,
        error_type=error_type,
        intent_ids=() if intent is None or intent.intent_id is None else (intent.intent_id,),
        intent_sides=() if intent is None else (intent.side,),
    )
    return AdaptiveEvaluation(decision, evaluation, intent, observation)
