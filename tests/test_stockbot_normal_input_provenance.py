import os
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


from quantbot.data import (
    DataQualityTier,
    FundamentalObservation,
    MarketCapObservation,
    NORMAL_H1_REQUIRED_TABLES,
    PointInTimeParquetBundle,
    SecurityClassification,
    SecurityIdentity,
    SecurityMasterSnapshot,
)
from quantbot.normal import (
    NormalH1AdapterOutput,
    NormalH1ExperimentInputs,
    NormalBacktestSession,
)
from quantbot.trading import PointInTimeSnapshot
from tests import test_stockbot_pit_bundle as pit_bundle_fixture


UTC = timezone.utc


class _Port:
    def monthly_returns(self, security_master):
        raise AssertionError("not read while sealing inputs")

    def asset_return_history(self, security_master, *, lookback_sessions):
        return {}

    def sleeve_return_history(self, security_master, sleeves, *, lookback_months):
        return {}

    def market_betas(self, security_master, *, lookback_sessions):
        return {}

    def average_dollar_volume(self, security_master, *, lookback_sessions):
        return {}

    def regime_context(self, security_master):
        raise AssertionError("not read while sealing inputs")


class _LiquidityPort(_Port):
    def __init__(self, history_sessions, adv_dollars):
        self.history_sessions = history_sessions
        self.adv_dollars = adv_dollars
        self.calls = []

    def asset_return_history(self, security_master, *, lookback_sessions):
        self.calls.append(("asset", lookback_sessions))
        return {
            security_id: tuple(0.001 for _ in range(self.history_sessions))
            for security_id in security_master.security_ids
        }

    def average_dollar_volume(self, security_master, *, lookback_sessions):
        self.calls.append(("adv", lookback_sessions))
        return {
            security_id: self.adv_dollars
            for security_id in security_master.security_ids
        }


class _StatefulSleevePort(_Port):
    def __init__(self):
        self.calls = 0

    def sleeve_return_history(self, security_master, sleeves, *, lookback_months):
        self.calls += 1
        return {
            sleeve.name: tuple(float(self.calls) for _ in range(lookback_months))
            for sleeve in sleeves
        }


def _session(dataset_hash):
    observed_at = datetime(2019, 6, 28, 20, tzinfo=UTC)
    security_id = "SEC-1"
    known_at = datetime(2010, 1, 1, tzinfo=UTC)
    identity = SecurityIdentity(security_id, date(2010, 1, 1), None, known_at)
    classification = SecurityClassification(
        security_id,
        "AAA",
        "NYSE",
        "Industrials",
        date(2010, 1, 1),
        None,
        known_at,
    )
    fundamental = FundamentalObservation(
        security_id,
        date(2018, 12, 31),
        datetime(2019, 2, 15, tzinfo=UTC),
        0,
        {
            "return_on_equity": 0.12,
            "gross_margin": 0.30,
            "debt_to_assets": 0.20,
            "accrual_ratio": 0.01,
        },
    )
    market_cap = MarketCapObservation(
        security_id,
        observed_at.date(),
        observed_at,
        2_000_000_000,
    )
    master = SecurityMasterSnapshot(
        as_of=observed_at,
        effective_date=observed_at.date(),
        effective_date_timezone="UTC",
        source_dataset_hash=dataset_hash,
        source_quality_tier=DataQualityTier.POINT_IN_TIME_VERIFIED,
        universes=("SP500", "RUSSELL1000"),
        security_ids=(security_id,),
        identity_by_security_id={security_id: identity},
        classification_by_security_id={security_id: classification},
        ticker_by_security_id={security_id: "AAA"},
        latest_fundamentals={security_id: fundamental},
        latest_market_caps={security_id: market_cap},
        known_corporate_actions=(),
        effective_corporate_actions=(),
    )
    return NormalBacktestSession(
        trading_date=observed_at.date(),
        open_prices={security_id: 100.0},
        close_prices={security_id: 101.0},
        signal_snapshot=PointInTimeSnapshot(
            observed_at=observed_at,
            security_master=master,
        ),
    )


