"""Strict runtime adapter of the hourly trend rules; not a registered service or admission.

Closed one-minute bars in, at most one ``StrategyIntentV1`` out, through the same
``_strict_intent`` conversion the registered sleeves and ``kairos_strategy.adaptive`` use.
The declared input is exactly the bars the rules read: the history window that ends at
the decision. Nothing here grants PAPER or LIVE permission.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from kairos_core.contracts import ClosedBarEventV1, StrategyIntentV1

from ..runtime import _strict_intent, canonical_closed_bars, closed_bar_to_candle
from .provenance import HourlyTrendSourceIdentity, hourly_trend_source_identity
from .rules import HourlyTrendCandidatesConfig, HourlyTrendState, evaluate_hourly_trend_state

_MINUTE_MS = 60_000
_QUARTER_MS = 15 * _MINUTE_MS
_HOUR_MS = 60 * _MINUTE_MS


@dataclass(frozen=True, slots=True)
class HourlyTrendEvaluation:
    """One decision of the rules on strict closed bars.

    ``state`` is ``None`` when the window is too short for the rules to be evaluated.
    ``intent`` is the strict form of the candidate, if the rules produced one.
    """

    identity: HourlyTrendSourceIdentity
    decision_ts_ms: int
    input_first_open_time_ms: int
    input_bar_count: int
    state: HourlyTrendState | None
    intent: StrategyIntentV1 | None


def evaluate_hourly_trend_closed_bars(
    bars: Sequence[ClosedBarEventV1],
    config: HourlyTrendCandidatesConfig | None = None,
) -> HourlyTrendEvaluation:
    """Evaluate the rules at the quarter hour that the newest closed bar ends.

    The stream must be one symbol's gap-free one-minute bars and must end exactly at a
    quarter-hour close; anything else raises instead of deciding on a guessed clock. Bars
    older than ``history_hours`` before the decision are not read and not declared.
    """

    identity = hourly_trend_source_identity(config)
    settings = HourlyTrendCandidatesConfig() if config is None else config
    ordered = canonical_closed_bars(bars)
    decision_ts_ms = ordered[-1].close_time_ms
    if (decision_ts_ms + 1) % _QUARTER_MS != 0:
        raise ValueError("the newest closed bar must end a quarter hour")
    first_open_ms = decision_ts_ms + 1 - settings.history_hours * _HOUR_MS
    window = tuple(bar for bar in ordered if bar.open_time_ms >= first_open_ms)
    state, raw = evaluate_hourly_trend_state([closed_bar_to_candle(bar) for bar in window], settings)
    if state is not None and state.decision_ts_ms != decision_ts_ms:
        raise ValueError("the rules decided on another clock than the newest closed bar")
    intent = None
    if raw is not None:
        if raw.decision_ts_ms != decision_ts_ms:
            raise ValueError("the candidate belongs to another decision than the newest closed bar")
        intent = _strict_intent(
            raw,
            strategy_revision=identity.revision,
            code_sha256=identity.strategy_code_sha256,
            config_fingerprint=identity.config_sha256,
            input_bars=window,
        )
    return HourlyTrendEvaluation(
        identity=identity,
        decision_ts_ms=decision_ts_ms,
        input_first_open_time_ms=window[0].open_time_ms,
        input_bar_count=len(window),
        state=state,
        intent=intent,
    )


__all__ = ["HourlyTrendEvaluation", "evaluate_hourly_trend_closed_bars"]
