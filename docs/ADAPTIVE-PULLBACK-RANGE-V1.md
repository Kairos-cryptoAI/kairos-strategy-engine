# One selected adaptive engineering hypothesis

Selected 2026-10-06. Identity: `adaptive_pullback_range_v1`, revision `1`.
Status: **RESEARCH ONLY, NOT ECONOMICALLY QUALIFIED OR CAMPAIGN-FROZEN**.

This is one concrete candidate, not a parameter competition or a claim of
optimality, expected profitability, a fixed monthly return or a trade quota.
It combines interpretable trend pullbacks and range reclaims, with a defensive
downward-shock overlay. No asset-specific tuning, order-flow dependency,
pyramiding, trailing stop, multiple targets or learned confidence sizing.

The user-selected target is adaptive frequency including zero trades. The
algorithm emits candidates only on fresh completed patterns. It does not
generate trades to meet the 500 naturally closed simulated-trade gate.

## Causal input and scheduling

- Fixed universe: BTC/ETH/SOL/BNB/XRP USD-M reference symbols.
- Exactly the latest 3,240 contiguous, complete UTC 1m bars (54h), for ONE
  symbol at a time; aggregate complete 5m/15m/1h buckets only.
- Evaluate on each completed UTC 5m boundary. Last two complete 1h bars must
  agree on the raw regime. All indicators use finite windows, not seeded EMA
  or Wilder recursions: a 14-bar mean true range, SMA20/SMA50, ER24 and a
  three-hour SMA20 slope. A full prefix and its rolling tail are identical.
- Empty/warmup, gap, unscheduled minute, undefined volatility, unavailable
  observation and numerical error are NOT a completed `NO_INTENT`.
- Historical bar timestamps do not prove real-time availability. The strict
  adapter requires the caller's actual observation clock; future/backdated
  input and naive/future producer envelopes raise, and a receipt later than
  the 60-second lifetime is unavailable. A plausible envelope alone is still
  not proof of trusted source availability.

The detector supplies an internal per-slot outcome. The existing intent-bound
`RegimeObservationV1` is created only for a real directional candidate, never
by inventing an intent for a quiet/failed slot.

## Executable regime and entry rules

Use `A` for simple ATR14 and `M` for SMA20. ER24 is absolute net movement divided
by total absolute movement over 24 complete hourly steps; zero variation has
zero ER. Zero ATR is unavailable. A raw hourly label is:

| label | conditions |
| --- | --- |
| BULL | close > SMA20 > SMA50; ER >= .30; three-hour SMA20 slope >= .25 hourly ATR |
| BEAR | mirrored BULL inequalities and slope <= -.25 hourly ATR |
| RANGE | ER <= .20; SMA gap <= .50 hourly ATR; absolute SMA20 slope <= .10 hourly ATR |
| UNCERTAIN | anything else, or disagreeing last two complete hourly labels |

A downward 5m close change <= -2 times the ATR fixed STRICTLY BEFORE that bar
overrides the hourly label to CRASH for 12 completed 5m slots including the
shock. After expiry, normal entries remain disabled until both hourly bars
used by the detector postdate the last shock. Evaluate that overlay separately
at each pattern timestamp; never transfer a later shock into an earlier P1.

### BULL/BEAR: trend pullback and reclaim

Let P1/P2/P3 be the latest three completed 5m bars. Freeze 15m SMA20 `M` and ATR
`A` at P1's close, and 5m ATR at P1. Require the same effective BULL/BEAR label
at both P1 and P3 (including shock/cooldown, not only the hourly base).

LONG: P1 close >= M+.25A; P2 low <= M+.10A and close in [M-.25A, M+.10A];
P3 close in [M+.25A, M+.75A]; both P2/P3 lows >= M-.50A. Stop at the lower
P2/P3 low minus .25 frozen 5m ATR. SHORT mirrors every price inequality and
uses the higher high plus .25 ATR. Fixed target 2R, maximum holding 120m.

There is no recurring order merely because price stays above/below its mean:
the three-bar transition must occur again. Holdings and one-position/risk
admission are downstream and do not feed back into the market detector.

### RANGE: bounded edge reclaim

Before the current 5m bar's OPEN, freeze the previous 16 complete 15m bars'
lower/upper extrema L/U. Freeze 15m ATR `A` BEFORE those 16 bars. Require width
U-L in [2A,6A] and at least two touches within .25A on each edge, separated by
at least four 15m bars. The preceding 5m close must lie in the middle half of
the range.

