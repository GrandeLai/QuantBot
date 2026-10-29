# Point-in-time Parquet bundle contract

StockBot does not keep production market data in the source repository. Set
`STOCKBOT_DATA_ROOT` to an existing directory outside the repository. That
directory must contain `manifest.json` and the immutable Parquet partitions
declared by the manifest.

This is a vendor-neutral envelope, not a vendor quality certification. A feed
is usable for formal validation only when its source can independently prove
historical membership, delistings, identifier changes, corporate actions and
the availability time of every observation.

## Required manifest identity

`manifest.json` has exactly these top-level fields:

```json
{
  "format_version": 1,
  "provider": "vendor-name",
  "bundle_id": "immutable-bundle-id",
  "schema_version": "vendor-schema-version",
  "quality_tier": "point_in_time_verified",
  "effective_date_timezone": "America/New_York",
  "created_at": "2026-07-17T00:00:00Z",
  "coverage_start": "2010-01-01",
  "coverage_end": "2028-07-31",
  "tables": {},
  "dataset_hash": "64 lowercase hex characters"
}
```

`dataset_hash` is SHA-256 over the canonical JSON manifest after removing the
`dataset_hash` field. Canonical JSON uses sorted keys, ASCII output and compact
separators. Changing any provider, schema, coverage, table, column, partition
path or partition digest therefore changes the dataset identity.

Each table declares:

```json
{
  "columns": ["..."],
  "access_date_column": "available_at",
  "partitions": [
    {
      "path": "daily_bars/2026.parquet",
      "sha256": "64 lowercase hex characters",
      "coverage_start": "2026-01-01",
      "coverage_end": "2026-12-31",
      "row_count": 1,
      "size_bytes": 1
    }
  ]
}
```

Partition paths are relative `.parquet` paths, unique across the bundle and
cannot contain `..`. Partitions within one table must be ordered, disjoint and
inside bundle coverage. Before a selected partition is decoded, its file size
and SHA-256 digest are verified.

## Required tables and columns

| Table | Required columns |
|---|---|
| `security_identities` | `security_id`, `issuer_id`, `listed_at`, `delisted_at`, `available_at` |
| `universe_memberships` | `security_id`, `universe`, `effective_from`, `effective_to`, `available_at` |
| `delistings` | `security_id`, `delisting_date`, `delisting_return`, `available_at` |
| `ticker_history` | `security_id`, `ticker`, `exchange`, `effective_from`, `effective_to`, `available_at` |
| `classifications` | `security_id`, `sector`, `industry`, `effective_from`, `effective_to`, `available_at` |
| `market_caps` | `security_id`, `session_date`, `market_cap`, `available_at` |
| `fundamentals` | `security_id`, `fiscal_period_end`, `return_on_equity`, `gross_margin`, `debt_to_assets`, `accrual_ratio`, `available_at` |
| `corporate_actions` | `security_id`, `action_type`, `effective_date`, `value`, `available_at` |
| `trading_calendar` | `exchange`, `session_date`, `open_at`, `close_at`, `available_at` |
| `daily_bars` | `security_id`, `session_date`, `open`, `high`, `low`, `close`, `volume`, `available_at` |
| `minute_bars` | `security_id`, `timestamp`, `open`, `high`, `low`, `close`, `volume`, `available_at` |
| `total_return_vintages` | `security_id`, `period_end`, `index_level`, `vintage_at`, `available_at` |

Additional columns are allowed, but missing required tables or columns fail
closed. All joins and contribution reports use stable `security_id`; ticker is
only a point-in-time execution label.

## Exposure permit

Set independent runtime trust roots and locations before a validation read:

```bash
export STOCKBOT_DATA_ROOT=/absolute/path/outside/StockBot/pit-bundle
export STOCKBOT_EXPOSURE_LEDGER_PATH=/absolute/path/exposure-ledger.jsonl
export STOCKBOT_EXPOSURE_HMAC_KEY='a separately managed secret'
export STOCKBOT_TRUSTED_CALENDAR_KEYS_JSON='{"calendar-issuer":"hex-encoded-key"}'
export STOCKBOT_TRUSTED_UNIVERSE_KEYS_JSON='{"universe-issuer":"hex-encoded-key"}'
export STOCKBOT_TRUSTED_EXPERIMENT_KEYS_JSON='{"experiment-issuer":"hex-encoded-key"}'
export STOCKBOT_NORMAL_ADAPTER_ATTESTATION_PATH=/absolute/path/normal-adapter-attestation.json
export STOCKBOT_TRUSTED_NORMAL_ADAPTER_KEYS_JSON='{"adapter-auditor":"hex-encoded-key"}'
export STOCKBOT_NORMAL_ADAPTIVE_VALIDATION_ARTIFACT_PATH=/absolute/path/normal-adaptive-validation.json
export STOCKBOT_TRUSTED_NORMAL_TRIAL_KEYS_JSON='{"normal-trial-auditor":"hex-encoded-key"}'
export STOCKBOT_DAYTRADE_ADAPTIVE_VALIDATION_ARTIFACT_PATH=/absolute/path/daytrade-adaptive-validation.json
```

Every protected PIT/dynamic cache fill, backtest, optimizer, comparison or
sealed run must call `ExposureLedger.claim()` before reading a cache partition
or invoking a market data API. The signed `DataAccessPermit` binds strategy
family, access mode, request interval, complete warmup interval, dataset hash,
configuration hash, cost-model hash and source-code hash. Ledger schema v2
names two inclusive intervals explicitly:

- `read_interval` is the complete permitted read, including warmup;
- `protected_holdout_interval` is the request/experiment interval used for
  exclusivity and anti-replay.

This separation keeps all warmup data permission-gated without letting a
research warmup accidentally consume a future holdout.

The read-only readiness pass checks partition manifests as inclusive
calendar-date coverage. It does not open Parquet rows or claim that every
exchange session is present; exact trading-session coverage is verified only
after a permit by the signed calendar and materialized read evidence.

The bundle checks both the requested slice and the complete physical coverage
of every partition it must open. A yearly partition cannot be opened with a
permit covering only one month; partitioning must be fine enough for the
intended validation windows. Hashing a partition is itself treated as data
access.

The append-only JSONL ledger uses an HMAC-authenticated hash chain and records
denied attempts as well as successful claims. Changing `run_id`, copying a
manifest or omitting warmup does not create a new unexposed interval. Formal
fixed/sealed access must use the absolute canonical ledger path from
`STOCKBOT_EXPOSURE_LEDGER_PATH`.

An exact retry of the same granted `run_id` and manifest is idempotent and
returns the original permit without appending another record. Reusing a
`run_id` with a different manifest is a chained denial. Existing schema-v1
records remain verifiable; because v1 did not distinguish the two intervals,
their complete historical read interval remains protected during migration.
Denied retries are also idempotent and return the first signed denial without
growing the chain. Historical v1 records for the exposed
`2025-01-01..2026-06-30` sealed window can be verified only; converting them
to `DataAccessPermit` or reclaiming that legacy window is forbidden.

The ledger records its own UTC observation time. A caller-provided future
`claimed_at` is denied and cannot create a permit. A sealed permit is rechecked
against the canonical ledger and current time on every protected read, so a
serialized permit cannot be replayed before the window closes. Legacy static
Normal and DayTrade loaders also reject any overlap with the formal sealed
interval when no exact permit exists. DayTrade reserves sealed access
exclusively for `daytrade-optimize-run --experiment-set
h9-rvol-reward-risk`; cache fill, backtest, compare and standalone H3 cannot
consume the one-shot window.

## DayTrade daily universe manifest

Dynamic DayTrade commands receive a small JSON manifest through
`--universe-manifest`. It is derived from the PIT bundle before minute bars are
loaded and has this shape:

```json
{
  "dataset_hash": "the PIT bundle dataset hash",
  "selection_policy": {
    "as_of": "prior_close",
    "history_sessions": 252,
    "minimum_price": 5.0,
    "adv_window_sessions": 20,
    "minimum_adv_dollars": 5000000.0,
    "limit": 50,
    "tie_break": "stable_security_id"
  },
  "minute_data_hash": "64 lowercase hex characters",
  "trading_calendar": {
    "exchange": "XNYS",
    "effective_date_timezone": "America/New_York",
    "issuer": "calendar-issuer",
    "issuer_version": "immutable-version",
    "generated_at": "2026-07-17T00:00:00Z",
    "coverage_start": "2024-01-02",
    "coverage_end": "2024-01-03",
    "missing_intervals": [],
    "sessions": [
      {"session_date": "2024-01-02", "close_at": "2024-01-02T21:00:00Z"},
      {"session_date": "2024-01-03", "close_at": "2024-01-03T21:00:00Z"}
    ],
    "content_hash": "64 lowercase hex characters",
    "signature": "HMAC-SHA256 over the canonical signed calendar"
  },
  "construction_evidence": {
    "proof_type": "stockbot_pit_bundle_core_v1",
    "issuer": "universe-issuer",
    "source_dataset_hash": "the PIT bundle dataset hash",
    "calendar_hash": "trading_calendar.content_hash",
    "selection_policy_hash": "canonical selection-policy hash",
    "input_rows_hash": "64 lowercase hex characters",
    "ranked_snapshots_hash": "canonical stable-ID snapshot hash",
    "content_hash": "64 lowercase hex characters",
    "signature": "HMAC-SHA256 over content_hash"
  },
  "expected_sessions": ["2024-01-02", "2024-01-03"],
  "snapshots": [
    {
      "trading_day": "2024-01-02",
      "ticker_by_security_id": {
        "stable-security-id": "AAPL"
      }
    }
  ],
  "suspensions": [
    {
      "trading_day": "2024-01-03",
      "security_id": "stable-security-id"
    }
  ]
}
```

Snapshots must be unique and date-sorted, contain no more than 50 active
members and exactly cover every declared session in the complete trade/warmup
read interval. The alternative `members` array may contain
`{"security_id": "...", "ticker": "..."}` objects. A suspension is accepted
only when it references an active stable ID on that session. Any other missing
minute data fails closed rather than silently shrinking the universe.

The example shows only two `trading_calendar.sessions` rows. Research/adaptive
H9 reconstruction may additionally carry a `historical_trial_artifact`; a
sealed universe must not carry it because that would mix earlier trial evidence
into the sealed dataset identity. Formal dynamic reads require a trusted signed
calendar whose sessions exactly equal the daily snapshot dates and whose
declared coverage has no missing intervals. They also require the signed
construction evidence above. The research/adaptive historical artifact contains
exactly the 20 completed H1-H8 trial IDs, aligned OOS dates/returns and, for each
trial, its experiment ID, run ID, permit record/manifest identity, permit
mode/read interval/claim time, dataset/config/cost/code hashes,
execution-evidence hash and dated-return hash.
The aggregate schema is `artifact_version: 2`; every nested proof uses
`proof_version: 1` and has its own trusted-issuer HMAC over all those fields.
Nested `run_type` must be `historical_optimization`, `strategy_family` must be
`daytrade`, and `policy_version` must equal the current acceptance policy.
Run IDs, permit-record hashes, execution-evidence hashes and proof hashes must
be unique across the 20 trials. Re-signing only the aggregate cannot alter a
trial proof or its returns.

