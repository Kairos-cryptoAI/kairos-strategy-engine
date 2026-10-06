"""Independent new-lineage identities; none of the legacy source files change."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..candles import Candle
from ..provenance import canonical_sha256, config_sha256, installed_source_tree_sha256
from .config import DEFAULT_CONFIG, AdaptiveConfig, require_fixed_config

ADAPTIVE_SOURCE_FILES = (
    "adaptive/__init__.py",
    "adaptive/adapter.py",
    "adaptive/config.py",
    "adaptive/logic.py",
    "adaptive/provenance.py",
    "candles.py",
    "models.py",
    "provenance.py",
    "registry.py",
    "runtime.py",
    "timeframes.py",
    "validation.py",
)


@dataclass(frozen=True, slots=True)
class AdaptiveSourceIdentity:
    strategy_code_sha256: str
    config_sha256: str
    detector_code_sha256: str
    detector_config_sha256: str


def adaptive_source_identity(config: AdaptiveConfig = DEFAULT_CONFIG) -> AdaptiveSourceIdentity:
    require_fixed_config(config)
    source, settings = installed_source_tree_sha256(ADAPTIVE_SOURCE_FILES), config_sha256(config)
    # The regime detector and generator are one reviewed finite source closure.
    # They cannot silently acquire different versions within a single candidate.
    return AdaptiveSourceIdentity(source, settings, source, settings)


def adaptive_window_sha256(candles: Sequence[Candle]) -> str:
    """Use the existing runtime's full closed-bar window hash schema.

    Float normalization matches ClosedBarEventV1 even when an offline caller
    supplies integer-valued prices. Logical source timestamps are not arrival
    timestamps; the separate adapter owns the actual observation clock.
    """
    return canonical_sha256(
        {
            "bars": [
                {
                    "base_volume": float(row.volume),
                    "close": float(row.close),
                    "close_time_ms": row.close_time_ms,
                    "contract_version": "closed-bar.v1",
                    "high": float(row.high),
                    "is_closed": True,
                    "low": float(row.low),
                    "open": float(row.open),
                    "open_time_ms": row.open_time_ms,
                    "quote_volume": float(row.quote_volume),
                    "symbol": row.symbol,
                    "taker_buy_base_volume": float(row.taker_buy_volume),
                    "taker_buy_quote_volume": float(row.taker_buy_quote_volume),
                    "timeframe": "1m",
                    "venue": "BINANCE_UM",
                }
                for row in candles
            ]
        }
    )
