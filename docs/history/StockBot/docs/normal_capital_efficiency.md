# Normal allocator capital efficiency

Status date: 2026-07-26.

## Finding

`NormalPortfolioAllocator` only ever shrinks weights. Every constraint —
single-stock cap, sector cap, ADV/liquidity cap, market-beta cap, gross cap and
the volatility target — is applied as a one-directional truncation, and the
budget freed by a truncation is never re-deployed. Volatility targeting is
likewise cap-only: it de-levers a book above `target_volatility_annual` but
never re-levers one below it.

Two compounding effects follow.

1. `_asset_risk_weights` clips the unconstrained mean-variance solution at zero
   (`max(solved[index], 0.0)`). On a realistic cross-section that funds roughly
   half of the `top_n = 20` selection, so the surviving names carry the whole
   normalized weight and are then truncated by the 5% single-name cap. The
   truncated remainder goes to cash.
2. `_apply_constraints` runs again after `_smooth`, so the smoothed book is
   re-truncated without ever being expanded back toward the permitted exposure.

Measured on a 60-name, 6-sector synthetic cross-section with the shipped
defaults (`top_n = 20`, 5% single-name cap, 25% sector cap, 1% ADV
participation, 15% annual volatility target, R1 regime at full confidence, run
to steady state):

| metric | value |
| --- | --- |
| target gross exposure | 1.00 |
| realized gross exposure | 0.39 |
| idle cash | 0.61 |
| funded names | 10 of 20 selected |
| realized annualized volatility | 0.027 |
| risk utilization | 0.18 |

The `[normal] top_n = 20` comment in `config_loader.py` states the intent
directly — "the PIT strategy's 5% single-name cap needs at least 20 names to
make 100% gross exposure mathematically attainable" — but the allocator cannot
reach it. Both the H1 baseline and the H1 candidate are affected identically,
so the relative comparison stays valid; the absolute return of both arms is
roughly a third of what the declared risk budget implies, which puts the
`validation.normal_min_net_excess_annual = 0.05` gate out of reach for
mechanical reasons rather than signal reasons.

## Change

Three opt-in `[normal]` settings, all defaulting to the historical behaviour so
existing configurations and frozen experiment identities are unchanged:

- `redistribute_constrained_capital` (default `false`) — after the caps are
  applied, hand the shortfall to the names that still have headroom, re-apply
  the sector caps, and repeat until the permitted exposure is reached or every
  name is capped. Runs only on the target-construction pass; the enforcement
  pass after `_smooth` still shrinks only, so the turnover control survives.
- `volatility_targeting` (default `"cap_only"`, alternative `"two_sided"`) —
  scale a below-target book up toward `target_volatility_annual`, bounded by
  the per-name, sector, beta and gross-exposure limits. This converts idle cash
  into intended exposure and can never introduce leverage.
- `min_funded_names` (default `0`) — when the long-only clip funds fewer than
  this many of the selected names, use the risk-scaled alpha weights, which
  fund the whole selection, instead of dropping the rest.

`NormalAllocationReport` now carries `target_exposure`, `capital_utilization`
and `risk_utilization` so under-investment is reported rather than silent.

Same synthetic cross-section, same signal, with
`redistribute_constrained_capital = true`, `volatility_targeting = "two_sided"`
and `min_funded_names = 20`:

| metric | legacy | capital-efficient |
| --- | --- | --- |
| realized gross exposure | 0.39 | 0.96 |
| funded names | 10 | 20 |
| capital utilization | 0.39 | 0.96 |
| risk utilization (asset vol 20%) | 0.30 | 0.72 |
| risk utilization (asset vol 35%) | 0.53 | 0.96 |
| gross exposure in R4 bear | 0.24 | 0.23 |

Risk limits still bind in the right direction: at 35% asset volatility the
capital-efficient profile de-levers to 0.74 gross and sits at the volatility
target, and bear-regime exposure is unchanged.

End-to-end through `PointInTimeNormalBacktester` (synthetic 6-year daily panel,
60 names, monthly rebalance, next-open fills, 5 bps per side, identical signal
and identical sessions for both arms):

