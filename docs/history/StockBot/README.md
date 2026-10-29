# StockBot

StockBot is the local implementation scaffold for the design in
`doc/us_quant_system_design.md`. The current code focuses on the dependency-free
core that can be tested offline before connecting Alpaca, FRED, Qlib, Chronos,
RD-Agent, DuckDB, or broker credentials.

## Trustworthy Return Upgrade

The July 2026 upgrade changes the project from "a backtest that reports a
return" into a system that can reject an untrustworthy return claim. The main
boundaries are now executable rather than documentary:

- Corrected normal and DayTrade baselines must use the same date range, dataset
  identity, configuration, code identity, and cost model as their candidates.
  Historical replay and free/development data cannot satisfy final admission.
- Comparable experiment output now uses the immutable
  `ExperimentRunReport` envelope. It binds the experiment/run identity, run
  type, UTC observation time, logical dataset, actual input materialization,
  configuration, source code, execution/cost model, named baseline/stress cost
  suite, result payload, metrics and admission decision under one deterministic
  report hash. The permit's
  `dataset_hash` remains the logical authorized dataset identity, while
  `input_materialization_hash` identifies what was actually read; both are
  validated and neither is substituted for the other.
- A sealed experiment now has one canonical execution state machine beside the
  exposure ledger. `begin` writes the immutable execution identity before the
  strategy runs; only `complete` may atomically persist the result and its
  receipt. A hand-written result file cannot recover a started run, an exact
  retry is idempotent, and formal evidence rejects any non-canonical execution
  ledger path.
- Formal strategy evidence is no longer appendable from a caller-authored
  `passed` summary. The materializer verifies the granted data permit, completed
  execution receipt, result hash, exact cost suite and a canonical raw execution
  artifact, then recomputes every admission metric before minting the
  hash-chained verifier proof. Normal artifacts conserve per-security PnL and
  independently recompute 5/10 bps fees and turnover from pre-trade equity and
  traded notional. DayTrade artifacts conserve fills/daily equity and recompute
  long/short fill prices from pre-cost event prices at 5/12 bps.
- Production PIT Parquet stays outside the repository under
  `STOCKBOT_DATA_ROOT`. Its manifest binds every partition hash and coverage
  interval. Every protected PIT/dynamic cache fill, backtest, optimizer and
  comparison must claim the complete `read_interval`, including warmup,
  through the canonical HMAC-chained exposure ledger before opening market
  data. Exclusive/replay protection uses the separately signed
  `protected_holdout_interval`, so a feature warmup does not consume an
  otherwise untouched future holdout.
  Normal read evidence must cover
  that entire interval without gaps, and its backtest sessions must exactly
  match the calendar sessions observed by the core reader.
- Formal Normal runs additionally require an independently HMAC-signed adapter
  attestation. It binds the actual callable implementation, schema mapping,
  provider, schema, bundle ID and dataset hash before the exposure claim. The
  attestation, adapter and source identities are verified again after adapter
  execution, so a read receipt cannot legitimize fabricated mapped output.
- Normal trading has point-in-time security master, universe, fundamentals,
  market-cap, corporate-action, total-return, and availability-time models.
  Low-entropy factor sleeves cover residual momentum, short-term reversal,
  quality, EPS revision, and sector/size neutralization with sealed provenance.
- DayTrade signals fill only on the next matching event and model per-side
  costs, gap-through stops, stop-first double touches, volume participation,
  partial fills, session cumulative VWAP with hysteresis, pre-entry RVOL/gap/
  market/sector filters, ATR/opening-range stops, portfolio heat, correlation,
  long/short separation, borrow cost, borrow availability, and SSR.
- DayTrade optimization now constructs those confirmation features directly
  from warmup minute data. Trade symbols remain separate from feature-only
  SPY/QQQ and explicit sector/industry proxies; ATR, same-window RVOL, gap,
  premarket volume, market direction, relative strength, and correlation use
  only information available at each decision timestamp. Feature data/config
  hashes and exact trial identities are emitted with optimization evidence.
- Minute-cache schema v2 partitions bars by the US/Eastern trading session,
  including extended-hours bars that cross a UTC day or month boundary. Older
  UTC-partitioned manifests fail closed instead of being merged into v2 data.
- DayTrade search is limited to immutable experiment registries. H1-H8 are
  imported only as 20 frozen historical trial declarations; preregistration
  never fabricates their completion, so the global registry starts with zero
  completed historical trials. H9 declares exactly one local trial that changes
  only `reward_risk` from 1.5 to 2.0 on the same dynamic Top-50/H3 baseline and
  can record only that trial when it is actually executed. Research/adaptive
  reconstruction fails closed until immutable, date-aligned H1-H8 OOS return
  histories are supplied in an HMAC-signed version-2 artifact.
  Every historical trial also carries its own verified HMAC proof binding the
  experiment/trial/run identity, run type, current policy, permit interval and
  timestamp, dataset/config/cost/code hashes, execution evidence and the exact
  dated return series used by DSR/PBO. Reused run, permit or execution hashes
  fail closed. A sealed H9 universe must not carry that historical artifact;
  the one-shot run instead consumes a separately signed 2023-2024 adaptive
  validation artifact containing the frozen full-window report and all aligned
  baseline plus 21 trial returns. An unavailable feature-dependent trial
  makes the evidence fail instead of disappearing. Every optimizer run retains
  aligned OOS daily returns for all candidates, exact candidate configs,
  candidate-level trade metrics and per-window metrics, plus reproducible DSR
  and CSCV/PBO inputs and hashes.
- Experiment outcomes are explicit and immutable. Normal H1, Normal H2 and
  DayTrade H9 are `preregistered`, H3 is `candidate_survived` only as the frozen H9
  baseline, and H4-H8 are `retired`. Retired families remain in the 21-trial
  multiplicity ledger but cannot be selected again. They can run only with
  `--historical-audit-replay`, which is marked `selection_eligible=false` and
  is forbidden from fixed-holdout or sealed data. H5 comparisons follow the
  same rule.
- Normal validation is fixed to research 2010-2020 and adaptive development
  2021-2024. DayTrade uses research 2019-2022 and adaptive development
  2023-2024. Those adaptive periods may reject but cannot prove a candidate.
  Because this project has no independent proof that the former
  2025-01-01..2026-06-30 interval was never read, it is permanently exposed;
  the active one-shot sealed period is 2026-08-01..2028-07-31 and cannot be
  claimed until it has completed. Ledger timestamps come from the internal UTC
  clock; caller-supplied future timestamps are denied. Every Normal/DayTrade
  loader rejects an unreleased sealed overlap before market data is opened,
  and the sole permitted DayTrade sealed command is the frozen H3-vs-H9 run.
  Purge/embargo, Deflated Sharpe Ratio, CSCV/PBO, leverage, concentration, cost
  stress, and exact candidate/baseline identity are admission requirements.
- Capital remains locked to 90% normal / 10% DayTrade until both strategy
  families pass independently. Small-capital live promotion additionally
  requires 60 unique Paper sessions with order, ledger, broker, and strategy-
  intent reconciliation under one stable artifact identity.
