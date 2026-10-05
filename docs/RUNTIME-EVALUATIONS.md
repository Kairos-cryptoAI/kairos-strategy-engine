# Runtime strategy observations

The ordinary closed-bar consumer publishes `StrategyEvaluationV1` on
`kairos.strategy.evaluation.v1`. These receipts describe what the existing
generator actually did; they are not an adaptive candidate, economic campaign,
promotion, order permission or scientific qualification. `authority` is always
`OBSERVATION_ONLY` and `trading_authority` is always false.

## States and authority

| Status | Meaning | Completed evaluation |
| --- | --- | --- |
| `INTENT` | The unchanged generator produced current-anchor directional intent bytes. | Yes |
| `NO_INTENT` | The scheduled generator succeeded with no current intent and its reviewed finite input dependencies are present. | Yes |
| `WARMUP` | Registered minimum history or actual complete-frame lookbacks are missing. | No |
| `NOT_SCHEDULED` | This closed minute is outside the registered or actual generator decision clock. | No |
| `DISABLED` | The explicit strategy list is empty, or PAPER approval is unavailable. | No |
| `UNAVAILABLE` | A quiet result has no reviewed readiness policy, history was quarantined, or a historical replay lacks an original observation. | No |
| `ERROR` | Evaluation failed; the receipt carries a sanitized exception class, not raw exception text. | No |

The empty default strategy list emits one engine-scoped `DISABLED` observation
without inventing a strategy ID. Enabled entries have one strategy-scoped
observation per accepted anchor, including the exact revision and registry
status. Explicit DRY_RUN research generation remains permitted for the same
registered strategies, including `REJECTED` entries. A `rejected` receipt does
not promote them. PAPER startup still rejects every non-approved entry; LIVE
startup remains prohibited. No registry, frozen generator, algorithm parameter,
stop/target/holding lifecycle or original `StrategyIntentV1` byte is changed.

## Readiness and causal evidence

The receipt binds the symbol, minute open/close, canonical anchor-bar hash,
explicit enabled roster and runtime policy digest. Completed observations also
bind the exact contiguous input window, source and configuration digests, and
any unchanged intent IDs with their directional sides. `decision_ts_ms` is the
closed market-bar clock. `observed_at_ms` / `produced_at` record actual local
receipt construction time; they are not PostgreSQL insertion attestations and
do not prove upstream provenance or absence of historical model lookahead.
Future bars are rejected before appending history or publishing outputs.

Six existing price sleeves reuse the causal adapter's reviewed finite seed and
lookback checks, based on actual complete UTC frames. This is not indicator
convergence or equivalence to an unspecified longer history. Quarter-hour flow
and regime-retest retain their existing research generator and actual boundary
clocks. Their directional output remains `INTENT`; their empty output is
`UNAVAILABLE/READINESS_POLICY_UNAVAILABLE`, because a simple whole-window count
does not prove phase-specific or reset/setup-state readiness. No fabricated
quiet analysis is used to fill that engineering gap.
Full frame counts with an undefined required volume denominator (for example,
zero-volume prior VWAP history) are also `UNAVAILABLE`, not a completed quiet
analysis. No valid unchanged directional result is suppressed by these checks.

## Publication, replay and recovery

`evaluation_id` / `message_id` identify one logical anchor/strategy/runtime
policy slot. The policy digest binds wrapper source, configured roster, runtime
mode/universe/window and registered generator/default-configuration digests.
Outcome, readiness, observation clock and optional diagnostic source fields do
not mint another logical slot. `receipt_sha256` separately binds the immutable
observation result. An inconsistent result for an existing logical slot must
conflict, not silently replace the original bytes.

The shell prepares outputs, publishes every observation, publishes the original
intents, and only then requests the input ACK. A failed publish or ACK retains
the exact prepared objects and retries the entire batch without reevaluating or
changing clocks/IDs. A later bar for the same symbol cannot pass its unfinished
predecessor. At-least-once transport may deliver physical duplicates with the
same exact logical ID and payload; this is not a second evaluation. A successful
exact replay produces no new outputs. Failure while preparing a receipt retains
the unobserved anchor so it cannot disappear behind an in-memory replay check.

The existing durable bus commits audit/outbox outputs and inbox completion in
one transaction before Redis ACK. No new schema or primary-DB migration is part
of this change. On a restored historical anchor the shell reads matching saved
observation/intent bytes for the exact current policy, verifies their roster and
references plus independently installed registry/source/default-configuration
fingerprints, and does not regenerate. A valid bar delivered on the wrong topic
is rejected before any history update, output or ACK. If no original receipt exists, it records
`UNAVAILABLE/HISTORICAL_REPLAY_NOT_EVALUATED`; it never backfills completed
analysis or a historical trading candidate. Changed policy/source, missing
intent bytes, duplicate roster or corrupted stored receipts are not silently
treated as a successful quiet evaluation.

`tests/test_runtime_evaluations.py` uses recording/failing buses and a type-
compatible read-only audit fake, never a DB, Redis, provider or exchange call.
These tests prove shell ordering, exact retry bytes/IDs and fail-closed recovery
validation. They are not a new native transactional, deployment or qualification
proof, and they do not reopen any approved forward-evidence or trading gate.
