from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace

import pytest
from kairos_core.bus import BusEnvelope, MessageBus
from kairos_core.enums import TradingMode
from kairos_core.topics import Topics

from kairos_strategy.candles import Candle
from kairos_strategy.config import StrategyEngineSettings
from kairos_strategy.runtime import (
    ClosedBarSequenceError,
    candle_to_closed_bar,
    generate_runtime_strategy_intents,
)
from kairos_strategy.service import StrategyEngineService
from kairos_strategy.sleeves import RangeMeanReversionConfig


class RecordingBus(MessageBus):
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict]] = []

    async def publish(self, topic: str, message) -> str:
        payload = self._to_payload(message)
        self.messages.append((topic, payload))
        return payload["message_id"]

    def subscribe(
        self,
        topic: str,
        *,
        group: str | None = None,
        consumer: str | None = None,
    ) -> AsyncIterator[BusEnvelope]:
        raise NotImplementedError

    async def ack(self, topic: str, envelope: BusEnvelope, *, group: str | None = None) -> None:
        raise NotImplementedError


def _candles() -> tuple[Candle, ...]:
    closes = [100 + (index % 2) * 0.2 for index in range(40)]
    closes[-2:] = [96.0, 98.0]
    rows: list[Candle] = []
    for aggregate_bar, close in enumerate(closes):
        for minute in range(5):
            index = aggregate_bar * 5 + minute
            rows.append(
                Candle(
                    symbol="BTCUSDT",
                    timeframe="1m",
                    open_time_ms=index * 60_000,
                    close_time_ms=(index + 1) * 60_000 - 1,
                    open=close,
                    high=close + 0.2,
                    low=close - 0.2,
                    close=close,
                    volume=10.0,
                    quote_volume=10.0 * close,
                    taker_buy_volume=5.0,
                    taker_buy_quote_volume=5.0 * close,
                )
            )
    return tuple(rows)


def _range_config() -> RangeMeanReversionConfig:
    return RangeMeanReversionConfig(
        vwap_lookback_bars=3,
        atr_period=2,
        regime_lookback_hours=2,
        maximum_regime_efficiency=1,
        maximum_abs_hourly_slope=1,
        band_atr_multiple=0.5,
        stop_atr_multiple=1,
        max_hold_bars=6,
    )


def _settings(**overrides) -> StrategyEngineSettings:
    return StrategyEngineSettings(
        bus_backend="memory",
        trading_symbols=["BTCUSDT"],
        window_bars=300,
        **overrides,
    )


def test_paper_is_reject_all_and_live_is_never_authorized():
    paper = StrategyEngineSettings(
        trading_mode=TradingMode.PAPER,
        bus_backend="redis",
        enabled_strategy_ids=[],
    )
    assert paper.enabled_strategy_ids == []

    with pytest.raises(ValueError, match="non-approved"):
        StrategyEngineSettings(
            trading_mode=TradingMode.PAPER,
            bus_backend="redis",
            enabled_strategy_ids=["range_mean_reversion_v1"],
        )
    with pytest.raises(ValueError, match="durable"):
        StrategyEngineSettings(trading_mode=TradingMode.PAPER, bus_backend="memory")
    with pytest.raises(ValueError, match="LIVE"):
        StrategyEngineSettings(trading_mode=TradingMode.LIVE)


def test_gap_conflict_and_reorder_permanently_block_the_symbol():
    bars = tuple(candle_to_closed_bar(candle) for candle in _candles()[:3])
    service = StrategyEngineService(_settings(), bus=RecordingBus())
    assert service._append_or_replay(bars[0])
    assert not service._append_or_replay(bars[0])

    with pytest.raises(ClosedBarSequenceError, match="gap or reorder"):
        service._append_or_replay(bars[2])
    assert "BTCUSDT" in service.blocked_symbols
    with pytest.raises(ClosedBarSequenceError, match="is blocked"):
        service._append_or_replay(bars[1])

    conflict_service = StrategyEngineService(_settings(), bus=RecordingBus())
    assert conflict_service._append_or_replay(bars[0])
    conflicting_candle = replace(_candles()[0], close=99.9, low=99.7)
    conflicting_bar = candle_to_closed_bar(conflicting_candle)
    with pytest.raises(ClosedBarSequenceError, match="conflicting"):
        conflict_service._append_or_replay(conflicting_bar)


