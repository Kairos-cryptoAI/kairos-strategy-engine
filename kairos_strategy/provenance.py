"""Canonical, platform-independent fingerprints for strategy generation."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from enum import Enum
from importlib import resources
from pathlib import PurePosixPath
from typing import Any

from .candles import Candle
from .validation import canonical_candles


def _canonical_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _canonical_value(asdict(value))
    if isinstance(value, Enum):
        return _canonical_value(value.value)
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("canonical mappings require string keys")
        return {key: _canonical_value(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical JSON cannot contain non-finite numbers")
        return 0.0 if value == 0 else value
    raise TypeError(f"unsupported canonical value type: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    """Encode supported values into the sole byte representation used for IDs."""

    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _logical_source_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or ":" in normalized
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise ValueError("source keys must be normalized package-relative paths")
    return path.as_posix()


def _normalized_source_bytes(value: str | bytes) -> bytes:
    text = value.decode("utf-8") if isinstance(value, bytes) else value
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def source_tree_sha256(sources: Mapping[str, str | bytes]) -> str:
    """Hash logical source names and LF-normalized UTF-8 contents.

    Absolute paths are intentionally rejected. This prevents a checkout drive,
    installation prefix, or Windows path separator from changing provenance.
    """

    normalized: dict[str, bytes] = {}
    for name, content in sources.items():
        logical = _logical_source_path(name)
        if logical in normalized:
            raise ValueError(f"duplicate normalized source path: {logical}")
        normalized[logical] = _normalized_source_bytes(content)
    if not normalized:
        raise ValueError("at least one source file is required")

    digest = hashlib.sha256()
    for logical in sorted(normalized):
        name_bytes = logical.encode("utf-8")
        content = normalized[logical]
        digest.update(len(name_bytes).to_bytes(4, "big"))
        digest.update(name_bytes)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def installed_source_tree_sha256(relative_paths: Sequence[str]) -> str:
    """Fingerprint installed package files using only stable logical names."""

    package = resources.files("kairos_strategy")
    sources = {
        _logical_source_path(path): package.joinpath(*PurePosixPath(path).parts).read_bytes()
        for path in relative_paths
    }
    return source_tree_sha256(sources)


def candle_payload(candle: Candle) -> dict[str, str | int | float]:
    return {
        "close": candle.close,
        "close_time_ms": candle.close_time_ms,
        "high": candle.high,
        "low": candle.low,
        "open": candle.open,
        "open_time_ms": candle.open_time_ms,
        "quote_volume": candle.quote_volume,
        "symbol": candle.symbol,
        "taker_buy_volume": candle.taker_buy_volume,
        "taker_buy_quote_volume": candle.taker_buy_quote_volume,
        "timeframe": candle.timeframe,
        "volume": candle.volume,
    }


def input_window_sha256(candles: Sequence[Candle]) -> str:
    ordered = canonical_candles(candles, expected_timeframe="1m")
    return canonical_sha256([candle_payload(candle) for candle in ordered])


def config_sha256(config: object) -> str:
    if not is_dataclass(config) or isinstance(config, type):
        raise TypeError("strategy config must be a dataclass instance")
    return canonical_sha256(config)


def features_sha256(features: Mapping[str, object]) -> str:
    return canonical_sha256(features)
