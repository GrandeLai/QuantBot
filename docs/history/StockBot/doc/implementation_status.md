# Implementation Status

This file tracks progress against `doc/us_quant_system_design.md`.

## Current Truth

The architecture, fail-closed data/evidence gates, strategy experiment paths,
and admission rules in the trustworthy-return upgrade are implemented and
covered offline. A trustworthy return improvement is **not** yet demonstrated:
the repository does not ship the signed production point-in-time datasets, a
completed one-shot sealed run, or 60 unique real Paper sessions required to
make that claim. Historical mega-cap and January-April 2024 backtests are
engineering smoke evidence only and cannot satisfy strategy admission.

## Implemented Offline

- Data-source registry, provider failover, partitioned DuckDB/Parquet path
  catalog, adjusted-price cleaning, trading-calendar forward fill,
  liquidity/history universe filtering, and point-in-time fundamentals
  alignment, plus survivor-bias audit checks for retained delisted ticker
  history. The point-in-time security master now uses stable security IDs,
  left-closed/right-open membership and classification intervals, independent
  start/end availability timestamps, immutable snapshots, point-in-time market
  capitalization, and an independent corporate-action cursor. Raw daily prices
  are content-hashed and bound to a signed trading calendar. Final admission
  requires independently signed security-master and price coverage, complete
  calendar coverage, and exact per-session stable-ID agreement. The
  implementation is split across domain, calendar, evidence, price, and query
  modules while retaining the original public import facade.
- Shared trading-spine domain types cover point-in-time snapshots, bars/quotes,
  signal frames, target portfolios, order intents/states, fills, portfolio
  ledger state, run manifests, and backtest reports. Backtest, Paper, and Live
  orchestration reuse strategy/risk/ledger semantics through replaceable clock,
  market-data, and execution ports. Contract tests verify identical order
  intent generation from identical inputs.
- Canonical TOML configuration is split into `data`, `normal`, `daytrade`,
  `portfolio`, `risk`, `execution`, and `validation`; unknown keys fail closed,
  secrets remain environment-only, and all strategy parameters participate in
  canonical configuration hashes. `STOCKBOT_DATA_ROOT` overrides only the
  external PIT data location and is excluded from secret/config payloads. The
  CLI is implemented as small named handlers. Architecture tests derive the
  registered command set from the current code, reject duplicate handlers,
  keep `cli.main` as a branch-free composition root and enforce handler decision
  complexity of at most 12; the documentation does not pin a command or line
  count that would become stale.
- The vendor-neutral PIT bundle requires immutable external Parquet partitions
  for stable identities, historical constituents, delistings, ticker history,
  classifications, market caps, fundamentals, corporate actions, calendars,
  daily/minute bars and total-return vintages. Manifest and partition hashes,
  schema, coverage and availability columns fail closed. A signed data-access
  permit must cover the requested slice and every physical partition opened.
  Permit-bound read sessions create core-owned receipts for the table, actual
  interval, partition paths and hashes; sealing fails unless the strategy's
  complete required-table set was read under the same permit, dataset and
  strategy family and every table's receipts cover the full permit interval
  without gaps. Core-observed calendar sessions are sealed into the evidence.
- `ExposureLedger` is an append-only JSONL hash chain authenticated with HMAC.
  It records granted and denied claims, includes complete warmup intervals,
  rejects overlapping exclusive reads and requires fixed/sealed admission to
  use one absolute canonical ledger. Admission verifies the real permit's
  dates, dataset/config/cost/code identity, signature and ledger membership;
  a boolean holdout claim is no longer accepted.
- Injectable clients for Alpaca, Alpaca News, Polygon minute bars/options snapshots,
  yfinance, FRED, SEC company facts, SEC recent 8-K/Form 4 submissions, SEC
  13F, SEC Form 4 ownership transactions, FINRA, Quandl/Nasdaq short-interest
  backup, Nasdaq Data Link, CBOE VIX, GDELT, and PyTrends.
