# DayTrade point-in-time feature pipeline

## Objective

Complete the preregistered H3/H4 DayTrade experiments without allowing a
decision to observe a later bar, a later session, or an untracked benchmark
dataset. January-April 2024 remains research data. The selected configuration
will be frozen before May-August 2024 is loaded for holdout evaluation.

## Data contract

- Trade symbols and feature-only symbols are separate. SPY, QQQ, and explicit
  sector/industry proxy ETFs may produce features but may never enter the
  tradable universe.
- Every feature snapshot is keyed by symbol and timezone-aware observation
  timestamp. Lookup is restricted to the same US/Eastern trading session.
- ATR uses only completed regular sessions before the decision session.
- Relative volume compares current regular-session cumulative volume through
  the decision minute with the same-window cumulative volume from prior
  completed sessions.
- Gap uses the current regular-session open and the previous completed regular
  close. Premarket volume uses only 04:00-09:30 US/Eastern bars from the current
  session.
- SPY, QQQ, and proxy returns use each instrument's current regular open and
  the latest bar no later than the decision timestamp.
- Correlation uses aligned close-to-close returns from completed sessions only.
- Missing warmup history or benchmark/proxy observations suppresses the
  snapshot. It is never filled with a future or previous-session snapshot.

## Components

1. `IntradayFeatureBuildConfig` is an immutable, validated description of the
   symbols, proxy mapping, observation window, and lookback lengths.
2. `build_intraday_feature_snapshots` is a deterministic pure transformation
   from minute bars to immutable feature snapshots.
3. The optimize CLI loads a warmup interval and all required feature-only
   symbols, builds snapshots, then passes only the requested stock symbols to
   the optimizer.
4. Optimization output and evidence record feature configuration, feature data
   identity, snapshot count, and declared/completed/executed trial identity.

## Validation

- Prefix stability: appending a future bar cannot revise an earlier snapshot.
- Session isolation: a missing current-session snapshot cannot reuse yesterday's
  feature row.
- Formula tests cover ATR, RVOL, gap, premarket volume, index direction, and
  proxy-relative strength.
- CLI integration proves that sufficient warmup and benchmark data executes all
  12 preregistered trials.
- Existing DayTrade and full repository tests remain green before any holdout
  return is inspected.

## H5 follow-up protocol

The May-August 2024 holdout exposed `opening-range Top-4` as a new observation,
not as an eligible winner. H5 therefore lives in a separate follow-up registry
and contains exactly one frozen configuration: opening-range ranking, four
active symbols, and positive opening momentum. It is never added to the H1-H4
optimizer or its original 12-trial multiplicity count.

The next comparison uses September-December 2024 once. A fixed-holdout manifest
must match the date window, provider/feed, symbol universe, cache schema,
baseline and candidate configuration hashes, source hash, and the 12 bps stress
cost before an atomic access claim is written. The 5 bps comparison and 12 bps
stress test run in the same process. Any subsequent tuning is a new hypothesis
and requires a later untouched period; this result cannot satisfy formal
2025-2026 sealed admission.

## H6 market-confirmation protocol

H5 reduced loss but remained negative after 5 bps costs in every evaluated
month. H6 does not tune on that period and does not combine with Top-4. It adds
one economically distinct component to the original Top-10 ORB baseline: both
SPY and QQQ must have a same-direction return at the decision timestamp of at
least `0`, `5`, or `10` bps. All three values were preregistered before the
2019-2022 research run.

The confirmation values now travel through `DayTradeConfig`, candidate
serialization, the point-in-time feature book, and the backtester filter.
`--experiment-set h6-market` supplies only the baseline and these three trials;
the H1-H4 12-trial ledger and H5 one-trial ledger are unchanged. Any H6 winner
must first show positive cost-adjusted research behavior before a later fixed
validation window is considered.

Multi-year research loading is checkpointed by calendar month whenever an
independent expected-session calendar is available. Each missing symbol-month
is fetched, range-filtered, written atomically, and revalidated before the next
month starts. Interrupted runs reuse completed content-hashed manifests rather
than rebuilding a multi-year in-memory response.

### H6 research result (2019-2022)

The preregistered three-threshold experiment is rejected and must not advance
to a later validation period. Across 12 nested walk-forward validation windows,
the dynamically selected path returned -28.46% after 5 bps per side, with
Sharpe -1.76 and profit factor 0.82. It reduced the baseline loss by 3.02
percentage points and drawdown by 4.56 percentage points, but only 2 of 12
validation windows were positive and the best aggregate configuration remained
the unfiltered baseline. The result is therefore evidence that the filter can
remove exposure, not evidence of positive alpha. The frozen decision and full
gate ledger are recorded in
`artifacts/daytrade/h6_market_2019_2022_acceptance.json`.

## Corrected H3 RVOL isolation and result

The original H3 wiring overloaded `volume_multiplier`: changing an RVOL
threshold also changed the current-minute volume breakout rule, while merely
supplying feature context implicitly enabled correlation-aware resizing. The
two concerns are now explicit and independent. `min_relative_volume` is its
own `DayTradeConfig` field, and `dynamic_risk_sizing` is opt-in. The focused
`--experiment-set h3-rvol` therefore changes only RVOL across the three values
preregistered on 2026-07-11.

On 2019-2022 nested research, RVOL 2.0 was selected in all 12 windows. Its
36-month OOS path was slightly positive after 5 bps per side (+0.14%, Sharpe
0.04, profit factor 1.01), but contained only 84 trades and six positive
windows. It did not pass research admission, but its stable selection and
positive cost-adjusted result justified freezing it for one development
holdout.

The one-shot January-October 2023 development holdout returned +1.60% after
5 bps per side (annualized 1.93%, Sharpe 1.67, profit factor 1.86, maximum
drawdown 0.46%). At 12 bps per side it remained positive at +0.62%, with profit
factor 1.28 and maximum drawdown 0.61%. The result is promising but not
admissible: it contains only 21 trades, and positive-profit concentration is
28.33% in the largest symbol and 56.67% in the top two. Paper and Live remain
disabled. The gate ledger is recorded in
`artifacts/daytrade/h3_2023_jan_oct_acceptance.json`.

## H7 RVOL-2 entry-window result

H7 kept RVOL 2.0 and every other H3 parameter fixed, then preregistered only
11:00 and 11:30 as alternatives to the 10:30 entry cutoff. The 2019-2022
nested OOS path increased trades from 84 to 89, but cumulative net return fell
from +0.14% to -0.52%, Sharpe fell from 0.04 to -0.13, and profit factor fell
from 1.01 to 0.95. Both fixed extended-window paths had negative OOS Sharpe.
DSR confidence was 28.79%. H7 is rejected and must not access a later period.
The complete decision is in
`artifacts/daytrade/h7_rvol_window_2019_2022_acceptance.json`.

## H8 RVOL-2 risk-scale result

H8 kept RVOL 2.0, the 10:30 cutoff, and the existing 0.75 risk multiples fixed.
It compared the fixed 0.6% stop with ATR and opening-range stop bases. The
fixed ATR path modestly improved cumulative net return from +0.14% to +0.50%,
Sharpe from 0.04 to 0.12, and profit factor from 1.01 to 1.05, but maximum
drawdown worsened from 1.75% to 2.27%, only 6 of 12 windows were positive, and
the sample remained 84 trades. The walk-forward selector returned only +0.02%
with Sharpe 0.01; DSR confidence was 38.28% and CSCV/PBO was 90%. Opening-range
scaling was negative. H8 is rejected without searching risk multiples. The
complete decision is in
`artifacts/daytrade/h8_rvol_risk_2019_2022_acceptance.json`.
