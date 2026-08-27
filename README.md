# kairos-strategy-engine

Deterministic strategy source of truth shared by Kairos offline research and
runtime candidate generation. The package also provides the thin
`kairos-strategy-engine` durable consumer used in deployment.

## Safety boundary

The generator modules contain only pure transformations from complete, closed
market bars, explicitly supplied immutable factor observations and an immutable
configuration to ordered strategy candidates. They contain no LLM client, exchange client, environment-secret
reads, wall-clock reads, or randomness. The same input bytes and configuration
therefore produce the same candidate bytes and IDs on Windows and Linux. The
package owns its minimal closed-bar value and indicator primitives, so research
does not depend on a collector or runtime service.

The service shell is intentionally separate from those pure modules. It reads
strict `ClosedBarEventV1` messages through the transactional inbox/outbox,
restores its bounded bar windows from the immutable PostgreSQL audit log, and
publishes only strict `StrategyIntentV1` messages. A gap, reorder, or conflicting
bar blocks that symbol. PAPER requires Redis/PostgreSQL, rejects LIVE authority,
and defaults to an empty strategy set. Because no existing sleeve is
`PAPER_APPROVED`, attempting to enable any of them in PAPER is a startup error.
Deploy the consumer with the `runtime` extra; research/backtest installations
do not pull the persistence or message-bus runtime.

Every currently registered sleeve is `REJECTED`, pre-gate `RESEARCH`,
`FORWARD_FROZEN`, or `INCONCLUSIVE` after a consumed evaluation that produced no strategy result. Calling
`generate_sleeve_intents(..., for_paper=True)` fails closed; this repository
does not authorize a strategy for PAPER or LIVE trading. Promotion evidence is
owned by `kairos-backtest`, and a future status change requires a separate,
reviewed commit after the offline gate passes.

## Owned generators

| Strategy ID | Revision | Status |
| --- | --- | --- |
| `trend_breakout_v1` | `1` | `REJECTED` |
| `trend_pullback_reclaim_v1` | `1` | `REJECTED` |
| `range_mean_reversion_v1` | `1` | `REJECTED` |
| `orderflow_volatility_expansion_v1` | `1` | `REJECTED` |
| `regime_veto_retest_reclaim_v1` | `1` | `REJECTED` |
| `quarter_hour_flow_v1` | `1` | `RESEARCH` |
| `right_tail_trend_v1` | `1` | `REJECTED` |
| `regime_aligned_right_tail_v1` | `1` | `FORWARD_FROZEN` |
| `crowded_trend_continuation_v1` | `1` | `REJECTED` |
| `donchian_ensemble_long_v1` | `1` | `INCONCLUSIVE` |
| `four_hour_sma200_long_v1` | `1` | `REJECTED` |

`quarter_hour_flow_v1` is a causal one-minute proxy for the first-ten-second
[quarter-hour order-flow effect documented by Kim and Hansen (2026)](https://arxiv.org/abs/2607.09426).
The proxy,
thresholds and fixed lifecycle are deliberately frozen before its reused-data
screen; the paper's result is not treated as Kairos alpha evidence.

`right_tail_trend_v1` is a separately motivated post-anatomy candidate. The
failed `market_anatomy_v1` gate was not reinterpreted as prototype authorization:
every archive used there remained reused development data. Once per UTC day the
candidate used the frozen 24-hour return-to-realized-variation score, with a
symmetric 2 ATR stop, 4R target and 72-hour timeout. Its consumed one-shot screen
was rejected because robustness stress profit factor was `1.0382706977`, below
the preregistered strict `>1.05` gate. No parameter was changed afterward and
the exact candidate remains blocked from PAPER.

`regime_aligned_right_tail_v1` is the separately registered trial-15 synthesis.
It preserves the exact daily right-tail signal and 2 ATR / 4R / 72-hour
lifecycle, then admits a long only above the last complete four-hour SMA200 and
a short only below it. The regime state cannot change side, size or exits. It
passed every absolute and base-improvement gate in its one-shot reused-data
screen. The exact source is therefore `FORWARD_FROZEN` for observations
beginning no earlier than 2026-09-01, but still fails closed for PAPER. Since
both source mechanisms and all archives through July 2026 had already been
observed, the result cannot establish alpha.

The runtime shell requires at least `48,000` contiguous one-minute bars for
this sleeve (200 complete four-hour bars) and evaluates it only at its UTC
daily decision boundary. Startup fails when the configured window is smaller,
instead of running indefinitely with a silent zero-signal history.

`crowded_trend_continuation_v1` is a post-hoc contextual candidate prompted by
the descriptive `derivatives_state_v1` study. It retains the study's exact
global thresholds: absolute 24-hour trend score at least `1`, open-interest
growth at least `5%`, and either direction-aligned premium at least `5 bps` or
funding at least `1 bp`. It evaluates every complete UTC hour, uses the prior
candidate's fixed 2 ATR / 4R geometry, and times out at the study's exact
24-hour outcome horizon. Its explicit factor-input registry is separate from
the price-only runtime registry and remains fail-closed for PAPER. Reused-data
evaluation may only reject it or freeze it for genuinely future evidence.
Its consumed screen was rejected: stress profit factor was `1.0499` and
`1.0458` against the preregistered strict `>1.05` gate, and each window had
fewer than 25 short trades. Positive aggregate returns do not override those
failures; the exact candidate remains blocked from PAPER.

`donchian_ensemble_long_v1` is a pure target-allocation implementation of the
long-only model published by Zarattini, Pagani and Barbon (2025). It combines
5/10/20/30/60/90/150/250/360-day close-based Donchian channels, monotonic
mid-channel stops and a 90-day volatility target of 25% capped at 2x. A relative
20% deadband applies only to volatility-driven resizing; signal entries and
exits update immediately. Targets decided at one UTC daily close become
effective on the next day. The allocation registry is separate from order
intents because the current PAPER lifecycle cannot yet execute dynamic target
weights or daily moving stops; `for_paper=True` therefore fails closed.
Its only preregistered reused-data attempt stopped on a checksum-verified
official Binance row whose taker-buy volume exceeds total volume. No portfolio
metric was persisted, so the model is neither accepted nor performance-rejected;
the attempt remains consumed and the registry records `INCONCLUSIVE`. The exact
[failure evidence](https://github.com/Kairos-cryptoAI/kairos-backtest/blob/main/reports/donchian-screen/REPORT.md)
remains fail-closed for PAPER.

The historical module paths in `kairos-backtest` are compatibility façades.
They re-export these exact modules and classes rather than maintaining copies.

## Determinism and provenance

Candidate provenance separates four fingerprints:

- normalized strategy source tree;
- immutable strategy configuration;
- complete ordered one-minute input window;
- explicit derived-feature evidence.

Source fingerprints use package-relative names and normalize CRLF/CR to LF.
Absolute checkout paths are rejected, so drive letters and install prefixes do
not affect a fingerprint. Canonical JSON is UTF-8, sorted, compact, rejects
non-finite numbers, and normalizes negative zero.

## Development

Install [uv](https://docs.astral.sh/uv/) and run:

```powershell
uv sync --locked
uv run --locked ruff check kairos_strategy tests
uv run --locked ruff format --check kairos_strategy tests
uv run --locked mypy kairos_strategy
uv run --locked bandit -q -r kairos_strategy
uv run --locked pytest -q --tb=short
uv build --no-sources
```

Part of the [Kairos](https://github.com/Kairos-cryptoAI/kairos) system. MIT licensed.