| profile | annual return | volatility | Sharpe | max drawdown | annual turnover | mean gross |
| --- | --- | --- | --- | --- | --- | --- |
| legacy | 0.036 | 0.070 | 0.55 | 0.14 | 0.99 | 0.40 |
| capital-efficient | 0.117 | 0.151 | 0.81 | 0.24 | 1.47 | 0.90 |

This measures how much of a given signal each configuration converts into
realized return. It is not an alpha claim, uses no market data, and does not
establish that the strategy has an edge.

## Preregistration: normal H2

The capital-efficient profile is declared as its own single-variable
experiment, `normal-h2-capital-efficiency`, preregistered 2026-07-26 with
its declaration retained for multiplicity accounting. After the research and
adaptive results below failed the Alpha gates, its fixed disposition is
`completed_rejected`; only explicit `--historical-audit-replay` remains
available, and fixed/sealed data are refused. It shares H1's baseline sleeve
(`residual_momentum_12_1_neutral`) and adds one component,
`capital_efficient_allocation`. The normal-family multiplicity ledger therefore
declares two trials, and both count towards global DSR/PBO accounting;
`run_normal_h1_experiment` now checks its own declared trial count rather than
the family total, so growing the ledger cannot silently disarm H1's guard.

`run_normal_h2_experiment` runs both arms from a single provider instance, so
signal, universe, sessions, costs and risk limits are identical by
construction and the allocator profile is the only free variable. The two arm
configurations are frozen in code as `NORMAL_H2_BASELINE_CONFIG` and
`NORMAL_H2_CANDIDATE_CONFIG`; every shared limit (`top_n`, single-stock,
sector, beta, volatility target, ADV participation, no-trade band, turnover
penalty, exit rank buffer) is asserted equal across the arms.

H2 gates, in addition to the H1 set:

- `no_capital_redeployment` — the candidate must actually hold more of the
  permitted exposure than the baseline, otherwise the hypothesis did nothing.
- `sharpe_degradation` — redeployment must not cost more than 0.05 Sharpe. The
  claim is that the discarded budget was earning nothing, not that more
  exposure is always better.
- DSR is deflated against both arms' Sharpe ratios rather than one.

The frozen H1 runner still rejects all three settings, so H2 cannot contaminate
H1.

Running the two frozen H2 configurations over the synthetic panel above
reproduces the intended split: the capital-efficiency gates pass and the
signal-quality gates fail.

| gate | value | threshold | result |
| --- | --- | --- | --- |
| `baseline_cagr_improvement` | +0.0804 | ≥ +0.02 | pass |
| `sharpe_degradation` | +0.2617 | ≥ −0.05 | pass |
| capital utilization | 0.40 → 0.90 | must increase | pass |
| `positive_windows` | 0.619 | ≥ 0.60 | pass |
| `annual_turnover` | 1.47 | ≤ 4.0 | pass |
| `net_excess_annual` | −0.0265 | ≥ +0.05 | **fail** |
| `sharpe` | 0.807 | ≥ 1.20 | **fail** |
| `two_x_cost_stress` | −0.0280 | ≥ +0.03 | **fail** |
| `max_drawdown` | 0.235 | ≤ 0.15 | **fail** |

That is the correct outcome for a synthetic panel with no real alpha: the
allocator repair works and the signal does not carry it. It also shows the
gates cannot be passed by capital efficiency alone.

## Free-data research run

`stockbot.normal.research_adapter` turns plain daily bars into the point-in-time
artifacts the normal provider consumes, so H2 can be exercised on real prices
without weakening the trust boundary. It builds a dataset-derived security
master, per-month market capitalizations, and a monthly total-return dataset
sealed with caller-supplied research keys, then serves them through
`PointInTimeMarketDataPort`. Everything it produces is
`DataQualityTier.DEVELOPMENT_FREE`, the master is never `admission_verified`,
and `require_final_admission` refuses it.