- Alpha158-lite technical factor coverage, Kronos distribution fallback,
  self-developed factor primitives for earnings revisions, short interest,
  13F smart-money position changes, insider activity, earnings sentiment,
  option IV skew, supply-chain pressure, and search trends, cross-sectional
  winsorized Z-score normalization,
  sector/market-cap neutralization, factor admission gates
  including turnover, Alpha158 subset acceptance, Kronos-vs-Alpha signal
  diversity checks, Alpha158 50-70 factor-count validation,
  candidate-pool average correlation validation,
  source-diverse candidate-pool construction with RD-Agent and self-developed
  factors sharing the differentiated-alpha quota, factor lifecycle review, IC decay
  alerts, crowded-factor weight reduction, IC/ICIR analytics, industry/group
  IC, Regime IC differentiation, quintile backtest, and turnover.
- Point-in-time low-entropy normal-trading sleeves implement 12-1/6-1/3-1
  residual momentum, short-term reversal, quality, EPS revision, and
  sector/size neutralization. Each sleeve binds its full strategy parameters,
  dataset artifacts, security-master admission, effective date, availability
  time, missing coverage, and provenance seal. Empty or incomplete final
  universes, future data, near-constant benchmarks, invalid corporate-action
  lifecycles, and non-finite/extreme inputs fail closed. Sleeve combination
  supports confidence, inverse-volatility, and shrinkage-covariance risk
  allocation plus no-trade bands, holding hysteresis, turnover penalties,
  sector/market-beta, liquidity, and volatility constraints.
- The corrected normal H1 runner freezes `top_n=20` and evaluates neutralized
  12-1 residual momentum against the same signal plus neutralized ROE, gross
  margin, debt ratio and accrual quality. Both arms share identical sessions,
  provider identity and constraints. Deployment adapter output is untrusted:
  the core seals it only after verifying required-table read receipts, permit
  and dataset identity, the adapter code/schema-mapping hash and the session
  hash; adapter sessions must exactly equal the core-observed calendar. A
  shared core view applies the same 252-session history, 20-session ADV and USD
  5 million liquidity eligibility to both arms. The exact Russell 1000 and S&P
  500 union is independently attested per universe and then checked against the
  deduplicated daily stable-ID/price set. Both strategy providers are derived
  from the same sealed input, and the adapter hash participates in configuration
  and code identity. Sealed admission additionally requires an independently
  HMAC-signed attestation over the actual callable implementation, schema
  mapping, provider/schema/bundle/dataset identity. It is verified before claim
  and, together with callable/source identity, again after execution. The
  quantity/cash backtester generates targets at close, trades only at the next
  open, preserves weight drift, applies 5/10 bps per-side costs and accounts
  for splits, cash distributions and stable-ID PnL concentration.
- Regime feature engineering, rule-based Regime detector, HMM adapter boundary,
  low-confidence state stabilization, HMM/rule consensus validation, Regime
  accuracy gate, earnings-season feature, default Regime/risk/system
  configuration, Walk-Forward window generation, and HMM 2010-2018 train /
  2019+ prediction window validation.
- Walk-Forward factor-weight optimization that normalizes training-window IC and
  excludes validation-window observations to prevent data snooping.
- Look-ahead review checklist covering publication dates, signal timing,
  future-column exclusion, train/validation separation, survivor-bias review,
  and adjusted-price usage.
- Quarterly self-trained time-series retraining windows that use the previous
  four years of data for each prediction quarter, plus an injected
  self-trained model adapter that combines Alpha158 and OHLCV features for
  PatchTST/Transformer/LSTM-style forecasters.