The one-shot sealed H9 run instead requires the independent file configured by
`STOCKBOT_DAYTRADE_ADAPTIVE_VALIDATION_ARTIFACT_PATH`. Its trusted issuer signs
the canonical 2023-2024 adaptive `ExperimentRunReport`, the global 21-trial
multiplicity result and the aligned baseline plus 21 daily return series. The
sealed run uses those frozen series only for DSR/PBO; it evaluates H3 versus H9
financial metrics solely on the new sealed interval.

H9 also freezes its complete `DayTradeConfig` and its evaluation protocol
before claiming data: three training months, one validation month, two effective
configurations, one-day purge/embargo, 60 warmup days and no observation-end or
sector-proxy override. The permit cost identity contains both the 5 bps base
cost and 12 bps stress cost. Any drift creates an error, not another H9 trial.

For H3, H9 and every sealed dynamic read, `selection_policy` must equal the
object above exactly; semantically similar aliases are not accepted. The
declared `minute_data_hash` must equal the digest of the bars actually loaded
from the frozen cache, and API fallback is not a substitute. The source PIT
`dataset_hash` is not by itself the evaluation identity. Before cache access,
StockBot computes the permit dataset hash as the canonical payload hash of:

```json
{
  "source_dataset_hash": "manifest.dataset_hash",
  "universe_manifest_hash": "hash of the complete universe JSON payload",
  "minute_data_hash": "manifest.minute_data_hash",
  "calendar_hash": "trading_calendar.content_hash",
  "construction_evidence_hash": "construction_evidence.content_hash",
  "historical_trial_artifact_hash": "research/adaptive artifact content_hash or missing"
}
```

`--dataset-hash`, when supplied, must equal this composite value. Consequently,
changing a daily member/ticker mapping, selection rule or frozen bar changes the
evaluation identity. Research/adaptive historical trial permits avoid a
recursive self-hash by
binding a separate canonical identity composed of the source dataset,
minute-data, calendar and construction-proof hashes. Each nested proof must
bind that identity and a structurally valid validation manifest. The sealed H9
permit binds a manifest without that same-period artifact and separately binds
the verified adaptive-validation identity in its configuration. The dynamic cache
directory must be below the external `STOCKBOT_DATA_ROOT`; that root must not be
inside the source repository.

## Run and report identities

The PIT manifest, the actual read and the experiment result are different
objects and therefore have different identities:

- `dataset_hash` is the logical dataset authorized by the
  `DataAccessPermit`. For dynamic DayTrade this can be the composite identity
  of the source bundle, universe manifest, minute data, calendar, construction
  proof and historical-trial artifact.
- `input_materialization_hash` is the digest of the actual permit-bound input
  materialization consumed by the run, such as sealed read evidence and
  sessions or the minute bars loaded from cache.
- `config_hash`, `code_hash`, `cost_model_hash` and `cost_suite_hash` bind the
  strategy settings, executable identity, execution/cost semantics and the
  named baseline/stress cost assumptions.
- `output_hash` binds the result materialization. `report_hash` binds the
  complete immutable `ExperimentRunReport`, including metrics and admission
  fields.

All supplied hashes are validated independently. In particular,
`dataset_hash == input_materialization_hash` is not an invariant: requiring it
would collapse an authorized logical bundle identity into one physical read
digest and would reject valid composite PIT datasets. Candidate/baseline
comparability instead requires explicit matching of the experiment, logical
dataset, actual input, configuration, code and cost identities.

`experiment-acceptance-report` performs this report-level integrity and
comparability check without opening a PIT partition, claiming a permit or
writing the exposure ledger. Its success cannot replace the signed permit,
adapter/data attestations, sealed-custody proof or the strategy's quantitative
admission thresholds.

## Validation windows

- Normal research: `2010-01-01..2020-12-31`.
- Normal adaptive development: `2021-01-01..2024-12-31`.
- DayTrade research: `2019-01-01..2022-12-31`.
- DayTrade adaptive development: `2023-01-01..2024-12-31`.
- Active one-shot sealed window for both families:
  `2026-08-01..2028-07-31`, claimable only after it has completed.

