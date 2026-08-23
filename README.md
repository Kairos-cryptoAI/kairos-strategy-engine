# kairos-strategy-engine

Deterministic strategy source of truth shared by Kairos offline research and
runtime candidate generation.

## Safety boundary

This package contains only pure transformations from complete, closed market
bars plus an explicit immutable configuration to ordered strategy candidates.
It contains no LLM client, exchange client, network calls, environment-secret
reads, wall-clock reads, or randomness. The same input bytes and configuration
therefore produce the same candidate bytes and IDs on Windows and Linux.
The package owns its minimal closed-bar value and indicator primitives, so it
does not depend on a collector or runtime service.

Every currently registered sleeve is `REJECTED`. Calling
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
