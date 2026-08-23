"""Fail-closed runtime settings for deterministic strategy generation."""

from __future__ import annotations

from typing import Self

from kairos_core.config import CoreSettings
from kairos_core.enums import TradingMode
from pydantic import Field, field_validator, model_validator

from .registry import get_strategy


class StrategyEngineSettings(CoreSettings):
    """Runtime shell configuration; strategy functions themselves remain pure."""

    service_name: str = "kairos-strategy-engine"
    trading_mode: TradingMode = TradingMode.DRY_RUN
    enabled_strategy_ids: list[str] = Field(default_factory=list)
    window_bars: int = Field(default=1_440, ge=2, le=10_080)

    @field_validator("enabled_strategy_ids")
    @classmethod
    def validate_strategy_ids(cls, value: list[str]) -> list[str]:
        normalized = sorted(set(value))
        if len(normalized) != len(value):
            raise ValueError("enabled_strategy_ids must be unique")
        for strategy_id in normalized:
            if not strategy_id or strategy_id != strategy_id.strip():
                raise ValueError("strategy IDs must be normalized non-empty strings")
            get_strategy(strategy_id)
        return normalized

    @model_validator(mode="after")
    def validate_authority(self) -> Self:
        if self.trading_mode is TradingMode.LIVE:
            raise ValueError("strategy-engine does not authorize LIVE trading")
        if self.trading_mode is TradingMode.PAPER:
            if self.environment.casefold() == "prod":
                raise ValueError("PAPER strategy-engine cannot run in a production environment")
            if self.bus_backend == "memory":
                raise ValueError("PAPER strategy-engine requires the durable Redis/PostgreSQL bus")
            disabled = [
                strategy_id
                for strategy_id in self.enabled_strategy_ids
                if not get_strategy(strategy_id).paper_enabled
            ]
            if disabled:
                raise ValueError(
                    "PAPER strategy-engine cannot enable non-approved strategies: " + ", ".join(disabled)
                )
        return self
