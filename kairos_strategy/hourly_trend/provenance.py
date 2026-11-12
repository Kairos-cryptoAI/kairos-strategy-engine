"""Own source identity; none of the legacy source files or registries change."""

from __future__ import annotations

from dataclasses import dataclass

from ..provenance import config_sha256, installed_source_tree_sha256
from .rules import STRATEGY_ID, STRATEGY_REVISION, HourlyTrendCandidatesConfig

# The finite source closure of the rules: their own files and every package file they import.
HOURLY_TREND_SOURCE_FILES = (
    "candles.py",
    "hourly_trend/__init__.py",
    "hourly_trend/provenance.py",
    "hourly_trend/rules.py",
    "models.py",
    "provenance.py",
    "timeframes.py",
    "validation.py",
)


@dataclass(frozen=True, slots=True)
class HourlyTrendSourceIdentity:
    strategy_id: str
    revision: str
    strategy_code_sha256: str
    config_sha256: str


def hourly_trend_source_identity(
    config: HourlyTrendCandidatesConfig | None = None,
) -> HourlyTrendSourceIdentity:
    """Fingerprints a consumer records next to every decision it takes from these rules."""

    settings = HourlyTrendCandidatesConfig() if config is None else config
    if not isinstance(settings, HourlyTrendCandidatesConfig):
        raise ValueError("config must be a HourlyTrendCandidatesConfig or None")
    return HourlyTrendSourceIdentity(
        strategy_id=STRATEGY_ID,
        revision=STRATEGY_REVISION,
        strategy_code_sha256=installed_source_tree_sha256(HOURLY_TREND_SOURCE_FILES),
        config_sha256=config_sha256(settings),
    )


__all__ = ["HOURLY_TREND_SOURCE_FILES", "HourlyTrendSourceIdentity", "hourly_trend_source_identity"]