- `daytrade-live-cycle --submit-orders` is fail-closed and performs no broker
  submission. This remains true until the formal strategy and Paper gates pass.

- The normal allocator applied every constraint as a one-directional shrink, so
  capital freed by the single-stock, sector and liquidity caps went to cash
  permanently and volatility targeting only ever de-levered. On a realistic
  cross-section the book settled near 39% gross exposure against a 100% target
  and used under a fifth of its volatility budget. `[normal]` now exposes
  `redistribute_constrained_capital`, `volatility_targeting` and
  `min_funded_names`, and `NormalAllocationReport` reports
  `capital_utilization` and `risk_utilization`. The repair is declared as
  `normal-h2-capital-efficiency`, a single-variable experiment
  that shares H1's baseline sleeve and adds only
  `capital_efficient_allocation`; the normal family now declares two trials and
  both count towards global DSR/PBO multiplicity. `run_normal_h2_experiment`
  drives both arms from one provider instance and additionally gates on
  `no_capital_redeployment` and `sharpe_degradation`. The code defaults remain
  the legacy profile and the frozen H1 runner rejects every non-default value,
  so H2 cannot contaminate H1. A development-free research replay
  over 64 US large caps confirms the capital repair (utilization 0.43 to 0.87
  in 2010-2020) and rejects H2 as alpha in both the research and adaptive
  windows. Its disposition is therefore `completed_rejected`: it remains in
  global DSR/PBO multiplicity and can run only with
  `--historical-audit-replay` on nonexclusive development data. It also shows
  the tested 15% volatility-target configurations did not satisfy the original
  15% formal drawdown ceiling.
  See
  [docs/normal_capital_efficiency.md](/Users/bytedance/code/StockBot/docs/normal_capital_efficiency.md).

- Two validation defects found while running that experiment are fixed. CSCV
  fold selection used to return "unavailable" whenever the observation count
  had no even divisor, so the PBO gate silently switched itself off on roughly
  half of all runs; `cscv_partition` now trims the remainder and reports
  `dropped_observations`, and with the gate live the H2 research window reports
  PBO 0.715 against a 0.05 threshold. `risk.max_normal_drawdown` was hashed but
  enforced nowhere; `NormalDrawdownPolicy` makes it enforceable on the daily
  risk clock and defaults to disabled.
- The drawdown acceptance ceiling and the operational trigger are separate
  controls even though both currently preserve the original `0.15` value.
  `validation.normal_max_drawdown` drives experiment and formal admission;
  `risk.max_normal_drawdown` drives the running-book review/halt policy. The
  acceptance ceiling is not inferred from the volatility target and cannot be
  relaxed above the named `NORMAL_ACCEPTANCE_MAX_DRAWDOWN = 0.15` policy. H2
  therefore remains rejected rather than being rescued by a looser drawdown
  gate. The DayTrade ceilings remain 0.05 and 0.07 stressed.
- The `[validation]` section had no consumers: every acceptance threshold was a
  literal duplicated between the experiment runners and `strategy_admission`,
  so editing `[validation]` changed nothing and the "runtime configuration
  cannot weaken admission policy" claim was vacuous in both directions. The
  thresholds are now `NormalAcceptanceThresholds` and
  `DayTradeAcceptanceThresholds`, consumed by both, with the cannot-weaken rule
  enforced on the type so no construction path can loosen a gate.
  `RuntimeConfig` exposes them and all six declared keys now drive a gate.
  The other unread keys are consumed too: `execution.slippage_bps_per_side`
  enters the per-side cost rate for both families (default 0.0),
  `portfolio.normal_fraction` is exposed via `RuntimeConfig.normal_capital()`,
  `normal.residual_skip_months` reaches the provider and is frozen at 1 by both
  runners, and `normal.execution_price` no longer offers the unimplemented
  "vwap" mode. `normal.universe_mode`, the broker names and the data feeds
  remain unread and are listed in the design note.
- `risk.max_daytrade_drawdown` had the same enforced-nowhere defect.
  `daytrade.enforce_drawdown_halt` (default false) stops new entries once the
  DayTrade sleeve is down by that fraction from its peak, decided from equity
  observable before the session opens. No DayTrade return claim follows: the
  family still has no admitted strategy.

No normal or DayTrade return improvement is claimed by the repository today.
The historical mega-cap and January-April 2024 results predate the corrected
admission protocol and are retained only as engineering smoke evidence.

## Implemented Core

- Data cleaning and liquid-universe filtering using adjusted close, trading
  calendar forward-fill, history depth, and average dollar volume thresholds.
- Point-in-time fundamental alignment with release-date plus two-day lag.
- Date-partitioned DuckDB/Parquet catalog layout and data-source registry for
  Alpaca, Polygon, yfinance, SEC EDGAR, FRED, FINRA, 13F, Alpaca News/GDELT,
  and PyTrends sources.
- Data coverage and survivor-bias audit gates, including explicit checks that
  delisted ticker history remains retained.
- Injectable API clients for Alpaca daily bars, Polygon minute bars, yfinance
  daily fallback, FRED series, SEC company facts, SEC 13F information tables,
  SEC recent 8-K/Form 4 submissions, SEC Form 4 ownership transactions, FINRA
  short volume, Quandl/Nasdaq short-interest backup, Nasdaq Data Link EPS
  revisions, CBOE VIX history, Polygon options snapshots, Alpaca News, GDELT
  news search, and PyTrends search interest.
- Provider failover helper for primary/backup data APIs.
- Alpha158-style local factor subset for momentum, volatility, volume
  relationship, moving-average deviation, RSI, Bollinger-position, and
  earnings-season signals.
- Cross-sectional factor winsorization/Z-score normalization plus sector and
  market-cap-bucket neutralization helpers.
- Kronos fallback feature extraction that emits `kronos_mean`, `kronos_vol`,
  and `kronos_skew` from recent returns.
- Self-developed factor primitives for earnings revisions, short-interest
  pressure, 13F smart-money position changes, insider activity, earnings
  sentiment/guidance, option IV skew, supply-chain margin pressure, and
  search-trend attention.
- Factor admission gates and factor pool de-correlation using the documented
  IC, ICIR, hit-rate, long-short, decay, OOS, and turnover standards.
- Alpha158 subset acceptance gates for average IC and quintile monotonicity, plus
  the documented 50-70 factor count, Kronos-vs-Alpha signal diversity checks,
  and candidate-pool average correlation validation.
- Candidate-pool builder that keeps a 20-40 factor target range with
  Alpha158/Kronos plus RD-Agent and self-developed factors sharing the
  documented differentiated-alpha bucket.
- IC series, ICIR, industry/group IC, quintile backtest, monotonicity, and
  turnover analytics, plus Regime IC differentiation checks.
- Factor lifecycle tools for IC decay detection and crowded-factor weight
  reduction.
- Rule-based Regime detection, Regime feature engineering, HMM adapter, and
  confidence stabilization, plus HMM/rule consensus validation and Regime
  accuracy evaluation, with an audit helper for the documented 2010-2018 HMM
  training and 2019+ out-of-sample prediction window.