Building this surfaced a real modelling constraint. A single present-day
download cannot be snapshotted at a historical date at all: its
`adjustment_vintage_at` postdates every historical `as_of`, and
`_validate_snapshot_request` rejects that outright. It is right to reject it —
one download of adjusted closes embeds every later split and dividend. The
adapter therefore rebuilds a truncated vintage per rebalance, each sealed at
the close of the last month it covers. Deriving the vintage from the
*requesting* session instead would give one cutoff two different vintages and
let a later snapshot poison the cache for an earlier one; that bug was found
and fixed, and is pinned by `ResearchAdapterVintageTest`.

Known biases, all recorded in the artifact:

- fixed present-day universe, so survivorship bias and no point-in-time index
  membership;
- adjustment factors themselves are current even though the vintage is
  truncated per rebalance;
- share count held constant, so market capitalization moves only with price;
- previous raw close used as the execution price, because free bars have no
  open;
- regime pinned at R1 with full confidence, which sets the exposure target to
  1.0 for *both* arms and so isolates the allocator leak from regime
  de-risking.

Run over 64 US large caps listed continuously since 2006 across nine sectors,
benchmark SPY, monthly rebalance, 5 bps per side:

| | research 2010-2020 | | adaptive dev 2021-2024 | |
| --- | --- | --- | --- | --- |
| | baseline | candidate | baseline | candidate |
| annual return | 0.0486 | 0.1045 | 0.0422 | 0.0670 |
| excess vs SPY | −0.0903 | −0.0343 | −0.0933 | −0.0685 |
| annual volatility | 0.0743 | 0.1567 | 0.0636 | 0.1276 |
| Sharpe | 0.676 | 0.713 | 0.681 | 0.572 |
| max drawdown | 0.190 | 0.332 | 0.094 | 0.197 |
| annual turnover | 1.18 | 1.55 | 1.21 | 1.74 |
| **capital utilization** | **0.430** | **0.869** | **0.450** | **0.878** |

Two conclusions, and they point in opposite directions.

The capital-efficiency repair is confirmed on real prices. Utilization roughly
doubles in both windows, realized volatility rises to the 15% target instead of
sitting near 7%, and annual return rises 5.6 and 2.5 percentage points. The
binding constraint becomes the volatility target rather than discarded capital,
which is the intended behaviour.

H2 is still rejected, in both windows, by its own gates. Residual momentum over
this universe underperforms SPY whether or not the capital is deployed, so
`net_excess_annual`, `sharpe` and `two_x_cost_stress` all fail. The adaptive
window — which may reject but cannot prove — additionally fails
`sharpe_degradation` and `dsr_confidence`. Deploying the idle capital raises
return and risk together; it does not manufacture alpha.

`max_drawdown` fails in the research window at 0.332 against a 0.15 limit. That
is a genuine configuration inconsistency rather than a signal problem, and it
is followed up in "The drawdown limit was never enforced" below.

Evidence: `artifacts/normal/h2_capital_efficiency_free_research.json`, marked
`formal_admission_eligible=false` and `counts_for_real_acceptance=false`.

## The PBO gate was switching itself off

Both runners chose CSCV folds with "largest fold count that divides the
observation count exactly, otherwise `None`". An odd session count has no even
divisor at all, so `pbo_unavailable` was reported and the overfitting gate
silently disappeared — on roughly half of all possible runs, decided by the
parity of the session count. The free-data H2 runs hit it immediately: 2769 and
1005 sessions, both odd, both `pbo_unavailable`.

CSCV drops the remainder by construction. `statistical_validation.cscv_partition`
now picks the fold count, trims the oldest `observations % folds` rows, and
reports `dropped_observations` so a trimmed sample is never mistaken for the
full one. Both the normal runners and the DayTrade statistical-validation path
use it; DayTrade keeps its declared `(8, 6, 4, 2)` preference.

The consequence is not cosmetic. With the gate actually running, the 2010-2020
H2 research window reports **PBO 0.715** against a 0.05 threshold, on 16 folds
with 1 observation dropped. The gate that had been silently disabled is the one
that rejects most strongly.

## The drawdown limit was never enforced