- TradingAgent scoring, default Regime factor-weight strategy matrix,
  qualitative LLM signal filtering, earnings-call JSON scoring, SEC 8-K
  negative-event flag extraction, watchlist hysteresis, capital-aware watchlist
  sizing, Regime Sharpe entry gating, earnings blackout, stop loss,
  negative-event removal, turnover-aware exit hysteresis, crisis clearing, risk
  weight construction, portfolio volatility scaling,
  Regime low-confidence/conflict deleveraging, factor-beta exposure scaling,
  drawdown review/halt actions with position scaling, cost model,
  tax-lot ordering, broker-reported buying-power/margin/restriction checks,
  liquidity shock detection, Regime turnover
  limiting, portfolio capacity estimation, optional SPY hedge overlay,
  broker-neutral execution with
  configurable order style, daily next-open/VWAP execution plans, and
  prepare-only primary/backup failover. Once broker submission starts, failures
  are marked uncertain for reconciliation instead of being replayed at the
  backup. Preparation-time failover requires independently reconciled backup
  holdings and never reuses the primary account's positions. Alpaca REST
  adapter, IBKR adapter, order journal,
  performance monitoring, daily pipeline health reports, daily and monthly
  schedule plans, APScheduler registrar, Streamlit renderer, and dashboard
  snapshot. Performance degradation monitoring now emits a three-month IR
  manual-review action with 50% position scaling. Earnings-call JSON scoring
  accepts both `sentiment_score` and the design-example `sentiment`/`confidence`
  fields.
- Independent `daytrade_intraday` event-driven Opening Range Breakout strategy
  for up to 10 stock symbols. Signals fill only on the next matching event;
  execution models per-side costs, gap-through stops, conservative stop-first
  double touches, volume participation, partial fills, and reserved buying
  power. Strategy hypotheses include cumulative session VWAP with hysteresis,
  entry-window/Top-K filtering, point-in-time RVOL/gap/premarket/market/sector
  confirmation, ATR/opening-range stops, current-equity/portfolio-heat/
  correlation sizing, and separate long/short borrow/SSR/gap-risk handling.
  The previous mechanical grid is replaced by immutable preregistered families.
  H1-H8 contribute 20 frozen historical declarations but no fabricated
  completion; a fresh global registry starts their completed count at zero. H9
  adds one local `reward_risk=2.0` trial and can record only that trial when it
  actually executes. Completion of H1-H8 requires an HMAC-signed version-2
  artifact with exact aligned returns. Every trial additionally requires its
  own verified HMAC proof over experiment/trial/run/run-type, current policy,
  permit interval and timestamp, dataset/config/cost/code, execution evidence
  and dated-return identities; duplicated run, permit or execution identities
  are rejected. Evidence uses purge/embargo windows and fails global
  DSR/PBO when that immutable history is absent.
  `daytrade-live-cycle` remains historical replay only: `--submit-orders`
  returns `live_trading_disabled` before loaders or submitters can run. Options
  remain out of scope.
- Daily trading workflow orchestration, DuckDB Parquet write adapter, runtime
  preflight, data coverage gate, CLI commands, and Paper Trading acceptance
  gate.
- Retry/backoff transport, provider-specific rate-limit reset header parsing,
  Alpaca pagination handling, runtime TOML config loading with environment
  overrides, live smoke-test gate, OHLCV schema validation, quarantine
  reporting, structured JSONL event logging, pipeline run reporting, and
  completion audit.
- Rate-limit `Retry-After` handling, Paper Trading JSONL journal, paper account
  equity snapshot journal, weekly snapshot rollup, paper evidence builder, and
  completion-audit evidence file loading.
- Packaging extras, Dockerfile, example runtime config, and live-smoke evidence
  file output.
- Evidence recorder and `collect-evidence` CLI for offline test/runtime import
  evidence.
- Live trading soak journal, live equity snapshot journal, snapshot-to-session
  drawdown rollup, status command, evidence builder, and CLI commands.
- Paper Trading run record, account snapshot record, snapshot-to-weekly-run
  rollup, status, and evidence CLI commands.
- `acceptance-cycle` CLI orchestration for daily Paper Trading/live-soak
  evidence capture, period rollups, evidence-file updates, and completion-audit
  status output.
- `acceptance-schedule` CLI plan for the after-close acceptance-cycle command
  template used by cron/APScheduler deployment.
