from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

import pytest

from kairos_strategy.candles import Candle
from kairos_strategy.provenance import (
    canonical_json_bytes,
    canonical_sha256,
    input_window_sha256,
    source_tree_sha256,
)


def _candle(index: int) -> Candle:
    return Candle(
        symbol="BTCUSDT",
        timeframe="1m",
        open_time_ms=index * 60_000,
        close_time_ms=(index + 1) * 60_000 - 1,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=10.0,
        quote_volume=1_005.0,
        taker_buy_volume=5.0,
    )


def test_source_fingerprint_is_independent_of_line_endings_and_separator():
    windows = {"sleeves\\example.py": b"first\r\nsecond\r\n"}
    linux = {"sleeves/example.py": b"first\nsecond\n"}

    assert source_tree_sha256(windows) == source_tree_sha256(linux)


@pytest.mark.parametrize("path", ["C:\\checkout\\strategy.py", "/tmp/strategy.py", "../strategy.py"])
def test_source_fingerprint_rejects_checkout_dependent_paths(path):
    with pytest.raises(ValueError, match="package-relative"):
        source_tree_sha256({path: "pass\n"})


def test_canonical_json_has_one_stable_zero_and_key_order():
    assert canonical_json_bytes({"z": -0.0, "a": 1}) == b'{"a":1,"z":0.0}'
    assert canonical_sha256({"z": -0.0, "a": 1}) == canonical_sha256({"a": 1, "z": 0.0})


def test_input_fingerprint_is_order_independent_but_value_sensitive():
    candles = [_candle(0), _candle(1)]

    assert input_window_sha256(candles) == input_window_sha256(list(reversed(candles)))
    assert input_window_sha256(candles) != input_window_sha256(
        [candles[0], replace(candles[1], close=100.75)]
    )


def test_strategy_package_does_not_import_impure_runtime_modules():
    forbidden = {
        "aiohttp",
        "datetime",
        "httpx",
        "openai",
        "os",
        "random",
        "requests",
        "secrets",
        "socket",
        "subprocess",
        "time",
    }
    package = Path(__file__).parents[1] / "kairos_strategy"
    violations: list[str] = []
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {alias.name.split(".", maxsplit=1)[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots = {node.module.split(".", maxsplit=1)[0]}
            else:
                continue
            for name in roots & forbidden:
                violations.append(f"{path.relative_to(package)}:{node.lineno}:{name}")

    assert violations == []
