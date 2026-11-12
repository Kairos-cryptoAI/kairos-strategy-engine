# `hourly_trend_candidates_v1`

Status: research only. Not registered, not PAPER-approved. Ported on 2026-11-12 from the
live runner (`kairos-autotrader` commit `05e171f`, `kairos_autotrader/strategy.py`), which
has traded these rules on EVEDEX PROD since 2026-11-11 behind a model review.

## Why it is here

`CONTRIBUTING.md` keeps one strategy implementation for research and runtime. The runner
had its own copy of these rules. `kairos_strategy.hourly_trend` is that copy moved into the
Strategy Engine, so the runner can delete it and research can evaluate what actually
trades.

## Isolation

The package follows the precedent of `kairos_strategy.adaptive`: it is NOT inserted into
the legacy registry, the durable consumer or the runtime requirements. `registry.py` is
part of every registered strategy's source fingerprint, so registering here would change
the eleven existing source identities, Trial 15 included. They stay intact
(`tests/test_adaptive_lineage.py`). The package has its own identity,
`hourly_trend_source_identity()`, over its finite source closure.

Registering it for the service or a campaign is a separate, source-pinned step.

## Rules

Input is complete, closed one-minute bars of one symbol. A decision is made at every closed
15-minute bar.

1. **Bars.** Hourly bars are rebuilt from 15-minute bars so that the newest one ends at
   the decision. Four-hour trend bars are complete epoch-aligned groups of those hourly
   bars. The window is the hourly bars that fit in `history_hours` (504) ending at the
   decision, less the first, partial hour; the signal reads the last `signal_window_bars`
   (300) of them.
2. **Trend.** `UP` when EMA20 of the 4h closes is above EMA50, the last 4h close is above
   EMA50 and the 20-bar efficiency ratio is at least 0.2. `DOWN` is the mirror. Otherwise
   `FLAT` and no candidate. EMAs are seeded with the first value of the window.
3. **Pullback reclaim.** In an `UP` trend: one of the last three hourly bars touched the
   hourly EMA20, the newest bar closed above the EMA20 and above its open, RSI14 within
   35..68. Raw stop: lowest low of the three bars minus 0.25 ATR. `DOWN` mirrors it with
   RSI within 32..65.
4. **Breakout.** Only when the pullback condition is false: close beyond the prior 20-hour
   extreme with RSI below 75 (long) or above 25 (short). Raw stop: 1.2 ATR from the close.
5. **Exits.** Stop distance is at least 0.8 ATR; a candidate whose distance exceeds 2.5 ATR
   is dropped. Target is 1.5 times the distance. Maximum holding time 720 minutes. ATR is
   the mean true range of the last 14 hourly steps.

Bars on both sides of a gap are treated as neighbours, as in the runner; only the last two
hourly bars must be consecutive. This differs from the gap-segmented indicators of the
registered sleeves and is kept for parity with the traded behaviour.

## API

- `generate_hourly_trend_candidates_intents(candles_1m, config=None)` returns a
  `SleeveIntent` for every decision that meets the rules.
- `evaluate_hourly_trend_state(candles_1m, config=None)` returns the state the rules saw
  at the newest decision, and the intent if there is one. The state is reviewer evidence
  and has no authority.

## Evidence so far

- Parity: on identical input the package reproduces the runner's candidates and features
  (`tests/test_hourly_trend_candidates.py`, frozen fixture of 169 candidates and 1525
  evaluated decisions produced by the runner's original code).
- EVEDEX minute history, 2026-10-07..2026-11-12, ten instruments, round-trip cost 0.13%:
  2009 unfiltered candidates, mean -0.077 R, standard error 0.025 before allowing for
  dependence between neighbouring decisions and instruments.
- Signal computed on Binance USD-M one-minute bars (public REST klines, not the
  checksum-verified archives), outcome judged on EVEDEX minute bars with levels moved by
  the price ratio of the two venues, 2026-10-07..2026-11-12, the same ten instruments and
  cost: 2167 candidates, mean -0.049 R, standard error 0.024. With the signal on EVEDEX
  bars over the same period: 2142 candidates, mean -0.080 R, standard error 0.024. 93% of
  the candidates are the same decisions; the difference is inside the noise. The median
  price difference between the venues was about -1 bp.
- One live trade. No statement about an edge follows from it.

## Boundaries

- No promotion and no PAPER permission follow from this port.
- The data above is EVEDEX history, not the checksum-verified Binance archives the offline
  gate requires.
- The runner places a model review and its own risk limits behind these candidates; none
  of that is part of this package.