- Runtime dependency evidence builder and `runtime-evidence` CLI command.
- Runtime smoke suite for DuckDB, Qlib Alpha158, Chronos, RD-Agent, Streamlit,
  and APScheduler adapters using injectable runtime objects.
- `runtime-smoke` CLI evidence writer plus a production runtime factory that
  attempts to assemble the installed DuckDB, Qlib Alpha158, RD-Agent,
  Chronos, Streamlit, and APScheduler runtimes, while recording explicit failure
  evidence for missing dependencies, missing Qlib US data, or unconfigured
  Chronos model inference. Chronos model-backed smoke can be enabled with
  `STOCKBOT_CHRONOS_MODEL_ID`. In the local Python 3.11 `.venv`, runtime import
  evidence passes for DuckDB, Qlib, Chronos, RD-Agent, Streamlit, and
  APScheduler; adapter smoke currently passes DuckDB, RD-Agent, Streamlit, and
  APScheduler.
- The legacy Phase 5 2015-2022 / 2023-2024 momentum smoke path remains for CLI
  compatibility and plumbing checks, but its adjusted-close mega-cap universe
  cannot produce final strategy admission.
  The `historical-replay` CLI replays the OOS validation daily equity curve into
  separate Paper Trading/live-soak JSONL logs for pipeline validation; replay
  output is explicitly marked as historical and does not replace elapsed-time
  production evidence.
  The analytics layer also exposes a production target gate built from
  `SystemConfig`, including the top-level Sharpe target of 1.2.
- Qlib/QuantConnect-style cross-backtest validation gate.
- Monthly RD-Agent discovery scheduling plus manual-review admission gate for
  RD-Agent generated factors. Generated-factor admission also requires an
  explicit economic rationale before pool entry, plus monthly research report
  summaries.
- Data/model artifact manifest helpers with DVC-style stage metadata export.
- Critical alert dispatcher and risk alert router for wiring key order failures,
  drawdown review/halt actions, and Regime switches to SMS providers.
- Overfitting guardrails for holdout length, holdout tuning, Regime coverage,
  and complexity-adjusted IC thresholds.
- Fixed validation protocols: normal research 2010-2020 and adaptive
  development 2021-2024; DayTrade research 2019-2022 and adaptive development
  2023-2024. Adaptive windows can reject but cannot prove a candidate. The old
  2025-01-01..2026-06-30 interval is explicitly exposed because no independent
  unread proof exists. The active one-shot sealed interval is therefore
  2026-08-01..2028-07-31 and cannot be claimed before completion. Purged splits
  and post-validation embargoes are executable.
- Multiple-testing diagnostics implement Deflated Sharpe Ratio and CSCV/PBO
  from aligned trial returns. Strategy admission requires full trial ledgers,
  DSR confidence at least 95%, PBO at most 5%, no leverage above 1x, an exact
  candidate/baseline date-data-cost identity, and a sealed holdout claim.
  Normal and DayTrade thresholds implement every return, Sharpe, drawdown,
  turnover, stress, stability, and concentration condition in the upgrade
  plan. Capital remains locked to 90% normal / 10% DayTrade until both pass.