`risk.max_normal_drawdown` was parsed, validated, included in the configuration
hash — and read by no strategy code anywhere. `NormalDrawdownPolicy` makes it
enforceable: halve exposure at the limit, go flat at 1.5x the limit, restore
full size below two thirds of it, with a hysteresis band in between so the book
does not re-lever and de-lever on every oscillation.

`PointInTimeNormalBacktester` applies it on the **daily** risk clock, not the
monthly signal clock. That distinction turned out to matter more than the
policy itself. Measured first with de-risking only at rebalances:

| profile | annual | max drawdown |
| --- | --- | --- |
| capital-efficient, no control | 0.1045 | 0.3316 |
| + monthly-only control | 0.0694 | **0.3316** |

The drawdown is completely unchanged and the return drops 3.5 points. A monthly
check sees the drawdown only after it has happened, sells the bottom, and then
misses the recovery. Moving the same policy to the daily risk clock is what
makes it a control at all:

| profile (research 2010-2020) | annual | volatility | Sharpe | max drawdown |
| --- | --- | --- | --- | --- |
| legacy | 0.0486 | 0.0743 | 0.676 | 0.1895 |
| capital-efficient | 0.1045 | 0.1567 | 0.713 | 0.3316 |
| capital-efficient + daily control | 0.0573 | 0.1247 | 0.510 | 0.2433 |

| profile (adaptive 2021-2024) | annual | volatility | Sharpe | max drawdown |
| --- | --- | --- | --- | --- |
| legacy | 0.0422 | 0.0636 | 0.681 | 0.0936 |
| capital-efficient | 0.0670 | 0.1276 | 0.572 | 0.1974 |
| capital-efficient + daily control | 0.0500 | 0.1045 | 0.520 | 0.1693 |

The control works — 0.332 to 0.243 — and still does not reach 0.15, while
costing 4.7 points of annual return and 0.20 Sharpe. Note the legacy profile
breaches the limit too, at 0.190.

So no tested configuration satisfies `0.15`, and daily de-risking cannot rescue
it. The policy defaults to disabled, so no existing run changes.
`NormalBacktestPoint` now carries `drawdown` and `exposure_scale`, and
`NormalBacktestResult` carries `drawdown_scaled_rebalances`.

## Current resolution: separate controls, original formal ceiling

The original implementation conflated two controls:

- `risk.max_normal_drawdown = 0.15` is an **operational** review/halt trigger
  for a running book — the same concept `risk.py` names `max_drawdown_halt`. It
  sits with the other operational limits and is now enforced by
  `NormalDrawdownPolicy`.
- The **acceptance** ceiling used to admit or reject a strategy was hardcoded
  inside both experiment runners instead of being supplied by `[validation]`.

They are now separate consumers even though both preserve the original `0.15`
value:

- `validation.normal_max_drawdown` drives backtest and formal admission.
- `risk.max_normal_drawdown` drives the running-book review/halt policy.
- `risk.max_portfolio_vol_annual` does not automatically loosen either gate.

The measured outcome remains useful evidence, but it is not authority to relax
the preregistered acceptance requirement:

| window | candidate max drawdown | ceiling | `max_drawdown` gate |
| --- | --- | --- | --- |
| research 2010-2020 | 0.3316 | 0.15 | **fails** |
| adaptive dev 2021-2024 | 0.1974 | 0.15 | **fails** |

Both windows fail the original formal drawdown requirement. That is an honest
rejection signal, not a configuration contradiction.

### Formal admission remains at 15%

`strategy_admission.evaluate_normal_admission` produces the formal
`normal_strategy_admission` evidence. Its ceiling and the experiment-runner
default share one named constant,
`NORMAL_ACCEPTANCE_MAX_DRAWDOWN = 0.15`. Runtime configuration may tighten this
value but cannot loosen it, and tests pin both sides of the 15% boundary.

The DayTrade ceilings are unchanged at 0.05 and 0.07 stressed. They are now
named constants rather than repeated literals, but their values did not move.
DayTrade deliberately keeps its acceptance ceiling equal to its operational
trigger: a strategy that would trip the halt should not be admitted. That
equality is a design choice there, unlike the normal case where the two numbers
were mathematically incompatible.

## The DayTrade drawdown limit was also enforced nowhere

