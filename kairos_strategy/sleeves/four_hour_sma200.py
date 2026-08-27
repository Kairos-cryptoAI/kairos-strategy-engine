"""Causal transcription of the published four-hour SMA200 long/flat rule."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass

from ..allocation import AllocationReason, TargetAllocation
from ..candles import Candle
from ..timeframes import TIMEFRAME_MS, aggregate
from ..validation import canonical_candles

_STRATEGY_ID = "four_hour_sma200_long_v1"
_TIMEFRAME = "4h"
_SOURCE_COMMIT = "5acae6b7a4ff53bacb47a348233060f6a7090b24"


@dataclass(frozen=True, slots=True)
class FourHourSma200Config:
    """Frozen defaults from Olanipekun (2026), used without parameter search."""

    sma_bars: int = 200
    target_weight: float = 1.0

    def __post_init__(self) -> None:
        if isinstance(self.sma_bars, bool) or not isinstance(self.sma_bars, int) or self.sma_bars < 2:
            raise ValueError("sma_bars must be an integer of at least two")
        if (
            isinstance(self.target_weight, bool)
            or not isinstance(self.target_weight, (int, float))
            or not math.isfinite(self.target_weight)
            or not 0 < self.target_weight <= 1
        ):
            raise ValueError("target_weight must be finite within (0, 1]")
        object.__setattr__(self, "target_weight", float(self.target_weight))

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


def _segments(rows: list[Candle]) -> list[list[Candle]]:
    size = TIMEFRAME_MS[_TIMEFRAME]
    result: list[list[Candle]] = []
    current: list[Candle] = []
    for row in rows:
        aligned = row.open_time_ms % size == 0 and row.close_time_ms == row.open_time_ms + size - 1
        contiguous = not current or row.open_time_ms == current[-1].open_time_ms + size
        if not aligned or not contiguous:
            if current:
                result.append(current)
            current = []
        if aligned:
            current.append(row)
    if current:
        result.append(current)
    return result


def _generate_segment(rows: list[Candle], settings: FourHourSma200Config) -> list[TargetAllocation]:
    allocations: list[TargetAllocation] = []
    for index in range(settings.sma_bars - 1, len(rows)):
        row = rows[index]
        window = rows[index - settings.sma_bars + 1 : index + 1]
        moving_average = math.fsum(item.close for item in window) / settings.sma_bars
        is_long = row.close > moving_average
        target_weight = settings.target_weight if is_long else 0.0
        active_horizons = (settings.sma_bars,) if is_long else ()
        thresholds = ((settings.sma_bars, moving_average),) if is_long else ()
        allocations.append(
            TargetAllocation(
                strategy_id=_STRATEGY_ID,
                symbol=row.symbol,
                decision_ts_ms=row.close_time_ms,
                effective_ts_ms=row.close_time_ms + 1,
                target_weight=target_weight,
                annualized_volatility=None,
                active_horizons=active_horizons,
                trailing_stops=thresholds,
                reason=AllocationReason.SIGNAL,
                metadata=(
                    ("config_sha256", settings.fingerprint),
                    ("source", "olanipekun_2026_where_the_edge_lives"),
                    ("source_commit", _SOURCE_COMMIT),
                    ("source_rule", "four_hour_close_gt_rolling_sma_next_bar"),
                    ("threshold_kind", "moving_average_exit"),
                ),
            )
        )
    return allocations


def generate_four_hour_sma200_allocations(
    candles_1m: list[Candle],
    config: FourHourSma200Config | None = None,
) -> list[TargetAllocation]:
    """Generate long/flat targets from complete UTC-aligned closed four-hour bars."""

    if config is None:
        settings = FourHourSma200Config()
    elif isinstance(config, FourHourSma200Config):
        settings = config
    else:
        raise ValueError("config must be a FourHourSma200Config or None")
    ordered = canonical_candles(candles_1m, expected_timeframe="1m")
    four_hour = aggregate(ordered, _TIMEFRAME)
    allocations: list[TargetAllocation] = []
    for segment in _segments(four_hour):
        allocations.extend(_generate_segment(segment, settings))
    return allocations