LONG: current low in [L-.25A,L], current close in [L+.10A,L+.50A]. Stop at the
current low minus .25 5m ATR fixed before the bar. SHORT mirrors at U. Target
the frozen midpoint, maximum holding 60m. A new center-to-edge event is needed
to rearm; repeatedly touching the same edge is not a new order.

### CRASH: protect first; optional support-retest SHORT

Freeze L and A with the same 16-bar/prior-ATR method before the latest shock
OPEN. If the shock does not close below L-.10A, produce no short. Only the
first completed retest/rejection at shock age 3..6 bars (15..30m) can qualify:
P2 high in [L-.10A,L+.25A], P2 close in [L-.10A,L+.10A], then P3 close in
[L-.50A,L-.10A). Any intervening close above L+.25A invalidates this setup.
Stop at the higher P2/P3 high plus .25 5m ATR fixed before P2; target 2R,
maximum holding 60m. The first structural match is consumed BEFORE economics,
LLM review and fill checks: cost rejection, veto or no-fill is not a retry.

This cannot predict a crash before it happens and does not guarantee profit
during one. Fast unretested selloffs can correctly produce zero new positions.

## Barriers, costs and authority

For every sleeve, stop distance must be .50..2 frozen 15m ATR and <=300bps;
all prices must be finite, positive and directionally executable. Entry is
eligible on the next minute and expires 60s after the decision. Timeout is
measured FROM ACTUAL FIRST FILL downstream, not from the signal clock.

Planning round-trip components: 4.5bps fee per side, 2bps full spread, 1bps
slippage per side, 2bps uncertainty, 3bps adverse carry and 2bps latency =
20bps. These are development assumptions, NOT current EVEDEX venue facts or
evidence that the eventual execution/model/feed costs fit them.

For entry reference E, stop S, target T and C=.002, the existing conservative
research arithmetic is retained: loss/unit = |E-S| + max(E,S)*C;
net reward/unit = max(0, |T-E| - max(E,T)*C). Require net reward/loss >=1.25
and planning cost <=.50 of gross stop-distance bps. The exact side-specific
arithmetic determines the boundary; no rounded minimum stop is substituted.
This is geometric feasibility, not expected value or empirically positive alpha.

Actual fill displacement, latency, spread/slippage/funding, source freshness,
model/feed dollars and venue liquidity remain independently measured gates.
The strategy does not size positions or waive them. Gap losses are not bounded
by simply declaring a stop. Existing deterministic risk ceilings stay at
0.25% per trade and 1% total open risk, without a regime/LLM confidence uplift.

## Integration and remaining qualification

Pure API: `evaluate_adaptive`, `generate_adaptive_intents`. Explicit opt-in
contract adapter: `evaluate_adaptive_closed_bars`, with a mandatory source-set
SHA and actual observation clock, returning strict candidate/evaluation/regime
evidence. Its capability policy is research-only; independently accepted frozen
pins and ordinary Risk admission are still required. No service is started.

The new namespace deliberately is NOT inserted into the legacy registry or
durable consumer. Existing 11 source/config fingerprints, Trial 15 and V4/V5
stay intact. Adding this candidate to the accepted durable adaptive publisher,
account-bound Macro policy and campaign is a separate source-pinned step.

Review continues to return only ALLOW/VETO/DEFER for the unchanged candidate.
Independent LLM proposals remain a separate arm, including quiet strategy
slots. This module neither calls models nor validates their economic edge.
Historical fitted labels, simulation engineering passes and research mappings
do not establish real-time news/model/venue qualification.

Next: bounded point-in-time economic replay on bull/range/bear/crash development
data, identical causal inputs and fills across three arms with actual delays
and all costs. Then, if eligible, freeze ONE own strategy/evaluator campaign
with the approved gates, at least 365 genuinely future days, 500 natural closes
and one sealed evaluator. No Trial 15 days transfer. Both required sealed
gates and separate venue/security/production checks remain mandatory.

Research motivation is NOT alpha evidence: the long-horizon
[time-series momentum study](https://docs.lhpedersen.com/TimeSeriesMomentum.pdf)
does not validate these intraday crypto rules. The
[probability-of-backtest-overfitting paper](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf)
motivates avoiding parameter competitions; selecting one human-designed
hypothesis still records prior research exposure, not immunity to overfitting.

All release readiness flags remain false and strategy policy is REJECT_ALL.
