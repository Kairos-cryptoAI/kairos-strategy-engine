"""Numerically stable deterministic indicators needed by strategy code."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np


def _positive_period(period: int, *, name: str) -> None:
    if isinstance(period, bool) or not isinstance(period, (int, np.integer)) or period <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _stable_mean(values: np.ndarray, *, name: str) -> float:
    scale = float(np.max(np.abs(values)))
    if scale == 0:
        return 0.0
    result = float(np.mean(values / scale) * scale)
    if not np.isfinite(result):
        raise ValueError(f"{name} arithmetic exceeds the finite numeric range")
    return result


def ema(values: Sequence[float] | np.ndarray, period: int) -> np.ndarray:
    """EMA seeded by the first full-period SMA, preserving a NaN warm-up."""

    _positive_period(period, name="EMA period")
    arr = np.asarray(values, dtype=float)
    if arr.ndim != 1:
        raise ValueError("EMA values must be one-dimensional")
    if arr.size == 0:
        return arr.copy()
    if np.any(np.isinf(arr)):
        raise ValueError("EMA values must not contain infinite values")
    finite_indices = np.flatnonzero(np.isfinite(arr))
    if finite_indices.size == 0:
        return np.full_like(arr, np.nan)
    start = int(finite_indices[0])
    if not np.all(np.isfinite(arr[start:])):
        raise ValueError("EMA values must be finite after any leading NaN warm-up prefix")

    alpha = 2.0 / (period + 1.0)
    out = np.full_like(arr, np.nan)
    finite_values = arr[start:]
    if finite_values.size < period:
        return out
    seed = _stable_mean(finite_values[:period], name="EMA")
    seed_index = start + period - 1
    out[seed_index] = seed
    previous = seed
    for index in range(seed_index + 1, arr.size):
        previous = alpha * arr[index] + (1 - alpha) * previous
        out[index] = previous
    return out
