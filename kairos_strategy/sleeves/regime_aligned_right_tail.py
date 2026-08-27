"""Right-tail trend intents admitted only in the matching slow price regime."""

from __future__ import annotations

import hashlib
import json
import math
from bisect import bisect_right
from dataclasses import asdict, dataclass

from kairos_core.enums import Side

from ..candles import Candle
from ..models import SleeveIntent
from ..timeframes import TIMEFRAME_MS, aggregate
from ..validation import canonical_candles
from .right_tail_trend import RightTailTrendConfig, generate_right_tail_trend_intents

_STRATEGY_VERSION = "regime_aligned_right_tail_v1"
_REGIME_TIMEFRAME = "4h"
_REGIME_MS = TIMEFRAME_MS[_REGIME_TIMEFRAME]


@dataclass(frozen=True, slots=True)
class RegimeAlignedRightTailConfig:
    """Frozen trial-15 composition without per-symbol or per-side tuning."""

    trend_lookback_hours: int = 24
    minimum_trend_score: float = 1.0
    atr_period_hours: int = 24
    stop_atr_multiple: float = 2.0
    target_reward_to_risk: float = 4.0
    max_hold_hours: int = 72
    decision_interval_hours: int = 24
    intent_valid_hours: int = 1
    regime_sma_bars: int = 200

    def __post_init__(self) -> None:
        base = RightTailTrendConfig(
            trend_lookback_hours=self.trend_lookback_hours,
            minimum_trend_score=self.minimum_trend_score,
            atr_period_hours=self.atr_period_hours,
            stop_atr_multiple=self.stop_atr_multiple,
            target_reward_to_risk=self.target_reward_to_risk,
            max_hold_hours=self.max_hold_hours,
            decision_interval_hours=self.decision_interval_hours,
            intent_valid_hours=self.intent_valid_hours,
        )
        for name in ("minimum_trend_score", "stop_atr_multiple", "target_reward_to_risk"):
            object.__setattr__(self, name, getattr(base, name))
        if (
            isinstance(self.regime_sma_bars, bool)
            or not isinstance(self.regime_sma_bars, int)
            or self.regime_sma_bars < 2
        ):
            raise ValueError("regime_sma_bars must be an integer of at least two")

    @property
    def base_config(self) -> RightTailTrendConfig:
        return RightTailTrendConfig(
            trend_lookback_hours=self.trend_lookback_hours,
            minimum_trend_score=self.minimum_trend_score,
            atr_period_hours=self.atr_period_hours,
            stop_atr_multiple=self.stop_atr_multiple,
            target_reward_to_risk=self.target_reward_to_risk,
            max_hold_hours=self.max_hold_hours,
            decision_interval_hours=self.decision_interval_hours,
            intent_valid_hours=self.intent_valid_hours,
        )

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


def _number(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("regime evidence must be finite")
    return format(0.0 if value == 0 else value, ".17g")


def _feature_hash(payload: dict[str, str | int]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _segments(rows: list[Candle]) -> list[list[Candle]]:
    result: list[list[Candle]] = []
    current: list[Candle] = []
    for row in rows:
        aligned = (
            row.open_time_ms % _REGIME_MS == 0 and row.close_time_ms == row.open_time_ms + _REGIME_MS - 1
        )
        contiguous = not current or row.open_time_ms == current[-1].open_time_ms + _REGIME_MS
        if not aligned or not contiguous:
            if current:
                result.append(current)
            current = []
        if aligned:
            current.append(row)
    if current:
        result.append(current)
    return result


def _regime_states(rows: list[Candle], sma_bars: int) -> list[tuple[int, float, float]]:
    states: list[tuple[int, float, float]] = []
    for segment in _segments(rows):
        for index in range(sma_bars - 1, len(segment)):
            current = segment[index]
            window = segment[index - sma_bars + 1 : index + 1]
            moving_average = math.fsum(row.close for row in window) / sma_bars
            states.append((current.close_time_ms, current.close, moving_average))
    return states


def _regime_allows(side: Side, close: float, moving_average: float) -> bool:
    if side is Side.LONG:
        return close > moving_average
    if side is Side.SHORT:
        return close < moving_average
    return False


def generate_regime_aligned_right_tail_intents(
    candles_1m: list[Candle],
    config: RegimeAlignedRightTailConfig | None = None,
) -> list[SleeveIntent]:
    """Filter the exact daily right-tail signal by the last complete 4h SMA state."""

    if config is None:
        settings = RegimeAlignedRightTailConfig()
    elif isinstance(config, RegimeAlignedRightTailConfig):
        settings = config
    else:
        raise ValueError("config must be a RegimeAlignedRightTailConfig or None")

    ordered = canonical_candles(candles_1m, expected_timeframe="1m")
    base_config = settings.base_config
    base_intents = generate_right_tail_trend_intents(ordered, base_config)
    states = _regime_states(aggregate(ordered, _REGIME_TIMEFRAME), settings.regime_sma_bars)
    state_times = [state[0] for state in states]
    accepted: list[SleeveIntent] = []
    for base in base_intents:
        state_index = bisect_right(state_times, base.decision_ts_ms) - 1
        if state_index < 0:
            continue
        regime_ts_ms, regime_close, regime_sma = states[state_index]
        age_ms = base.decision_ts_ms - regime_ts_ms
        if age_ms < 0 or age_ms >= _REGIME_MS:
            continue
        if not _regime_allows(base.side, regime_close, regime_sma):
            continue

        base_metadata = dict(base.metadata)
        feature_payload: dict[str, str | int] = {
            "base_feature_hash": base_metadata["feature_hash"],
            "base_intent_id": base.intent_id,
            "config_sha256": settings.fingerprint,
            "decision_ts_ms": base.decision_ts_ms,
            "regime_close": _number(regime_close),
            "regime_close_ts_ms": regime_ts_ms,
            "regime_sma": _number(regime_sma),
            "regime_sma_bars": settings.regime_sma_bars,
            "side": base.side.value,
            "strategy_version": _STRATEGY_VERSION,
            "symbol": base.symbol,
        }
        accepted.append(
            SleeveIntent(
                sleeve_id=_STRATEGY_VERSION,
                symbol=base.symbol,
                side=base.side,
                decision_ts_ms=base.decision_ts_ms,
                entry_eligible_ts_ms=base.entry_eligible_ts_ms,
                entry_expires_ts_ms=base.entry_expires_ts_ms,
                reference_price=base.reference_price,
                signal_strength=base.signal_strength,
                gross_reward_bps=base.gross_reward_bps,
                exit_plan=base.exit_plan,
                metadata=(
                    ("atr", base_metadata["atr"]),
                    ("base_config_sha256", base_config.fingerprint),
                    ("base_feature_hash", base_metadata["feature_hash"]),
                    ("base_intent_id", base.intent_id),
                    ("config_sha256", settings.fingerprint),
                    ("decision_clock", "utc_epoch_aligned"),
                    ("feature_hash", _feature_hash(feature_payload)),
                    ("regime_close", _number(regime_close)),
                    ("regime_close_ts_ms", str(regime_ts_ms)),
                    ("regime_sma", _number(regime_sma)),
                    ("regime_sma_bars", str(settings.regime_sma_bars)),
                    ("regime_timeframe", _REGIME_TIMEFRAME),
                    ("strategy_version", _STRATEGY_VERSION),
                    ("trend_score", base_metadata["trend_score"]),
                ),
            )
        )
    return accepted


__all__ = [
    "RegimeAlignedRightTailConfig",
    "generate_regime_aligned_right_tail_intents",
]
