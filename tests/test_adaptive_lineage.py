"""Regression guards for legacy strategy identities during adaptive work."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kairos_strategy.provenance import config_sha256, installed_source_tree_sha256
from kairos_strategy.registry import (
    ALLOCATION_STRATEGIES,
    CONTEXTUAL_STRATEGIES,
    STRATEGIES,
    get_strategy,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "adaptive_v1_legacy_sources.json"


def _legacy_definitions():
    return {
        **STRATEGIES,
        **CONTEXTUAL_STRATEGIES,
        **ALLOCATION_STRATEGIES,
    }


def test_installed_legacy_source_and_config_fingerprints_match_baseline():
    expected = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    definitions = _legacy_definitions()

    assert set(definitions) == set(expected)
    for strategy_id, definition in definitions.items():
        assert installed_source_tree_sha256(definition.source_files) == expected[strategy_id]["source"]
        assert config_sha256(definition.config_type()) == expected[strategy_id]["config"]


def test_adaptive_identity_is_not_in_legacy_registry_or_paper_allowlist():
    with pytest.raises(KeyError, match="unknown strategy_id: adaptive_pullback_range_v1"):
        get_strategy("adaptive_pullback_range_v1")

    assert all(not definition.paper_enabled for definition in _legacy_definitions().values())