- DayTrade dynamic-universe snapshots are keyed by stable security ID and use
  only prior-close-visible members/tickers. The builder enforces 252 sessions,
  USD 5 price, 20 complete ADV sessions, USD 5 million minimum ADV and a
  deterministic Top-50 selection with stable-security-ID tie-breaking;
  unexplained missing data fails the month. H3/H9 and sealed reads require the
  manifest to bind the exact prior-close/252-session/USD-5/20-session/USD-5M/
  Top-50 policy and a `minute_data_hash` that is checked against the actual
  frozen bars, plus a trusted signed calendar with exact session coverage and a
  trusted PIT construction proof. The permit dataset hash composes the source
  PIT dataset, complete universe manifest, minute data, calendar, construction
  proof and historical-artifact identities. Historical permits use the
  non-recursive source/minute/calendar/construction identity, while H9 binds the
  final artifact hash. Frozen H3 uses RVOL
  2.0, 10:30 cutoff, 0.6% stop and 1.5R. H9 changes only `reward_risk` to 2.0,
  computes only RVOL features, derives its target from the actual fill and
  resolves a double touch stop-first. The global H1-H9 ledger retains 21
  declarations and will not reset DSR/PBO at H9; only the actually executed H9
  trial is marked complete by an H9 run. Research, adaptive-development and
  fixed-holdout results can only survive development; formal admission requires
  a completed canonical sealed permit. The acceptance path freezes every
  `DayTradeConfig` field plus its 3/1-month walk-forward, two configurations,
  60-day warmup, one-day purge/embargo and empty feature overrides before claim.
  Its cost identity binds the 5 bps base and 12 bps stress models. Formal
  strategy evidence uses the permit's exact dataset/config/cost/code identities
  and the current acceptance-policy version.
- Paper promotion requires at least 60 unique `paper_trading` sessions after
  strategy admission. Every session must reconcile orders, ledger, broker, and
  strategy-intent contracts under one stable dataset/config/code identity.
  Historical replay cannot satisfy this gate.

## External Dependencies Still Needed

- Real credentials and live API smoke tests for Alpaca, Polygon, FRED, SEC
  EDGAR, FINRA, Nasdaq Data Link, and news providers.
- Qlib Alpha158 full expression engine, US data conversion, and production
  factor-frame persistence. The local adapter smoke reaches Qlib initialization
  but fails until `~/.qlib/us_data` contains day-frequency US market data.
- Chronos/TimesFM/Moirai production model selection and optional self-trained
  time-series model training. The local package import passes, and adapter
  smoke can load a configured Chronos model through `STOCKBOT_CHRONOS_MODEL_ID`.
- RD-Agent generated-factor execution and generated code review beyond the
  local package/import and adapter smoke checks.
- A production vendor feed containing verified historical constituents,
  delisted securities, identifier changes, classifications, fundamentals,
  corporate actions, minute/daily bars, total-return vintages and availability
  times, plus a deployment adapter that maps the vendor's physical columns and
  independent attestations into StockBot's point-in-time domain types. The
  envelope, strategy port and fail-closed admission gate are implemented;
  without this real bundle/adapter the CLI deliberately cannot supply credible
  full-universe OOS return evidence.
- A completed sealed evaluation for each strategy family, followed by at least
  60 unique real Paper sessions with all reconciliation checks passing. Only
  then can a separately authorized small-capital Live phase begin.

## Verification

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -q
PYTHONPYCACHEPREFIX=/tmp/stockbot-pycache .venv/bin/python -m compileall -q src tests
```

The tests are intentionally offline and validate the deterministic core rules.
Use the current command output as the authoritative discovered-test total and
pass/fail result; this status file intentionally does not freeze a date, test
count, command count or dispatcher line count. CLI architecture contracts
automatically verify unique handlers, a branch-free composition root and
handler decision complexity no greater than 12.

The verification contracts include:

- DayTrade execution contracts cover next-event fills, missing/duplicate
  sessions, feed coverage, costs, stop gaps, double touches, partial volume,
  buying-power conservation, long/short rules, point-in-time features, and
  declared/completed experiment accounting.
- Normal-trading contracts cover point-in-time availability, corporate actions,
  delistings, stable identifiers, complete universe coverage, factor provenance,
  neutralization, portfolio constraints, and future-information rejection.
- Statistical/admission contracts cover purge/embargo, exclusive sealed claims,
  DSR, CSCV/PBO, all normal/DayTrade thresholds, the 90/10 allocation lock, and
  60-session Paper reconciliation.
- `completion-audit` remains intentionally incomplete without signed production
  datasets, sealed strategy results, and real elapsed-time Paper evidence. No
  historical replay or prior raw return number is promoted as credible Alpha.
