"""Minute evidence checks; file integrity and causal availability are not source authentication.

An evidence bundle is metadata only. Its three referenced documents contain the
observations, action coverage and cost samples. ``inspect_evidence`` deliberately
never opens these documents or prices: independent studies can inspect metadata,
register the access, and only then call ``verify_market_evidence``.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from quantbot.ai.corporate_actions import normalize_actions, supported
from quantbot.ai.research_catalog import check_window, claim_development, load
from quantbot.ai.vintages import instant
from quantbot.factors.conditional_study import check_ref, file_ref, read, save
from quantbot.evidence import source_tree_hash

from .data import FIELDS, checked_protocol, load_bars


COMPONENTS = ("historical_availability", "corporate_actions", "execution_costs")


def evidence_schema():
    """The explicit input contract, suitable for a CLI report or a data adapter."""
    return {
        "schema_version": 1,
        "bundle": ["schema_version", "market", "symbols", "range", "data", "data_kind",
                   "price_basis", *COMPONENTS],
        "data_kind": ["historical_unverified", "synthetic"],
        "range": {"start": "inclusive ISO date", "end_exclusive": "exclusive ISO date"},
        "references": "Every data/component reference is an existing absolute path plus sha256.",
        "document_header": ["schema_version", "market", "symbols", "range", "data", "data_kind",
                            "provider", "source", "captured_at"],
        "historical_availability": {
            "extra_fields": ["price_basis=raw", "rows"],
            "row_fields": [*FIELDS, "available_at", "observed_at"],
            "time_rule": "bar close <= available_at <= observed_at <= captured_at; observed_at <= first 15m decision",
        },
        "corporate_actions": {
            "extra_fields": ["price_basis=raw", "coverage", "actions"],
            "coverage_fields": ["session", "symbol", "available_at", "observed_at"],
            "actions": "quantbot.ai.corporate_actions.normalize_actions schema; empty list for crypto",
            "time_rule": "coverage and action availability must be no later than session open",
        },
        "execution_costs": {
            "extra_fields": ["rows"],
            "row_fields": ["session", "symbol", "available_at", "observed_at", "sample_count", "venue",
                           "fee_bps", "half_spread_bps", "slippage_bps", "impact_bps", "latency_seconds"],
            "time_rule": "observations must precede session open; every symbol/session must have evidence",
            "model_rule": "reported costs and latency must be covered by the frozen base scenario",
        },
        "trust_boundary": "Local consistency checks do not authenticate a provider, certify source completeness, or grant trading admission.",
    }


def _reference(ref):
    if (not isinstance(ref, dict) or set(ref) != {"path", "sha256"}
            or not isinstance(ref["path"], str) or not Path(ref["path"]).is_absolute()
            or not isinstance(ref["sha256"], str) or len(ref["sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in ref["sha256"])):
        raise ValueError("evidence reference requires absolute path and sha256")
    return ref


def _bundle(path_or_ref, market, symbols, read_start, read_end):
    ref = _reference(path_or_ref) if isinstance(path_or_ref, dict) else file_ref(path_or_ref)
    payload = read(check_ref(ref))
    required = set(evidence_schema()["bundle"])
    if not isinstance(payload, dict) or set(payload) != required or type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ValueError("evidence bundle fields or schema_version are invalid")
    expected_range = {"start": read_start,
                      "end_exclusive": (datetime.fromisoformat(read_end) + timedelta(days=1)).date().isoformat()}
    if (payload["market"] != market or payload["symbols"] != sorted(symbols)
            or payload["range"] != expected_range):
        raise ValueError("evidence market, symbols or range mismatch")
    if payload["price_basis"] != "raw" or payload["data_kind"] not in {"historical_unverified", "synthetic"}:
        raise ValueError("evidence requires raw prices and an explicit supported data_kind")
    for name in ("data", *COMPONENTS):
        _reference(payload[name])
    check_ref(ref)
    return ref, payload


def inspect_evidence(evidence_path, market, symbols, read_start, read_end, execution_model):
    """Check metadata only; do not hash/open prices or observation documents.

    ``read_end`` is inclusive, matching the research catalog access API. Frozen
    execution settings are returned for the consumer to bind in its plan; costs
    are verified only after opening the observation documents in the run stage.
    """
    ref, bundle = _bundle(evidence_path, market, symbols, read_start, read_end)
    missing = [name for name in ("data", *COMPONENTS) if not Path(bundle[name]["path"]).is_file()]
    return {"evidence_ref": ref, "data_ref": bundle["data"], "metadata_verified": True,
            "metadata_ready": not missing,
            "data_kind": bundle["data_kind"], "synthetic": bundle["data_kind"] == "synthetic",
            "pit_verified": False, "costs_verified": False, "evidence_verified": False,
            "authenticity_verified": False, "execution_model": execution_model,
            "components": {name: bundle[name] for name in COMPONENTS},
            "prices_read": False, "reasons": [*("missing_file:" + name for name in missing),
                                              "observation_documents_not_read", "authenticity_unverified"]}


def _numeric(value):
    if isinstance(value, bool):
        raise ValueError("numeric evidence cannot be a boolean")
    try:
        value = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("evidence values must be finite numbers") from None
    if not value.is_finite():
        raise ValueError("evidence values must be finite numbers")
    return value


def _document(ref, bundle, extra_fields):
    value = read(check_ref(ref))
    required = set(evidence_schema()["document_header"]) | set(extra_fields)
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("evidence document fields are incomplete or unexpected")
    for name in ("schema_version", "market", "symbols", "range", "data", "data_kind"):
        if value[name] != bundle[name]:
            raise ValueError("evidence document binding mismatch: " + name)
    for name in ("provider", "source"):
        if not isinstance(value[name], str) or not value[name].strip():
            raise ValueError("evidence document requires provider and source provenance")
    if instant(value["captured_at"]) > datetime.now(UTC):
        raise ValueError("evidence captured_at is in the future")
    return value


def _times(row, captured, cutoff, *, earliest=None):
    available, observed = instant(row["available_at"]), instant(row["observed_at"])
    if not available <= observed <= captured or observed > cutoff or (earliest and available < earliest):
        raise ValueError("evidence is unavailable at the decision cutoff or has impossible timestamps")


def _availability(document, frame, sessions):
    if document["price_basis"] != "raw":
        raise ValueError("historical observations must use raw prices")
    expected = {(r.timestamp.to_pydatetime(), r.symbol): r
                for r in frame.itertuples(index=False)}
    captured = instant(document["captured_at"])
    seen = set()
    for row in document["rows"]:
        if not isinstance(row, dict) or set(row) != set(FIELDS) | {"available_at", "observed_at"}:
            raise ValueError("historical row requires OHLCV, available_at and observed_at")
        stamp = instant(row["timestamp"])
        key = stamp, row["symbol"]
        if key not in expected or key in seen:
            raise ValueError("historical observations contain duplicate or unexpected bars")
        expected_row = expected[key]
        if any(_numeric(row[k]) != _numeric(getattr(expected_row, k)) for k in FIELDS[2:]):
            raise ValueError("historical observation OHLCV differs from the frozen data")
        opening, closing = sessions[stamp.date().isoformat()]
        # The first decision that can consume this completed bar is the end of
        # its 15-minute aggregate. A last-bar correction after this cutoff fails.
        offset = int((stamp - opening.to_pydatetime()).total_seconds())
        cutoff = min(opening.to_pydatetime() + timedelta(seconds=(offset // 900 + 1) * 900),
                     closing.to_pydatetime())
        _times(row, captured, cutoff, earliest=stamp + timedelta(minutes=5))
        seen.add(key)
    if seen != set(expected):
        raise ValueError("historical observations do not cover every frozen minute row")
    return {"verified": True, "rows": len(seen), "basis": "locally_consistent_declared_observation_journal"}


def _session_rows(rows, document, sessions, symbols, fields):
    expected = {(day, symbol) for day in sessions for symbol in symbols}
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != fields:
            raise ValueError("session evidence fields are incomplete or unexpected")
        key = row["session"], row["symbol"]
        if key not in expected or key in seen:
            raise ValueError("session evidence contains duplicate or out-of-range records")
        _times(row, instant(document["captured_at"]), sessions[key[0]][0].to_pydatetime())
        seen.add(key)
    if seen != expected:
        raise ValueError("session evidence does not cover every symbol and session")


def _actions(document, sessions, bundle, frame):
    if document["price_basis"] != "raw":
        raise ValueError("company actions require raw execution prices")
    _session_rows(document["coverage"], document, sessions, bundle["symbols"],
                  {"session", "symbol", "available_at", "observed_at"})
    actions = normalize_actions(document["actions"], bundle["symbols"], instant(document["captured_at"]))
    for action in actions:
        if action["date"] not in sessions or instant(action["available_at"]) > sessions[action["date"]][0]:
            raise ValueError("company action is outside range or unavailable before session open")
    if bundle["market"] == "crypto" and actions:
        raise ValueError("crypto asset actions are not supported by the frozen spot replay")
    unsupported = [a["type"] for a in actions if not supported(a)]
    if unsupported:
        raise ValueError("unsupported company action types: " + ",".join(unsupported))
    from .actions import check_cash_distributions

    prior_close = {}
    for day, rows in frame.groupby(frame["timestamp"].dt.strftime("%Y-%m-%d"), sort=True):
        check_cash_distributions([a for a in actions if a["date"] == day], prior_close)
        prior_close.update({symbol: values.iloc[-1]["close"] for symbol, values in rows.groupby("symbol")})
    return {"verified": True, "covered_symbol_sessions": len(document["coverage"]),
            "actions": actions, "action_count": len(actions), "price_basis": "raw",
            "completeness_basis": "declared_source_coverage_not_independent_authentication"}


def _costs(document, sessions, bundle, model):
    cost_fields = ("fee_bps", "half_spread_bps", "slippage_bps", "impact_bps", "latency_seconds")
    fields = {"session", "symbol", "available_at", "observed_at", "sample_count", "venue", *cost_fields}
    _session_rows(document["rows"], document, sessions, bundle["symbols"], fields)
    limits = {"fee_bps": min(model["fee_bps"]), "half_spread_bps": min(model["half_spread_bps"]),
              "slippage_bps": min(model["slippage_bps"]),
              "impact_bps": model["impact_bps_at_participation_cap"],
              "latency_seconds": min(model["latency_seconds"])}
    maxima = {key: Decimal(0) for key in cost_fields}
    for row in document["rows"]:
        if type(row["sample_count"]) is not int or row["sample_count"] <= 0 or not str(row["venue"]).strip():
            raise ValueError("execution evidence requires positive sample_count and venue")
        for key in cost_fields:
            value = _numeric(row[key])
            if value < 0 or value > _numeric(limits[key]):
                raise ValueError("frozen base execution model does not cover observed " + key)
            maxima[key] = max(maxima[key], value)
    return {"verified": True, "covered_symbol_sessions": len(document["rows"]),
            "max_observed": {key: str(value) for key, value in maxima.items()},
            "base_model_covers_observations": True,
            "boundary": "Observed samples do not establish future tails, capacity or real fills."}


def verify_market_evidence(protocol, market, section):
    """Read allowed market data after the caller has registered its intended access.

    This verifies internal evidence, never external authenticity or admission.
    The public development wrapper below checks the cumulative catalog first;
    independent validation must register/check its own catalog before calling.
    """
    result = {"market": market, "status": "blocked", "evidence_verified": False,
              "authenticity_verified": False, "authenticity_status": "unverified",
              "admission_ready": False, "prices_read": False,
              "checks": {name: {"verified": False, "status": "not_checked"}
                         for name in ("raw_minutes", *COMPONENTS)}, "reasons": [],
              "pit_verified": False, "costs_verified": False, "execution_compatible": False,
              "checked_references": []}
    start, end = protocol["range"]["start"], protocol["range"]["end_exclusive"]
    symbols = protocol["markets"][market]["symbols"]
    overlaps = [p for p in protocol["protected_intervals"] if p["market"] == market
                and ("*" in p["symbols"] or set(symbols) & set(p["symbols"]))
                and start <= p["end"] and end > p["start"]]
    result["checks"]["protection"] = {"verified": not overlaps}
    if start >= end or overlaps:
        result["reasons"] = ["invalid_or_protected_range"]
        return result
    if section.get("data") is None:
        result["reasons"] = ["minute_data_missing", *section.get("reasons", []),
                             "historical_availability_missing", "corporate_action_coverage_missing",
                             "execution_cost_evidence_missing"]
        return result
    data_ref = _reference(section["data"])
    result["data"] = data_ref
    bundle = None
    if section.get("evidence") is not None:
        try:
            ref, bundle = _bundle(section["evidence"], market, symbols, start,
                                  (datetime.fromisoformat(end) - timedelta(days=1)).date().isoformat())
            if bundle["data"] != data_ref:
                raise ValueError("evidence bundle data reference mismatch")
            result["evidence"] = ref
            result["checked_references"].append(ref)
            result["data_kind"] = bundle["data_kind"]
        except (ValueError, KeyError, TypeError, OSError) as exc:
            result["reasons"] = ["invalid_evidence_metadata: " + str(exc)]
            return result
    if bundle is not None:
        for name in COMPONENTS:
            try:
                check_ref(bundle[name])
            except (ValueError, OSError) as exc:
                result["checks"][name] = {"verified": False, "reason": str(exc)}
                result["reasons"] = ["invalid_" + name]
                return result
    try:
        result["prices_read"] = True
        data_path = check_ref(data_ref)
        result["checked_references"].append(data_ref)
        frame, sessions, quality = load_bars(data_path, protocol, market)
        check_ref(data_ref)
        result["checks"]["raw_minutes"] = {**quality, "verified": quality["complete"]}
    except (ValueError, KeyError, TypeError, OSError) as exc:
        result["reasons"] = ["invalid_raw_minutes: " + str(exc)]
        return result
    if not quality["complete"]:
        result["reasons"].append("incomplete_minute_grid")
    if bundle is None:
        result["reasons"] += ["historical_availability_missing", "corporate_action_coverage_missing",
                              "execution_cost_evidence_missing", "authenticity_unverified"]
        return result
    validators = {
        "historical_availability": (["price_basis", "rows"], lambda doc: _availability(doc, frame, sessions)),
        "corporate_actions": (["price_basis", "coverage", "actions"], lambda doc: _actions(doc, sessions, bundle, frame)),
        "execution_costs": (["rows"], lambda doc: _costs(doc, sessions, bundle, protocol["execution_model"])),
    }
    for name, (fields, validate) in validators.items():
        try:
            document = _document(bundle[name], bundle, fields)
            result["checked_references"].append(bundle[name])
            result["checks"][name] = validate(document)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            result["checks"][name] = {"verified": False, "reason": str(exc)}
            result["reasons"].append("invalid_" + name)
    try:
        for reference in (data_ref, result["evidence"], *(bundle[name] for name in COMPONENTS)):
            check_ref(reference)
        result["checks"]["input_integrity"] = {"verified": True}
    except (ValueError, OSError) as exc:
        result["checks"]["input_integrity"] = {"verified": False, "reason": str(exc)}
        result["reasons"].append("evidence_changed_or_missing_during_check")
    result["execution_compatible"] = result["checks"]["corporate_actions"]["verified"]
    result["checks"]["execution_compatibility"] = {
        "verified": result["execution_compatible"],
        "boundary": "Ordinary USD dividends and splits require flat inventory and no orders before the effective open; raw prices and cash-dividend gaps are retained."}
    if not result["execution_compatible"]:
        result["reasons"].append("intraday_corporate_action_replay_unverified")
    result["pit_verified"] = result["checks"]["historical_availability"]["verified"]
    result["costs_verified"] = result["checks"]["execution_costs"]["verified"]
    result["consistency_verified"] = all(check["verified"] for name, check in result["checks"].items()
                                         if name != "execution_compatibility")
    if bundle["data_kind"] == "synthetic":
        result["reasons"].append("synthetic_data_not_financial_evidence")
    result["evidence_verified"] = (result["consistency_verified"] and result["execution_compatible"]
                                   and bundle["data_kind"] != "synthetic")
    result["status"] = "evidence_verified_authenticity_unverified" if result["evidence_verified"] else "blocked"
    result["reasons"].append("authenticity_unverified")
    return result



def replay_action_binding(protocol, market, section, frame, sessions):
    """Bind the existing evidence bundle and its action document for raw replay."""
    if section.get("evidence") is None:
        return None  # Legacy diagnostics remain unverified; do not invent coverage.
    ref, bundle = _bundle(section["evidence"], market, protocol["markets"][market]["symbols"],
                          protocol["range"]["start"],
                          (datetime.fromisoformat(protocol["range"]["end_exclusive"])
                           - timedelta(days=1)).date().isoformat())
    if bundle["data"] != section["data"]:
        raise ValueError("evidence bundle data reference mismatch")
    document = _document(bundle["corporate_actions"], bundle, ["price_basis", "coverage", "actions"])
    result = _actions(document, sessions, bundle, frame)
    check_ref(ref)
    check_ref(bundle["corporate_actions"])
    return {"evidence": ref, "document": bundle["corporate_actions"], "actions": result["actions"]}


def check_frozen_action_refs(frozen):
    for binding in frozen.get("corporate_actions", {}).values():
        if binding is not None:
            check_ref(binding["evidence"])
            check_ref(binding["document"])


def frozen_replay_actions(frozen, protocol, market, frame, sessions):
    """Research and attribution consume the same frozen action contract."""
    check_frozen_action_refs(frozen)
    manifest = read(check_ref(frozen["data_manifest"]))
    section = manifest[market]
    if section["data"] != frozen["data"][market]:
        raise ValueError("冻结分钟数据与公司行动清单绑定不一致")
    binding = replay_action_binding(protocol, market, section, frame, sessions)
    if "corporate_actions" in frozen:
        if binding != frozen["corporate_actions"][market]:
            raise ValueError("冻结公司行动输入改变")
    elif binding is not None and binding["actions"]:
        raise ValueError("历史冻结未绑定公司行动，须新建冻结后重跑")
    return binding["actions"] if binding is not None else []


def evidence_check(protocol_path, data_manifest_path, output, policy_snapshots_path=None, catalog_path=None):
    """Check both development markets without downloading data or relaxing gates."""
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    protocol_ref, manifest_ref = file_ref(protocol_path), file_ref(data_manifest_path)
    source_hash = source_tree_hash()
    snapshots_ref = file_ref(policy_snapshots_path) if policy_snapshots_path is not None else None
    snapshots = read(check_ref(snapshots_ref)) if snapshots_ref else ()
    protocol = checked_protocol(protocol_path, policy_snapshots=snapshots)
    manifest = read(check_ref(manifest_ref))
    if manifest["protocol"] != protocol_ref:
        raise ValueError("data manifest is not bound to the frozen protocol")
    catalog_ref = file_ref(catalog_path) if catalog_path is not None else protocol["prior_catalog"]
    catalog = load(check_ref(catalog_ref))
    ancestor_ref, ancestor = catalog_ref, catalog
    while ancestor_ref != protocol["prior_catalog"]:
        if ancestor["parent"] is None:
            raise ValueError("evidence catalog must descend from the protocol prior_catalog")
        ancestor_ref = ancestor["parent"]
        ancestor = read(check_ref(ancestor_ref))
    results = {}
    for market in ("crypto", "stocks"):
        access = check_window(catalog, market, protocol["markets"][market]["symbols"],
                              protocol["range"]["start"],
                              (datetime.fromisoformat(protocol["range"]["end_exclusive"]) - timedelta(days=1)).date().isoformat(),
                              role="development")
        if not access["allowed_by_index"]:
            results[market] = {"status": "blocked", "evidence_verified": False, "prices_read": False,
                               "authenticity_verified": False, "admission_ready": False,
                               "checks": {"protection": access}, "reasons": access["reasons"]}
        else:
            if manifest[market].get("data") is not None:
                # Observations contain prices too. Record the complete read
                # interval before opening either data or observation files;
                # a failed check must not make that interval unseen again.
                claimed_path = target.with_name(f"{target.stem}.catalog-{market}.json")
                catalog = claim_development(check_ref(catalog_ref), claimed_path,
                    market=market, symbols=protocol["markets"][market]["symbols"],
                    start=protocol["range"]["start"],
                    end=(datetime.fromisoformat(protocol["range"]["end_exclusive"])
                         - timedelta(days=1)).date().isoformat(),
                    purpose="intraday_evidence_check")
                catalog_ref = file_ref(claimed_path)
            results[market] = verify_market_evidence(protocol, market, manifest[market])
    for reference in (protocol_ref, manifest_ref, catalog_ref):
        check_ref(reference)
    for result in results.values():
        for reference in result.get("checked_references", []):
            check_ref(reference)
    checked_protocol(protocol_path, policy_snapshots=snapshots)
    load(check_ref(catalog_ref))
    if source_tree_hash() != source_hash:
        raise ValueError("source changed during evidence checks")
    report = {"schema_version": 1, "protocol": protocol_ref,
              "data_manifest": manifest_ref, "catalog": catalog_ref, "source_hash": source_hash,
              "created_at": datetime.now(UTC).isoformat(),
              "markets": results, "required_schema": evidence_schema(),
              "model_calls": 0, "broker_calls": 0, "trading_enabled": False,
              "formal_admission": False, "sealed_holdout_evaluated": False}
    if snapshots_ref:
        check_ref(snapshots_ref)
        report["policy_snapshots"] = snapshots_ref
    save(target, report)
    return report