- Regime-aware `TradingAgent` composite scoring plus watchlist entry/exit
  hysteresis, default Regime factor-weight matrix, earnings blackout, stop
  loss, negative-event removal, and R4/R5 clearing. New entries can be gated by
  historical Regime Sharpe. Watchlist sizing scales from 20 to 40 names based
  on account capital, and exit hysteresis relaxes when expected turnover
  exceeds the target.
- Risk manager with documented Regime exposure scaling, single-stock caps,
  sector caps, factor-beta caps, portfolio volatility scaling, Regime
  low-confidence/conflict deleveraging, and drawdown review/halt actions with
  position scaling.
- Overfitting guardrails for holdout length, holdout tuning, cross-Regime
  coverage, and complexity-adjusted IC thresholds.
- Look-ahead review checklist for publication dates, future-column exclusion,
  train/validation separation, survivor-bias review, and adjusted-price usage.
- Quarterly retraining-window generation for self-trained time-series models
  using the previous four years of data, plus a self-trained model adapter that
  feeds Alpha158 and OHLCV features into injected PatchTST/Transformer/LSTM-like
  forecasters.
- Walk-Forward factor-weight optimization that uses only training-window IC
  observations before applying weights to the validation window.
- Trading-cost, tax-lot, broker-reported buying-power/margin/restriction,
  liquidity-shock, Regime turnover-limiting, and performance-summary helpers.
- Inverse-volatility risk parity, simplified mean-variance scoring, portfolio
  capacity estimation, optional SPY hedge overlay, OOS backtest acceptance
  gates, production target gates from system config, Phase 5 train/OOS window
  validation, an executable fixed-parameter OOS momentum backtest runner, and
  Qlib/QuantConnect-style cross-backtest validation.
- Broker-neutral `AutoExecutor` that prepares and preflights an immutable
  differential-rebalance plan before submitting it, using current broker
  account facts. Backup routing is allowed only when primary preparation fails
  before any submit call; a submission exception is treated as uncertain and
  requires reconciliation. It also exposes an emergency exit hook with
  configurable order style/time-in-force, daily next-open/VWAP execution plans,
  Alpaca and IBKR adapters, and a JSONL order journal.
  A legacy submit call returning `None` is not broker-confirmed order state;
  reconciled residual failover can only be added through the shared
  `ExecutionPort` order-state machine.
  Preparation-time failover also requires an independently reconciled
  `backup_current_holdings`; primary-broker holdings are never reused for the
  backup account.
- Independent `daytrade_intraday` event-driven Opening Range Breakout strategy
  for up to 10 stock symbols, with next-event execution, cumulative session
  VWAP, conservative intrabar ordering, realistic cost/capacity handling,
  point-in-time confirmation features, volatility-scaled stops, portfolio heat
  sizing, separate long/short rules, broker-fact risk gates, preregistered
  experiments, JSONL journals, and strategy-specific admission evidence.
  Options remain out of scope.
- DuckDB Parquet write adapter, data coverage gate, daily trading workflow
  orchestration, daily after-US-close schedule plan, monthly RD-Agent discovery
  schedule plan, APScheduler registrar, pipeline runner, monitoring degradation
  logic with three-month IR review actions, daily pipeline health reports,
  Streamlit renderer, and dashboard snapshot model.
- Runtime preflight and Paper Trading acceptance gates.
- Retry/backoff JSON transport, Alpaca pagination support, runtime TOML config
  loader with environment overrides, provider-specific rate-limit reset header
  parsing, live smoke-test gate, data-quality quarantine, structured JSONL event
  logging, pipeline run report, and requirement-level completion audit.
- Critical alert dispatcher for wiring broker/order failures, drawdown review
  actions, and Regime switches into an SMS provider in production.
- LLM earnings-call JSON response parsing for both `sentiment_score` and
  design-example `sentiment`/`confidence` fields, plus SEC 8-K item-code
  mapping for negative event filters.
- Rate-limit handling via `Retry-After`, Paper Trading JSONL run journal, and
  evidence-file loading for completion audits.
- RD-Agent monthly research report summaries and data/model artifact manifests
  that can export DVC-style stage metadata, with admission requiring explicit
  economic rationale before generated factors enter the pool.

## CLI

The package registers a `stockbot` console script when installed. During local
development without installation, run the module directly with `PYTHONPATH=src`.

