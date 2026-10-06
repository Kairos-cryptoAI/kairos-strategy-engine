"""Fail-closed contract tests for the isolated adaptive observation adapter."""

from __future__ import annotations

from dataclasses import replace

import pytest
from kairos_core.contracts.base import datetime_from_unix_ms
from kairos_core.contracts.regime_capability import (
    CapabilityRegime,
    StrategyRegimeCapabilityV1,
)
from kairos_core.enums import Side, TradingMode

from kairos_strategy.adaptive import (
    DEFAULT_CONFIG,
    adaptive_source_identity,
    build_adaptive_capability_policy,
    evaluate_adaptive_closed_bars,
)
from kairos_strategy.adaptive.provenance import adaptive_window_sha256
from kairos_strategy.candles import Candle
from kairos_strategy.registry import PaperStrategyDisabledError
from kairos_strategy.runtime import candle_to_closed_bar

_SOURCE_SET = "a" * 64
_MINUTE_MS = 60_000


def _candles(
    *, count: int = DEFAULT_CONFIG.history_bars, start_ms: int = 0, zero_atr: bool = False
) -> list[Candle]:
    rows = []
    for index in range(count):
        timestamp = start_ms + index * _MINUTE_MS
        center = 100.0
        spread = 0.0 if zero_atr else 0.1
        rows.append(
            Candle(
                symbol="BTCUSDT",
                timeframe="1m",
                open_time_ms=timestamp,
                close_time_ms=timestamp + _MINUTE_MS - 1,
                open=center,
                high=center + spread,
                low=center - spread,
                close=center,
                volume=1.0,
                quote_volume=100.0,
                taker_buy_volume=0.5,
                taker_buy_quote_volume=50.0,
            )
        )
    return rows


def _bars(**kwargs):
    return tuple(candle_to_closed_bar(row) for row in _candles(**kwargs))


def _evaluate(bars, *, observed_at_ms=None, source_set_sha256=_SOURCE_SET, trading_mode=TradingMode.DRY_RUN):
    clock = max(bar.close_time_ms for bar in bars) if observed_at_ms is None else observed_at_ms
    return evaluate_adaptive_closed_bars(
        bars,
        observed_at_ms=clock,
        source_set_sha256=source_set_sha256,
        trading_mode=trading_mode,
    )


@pytest.mark.parametrize("mode", [TradingMode.PAPER, TradingMode.LIVE])
def test_paper_and_live_are_rejected(mode):
    with pytest.raises(PaperStrategyDisabledError):
        _evaluate(_bars(), trading_mode=mode)


def test_warmup_is_not_a_completed_no_intent_evaluation():
    result = _evaluate(_bars(count=DEFAULT_CONFIG.history_bars - 1))
    assert result.evaluation.status == "WARMUP"
    assert not result.evaluation.evaluation_complete
    assert result.evaluation.status != "NO_INTENT"
    assert result.intent is None


def test_unscheduled_bar_is_not_no_intent():
    result = _evaluate(_bars(start_ms=_MINUTE_MS))
    assert result.evaluation.status == "NOT_SCHEDULED"
    assert not result.evaluation.evaluation_complete
    assert result.evaluation.status != "NO_INTENT"
    assert result.intent is None


def test_gap_is_unavailable_and_not_complete():
    bars = list(_bars())
    bars.pop(100)
    result = _evaluate(tuple(bars))
    assert result.evaluation.status == "UNAVAILABLE"
    assert result.evaluation.reason_code == "CLOSED_HISTORY_GAP"
    assert not result.evaluation.evaluation_complete
    assert result.intent is None


def test_flat_zero_atr_is_unavailable():
    result = _evaluate(_bars(zero_atr=True))
    assert result.evaluation.status == "UNAVAILABLE"
    assert result.evaluation.reason_code == "UNDEFINED_VOLATILITY_CONTEXT"
    assert not result.evaluation.evaluation_complete
    assert result.intent is None


def test_late_observation_is_unavailable_not_no_intent():
    bars = _bars()
    result = _evaluate(bars, observed_at_ms=bars[-1].close_time_ms + DEFAULT_CONFIG.entry_lifetime_ms + 1)
    assert result.evaluation.status == "UNAVAILABLE"
    assert result.evaluation.reason_code == "LATE_OBSERVATION"
    assert not result.evaluation.evaluation_complete
    assert result.intent is None


def test_backdated_observation_clock_and_malformed_source_digest_are_rejected():
    bars = _bars()
    with pytest.raises(ValueError, match="earlier than its closed input"):
        _evaluate(bars, observed_at_ms=bars[-1].close_time_ms - 1)
    with pytest.raises(ValueError, match="source-set SHA-256"):
        _evaluate(bars, source_set_sha256="A" * 64)


def test_duplicate_and_mixed_symbol_inputs_are_rejected():
    bars = list(_bars())
    with pytest.raises(ValueError):
        _evaluate(tuple([*bars[:-1], bars[-2]]))
    mixed = list(bars)
    mixed[100] = candle_to_closed_bar(replace(_candles()[100], symbol="ETHUSDT"))
    with pytest.raises(ValueError):
        _evaluate(tuple(mixed))


def test_shuffled_replay_has_identical_decision_receipt_and_lineage():
    bars = _bars()
    ordered = _evaluate(bars)
    replayed = _evaluate(tuple(reversed(bars)))
    assert replayed == ordered
    assert ordered.evaluation.input_window_sha256 == adaptive_window_sha256(_candles())
    identity = adaptive_source_identity()
    assert ordered.evaluation.strategy_code_sha256 == identity.strategy_code_sha256
    assert ordered.evaluation.config_sha256 == identity.config_sha256


def test_policy_maps_exact_four_regimes_and_forbids_uncertain_capability():
    policy = build_adaptive_capability_policy(_SOURCE_SET)
    assert {item.regime for item in policy.capabilities} == {
        CapabilityRegime.BULL,
        CapabilityRegime.BEAR,
        CapabilityRegime.RANGE,
        CapabilityRegime.CRASH,
    }
    assert len(policy.capabilities) == 4
    template = policy.capabilities[0]
    with pytest.raises(ValueError, match="uncertainty and FLAT"):
        StrategyRegimeCapabilityV1(
            strategy_id=template.strategy_id,
            strategy_revision=template.strategy_revision,
            strategy_code_sha256=template.strategy_code_sha256,
            config_sha256=template.config_sha256,
            regime=CapabilityRegime.UNCERTAIN,
            sides=(Side.LONG,),
        )


@pytest.mark.parametrize("naive", (False, True))
def test_ambiguous_producer_envelope_is_rejected_not_relabelled_as_timely_input(naive):
    bars = list(_bars())
    clock = bars[-1].close_time_ms
    producer_clock = (
        datetime_from_unix_ms(clock).replace(tzinfo=None) if naive else datetime_from_unix_ms(clock + 1)
    )
    bars[100] = bars[100].model_copy(update={"produced_at": producer_clock})
    with pytest.raises(ValueError, match="future or naive producer envelope"):
        _evaluate(bars, observed_at_ms=clock)