`risk.max_daytrade_drawdown` had the same defect as its normal counterpart: it
was validated, hashed, and translated into `DayTradeConfig.from_runtime` — and
then read by nothing. `DayTradeConfig.enforce_drawdown_halt` (default `false`)
now enforces it by stopping new entries once the DayTrade sleeve is down by
that fraction from its peak. The halt is decided from equity observable before
the session opens, so it can only remove entries that had not happened yet, and
a halted session records `stopped_reason="max_daytrade_drawdown"`.

It defaults to disabled so the frozen H3/H9 results stay reproducible. No
DayTrade return claim is made or implied: the family has no admitted strategy
and the evidence in `artifacts/daytrade/` still rejects every hypothesis.

## The `[validation]` section was decorative

An audit of every declared configuration key against its consumers found that
the entire `[validation]` section had none. All six acceptance thresholds were
literals, and each was written twice — once in `normal/experiment_runner.py`
and once in `strategy_admission.py`:

```
experiment_runner.py:400  (excess_alpha_annual, 0.05, "net_excess_annual")
strategy_admission.py:179 (net_excess_annual,   0.05, "net_excess_annual")
```

Editing `[validation]` in a runtime TOML changed nothing. That made the
README's claim — "runtime configuration cannot weaken admission policy: the
validation thresholds in the example are minimum floors" — vacuous in both
directions: the configuration could not weaken admission, but it could not
strengthen it either, because it was not read.

The thresholds are now two frozen types, `NormalAcceptanceThresholds` and
`DayTradeAcceptanceThresholds`, declared in `strategy_admission` and consumed
by both the experiment runners and the formal admission gates. Every literal is
gone from both files.

The "cannot weaken" rule is enforced **on the type**, not at a call site:

```python
NormalAcceptanceThresholds(min_sharpe=1.19)
# ValueError: min_sharpe cannot weaken admission policy: must be at least 1.2
NormalAcceptanceThresholds(max_drawdown=0.151)
# ValueError: max_drawdown cannot weaken admission policy: must be at most 0.15
NormalAcceptanceThresholds(min_sharpe=1.50, max_drawdown=0.10)   # fine
```

That placement matters. No construction path — runtime TOML, a test helper, or
a future caller — can produce a looser gate, so the README claim is now a
property of the code rather than a convention.

`RuntimeConfig` exposes the bridge: `normal_acceptance_thresholds()`,
`daytrade_acceptance_thresholds()` and `paper_promotion_min_sessions()` cover
all six declared keys. `validation.normal_max_drawdown` is additionally capped
at the policy ceiling of 0.15, so a loadable configuration can never build an
invalid threshold object.

This is a pure refactor: re-running H2 over the free-data research window
reproduces the previous numbers exactly, including `pbo=0.7152` and the same
five failed gates.

## The other unread keys

The same audit found four more keys carried and hashed but never read. Each is
now consumed, with a behaviour-preserving default.

**`execution.slippage_bps_per_side`** — slippage was never applied anywhere. It
is a per-side execution cost, so it now enters the same rate as commissions:
`PointInTimeNormalBacktester(slippage_bps_per_side=...)` and
`DayTradeConfig.slippage_bps`, both defaulting to `0.0`, and
`RuntimeConfig.normal_cost_bps_per_side()` /
`daytrade_cost_bps_per_side()` return the combined figure. Splitting a cost
between the two knobs is exactly equivalent to charging it as one, which is
what the tests assert. Sensitivity on the capital-efficient arm over
2010-2020 at 1.55 annual turnover:

| slippage bps/side | annual return | Sharpe | fees |
| --- | --- | --- | --- |
| 0 | 0.1045 | 0.713 | 3153 |
| 2 | 0.1038 | 0.709 | 4394 |
| 5 | 0.1028 | 0.703 | 6239 |
| 10 | 0.1012 | 0.694 | 9270 |

A monthly-rebalanced book is not very slippage-sensitive: 10 bps per side costs
0.33 percentage points a year. The frozen H1 cost-model hash now records
`slippage_bps_per_side: 0.0` explicitly, so a later slippage-bearing run cannot
claim the same cost identity.