```bash
PYTHONPATH=src python3 -m stockbot.cli schedule
PYTHONPATH=src python3 -m stockbot.cli preflight --json
PYTHONPATH=src python3 -m stockbot.cli live-smoke --checks-json '[{"name":"alpaca","passed":true},{"name":"fred","passed":false}]'
PYTHONPATH=src python3 -m stockbot.cli paper-run-record --log data/paper_runs.jsonl --period week1 --errors 0 --paper-return 0.01 --backtest-return 0.01
PYTHONPATH=src python3 -m stockbot.cli paper-snapshot-record --log data/paper_snapshots.jsonl
PYTHONPATH=src python3 -m stockbot.cli paper-rollup-snapshots --snapshot-log data/paper_snapshots.jsonl --runs-log data/paper_runs.jsonl --backtest-returns-json '{"2026-W28":0.008}'
PYTHONPATH=src python3 -m stockbot.cli paper-acceptance --runs-json '[{"period":"week1","errors":0,"paper_return":0.01,"backtest_return":0.01},{"period":"week2","errors":0,"paper_return":0.01,"backtest_return":0.01},{"period":"week3","errors":0,"paper_return":0.01,"backtest_return":0.01},{"period":"week4","errors":0,"paper_return":0.01,"backtest_return":0.01}]'
PYTHONPATH=src python3 -m stockbot.cli paper-acceptance --runs-log data/paper_runs.jsonl
PYTHONPATH=src python3 -m stockbot.cli paper-status --log data/paper_runs.jsonl
PYTHONPATH=src python3 -m stockbot.cli paper-evidence --log data/paper_runs.jsonl --evidence-file data/evidence.jsonl
PYTHONPATH=src python3 -m stockbot.cli collect-evidence --evidence-file data/evidence.jsonl --offline-tests-detail "unittest passed" --import-check duckdb_parquet_runtime:duckdb --import-check streamlit_dashboard_runtime:streamlit
PYTHONPATH=src python3 -m stockbot.cli runtime-evidence --evidence-file data/evidence.jsonl
PYTHONPATH=src python3 -m stockbot.cli runtime-smoke --evidence-file data/evidence.jsonl
PYTHONPATH=src python3 -m stockbot.cli backtest-acceptance --summary-json '{"excess_alpha_annual":0.06,"sharpe":1.1,"max_drawdown":0.18}' --annual-turnover 3.5 --two-way-cost-rate 0.0015 --evidence-file data/evidence.jsonl
PYTHONPATH=src python3 -m stockbot.cli oos-backtest-run --data-source auto --evidence-file data/evidence.jsonl
PYTHONPATH=src python3 -m stockbot.cli historical-replay --data-source yfinance --evidence-file data/historical_replay_evidence.jsonl --paper-snapshot-log data/historical_paper_snapshots.jsonl --paper-runs-log data/historical_paper_runs.jsonl --live-snapshot-log data/historical_live_snapshots.jsonl --live-runs-log data/historical_live_runs.jsonl
PYTHONPATH=src python3 -m stockbot.cli daytrade-schedule --json
PYTHONPATH=src python3 -m stockbot.cli daytrade-cache-fill --data-source alpaca --symbols AAPL,MSFT,NVDA,AMZN,META,GOOGL,AMD,TSLA,AVGO,NFLX --start 2024-01-02 --end 2024-12-31 --cache-dir data/daytrade_cache
PYTHONPATH=src python3 -m stockbot.cli daytrade-backtest-run --data-source alpaca --symbols AAPL,MSFT,NVDA,AMZN,META,GOOGL,AMD,TSLA,AVGO,NFLX --start 2024-01-02 --end 2024-01-31 --trades-log data/daytrade_trades.jsonl --daily-log data/daytrade_daily.jsonl --evidence-file data/daytrade_evidence.jsonl
PYTHONPATH=src python3 -m stockbot.cli daytrade-backtest-run --data-source alpaca --symbols AAPL,MSFT,NVDA,AMZN,META,GOOGL,AMD,TSLA,AVGO,NFLX --symbol-filter opening-range --max-active-symbols 4 --require-positive-opening-momentum --start 2024-01-02 --end 2024-01-31 --trades-log data/daytrade_filtered_trades.jsonl --daily-log data/daytrade_filtered_daily.jsonl --evidence-file data/daytrade_filtered_evidence.jsonl
PYTHONPATH=src python3 -m stockbot.cli daytrade-backtest-run --data-source alpaca --symbols AAPL,MSFT,NVDA,AMZN,META,GOOGL,AMD,TSLA,AVGO,NFLX --symbol-filter opening-range --max-active-symbols 4 --require-positive-opening-momentum --max-losing-trades-per-day 2 --start 2024-01-02 --end 2024-01-31 --trades-log data/daytrade_guarded_trades.jsonl --daily-log data/daytrade_guarded_daily.jsonl --evidence-file data/daytrade_guarded_evidence.jsonl
PYTHONPATH=src python3 -m stockbot.cli daytrade-optimize-run --historical-audit-replay --data-source alpaca --symbols AAPL,MSFT,NVDA,AMZN,META,GOOGL,AMD,TSLA,AVGO,NFLX --start 2024-01-02 --end 2024-12-31 --cache-dir data/daytrade_cache --output-file data/daytrade_optimization.json --evidence-file data/daytrade_optimization_evidence.jsonl
PYTHONPATH=src python3 -m stockbot.cli daytrade-optimize-run --experiment-set h6-market --historical-audit-replay --data-source alpaca --symbols AAPL,MSFT,NVDA,AMZN,META,GOOGL,AMD,TSLA,AVGO,NFLX --start 2019-01-02 --end 2022-12-31 --cache-dir data/daytrade_cache --cost-bps 5 --max-volume-participation 0.10 --train-months 12 --validation-months 3 --max-candidates 4 --output-file artifacts/daytrade/h6_market_2019_2022_research.json --evidence-file artifacts/daytrade/h6_market_2019_2022_evidence.jsonl
PYTHONPATH=src python3 -m stockbot.cli daytrade-compare-configs --data-source alpaca --symbols AAPL,MSFT,NVDA,AMZN,META,GOOGL,AMD,TSLA,AVGO,NFLX --start 2024-01-02 --end 2024-12-31 --cache-dir data/daytrade_cache --walk-forward-config-json data/daytrade_optimization.json --output-file data/daytrade_compare.json
PYTHONPATH=src python3 -m stockbot.cli daytrade-compare-configs --historical-audit-replay --data-source alpaca --symbols AAPL,MSFT,NVDA,AMZN,META,GOOGL,AMD,TSLA,AVGO,NFLX --start 2024-09-01 --end 2024-12-31 --cache-dir data/daytrade_cache --cost-bps 5 --max-volume-participation 0.10 --walk-forward-config-json artifacts/daytrade/h5_opening_range_top4_config.json --stress-cost-bps 12 --output-file artifacts/daytrade/h5_2024q4_raw.json
PYTHONPATH=src python3 -m stockbot.cli daytrade-paper-cycle --data-source alpaca --symbols AAPL,MSFT,NVDA --start 2024-01-02 --end 2024-01-02 --trades-log data/daytrade_paper_trades.jsonl --daily-log data/daytrade_paper_daily.jsonl --evidence-file data/daytrade_paper_evidence.jsonl
PYTHONPATH=src python3 -m stockbot.cli daytrade-status --trades-log data/daytrade_paper_trades.jsonl --daily-log data/daytrade_paper_daily.jsonl --min-sessions 60
uv run --extra moomoo python -m stockbot.cli moomoo-simulate snapshot
uv run --extra moomoo python -m stockbot.cli moomoo-simulate place --code US.AAPL --side BUY --quantity 1 --price 1.00 --confirm-simulated-action
uv run --extra moomoo python -m stockbot.cli moomoo-simulate query --order-id 123456789
uv run --extra moomoo python -m stockbot.cli moomoo-simulate cancel --order-id 123456789 --confirm-simulated-action
uv run --extra moomoo python -m stockbot.cli moomoo-simulate reconcile --submission-marker STOCKBOT:0123456789abcdef0123456789abcdef
uv run --extra moomoo python -m stockbot.cli moomoo-paper-canary --preflight-only --confirm-simulated-action
uv run --extra moomoo python -m stockbot.cli moomoo-paper-canary --confirm-simulated-action
PYTHONPATH=src python3 -m stockbot.cli live-soak-record --log data/live_runs.jsonl --day 2026-06-26 --errors 0 --drawdown 0.01 --notes "small-capital live validation"
PYTHONPATH=src python3 -m stockbot.cli live-soak-snapshot-record --log data/live_snapshots.jsonl --equity 100000
PYTHONPATH=src python3 -m stockbot.cli live-soak-rollup-snapshots --snapshot-log data/live_snapshots.jsonl --runs-log data/live_runs.jsonl --baseline-equity 100000
PYTHONPATH=src python3 -m stockbot.cli live-soak-status --log data/live_runs.jsonl --min-sessions 20
PYTHONPATH=src python3 -m stockbot.cli live-soak-evidence --log data/live_runs.jsonl --evidence-file data/evidence.jsonl --min-sessions 20
PYTHONPATH=src python3 -m stockbot.cli acceptance-cycle --evidence-file data/evidence.jsonl --paper-backtest-returns-json '{"2026-W28":0.008}'
PYTHONPATH=src python3 -m stockbot.cli acceptance-schedule --json --evidence-file data/evidence.jsonl
PYTHONPATH=src python3 -m stockbot.cli completion-audit --evidence-json '{"offline_tests":{"passed":true,"detail":"unittest ok"}}'
PYTHONPATH=src python3 -m stockbot.cli completion-audit --evidence-file data/evidence.jsonl
PYTHONPATH=src python3 -m stockbot.cli experiment-acceptance-report --report-file artifacts/candidate-report.json --baseline-report-file artifacts/baseline-report.json --output-file artifacts/acceptance-report.json
```