The former `2025-01-01..2026-06-30` interval has no independent unread-access
proof in this project and is permanently classified as exposed. It cannot
satisfy formal admission. Adaptive-development results may reject a candidate,
but may not prove success.

This is an application-level fail-closed boundary, not proof of external
custody. Formal deployment must also keep sealed Parquet/universe payloads and
release authority outside the research process, for example through
independent storage ACL/KMS and immutable access logs. A process that can
directly open plaintext files or mint trust signatures can bypass any Python
API; the repository must not claim that external non-exposure has been proven.

## Current execution boundary

`PointInTimeParquetBundle` implements the integrity-checked storage boundary.
`PointInTimeMarketDataPort` is the provider-neutral strategy boundary, and
`PointInTimeNormalRequestProvider` connects verified snapshots to the frozen
residual-momentum/quality experiment, portfolio allocator and next-open
backtester. A deployment still has to supply a verified adapter that maps its
vendor's physical columns and independent attestations into those public
point-in-time domain types.

For normal H1, a deployment adapter does not receive the bundle path as an
unrestricted data source. It receives a permit-bound `BundleReadSession` and
must use `read_table()` for every physical table. Each call records a
`BundleReadReceipt` containing the table, actual read interval, partition paths
and verified partition hashes. The core can seal the session only after all
normal-required tables have receipts; `minute_bars` is not part of that
daily-strategy set. For every required table, the union of receipts must cover
the complete permit interval, including warmup, without a gap. The core extracts
the trading sessions from the actual `trading_calendar` reader result and the
adapter's backtest sessions must equal them exactly. `BundleReadEvidence` then
binds those sessions and receipts to one permit record hash, dataset hash and
`normal` strategy family.

The adapter's mapped `PointInTimeMarketDataPort` and sessions are explicitly
untrusted output. The core creates `NormalH1ExperimentInputs` only after it has
verified the read evidence, adapter code/schema-mapping hash and deterministic
session hash. The shared view freezes 252 asset-return sessions, 20 ADV sessions
and a USD 5 million minimum ADV, and excludes an ineligible security from both
arms. Both baseline and candidate providers are derived from that one core-
sealed object over the exact deduplicated `RUSSELL1000 + SP500` union,
preventing either arm from silently using a different data view. Each component
universe is independently attested; daily security-master and price IDs must
equal their union. The adapter hash also participates in configuration and code
identity, so changing a physical-column mapping invalidates the comparison
identity.

For a sealed run, receipts alone are insufficient: the file named by
`STOCKBOT_NORMAL_ADAPTER_ATTESTATION_PATH` must be absolute and must contain a
trusted issuer's HMAC assertion over the core-computed callable implementation
hash, schema-mapping hash, provider, schema version, bundle ID and dataset hash.
Formal adapters are plain functions without closure/default runtime state. The
attestation issuer/content hash participates in permit config/code identity;
the core rechecks the callable, attestation file and source-tree hash after the
adapter returns. Research/adaptive runs may execute without this trust anchor,
but can never produce formal admission.

Formal Normal and DayTrade evidence records the exact permit dataset,
configuration, cost-model and code hashes plus the current acceptance-policy
version. A completed execution receipt alone is insufficient: the formal
materializer also re-verifies the granted permit and canonical execution ledger,
then recomputes admission from the raw execution artifact. Normal raw points
prove equity/PnL conservation and 5/10 bps fees from traded notional; DayTrade
raw fills retain pre-cost event prices so 5/12 bps long/short execution prices
and the daily ledger can be recomputed. Development evidence uses a separate
requirement ID/run type and cannot satisfy completion audit.

Until such an adapter and a real bundle are supplied,
`normal-experiment-run` reports that the verified adapter is unavailable and
exits before claiming the protected interval or opening a Parquet partition; it
does not report a return. Static legacy normal and DayTrade commands remain
development smoke paths and cannot satisfy strategy admission.