**`portfolio.normal_fraction`** — the 90/10 split was enforced only on the
DayTrade side, which scales by `daytrade_fraction` inside `DayTradeConfig`; the
normal runners ran on full account capital. `RuntimeConfig.normal_capital()`
and `daytrade_capital()` now expose both sleeves. Re-running the research
window at 90,000 instead of 100,000 gives an identical 0.1045 annual return and
0.713 Sharpe, with fees scaling by exactly 0.9: the liquidity cap
(`adv x max_adv_participation / capital`) is nowhere near binding at this
account size, so the split changes account-level attribution and dollar
amounts, not the sleeve's own return. It would begin to matter at an account
size where the ADV cap binds.

**`normal.residual_skip_months`** — the provider hardcoded
`skip_recent_months=1`, so the key was inert.
`PointInTimeNormalRequestProvider` now takes it, both frozen runners reject
anything but `1`, and `_require_same_normal_data_identity` compares it across
arms so the two cannot silently diverge.

**`normal.execution_price`** — the schema accepted `"vwap"`, but only
next-open execution exists. Offering an option the code cannot honour is worse
than not offering it, so the choice set is now `{"next_open"}`.

## The CLI never read a configuration file

Chasing the last three keys turned up the root cause: `load_runtime_config`
had **no caller anywhere in the package**. No command took a config path, so
the whole `[data]`, `[execution]` and `[portfolio]` surface was inert no matter
how carefully each key was validated and hashed.

`resolve_runtime_config(args, environ)` now loads the TOML named by `--config`
or `STOCKBOT_CONFIG`, and returns `None` when neither is given, so commands
that do not need one are unaffected. On top of that:

- **`data.daily_feed` / `data.minute_feed`** sit at the end of the Alpaca feed
  resolution chain — flag, then environment, then config — so an explicit
  override still wins.
- **`execution.primary_broker` / `backup_broker`** were free strings: a
  configuration could name a broker the package cannot drive and nothing would
  object until live submission. They are now restricted to the implemented
  adapters, `broker_adapter_type` resolves a name to its adapter and fails
  closed on anything else, `build_broker_failover_executor` builds the executor
  from the configured identities, and naming the same broker twice is rejected
  because that is not failover.
- **`normal.universe_mode`** now has teeth rather than one legal value.
  `static_mega_cap` is expressible so the legacy path can declare what it
  actually uses, and `require_legacy_universe_mode` refuses to serve the fixed
  mega-cap list to a configuration that declared `point_in_time`.
- **`normal.execution_price`** is checked against
  `IMPLEMENTED_EXECUTION_PRICE` by the normal experiment handler, so a
  configured execution model the backtester does not implement fails before the
  run rather than being silently ignored.
- **`validation.min_paper_sessions`** reaches the gate through
  `paper_promotion_gate_from_runtime`, which still clamps to the 60-session
  floor, so configuration can only tighten it.

## The audit is now a test

`DeclaredKeysHaveConsumersTest` greps every canonical key for a consumer
outside `config_loader`, so a newly added key cannot quietly join the dead
list. It is what caught two keys this round that had only been *validated*
more strictly rather than actually consumed — restricting a choice set is not
the same as reading the value. Deliberate exceptions live in
`KNOWN_UNCONSUMED` and must carry a reason; there is currently one,
`execution.base_url`, which deployment wiring passes to broker clients by
value.

## Deployment safety default

`config/stockbot.example.toml` now matches the frozen H1-safe defaults
(`redistribute_constrained_capital = false`, `volatility_targeting =
"cap_only"`, `min_funded_names = 0`). The rejected H2 values remain available
only inside the explicit historical-audit runner and are not a deployment
profile.

## Not yet done

The free-data run rejects H2 and cannot admit it either way. A return claim
still requires
the real point-in-time bundle, the canonical exposure ledger and HMAC key, a
production normal adapter with its signed attestation, and a purged/embargoed
walk-forward run against the corrected baseline followed by the fixed holdout.
`strategy-readiness` reports those blockers; none of them are satisfied in this
environment.