`experiment-acceptance-report` is a non-market-data verifier: it validates the
canonical report hash and admission flag, and optionally checks the candidate
against a baseline's experiment, logical dataset, actual input, configuration,
code, cost model and cost suite. It does not read market data, claim a permit or
write the exposure ledger. Each `--report-file` may contain either the direct
`experiment_run_report` object or the enclosing CLI payload emitted by a run.
A valid/comparable report is still not evidence of profitable alpha unless its
admission result and external PIT custody evidence also pass.

For any dynamic-universe or fixed/sealed DayTrade read, configure
`STOCKBOT_EXPOSURE_HMAC_KEY` and an absolute
`STOCKBOT_EXPOSURE_LEDGER_PATH`, then pass the same canonical ledger path and a
unique run ID to the command. The append-only exposure claim is written before
cache/API access and binds dates, warmup, dataset, configuration, cost model and
source code. Changing a run ID or copying a manifest cannot reopen an already
exposed exclusive interval. Failed runs still consume the interval.

`--universe-manifest` selects the PIT dynamic mode. Its daily snapshots use
stable security IDs and previous-session-visible tickers. The upstream builder
requires 252 sessions of history, price at least USD 5 and 20 complete sessions
of average dollar volume at least USD 5 million, then deterministically keeps
the 50 most liquid names, breaking ties by stable security ID. Formal H3/H9 and
sealed dynamic reads require the manifest's `selection_policy` to equal the
following object exactly:

```json
{
  "as_of": "prior_close",
  "history_sessions": 252,
  "minimum_price": 5.0,
  "adv_window_sessions": 20,
  "minimum_adv_dollars": 5000000.0,
  "limit": 50,
  "tie_break": "stable_security_id"
}
```

The manifest must also bind `minute_data_hash` to the actual frozen minute-bar
cache. StockBot verifies that digest after loading the bars. Formal H3/H9 and
sealed reads additionally require a trusted HMAC-signed trading calendar with
complete coverage and a trusted HMAC-signed PIT construction proof binding the
source dataset, calendar, exact selection policy, input rows and ranked stable-
ID snapshots. Research/adaptive H9 reconstruction also requires a separately
signed historical H1-H8 artifact. A sealed H9 manifest must omit it and instead
uses the independently
signed artifact configured by
`STOCKBOT_DAYTRADE_ADAPTIVE_VALIDATION_ARTIFACT_PATH`. The permit's dataset
identity is a composite hash of the source PIT
`dataset_hash`, complete universe-manifest hash, minute-data hash, calendar
hash, construction-proof hash and historical-artifact hash; `--dataset-hash`,
when provided, must pin that composite identity. Configure the corresponding
trusted issuer maps through `STOCKBOT_TRUSTED_CALENDAR_KEYS_JSON`,
`STOCKBOT_TRUSTED_UNIVERSE_KEYS_JSON` and
`STOCKBOT_TRUSTED_EXPERIMENT_KEYS_JSON`. Dynamic cache data must live under the
external `STOCKBOT_DATA_ROOT`, which itself must be outside this repository.
The static `--symbols` and repository-local `data/daytrade_cache` examples above
are legacy development smoke only; static mode retains the maximum of ten names
and cannot satisfy formal admission. See
[`docs/pit_bundle_contract.md`](docs/pit_bundle_contract.md) for the external
bundle and permit contract.

`daytrade-optimize-run` retains the immutable `h1-h4` compatibility ledger,
but that set contains retired H4 and therefore requires
`--historical-audit-replay`. H6, H7 and H8 are also retired and accept only the
same audit flag; their output cannot select or promote a candidate. Use
`--experiment-set h3-rvol` only to reconstruct the frozen RVOL-2 baseline on
the dynamic PIT universe; its walk-forward winner is diagnostic and
`selection_eligible=false`.
`--experiment-set h9-rvol-reward-risk` requires the dynamic PIT universe and
executes only the frozen H3 baseline plus the single 2R candidate. Its local
completed-trial count can become one only after that H9 trial executes, while
statistical validation retains all 21 H1-H9 declarations and fails closed when
the prior aligned returns needed for global DSR/PBO are unavailable.
Research/adaptive reconstruction imports a trusted signed artifact containing
exactly the 20 H1-H8 trial IDs, aligned OOS dates/returns and per-trial permit
and execution identities. Importing preregistrations alone leaves their
completed count at zero. The sealed run does not rerun H1-H8: it verifies the
signed 2023-2024 adaptive report and uses its aligned baseline plus 21 trial
series only for the frozen DSR/PBO calculation. Research and
adaptive-development runs may report only `candidate_survived`; only a
completed canonical sealed permit can produce `admit`.
The acceptance path freezes the complete `DayTradeConfig`, the 3-month/1-month
walk-forward split, two effective configurations, 60-day feature warmup, one-
day purge/embargo and empty feature overrides before the data claim. The 5 bps
base cost and 12 bps stress cost share one permit cost identity. Parameter or
split drift is rejected instead of creating an undeclared H9 variant.
RVOL is separate from the legacy minute-bar `volume_multiplier`, and feature
filtering does not implicitly turn on correlation-based position resizing.
Each subset preserves its own declared trial ledger instead of silently adding
an interaction to H1-H4.
When an independent trading calendar is available, multi-month cache loads
fetch and verify one missing calendar month at a time. Completed month
manifests survive interruption, and a resumed run requests only the remaining
symbol-month partitions.

Run the read-only readiness check before any claim:

```bash
PYTHONPATH=src python -m stockbot.cli strategy-readiness \
  --strategy normal-h1 --access-mode research \
  --start 2010-01-01 --end 2020-12-31 \
  --warmup-start 2007-01-01 --warmup-end 2009-12-31

PYTHONPATH=src python -m stockbot.cli strategy-readiness \
  --strategy normal-h2 --access-mode research \
  --start 2010-01-01 --end 2020-12-31 \
  --warmup-start 2007-01-01 --warmup-end 2009-12-31

PYTHONPATH=src python -m stockbot.cli strategy-readiness \
  --strategy daytrade-h9 --access-mode sealed \
  --start 2026-08-01 --end 2028-07-31 \
  --warmup-start 2026-06-02 --warmup-end 2026-07-31
```

`normal-h2` is retained in readiness output for audit visibility, but
`experiment_completed_rejected` always prevents a new candidate claim.

