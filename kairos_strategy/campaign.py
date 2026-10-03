"""Opt-in causal campaign adapter for an explicitly selected pure strategy.

This module neither chooses an economic candidate nor starts a service. The
caller supplies an independently verified, read-only history resolver. The
resolver must identify durable immutable history in production; the evaluator
rechecks the full returned window, not a caller's ``verified`` flag. No bus,
provider, exchange, risk admission, primary connection or trading capability
is created here. Legacy strategy contracts and source fingerprints stay intact.

Readiness covers the selected configuration's finite seed/lookback dependencies
using actual complete UTC frames. It is not EMA convergence or equivalence to
an unspecified longer history. Stateful/phase-specific sleeves without a
reviewed readiness policy are explicitly unsupported by this new adapter.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import Any, Protocol, cast

from kairos_core.contracts import ClosedBarEventV1, ResearchObservationWindowV1, StrategyIntentV1
from kairos_core.contracts.base import canonical_sha256 as contract_sha256
from kairos_core.contracts.base import datetime_from_unix_ms
from kairos_persistence.causal_campaign import CampaignMarketContextV1, CausalBaselineResultV1
from kairos_persistence.research_evidence import ResearchSourceReceiptV1

from .provenance import canonical_sha256, config_sha256, installed_source_tree_sha256
from .registry import get_strategy
from .runtime import canonical_closed_bars, closed_bar_to_candle, generate_runtime_strategy_intents
from .runtime_requirements import get_runtime_requirements
from .sleeves import (
    OrderFlowVolatilityExpansionConfig,
    RangeMeanReversionConfig,
    RegimeAlignedRightTailConfig,
    RightTailTrendConfig,
    TrendBreakoutConfig,
    TrendPullbackReclaimConfig,
)
from .timeframes import TIMEFRAME_MS, aggregate

_MAX_WINDOW_BARS = 50_000
_ADAPTER_FILES = ("campaign.py", "runtime_requirements.py")
_POLICY_VERSION = "causal-registered-strategy-evaluator.v2"


class CausalStrategyEvaluationError(ValueError):
    """The declared source/history/evaluator cannot honestly produce an outcome."""


class CausalStrategyUnsupportedError(CausalStrategyEvaluationError):
    """This adapter has no reviewed readiness policy for the selected sleeve."""


class ReadOnlyBarWindowResolver(Protocol):
    """Resolve one immutable declared window; no implicit location or fetch API.

    The scheduler owns the call deadline. Implementations must be read-only and
    return the complete exact window. A production implementation must resolve
    independently durable verified history, not a mutable in-memory fixture.
    """

    async def load(self, reference: str) -> tuple[ClosedBarEventV1, ...]: ...


class CausalStrategyEvaluator:
    """Evaluate real registered code without rewriting its clock or provenance.

    Every constructor argument is explicit. ``minimum_window_bars`` is a frozen
    operational precondition, not an invented strategy parameter or a scientific
    qualification. It must cover the selected configuration's real warmup and
    cannot be lower than the registered runtime requirement. The artifact binds
    that value and cadence. The caller still needs a separately preregistered
    campaign; constructing this object does not register, freeze or promote one.
    """

    def __init__(
        self,
        *,
        strategy_id: str,
        strategy_revision: str,
        config: object,
        minimum_window_bars: int,
        bar_window_resolver: ReadOnlyBarWindowResolver,
        campaign_id: str,
        schedule_digest: str,
        candidate_protocol_digest: str,
    ) -> None:
        self._strategy_id = _identifier(strategy_id, "strategy_id")
        self._strategy_revision = _identifier(strategy_revision, "strategy_revision")
        self._campaign_id = _identifier(campaign_id, "campaign_id")
        self._schedule_digest = _digest(schedule_digest, "schedule_digest")
        self._protocol_digest = _digest(candidate_protocol_digest, "candidate_protocol_digest")
        definition = get_strategy(self._strategy_id)
        if definition.revision != self._strategy_revision:
            raise CausalStrategyEvaluationError("selected registry revision differs from declared revision")
        if type(config) is not definition.config_type:
            raise TypeError("config must be the explicitly selected registered dataclass type")
        # Reconstruct to rerun the existing dataclass's validation, then retain
        # an independent copy. No config defaults or parameter changes are made.
        self._config = replace(deepcopy(cast(Any, config)))
        self._definition = replace(definition)
        self._requirements = replace(get_runtime_requirements(self._strategy_id))
        self._frame_requirements = _complete_frame_requirements(self._strategy_id, self._config)
        minimum_seed_bars = max(
            self._requirements.minimum_window_bars,
            *(TIMEFRAME_MS[frame] // 60_000 * count for frame, count in self._frame_requirements),
        )
        if (
            isinstance(minimum_window_bars, bool)
            or not isinstance(minimum_window_bars, int)
            or not minimum_seed_bars <= minimum_window_bars <= _MAX_WINDOW_BARS
        ):
            raise CausalStrategyEvaluationError(
                "explicit warmup must cover configured complete-frame and registered bounds up to 50000"
            )
        if not callable(getattr(bar_window_resolver, "load", None)):
            raise TypeError("an explicit read-only bar-window resolver is required")
        self._minimum_window_bars = minimum_window_bars
        self._resolver = bar_window_resolver
        self._config_sha256 = config_sha256(self._config)
        self._strategy_source_sha256 = installed_source_tree_sha256(definition.source_files)
        self._adapter_source_sha256 = installed_source_tree_sha256(_ADAPTER_FILES)
        self._artifact_sha256 = self._artifact()

    @property
    def artifact_sha256(self) -> str:
        return self._artifact_sha256

    @property
    def strategy_id(self) -> str:
        return self._strategy_id

    @property
    def strategy_revision(self) -> str:
        return self._strategy_revision

    def _artifact(self) -> str:
        return canonical_sha256(
            {
                "policy_version": _POLICY_VERSION,
                "strategy_id": self._strategy_id,
                "strategy_revision": self._strategy_revision,
                "strategy_source_sha256": self._strategy_source_sha256,
                "config_sha256": self._config_sha256,
                "adapter_source_sha256": self._adapter_source_sha256,
                "minimum_window_bars": self._minimum_window_bars,
                "maximum_window_bars": _MAX_WINDOW_BARS,
                "complete_frame_requirements": self._frame_requirements,
                "warmup": "actual-complete-frames-finite-seed-only-not-convergence",
                "orderflow_warmup": "trailing-contiguous-positive-volume-5m-segment-only",
                "decision_interval_bars": self._requirements.decision_interval_bars,
                "decision_phase_bars": self._requirements.decision_phase_bars,
                "history": "complete-ordered-canonical-contiguous-window-only",
                "selection": "exact-anchor-close-only-reject-multiple",
                "intent": "unchanged-strategy-intent.v1-for-paper-false",
                "context_schema_sha256": contract_sha256(CampaignMarketContextV1.model_json_schema()),
                "result_schema_sha256": contract_sha256(CausalBaselineResultV1.model_json_schema()),
            }
        )

    def _assert_frozen(self) -> None:
        if (
            get_strategy(self._strategy_id) != self._definition
            or get_runtime_requirements(self._strategy_id) != self._requirements
            or type(self._config) is not self._definition.config_type
            or config_sha256(self._config) != self._config_sha256
            or _complete_frame_requirements(self._strategy_id, self._config) != self._frame_requirements
            or installed_source_tree_sha256(self._definition.source_files) != self._strategy_source_sha256
            or installed_source_tree_sha256(_ADAPTER_FILES) != self._adapter_source_sha256
            or self._artifact() != self._artifact_sha256
        ):
            raise CausalStrategyEvaluationError("selected evaluator source, config or policy changed")

    async def evaluate(
        self,
        *,
        window: ResearchObservationWindowV1,
        sources: tuple[ResearchSourceReceiptV1, ...],
    ) -> CausalBaselineResultV1:
        self._assert_frozen()
        if not isinstance(window, ResearchObservationWindowV1):
            raise TypeError("window must be a typed preregistered observation window")
        window = ResearchObservationWindowV1.model_validate(window.model_dump(mode="json"))
        market = self._market_source(window, sources)
        context_source_digest = market.receipt_sha256
        if context_source_digest is None:
            raise CausalStrategyEvaluationError(
                "market context lacks its independently saved receipt identity"
            )
        context = CampaignMarketContextV1.model_validate(deepcopy(market.content))
        anchor = context.anchor_bar
        if (
            anchor.symbol != window.symbol
            or anchor.timeframe != window.timeframe
            or context.market_snapshot.symbol != window.symbol
            or context.market_snapshot.timeframe != window.timeframe
            or anchor.close_time_ms >= window.market_as_of_ts_ms
        ):
            raise CausalStrategyEvaluationError("market context differs from the causal window or anchor")
        snapshot_time = context.market_snapshot.produced_at
        if snapshot_time.tzinfo is None or snapshot_time.utcoffset() is None:
            raise CausalStrategyEvaluationError("market snapshot requires an explicit aware source clock")
        # Integer milliseconds without rounding a future sub-millisecond event
        # backwards into the allowed decision cutoff.
        if snapshot_time.microsecond % 1000:
            raise CausalStrategyEvaluationError("market source clock must be exact milliseconds")
        since_epoch = snapshot_time - datetime_from_unix_ms(0)
        snapshot_ts_ms = (
            since_epoch.days * 86_400_000 + since_epoch.seconds * 1000 + since_epoch.microseconds // 1000
        )
        if not anchor.close_time_ms <= snapshot_ts_ms <= market.source_as_of_ts_ms:
            raise CausalStrategyEvaluationError("market snapshot predates anchor or exceeds source clock")

        loaded = await self._resolver.load(context.bar_window_reference)
        # A resolver await cannot silently admit a changed evaluator/config.
        self._assert_frozen()
        bars = self._verified_window(loaded, context, window)
        closed_minute = (anchor.close_time_ms + 1) // 60_000
        if len(bars) < self._minimum_window_bars:
            raise CausalStrategyEvaluationError("declared history is below the frozen warmup requirement")
        if (
            closed_minute % self._requirements.decision_interval_bars
            != self._requirements.decision_phase_bars
        ):
            raise CausalStrategyEvaluationError("anchor is not a registered strategy evaluation clock")
        self._assert_complete_frame_readiness(bars)

        # Existing deterministic generator, same config values and full causal
        # input. Do not substitute an intent or adapt trailing/expiry/IDs here.
        generated = generate_runtime_strategy_intents(
            self._strategy_id, bars, deepcopy(self._config), for_paper=False
        )
        self._assert_frozen()
        candidates = tuple(intent for intent in generated if intent.decision_ts_ms == anchor.close_time_ms)
        if len(candidates) > 1:
            raise CausalStrategyEvaluationError("multiple anchor intents are ambiguous in this baseline")
        intent = candidates[0] if candidates else None
        if intent is not None:
            self._verify_intent(intent, bars, context, window)
        return CausalBaselineResultV1(
            context_source_receipt_sha256=context_source_digest,
            bar_window_sha256=context.bar_window_sha256,
            bar_count=len(bars),
            intent=intent,
        )

    def _assert_complete_frame_readiness(self, bars: tuple[ClosedBarEventV1, ...]) -> None:
        candles = [closed_bar_to_candle(bar) for bar in bars]
        for frame, required in self._frame_requirements:
            rows = aggregate(candles, frame)
            available = len(rows)
            if self._strategy_id == "orderflow_volatility_expansion_v1":
                # The unchanged orderflow generator resets all features at a
                # zero-volume bucket. Earlier complete frames cannot seed the
                # current positive-volume segment (_segments in that sleeve).
                available = 0
                for row in reversed(rows):
                    if row.volume <= 0:
                        break
                    available += 1
            if available < required:
                raise CausalStrategyEvaluationError(
                    f"configured {frame} warmup requires {required} complete frames; only {available} ready"
                )

    def _market_source(self, window, sources) -> ResearchSourceReceiptV1:
        if not isinstance(sources, tuple) or not sources:
            raise TypeError("sources must be the explicit nonempty typed frozen source tuple")
        validated = []
        for source in sources:
            if not isinstance(source, ResearchSourceReceiptV1) or source.receipt_sha256 is None:
                raise TypeError("source must carry its independently saved typed receipt identity")
            receipt = ResearchSourceReceiptV1.model_validate(source.model_dump(mode="json"))
            if (
                receipt.campaign_id != self._campaign_id
                or receipt.sample_id != window.sample_id
                or receipt.schedule_digest != self._schedule_digest
                or receipt.candidate_protocol_digest != self._protocol_digest
                or not receipt.source_as_of_ts_ms <= receipt.observed_at_ts_ms <= window.market_as_of_ts_ms
            ):
                raise CausalStrategyEvaluationError(
                    "source scope or actual capture clock differs from window"
                )
            validated.append(receipt)
        keys = [(source.source_kind, source.source_name) for source in validated]
        if len(set(keys)) != len(keys) or len({x.receipt_sha256 for x in validated}) != len(validated):
            raise CausalStrategyEvaluationError("source bundle repeats a source slot or receipt")
        market = tuple(source for source in validated if source.source_kind == "MARKET_SNAPSHOT")
        if len(market) != 1:
            raise CausalStrategyEvaluationError("source bundle must contain exactly one market context")
        if (
            window.market_snapshot_sha256 is not None
            and market[0].content_sha256 != window.market_snapshot_sha256
        ):
            raise CausalStrategyEvaluationError("market context hash differs from preregistered window")
        return market[0]

    @staticmethod
    def _verified_window(loaded, context, window) -> tuple[ClosedBarEventV1, ...]:
        if not isinstance(loaded, tuple) or not 1 <= len(loaded) <= _MAX_WINDOW_BARS:
            raise CausalStrategyEvaluationError("resolver must return one bounded complete typed tuple")
        if any(not isinstance(bar, ClosedBarEventV1) for bar in loaded):
            raise TypeError("resolver history must contain only typed canonical closed bars")
        bars = tuple(ClosedBarEventV1.model_validate(bar.model_dump(mode="json")) for bar in loaded)
        ordered = canonical_closed_bars(bars)
        if ordered != bars:
            raise CausalStrategyEvaluationError("resolved window is not in its declared canonical order")
        digest = contract_sha256({"bars": [bar.identity_payload() for bar in bars]})
        if (
            len(bars) != context.bar_count
            or digest != context.bar_window_sha256
            or bars[0].open_time_ms != context.first_open_time_ms
            or bars[-1].identity_payload() != context.anchor_bar.identity_payload()
            or any(
                bar.symbol != window.symbol
                or bar.timeframe != window.timeframe
                or bar.close_time_ms > context.anchor_bar.close_time_ms
                or bar.close_time_ms >= window.market_as_of_ts_ms
                for bar in bars
            )
        ):
            raise CausalStrategyEvaluationError(
                "resolved history differs from complete declared causal context"
            )
        return bars

    def _verify_intent(self, intent, bars, context, window) -> None:
        if not isinstance(intent, StrategyIntentV1):
            raise TypeError("registered adapter must return StrategyIntentV1 values")
        StrategyIntentV1.model_validate(intent.model_dump(mode="json"))
        if (
            intent.strategy_id != self._strategy_id
            or intent.strategy_revision != self._strategy_revision
            or intent.symbol != window.symbol
            or intent.timeframe != window.timeframe
            or intent.venue != context.anchor_bar.venue
            or intent.provenance.strategy_code_sha256 != self._strategy_source_sha256
            or intent.provenance.config_sha256 != self._config_sha256
            or intent.provenance.input_window_sha256 != context.bar_window_sha256
            or intent.provenance.input_bar_sha256s != tuple(bar.bar_sha256 for bar in bars)
            or window.paired_at_ts_ms >= intent.entry_expires_ts_ms
        ):
            raise CausalStrategyEvaluationError(
                "generated intent lineage or expiry differs from causal context"
            )


def _complete_frame_requirements(strategy_id: str, config: object) -> tuple[tuple[str, int], ...]:
    """Finite dependencies derived from unchanged registered indicator code.

    Aggregate alignment is checked separately; multiplying these counts by
    frame duration is only a constructor lower bound, never proof of readiness.
    """
    if strategy_id == "range_mean_reversion_v1" and isinstance(config, RangeMeanReversionConfig):
        # Both the current and previous bar need prior VWAP and seeded ATR.
        return (
            ("5m", max(config.vwap_lookback_bars + 2, config.atr_period + 1, 2)),
            ("1h", config.regime_lookback_hours + 1),
        )
    if strategy_id == "trend_breakout_v1" and isinstance(config, TrendBreakoutConfig):
        count = max(config.donchian_lookback + 1, config.atr_period)
        if config.minimum_volume_surprise is not None or config.minimum_directional_taker_share is not None:
            count = max(count, config.volume_lookback + 1)
        return (("5m", count), ("1h", config.regime_lookback_hours + 1))
    if strategy_id == "trend_pullback_reclaim_v1" and isinstance(config, TrendPullbackReclaimConfig):
        return (
            ("5m", max(config.reclaim_ema_period, config.trend_ema_period, config.atr_period) + 1),
            (
                "1h",
                max(
                    config.hourly_slow_ema_period,
                    config.hourly_fast_ema_period + config.hourly_rising_lookback,
                    config.hourly_efficiency_lookback + 1,
                ),
            ),
        )
    if strategy_id == "orderflow_volatility_expansion_v1" and isinstance(
        config, OrderFlowVolatilityExpansionConfig
    ):
        return (
            (
                "5m",
                1
                + max(
                    config.baseline_lookback,
                    config.compression_long_lookback,
                    config.persistence_lookback - 1,
                    config.flip_lookback,
                    config.atr_period,
                ),
            ),
        )
    if strategy_id == "right_tail_trend_v1" and isinstance(config, RightTailTrendConfig):
        return (("1h", max(config.trend_lookback_hours + 1, config.atr_period_hours)),)
    if strategy_id == "regime_aligned_right_tail_v1" and isinstance(config, RegimeAlignedRightTailConfig):
        return (
            ("1h", max(config.trend_lookback_hours + 1, config.atr_period_hours)),
            ("4h", config.regime_sma_bars),
        )
    # RegimeRetest carries trigger/setup state and resets indicators across
    # zero-volume segments. QuarterHour uses strict-prior ATR and phase/boundary
    # 1m inputs. A whole-window count is not a faithful readiness policy for
    # either; do not turn missing engineering support into a NO_INTENT outcome.
    raise CausalStrategyUnsupportedError("selected strategy lacks a reviewed causal readiness policy")


def _identifier(value: str, name: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or not value[0].isascii()
        or not value[0].isalnum()
        or any(not char.isascii() or not (char.isalnum() or char in "._:-") for char in value)
    ):
        raise ValueError(f"{name} must be a normalized explicit identifier")
    return value


def _digest(value: str, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return value


__all__ = [
    "CausalStrategyEvaluator",
    "CausalStrategyEvaluationError",
    "CausalStrategyUnsupportedError",
    "ReadOnlyBarWindowResolver",
]
