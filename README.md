# kairos-strategy-engine

Deterministic strategy source of truth shared by Kairos offline research and
runtime candidate generation. The package also provides the thin
`kairos-strategy-engine` durable consumer used in deployment.

## Safety boundary

The generator modules contain only pure transformations from complete, closed
market bars plus an explicit immutable configuration to ordered strategy
candidates. They contain no LLM client, exchange client, environment-secret
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

Every currently registered sleeve is either `REJECTED` or pre-gate `RESEARCH`. Calling
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
| `right_tail_trend_v1` | `1` | `RESEARCH` |

`quarter_hour_flow_v1` is a causal one-minute proxy for the first-ten-second
[quarter-hour order-flow effect documented by Kim and Hansen (2026)](https://arxiv.org/abs/2607.09426).
The proxy,
thresholds and fixed lifecycle are deliberately frozen before its reused-data
screen; the paper's result is not treated as Kairos alpha evidence.

`right_tail_trend_v1` is the first post-anatomy candidate. Once per UTC day it
uses the frozen 24-hour return-to-realized-variation score measured by
`market_anatomy_v1`, with a symmetric 2 ATR stop, 4R target and 72-hour timeout.
The intentionally small parameter surface tests positive-skew trend capture,
not another indicator conjunction. Its defaults are fixed before any new
post-July-2026 archive is opened and it remains blocked from PAPER.

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