It reports `ready_to_claim`, `formal_time_eligible`, stable blocked reason
codes and checks deferred until after a permit. It does not create a ledger
claim, read Parquet/minute contents, call a market-data API, or print paths,
keys or signatures. Before reporting readiness it checks that the PIT manifest
and every table's partition metadata cover the complete request/warmup read
interval, the canonical ledger parent is writable, and any existing ledger
chain verifies with its configured HMAC key. The JSON result includes both
`requested_interval` and the warmup-expanded `read_interval`. Partition
coverage here is the manifest's inclusive calendar-date coverage; exact
trading-session coverage remains a permit-gated deferred check.
Readiness accepts only the frozen full request window for the selected family
and access mode; a convenient positive subwindow cannot become evidence.
Normal warmups are frozen with the experiment protocol: research uses
`2007-01-01..2009-12-31`, adaptive development uses
`2018-01-01..2020-12-31`, and sealed uses
`2023-08-01..2026-07-31`.
DayTrade H3/H9 additionally require the same 60-calendar-day warmup used by
the execution command, and H3 readiness is research-only. A configured Normal
adapter is identified from source without importing or executing its module;
an explicitly injected test/deployment callable is inspected directly.

## Runtime Setup

The default project environment contains only the dependency-free offline core.
Keep optional profiles separate instead of installing every extra into the
same project-local `.venv`: the combined research stack includes Torch,
PyArrow, MLflow, Transformers, RD-Agent, Jupyter, and their transitive
dependencies, and can exceed 1.8 GB without increasing the source-code size.

Use `uv` to synchronize only the profile needed for the current task:

```bash
uv sync --locked --no-dev
uv sync --locked --extra data
uv sync --locked --extra live
uv sync --locked --extra moomoo
uv sync --locked --extra research
uv sync --locked --extra dashboard
```

Running `uv sync --locked --no-dev` again removes optional packages from the
project environment while preserving every optional feature in `uv.lock` for
later on-demand installation. Keep `UV_CACHE_DIR` outside the repository when
using a project-specific cache.

The equivalent `pip` profile installs remain available:

```bash
python3 -m pip install ".[data]"
python3 -m pip install ".[live]"
python3 -m pip install ".[moomoo]"
python3 -m pip install ".[research]"
python3 -m pip install ".[dashboard]"
```

Use [config/stockbot.example.toml](/Users/bytedance/code/StockBot/config/stockbot.example.toml)
as the starting point for local configuration. Runtime configuration has seven
canonical sections: `[data]`, `[normal]`, `[daytrade]`, `[portfolio]`, `[risk]`,
`[execution]`, and `[validation]`. Unknown sections and keys are rejected rather
than ignored.

Alpaca credentials must come only from the environment and must never be stored
in TOML:

```bash
export ALPACA_API_KEY="..."
export ALPACA_SECRET_KEY="..."
```

Moomoo support is deliberately isolated in the `moomoo` profile and uses the
official `moomoo-api` package. Install and start the signed Moomoo OpenD GUI,
log in manually, and leave its local API listener on `127.0.0.1:11111`. A Futu
OpenD installation cannot be reused for this connection. StockBot never accepts
a trading password and never calls `unlock_trade`. The adapter is limited to
US-equity simulated accounts. Every account-scoped SDK operation that accepts
an environment passes `TrdEnv.SIMULATE` explicitly; `get_acc_list()` has no
environment parameter and is used only to select a validated simulated
account. The CLI accepts only a loopback OpenD host. Connection values may be
supplied without credentials:

```bash
export MOOMOO_OPEND_HOST="127.0.0.1"
export MOOMOO_OPEND_PORT="11111"
export MOOMOO_SIM_ACC_ID="123456789"
export MOOMOO_SECURITY_FIRM="FUTUINC"
```

`FUTUINC` is the official Moomoo US `SecurityFirm` enum value; it does not mean
that StockBot connects to or can reuse Futu OpenD. If `MOOMOO_SIM_ACC_ID` is
omitted, StockBot proceeds only when Moomoo OpenD exposes one unambiguous active
simulated account. `snapshot` reads account, position, and today-order state
with cache refresh. `place` submits only a DAY limit order; `query` reconciles
its broker status; `cancel` cancels one explicit order. If an order is no
longer in today's list, query and submission-marker reconciliation fall back
to one explicit 90-calendar-day history window and still require one exact
match. Simulated-account writes require `--confirm-simulated-action`. Any
non-success or exception after a submit/cancel call is reported as
`reconciliation_required` and is never retried automatically. Returned
non-terminal order states also carry that flag so callers know to query again.

`moomoo-paper-canary` is a separate engineering canary for the Moomoo US
simulated account. It is not the Normal H1 or DayTrade H9 strategy and its
orders, P&L, and session records cannot be admitted as return evidence or used
to satisfy `PaperPromotionGate`. The default independent universe is AAPL,
MSFT, NVDA, AMZN, META, GOOGL, AMD, TSLA, AVGO, and NFLX. The canary is
long-only, sizes whole lots within both available buying power and
`--capital-fraction` (default 10%), persists reconciliation state, and uses a
single-instance lock. There is no separate maximum-share or maximum-position
count switch; that does not bypass the configured symbol universe, buying
power, whole-lot sizing, long-only rule, stale-quote rejection, or mandatory
end-of-day liquidation gate. The liquidation lead is checked against the
configured symbol count and broker write capacity; a basket that cannot fit
the worst-case sequence of one cancellation plus one exit attempt per symbol
before close is rejected before the run starts rather than accepted with an
impossible flattening promise.

Run the non-ordering preflight first. `--preflight-only` checks the simulated
account, market session, quote freshness, state file, and lock path but never
submits or cancels an order. The full command requires
`--confirm-simulated-action`, emits machine-readable NDJSON events, and blocks
while the canary is active. By default it begins flattening ten minutes before
the regular US close and returns success only after broker reconciliation
proves the account is flat. A HALT or unresolved broker outcome exits with code
2; a definite local configuration failure exits with code 1.

If any submit/cancel result becomes unknown, the durable session enters
containment permanently: it never resumes the remaining entry basket, bulk-
reconciles today's orders plus one bounded history window to stay within OpenD
query limits, cancels known working entries once, and flattens any confirmed
fills as soon as fresh account state is available. The active process continues
read-only reconciliation after a lost entry, exit, or cancel acknowledgement
until the broker state converges or the regular session closes; it never repeats
the uncertain write. A restart from `ENTERING`, `LIQUIDATING`, or an ENTRY
`SEND_STARTED` state goes directly to containment and never resumes the entry
basket. Missing cached rows are never treated as proof that an unknown order was
not submitted. Polling cannot be configured below five seconds. Within one
process, fresh funds, positions, today-order, and history-order reads share
endpoint/account-specific SDK limiters; place and cancel calls also enforce
their documented sliding windows and minimum spacing. Place/cancel WAL
timestamps preserve write capacity across canary restarts, with entry capacity
reserved for potential exits. A quote that ages while waiting for capacity is
fetched again; an ENTRY quantity is then reduced to keep its original notional
budget. Every write is rechecked after WAL persistence and the final event
callback, and again inside the adapter after any shared-quota wait, immediately
before the SDK call. If the deadline crossed while the SDK call provably had not
begun, the intent is durably marked `ABORTED`; a delayed quote, suspended process,
or exhausted rate window therefore cannot start another entry after the gate.
Event-output failures do not interrupt broker containment. Transient
order/account/quote reads during liquidation are retried at a bounded cadence
until they recover or the regular session closes; stale exit quotes are
refreshed, and non-atomic position/order snapshots are reread until their
canary-owned quantities converge. Cumulative filled quantity and terminal order
evidence may only move forward across rereads; a temporarily missing order or
position row, or a regressed broker row, is never accepted as proof of `FLAT`.