class NormalInputProvenanceTest(unittest.TestCase):
    def test_core_seals_one_shared_port_to_bundle_reads_and_permit(self):
        fixture = pit_bundle_fixture.PointInTimeParquetBundleTest()
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as ledger_root:
            root_path = Path(root)
            payload, reader = fixture._build(root_path)
            with patch.dict(os.environ, {
                "STOCKBOT_EXPOSURE_HMAC_KEY": "test-key",
                "STOCKBOT_EXPOSURE_LEDGER_PATH": str(Path(ledger_root).resolve()),
            }, clear=False):
                permit = fixture._permit(ledger_root, payload["dataset_hash"])
                read_session = PointInTimeParquetBundle(
                    root_path,
                    reader=reader,
                ).begin_read_session(permit=permit, strategy_family="normal")
                for table in NORMAL_H1_REQUIRED_TABLES:
                    read_session.read_table(
                        table,
                        start=date(2019, 1, 1),
                        end=date(2019, 12, 31),
                    )
                evidence = read_session.seal(required_tables=NORMAL_H1_REQUIRED_TABLES)
                port = _Port()
                inputs = NormalH1ExperimentInputs.from_adapter_output(
                    NormalH1AdapterOutput(port, (_session(payload["dataset_hash"]),)),
                    permit=permit,
                    read_evidence=evidence,
                    adapter_code_hash="d" * 64,
                )

                baseline, candidate = inputs.providers()
                self.assertIs(baseline.market_data, candidate.market_data)
                self.assertIsNot(baseline.market_data, port)
                self.assertEqual(inputs.read_evidence.permit_record_hash, permit.record_hash)
                self.assertTrue(inputs.provenance_verified)

                mismatched = replace(
                    _session(payload["dataset_hash"]),
                    signal_snapshot=PointInTimeSnapshot(
                        observed_at=_session(payload["dataset_hash"]).signal_snapshot.observed_at,
                        security_master=replace(
                            _session(payload["dataset_hash"]).signal_snapshot.security_master,
                            source_dataset_hash="f" * 64,
                        ),
                    ),
                )
                with self.assertRaisesRegex(PermissionError, "different dataset"):
                    NormalH1ExperimentInputs.from_adapter_output(
                        NormalH1AdapterOutput(port, (mismatched,)),
                        permit=permit,
                        read_evidence=evidence,
                        adapter_code_hash="d" * 64,
                    )

    def test_core_view_excludes_short_history_and_low_adv_for_both_arms(self):
        fixture = pit_bundle_fixture.PointInTimeParquetBundleTest()
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as ledger_root:
            root_path = Path(root)
            payload, reader = fixture._build(root_path)
            with patch.dict(os.environ, {
                "STOCKBOT_EXPOSURE_HMAC_KEY": "test-key",
                "STOCKBOT_EXPOSURE_LEDGER_PATH": str(Path(ledger_root).resolve()),
            }, clear=False):
                permit = fixture._permit(ledger_root, payload["dataset_hash"])
                read_session = PointInTimeParquetBundle(
                    root_path,
                    reader=reader,
                ).begin_read_session(permit=permit, strategy_family="normal")
                for table in NORMAL_H1_REQUIRED_TABLES:
                    read_session.read_table(
                        table,
                        start=date(2019, 1, 1),
                        end=date(2019, 12, 31),
                    )
                evidence = read_session.seal(required_tables=NORMAL_H1_REQUIRED_TABLES)
                port = _LiquidityPort(history_sessions=12, adv_dollars=4_999_999.0)
                inputs = NormalH1ExperimentInputs.from_adapter_output(
                    NormalH1AdapterOutput(
                        port,
                        (_session(payload["dataset_hash"]),),
                    ),
                    permit=permit,
                    read_evidence=evidence,
                    adapter_code_hash="d" * 64,
                )

                baseline, candidate = inputs.providers()
                master = inputs.sessions[0].signal_snapshot.security_master
                for provider in (baseline, candidate):
                    self.assertEqual(
                        provider.market_data.asset_return_history(
                            master,
                            lookback_sessions=252,
                        ),
                        {},
                    )
                    self.assertEqual(
                        provider.market_data.average_dollar_volume(
                            master,
                            lookback_sessions=20,
                        ),
                        {},
                    )
                self.assertEqual(port.calls, [("asset", 252), ("adv", 20)])

                base = _session(payload["dataset_hash"])
                wrong_universe = replace(
                    base,
                    signal_snapshot=PointInTimeSnapshot(
                        observed_at=base.signal_snapshot.observed_at,
                        security_master=replace(
                            base.signal_snapshot.security_master,
                            universes=("NASDAQ100",),
                        ),
                    ),
                )
                with self.assertRaisesRegex(
                    PermissionError,
                    "exactly the historical SP500 and RUSSELL1000 union",
                ):
                    NormalH1ExperimentInputs.from_adapter_output(
                        NormalH1AdapterOutput(port, (wrong_universe,)),
                        permit=permit,
                        read_evidence=evidence,
                        adapter_code_hash="d" * 64,
                    )

    def test_core_view_rejects_stateful_shared_sleeve_history(self):
        from quantbot.normal.experiment_runner import _NormalH1MarketDataView

        master = _session("a" * 64).signal_snapshot.security_master
        momentum = SimpleNamespace(name="residual_momentum_12_1_neutral")
        quality = SimpleNamespace(name="quality_profitability_neutral")
        view = _NormalH1MarketDataView(_StatefulSleevePort())

        baseline = view.sleeve_return_history(
            master,
            (momentum,),
            lookback_months=36,
        )
        self.assertEqual(baseline[momentum.name], (1.0,) * 36)
        with self.assertRaisesRegex(
            PermissionError,
            "changed a shared sleeve history between arms",
        ):
            view.sleeve_return_history(
                master,
                (momentum, quality),
                lookback_months=36,
            )

    def test_adapter_cannot_cherry_pick_sessions_from_core_calendar(self):
        fixture = pit_bundle_fixture.PointInTimeParquetBundleTest()
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as ledger_root:
            root_path = Path(root)
            payload, reader = fixture._build(root_path)
            reader.rows_by_path["trading_calendar-2019.parquet"] = (
                {"session_date": date(2019, 6, 27)},
                {"session_date": date(2019, 6, 28)},
            )
            with patch.dict(os.environ, {
                "STOCKBOT_EXPOSURE_HMAC_KEY": "test-key",
                "STOCKBOT_EXPOSURE_LEDGER_PATH": str(Path(ledger_root).resolve()),
            }, clear=False):
                permit = fixture._permit(ledger_root, payload["dataset_hash"])
                read_session = PointInTimeParquetBundle(
                    root_path,
                    reader=reader,
                ).begin_read_session(permit=permit, strategy_family="normal")
                for table in NORMAL_H1_REQUIRED_TABLES:
                    read_session.read_table(
                        table,
                        start=date(2019, 1, 1),
                        end=date(2019, 12, 31),
                    )
                evidence = read_session.seal(required_tables=NORMAL_H1_REQUIRED_TABLES)

                with self.assertRaisesRegex(
                    PermissionError,
                    "exactly match the core-observed trading calendar",
                ):
                    NormalH1ExperimentInputs.from_adapter_output(
                        NormalH1AdapterOutput(
                            _Port(),
                            (_session(payload["dataset_hash"]),),
                        ),
                        permit=permit,
                        read_evidence=evidence,
                        adapter_code_hash="d" * 64,
                    )

    def test_inputs_cannot_be_self_declared_by_an_adapter(self):
        with self.assertRaisesRegex(PermissionError, "sealed by the core"):
            NormalH1ExperimentInputs(
                market_data=_Port(),
                sessions=(),
                permit=None,
                read_evidence=None,
                adapter_code_hash="a" * 64,
                session_hash="b" * 64,
            )


if __name__ == "__main__":
    unittest.main()