def test_runtime_shell_publishes_exactly_the_pure_generator_output(monkeypatch):
    bars = tuple(candle_to_closed_bar(candle) for candle in _candles())
    expected = generate_runtime_strategy_intents("range_mean_reversion_v1", bars, _range_config())
    assert len(expected) == 1

    bus = RecordingBus()
    service = StrategyEngineService(
        _settings(enabled_strategy_ids=["range_mean_reversion_v1"]),
        bus=bus,
    )
    for bar in bars[:-1]:
        service._append_or_replay(bar)

    original = generate_runtime_strategy_intents

    def configured_generator(strategy_id, history, config=None, *, for_paper=False):
        assert config is None
        return original(strategy_id, history, _range_config(), for_paper=for_paper)

    monkeypatch.setattr("kairos_strategy.service.generate_runtime_strategy_intents", configured_generator)
    emitted = asyncio.run(service.process_bar(bars[-1]))

    assert [intent.model_dump(mode="json") for intent in emitted] == [
        intent.model_dump(mode="json") for intent in expected
    ]
    assert bus.messages == [(Topics.STRATEGY_INTENT, expected[0].to_payload())]


def test_empty_strategy_set_consumes_valid_bars_without_emitting_candidates():
    bus = RecordingBus()
    service = StrategyEngineService(_settings(), bus=bus)
    emitted = asyncio.run(service.process_bar(candle_to_closed_bar(_candles()[0])))
    assert emitted == ()
    assert bus.messages == []


def test_durable_restore_accepts_an_older_backfill_prefix_but_still_blocks_live_reorder():
    bars = tuple(candle_to_closed_bar(candle) for candle in _candles()[:5])
    service = StrategyEngineService(_settings(), bus=RecordingBus())

    # Simulate a bounded Postgres restore whose retained window starts after
    # the producer's deterministic REST-backfill prefix.
    assert service._append_or_replay(bars[2])
    assert service._append_or_replay(bars[3])
    service._restored_through_ms = {"BTCUSDT": bars[3].open_time_ms}

    assert not service._append_or_replay(bars[0])
    assert not service._append_or_replay(bars[1])
    assert not service._append_or_replay(bars[2])
    assert service._append_or_replay(bars[4])

    # Once processing passes the restore watermark, an out-of-order live bar
    # remains a hard integrity violation.
    reordered = replace(
        _candles()[4],
        open_time_ms=bars[4].open_time_ms + 2 * 60_000,
        close_time_ms=bars[4].close_time_ms + 2 * 60_000,
    )
    with pytest.raises(ClosedBarSequenceError, match="gap or reorder"):
        service._append_or_replay(candle_to_closed_bar(reordered))


def test_durable_restore_quarantines_only_the_conflicting_symbol():
    btc_candles = _candles()[:3]
    btc_bars = tuple(candle_to_closed_bar(candle) for candle in btc_candles)
    conflicting_btc = candle_to_closed_bar(replace(btc_candles[0], close=99.9, low=99.7))
    eth_bars = tuple(
        candle_to_closed_bar(replace(candle, symbol="ETHUSDT")) for candle in btc_candles[:2]
    )
    settings = StrategyEngineSettings(
        bus_backend="memory",
        trading_symbols=["BTCUSDT", "ETHUSDT"],
        window_bars=300,
    )
    service = StrategyEngineService(settings, bus=RecordingBus())

    service._restore_payloads(
        [
            btc_bars[0].model_dump(mode="json"),
            conflicting_btc.model_dump(mode="json"),
            btc_bars[1].model_dump(mode="json"),
            eth_bars[0].model_dump(mode="json"),
            eth_bars[1].model_dump(mode="json"),
        ]
    )

    assert service.blocked_symbols == {
        "BTCUSDT": f"conflicting closed bar at {btc_bars[0].open_time_ms}"
    }
    assert [bar.open_time_ms for bar in service._bars["ETHUSDT"]] == [
        eth_bars[0].open_time_ms,
        eth_bars[1].open_time_ms,
    ]
    assert service._restored_through_ms == {"ETHUSDT": eth_bars[1].open_time_ms}