The durable files default to
`data/moomoo_paper_canary_state.json` and
`data/moomoo_paper_canary_state.lock`. Run from the repository root and never
start the same simulated account concurrently with a different state/lock
path; a different working directory would otherwise create a separate lock.
The adapter coordinates quotas across clients in the same process, but it does
not coordinate a second process using a different lock path.
On the next trading day, a completed state is copied immutably into
`data/moomoo_paper_canary_state_sessions/` before a new run is created; an
unfinished prior state never rolls into a new entry run and may proceed only
through current-session containment/reconciliation. A conflicting archive or
an unresolved state older than the broker's 90-calendar-day history boundary
requires manual recovery. A prior-day HALT remains eligible for current-day
containment even if the process is interrupted while waiting for that day's
open; a HALT actually raised during the current session remains a manual gate.
To change timing or allocation without
adding hidden strategy behavior, use the explicit controls:

```bash
uv run --extra moomoo python -m stockbot.cli moomoo-paper-canary \
  --symbols AAPL,MSFT,NVDA,AMZN,META,GOOGL,AMD,TSLA,AVGO,NFLX \
  --capital-fraction 0.10 \
  --flatten-minutes-before-close 10 \
  --poll-seconds 5 \
  --max-quote-age-seconds 5 \
  --marketable-limit-buffer-bps 50 \
  --confirm-simulated-action
```

For real validation, first run `snapshot`, then use the smallest acceptable
limit order and complete `place` -> `query` -> `cancel` -> `query`. Local unit
tests with a fake SDK and a successful connection smoke do not establish live
profitability or trustworthy return evidence.

The former `[broker]` section remains a read-only compatibility input and maps
to `[execution]`; it cannot be used together with `[execution]`. New
configuration should use `[execution]` exclusively. The public configuration
payload used for hashing excludes both credentials.

Runtime configuration cannot weaken admission policy: complete data coverage
must remain enabled, live daytrade submission must remain disabled, and the
validation thresholds in the example are minimum floors. Live authority is a
separate admission state, not a TOML setting.

Point-in-time research datasets use stable security IDs and model listing,
delisting, classification, universe membership, fundamentals, and corporate
actions with separate effective dates and `available_at` timestamps. Interval
termination facts have their own availability timestamp, so a historical
snapshot cannot see a later delisting or symbol change. Market capitalization
is a separate point-in-time observation rather than a static classification.

Historical universe construction consumes raw close/raw volume observations
whose availability is no earlier than the signed exchange-calendar close, then
applies history and dollar-volume rules over the calendar's completed sessions.
Final strategy admission jointly verifies HMAC-bound security-master, price, and
calendar artifacts; the signed calendar must cover the complete requested
window without gaps, and every session's price security IDs must exactly match
that session's point-in-time universe. Trust keys must come from an
environment-controlled trust store, never strategy TOML. Free/development data
remains research-only. This repository ships neither production vendor data nor
trust keys, so these guardrails do not establish a credible return improvement
by themselves.

The [Dockerfile](/Users/bytedance/code/StockBot/Dockerfile) uses Python 3.11 and
defaults to `stockbot preflight --json`.

## Local Verification

The implementation and production runtime both require Python 3.11 or newer.

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --locked python -m unittest discover -s tests -q
PYTHONPYCACHEPREFIX=/tmp/stockbot-pycache uv run --locked python -m compileall -q src tests
```

Current test coverage is standard-library `unittest` only and does not require
network access, broker credentials, pandas, numpy, Qlib, or model downloads.
The command output is the source of truth for the current discovered-test total
and pass/fail state; this README deliberately does not hard-code a count that
will become stale as contracts are added.

For full production runtime validation, keep the all-extras environment outside
the source tree so it does not turn the repository into a multi-gigabyte
workspace:

```bash
export UV_PROJECT_ENVIRONMENT="$HOME/.cache/stockbot/full-runtime"
uv sync --locked --all-extras
```

The full-profile runtime import evidence passes for DuckDB, Qlib, Chronos,
RD-Agent, Streamlit, and APScheduler. Adapter-level `runtime-smoke` additionally
passes DuckDB, RD-Agent, Streamlit, and APScheduler; Qlib still requires a
populated `~/.qlib/us_data` provider, and Chronos loads a model-backed pipeline
when `STOCKBOT_CHRONOS_MODEL_ID` is set. Unset `UV_PROJECT_ENVIRONMENT` before
returning to the project-local minimal environment.

## External Integration Boundaries

The following design items are intentionally represented as interfaces or local
fallbacks until credentials and dependencies are available:

- Alpaca / IBKR: provide credentials and production client objects to the
  implemented adapters.
- Moomoo OpenD: the implemented integration uses the official `moomoo-api` SDK
  and supports read-only simulated-account snapshots and explicit simulated
  limit-order place/query/cancel operations, plus the isolated engineering
  Paper canary described above. It has no real-account mode and does not unlock
  trading. Moomoo OpenD must be installed, started, and logged in manually;
  Futu OpenD cannot be reused. A successful connectivity, order smoke, or
  canary session is operational evidence, not live-return or
  `PaperPromotionGate` reconciliation evidence.
- Qlib Alpha158: provide a US Qlib provider at `~/.qlib/us_data` and the real
  Alpha158 factory to `stockbot.integrations.qlib.QlibAlpha158Adapter`.
- Chronos / TimesFM / Moirai: attach model inference behind
  `stockbot.integrations.chronos.ChronosAdapter`.
- HMM Regime model: feed predictions into `stockbot.regime.StableRegimeDetector`.
- DuckDB / Parquet: local adapter smoke passes with the installed runtime.
- Live data APIs: pass real connections/transports and credentials to the
  implemented adapters.
- Completion status: use `completion-audit` to keep missing live evidence
  explicit instead of treating offline tests as full production completion.
- Evidence workflow: use `collect-evidence` and `live-smoke --evidence-file`
  to append JSONL evidence, then run `completion-audit --evidence-file`.
  For the time-based Paper Trading and live-soak gates, run `acceptance-cycle`
  daily after close to capture snapshots, roll up completed periods, append
  current paper/live evidence, and emit the current completion-audit status.
  Use `acceptance-schedule` to print the after-close schedule and command
  template for cron/APScheduler deployment.
- Runtime workflow: use `runtime-evidence` to append the default runtime import
  evidence for DuckDB, Qlib, Chronos, RD-Agent, Streamlit, and APScheduler.
- Runtime smoke workflow: `stockbot.runtime_smoke.RuntimeSmokeSuite` exercises
  the DuckDB, Qlib, Chronos, RD-Agent, Streamlit, and APScheduler adapters with
  injected runtime objects before recording evidence.
- `runtime-smoke --evidence-file` records adapter-level smoke evidence. The
  default CLI path uses `stockbot.runtime_factory.RuntimeSmokeFactory` to load
  the installed DuckDB, Qlib, RD-Agent, Streamlit, and APScheduler runtimes; if
  packages are missing, it records structured failure evidence instead of
  raising. Chronos loads a model-backed pipeline only when
  `STOCKBOT_CHRONOS_MODEL_ID` is set, so smoke tests do not trigger implicit
  model downloads.
- Legacy OOS smoke workflow: `oos-backtest-run` still downloads adjusted-close
  data and exercises the fixed 2015-2022 / 2023-2024 momentum path for backward
  compatibility. Its mega-cap universe and adjusted-close inputs cannot produce
  `normal_strategy_admission`. Final admission must use the fixed protocol,
  verified point-in-time artifacts, corrected baseline, and sealed holdout.
- PIT normal workflow: `normal-experiment-run --experiment-id` explicitly
  dispatches either `normal-h1-quality-diversification` (the sole active
  quality-alpha experiment) or `normal-h2-capital-efficiency` (the allocator
  utilization audit). H2 is terminally rejected as Alpha and requires
  `--historical-audit-replay`; fixed/sealed data are refused. Both paths reuse
  the same permit-bound input, sessions, baseline provider and cost protocol;
  selecting H2 does not change H1's frozen configuration. H1 fixes `top_n=20`,
  next-open execution, 5 bps
  per-side cost and 10 bps stress, and gives both arms identical sessions and
  risk constraints. A deployment
  adapter receives only a permit-bound `BundleReadSession`. Each successful
  table read creates a core-owned receipt binding its interval, partition paths
  and partition hashes; the core refuses to seal the run unless every required
  normal table was read under the same permit, dataset and strategy family and
  the union of its receipts covers the complete permit interval without gaps.
  Backtest sessions must exactly match the core-observed trading-calendar
  sessions. The shared core view also fixes 252 asset-return sessions, 20 ADV
  sessions and a USD 5 million minimum ADV before either experiment arm runs.
  Adapter output remains untrusted until `NormalH1ExperimentInputs` binds that
  read evidence together with the adapter code/schema-mapping hash and the
  backtest-session hash. Baseline and candidate providers are then created from
  that same sealed input over the exact admitted `RUSSELL1000 + SP500` union.
  Each constituent universe is independently attested, while daily stable IDs
  and prices must equal the deduplicated union. The adapter hash participates
  in both configuration and source-code identity. Sealed runs also require an
  absolute `STOCKBOT_NORMAL_ADAPTER_ATTESTATION_PATH` and trusted issuer keys in
  `STOCKBOT_TRUSTED_NORMAL_ADAPTER_KEYS_JSON`; the signed assertion binds the
  actual callable/schema and PIT bundle identity. Formal adapters must be plain
  functions without closure/default-argument runtime state. The trust evidence
  is verified before claim and again after execution. Standard CLI execution
  resolves the deployment function from `--adapter module:callable` or
  `STOCKBOT_NORMAL_ADAPTER`; in-process injection remains a test/integration
  seam. The supplied ledger must equal the absolute canonical
  `STOCKBOT_EXPOSURE_LEDGER_PATH`. `normal-experiment-run`
  requires an evidence file. Without a deployment-supplied adapter and real PIT
  bundle, the command exits before claiming the interval or opening a protected
  partition and does not report a return.
- Historical replay workflow: use `historical-replay` to replay the OOS
  validation daily equity curve into separate Paper Trading and live-soak JSONL
  logs. This validates logging, rollup, status, and evidence plumbing with
  historical data; its output is marked `counts_for_real_acceptance=false` and
  does not replace the real elapsed-time Paper Trading or live-soak gates.
- Daytrade workflow: use `daytrade-backtest-run` to validate the independent
  `daytrade_intraday` Opening Range Breakout + VWAP strategy on minute bars,
  then run `daytrade-paper-cycle` and monitor progress with `daytrade-status`.
  Static compatibility mode accepts at most 10 stock symbols per run and defaults to
  `AAPL,MSFT,NVDA,AMZN,META,GOOGL,AMD,TSLA,AVGO,NFLX`; options are intentionally
  out of scope. This static path is a legacy development smoke path even when
  it emits materialization/output hashes; it cannot establish DayTrade alpha
  or satisfy formal admission. Dynamic PIT mode accepts a daily Top-50
  universe manifest but
  retains the three-position, six-trade and 10% capital limits. Use
  `--symbol-filter opening-range` with `--max-active-symbols`
  to restrict each day to the strongest opening candidates without using future
  intraday data; `--require-positive-opening-momentum` excludes weak opens.
  Use `--max-losing-trades-per-day` or
  `--max-consecutive-losing-trades-per-day` to stop opening new daytrade
  positions after a configured number of realized losses while still managing
  already-open positions normally.
  Use `daytrade-cache-fill` to materialize minute bars outside formal protected
  windows before long backtests. `daytrade-optimize-run` executes immutable,
  preregistered single-variable families with purged/embargoed walk-forward
  windows; the previous large mechanical grid is no longer used. H9 changes
  only the reward/risk target from 1.5R to 2R on the frozen H3 configuration.
  The full baseline configuration and walk-forward protocol are frozen before
  claim; optimization evidence records declared/completed trial counts and
  never enables live trading. Formal Normal and DayTrade evidence copies the
  permit's dataset/config/cost/code hashes and current policy version verbatim.
  Formal completion now requires independent `normal_strategy_admission`,
  `daytrade_strategy_admission`, and `paper_reconciliation_60_sessions`
  evidence; historical backtest or soak records cannot substitute for them.
  `daytrade-live-cycle` is currently retained for historical replay only.
  Supplying `--submit-orders` unconditionally returns
  `live_trading_disabled`; it does not submit real broker orders.
- Paper Trading workflow: record daily Paper Trading equity with
  `paper-snapshot-record`, roll completed ISO weeks into `paper_runs.jsonl` with
  `paper-rollup-snapshots` and the matching backtest weekly returns, check
  progress with `paper-status`, then write audit evidence with `paper-evidence`.
  These legacy weekly tools remain useful for monitoring, but live promotion
  additionally requires `PaperPromotionGate`: at least 60 unique real Paper
  sessions, stable dataset/config/code hashes, prior strategy admission, and
  successful order/ledger/broker/intent reconciliation on every session.
  `paper-run-record` remains available for manually supplied weekly returns but
  cannot by itself satisfy the reconciliation gate.
- Live soak workflow: record daily live equity snapshots with
  `live-soak-snapshot-record`, roll snapshots into drawdown sessions with
  `live-soak-rollup-snapshots`, check progress with `live-soak-status`, then
  convert the accumulated log into `live_trading_soak` evidence with
  `live-soak-evidence`. `live-soak-record` remains available for manually
  supplied daily drawdown observations.

See [docs/implementation_status.md](docs/implementation_status.md) for the
current requirement-by-requirement status against the design.
