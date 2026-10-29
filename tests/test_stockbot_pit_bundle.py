import hashlib
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.data import (
    NORMAL_H1_REQUIRED_TABLES,
    PIT_BUNDLE_REQUIRED_COLUMNS,
    PointInTimeParquetBundle,
    canonical_pit_bundle_manifest_hash,
)
from quantbot.exposure import ExposureLedger, ValidationWindowManifest


class _Table:
    def __init__(self, columns, rows=()):
        self.column_names = tuple(columns)
        self._rows = tuple(rows)

    def to_pylist(self):
        return list(self._rows)


class _Reader:
    def __init__(self):
        self.columns_by_path = {}
        self.rows_by_path = {}
        self.reads = []

    def read(self, path, date_column, start, end):
        self.reads.append((path.name, date_column, start, end))
        return _Table(
            self.columns_by_path[path.name],
            self.rows_by_path.get(path.name, ()),
        )

    @staticmethod
    def concat(parts):
        return parts[0]


class PointInTimeParquetBundleTest(unittest.TestCase):
    def _build(self, root, *, omit_column=None):
        tables = {}
        reader = _Reader()
        for table_name, required in PIT_BUNDLE_REQUIRED_COLUMNS.items():
            path = f"{table_name}-2019.parquet"
            content = (table_name + "-fixture").encode()
            (root / path).write_bytes(content)
            columns = sorted(required)
            if omit_column and omit_column[0] == table_name:
                columns.remove(omit_column[1])
            reader.columns_by_path[path] = columns
            if table_name == "trading_calendar":
                reader.rows_by_path[path] = (
                    {
                        "exchange": "NYSE",
                        "session_date": date(2019, 6, 28),
                        "open_at": "2019-06-28T13:30:00Z",
                        "close_at": "2019-06-28T20:00:00Z",
                        "available_at": "2019-06-28T00:00:00Z",
                    },
                )
            access_column = "available_at"
            tables[table_name] = {
                "columns": columns,
                "access_date_column": access_column,
                "partitions": [
                    {
                        "path": path,
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "coverage_start": "2019-01-01",
                        "coverage_end": "2019-12-31",
                        "row_count": 1,
                        "size_bytes": len(content),
                    }
                ],
            }
        payload = {
            "format_version": 1,
            "provider": "vendor-neutral-test",
            "bundle_id": "pit-2019-v1",
            "schema_version": "1",
            "quality_tier": "point_in_time_verified",
            "effective_date_timezone": "America/New_York",
            "created_at": "2020-01-02T00:00:00+00:00",
            "coverage_start": "2019-01-01",
            "coverage_end": "2019-12-31",
            "tables": tables,
        }
        payload["dataset_hash"] = canonical_pit_bundle_manifest_hash(payload)
        (root / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
        return payload, reader

    def _permit(self, ledger_root, dataset_hash):
        ledger = ExposureLedger(ledger_root)
        return ledger.claim(
            ValidationWindowManifest(
                strategy_family="normal",
                mode="research",
                request_start=date(2019, 1, 1),
                request_end=date(2019, 12, 31),
                dataset_hash=dataset_hash,
                config_hash="a" * 64,
                cost_model_hash="b" * 64,
                code_hash="c" * 64,
            ),
            run_id="pit-bundle-test",
            claimed_at=datetime(2020, 1, 2, tzinfo=timezone.utc),
        )

    def test_external_bundle_requires_authentic_permit_before_partition_read(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as ledger_root:
            root = Path(root)
            payload, reader = self._build(root)
            with patch.dict(os.environ, {
                "STOCKBOT_EXPOSURE_HMAC_KEY": "test-key",
                "STOCKBOT_EXPOSURE_LEDGER_PATH": str(Path(ledger_root).resolve()),
            }, clear=False):
                permit = self._permit(ledger_root, payload["dataset_hash"])
                bundle = PointInTimeParquetBundle(root, reader=reader)
                table = bundle.read_table(
                    "daily_bars",
                    permit=permit,
                    start=date(2019, 3, 1),
                    end=date(2019, 3, 31),
                    strategy_family="normal",
                )

            self.assertIn("security_id", table.column_names)
            self.assertEqual(reader.reads[0][0], "daily_bars-2019.parquet")

    def test_partition_larger_than_claimed_actual_interval_is_rejected_before_read(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as ledger_root:
            root = Path(root)
            payload, reader = self._build(root)
            with patch.dict(os.environ, {
                "STOCKBOT_EXPOSURE_HMAC_KEY": "test-key",
                "STOCKBOT_EXPOSURE_LEDGER_PATH": str(Path(ledger_root).resolve()),
            }, clear=False):
                ledger = ExposureLedger(ledger_root)
                permit = ledger.claim(
                    ValidationWindowManifest(
                        strategy_family="normal",
                        mode="research",
                        request_start=date(2019, 3, 1),
                        request_end=date(2019, 3, 31),
                        dataset_hash=payload["dataset_hash"],
                        config_hash="a" * 64,
                        cost_model_hash="b" * 64,
                        code_hash="c" * 64,
                    ),
                    run_id="pit-partial-test",
                    claimed_at=datetime(2020, 1, 2, tzinfo=timezone.utc),
                )
                bundle = PointInTimeParquetBundle(root, reader=reader)
                with self.assertRaisesRegex(PermissionError, "outside the permitted interval"):
                    bundle.read_table(
                        "daily_bars",
                        permit=permit,
                        start=date(2019, 3, 1),
                        end=date(2019, 3, 31),
                        strategy_family="normal",
                    )

            self.assertEqual(reader.reads, [])

    def test_tampered_partition_and_incomplete_schema_fail_closed(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as ledger_root:
            root = Path(root)
            payload, reader = self._build(root)
            (root / "daily_bars-2019.parquet").write_bytes(b"tampered")
            with patch.dict(os.environ, {
                "STOCKBOT_EXPOSURE_HMAC_KEY": "test-key",
                "STOCKBOT_EXPOSURE_LEDGER_PATH": str(Path(ledger_root).resolve()),
            }, clear=False):
                permit = self._permit(ledger_root, payload["dataset_hash"])
                bundle = PointInTimeParquetBundle(root, reader=reader)
                with self.assertRaisesRegex(ValueError, "size does not match"):
                    bundle.read_table(
                        "daily_bars",
                        permit=permit,
                        start=date(2019, 1, 1),
                        end=date(2019, 12, 31),
                        strategy_family="normal",
                    )

        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            self._build(root, omit_column=("fundamentals", "return_on_equity"))
            with self.assertRaisesRegex(ValueError, "fundamentals.*return_on_equity"):
                PointInTimeParquetBundle(root, reader=_Reader())

    def test_bundle_root_inside_repository_is_rejected(self):
        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=repository) as root:
            root = Path(root)
            self._build(root)
            with self.assertRaisesRegex(ValueError, "outside the repository"):
                PointInTimeParquetBundle(root, reader=_Reader())

    def test_normal_read_evidence_requires_every_table_and_one_permit(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as ledger_root:
            root = Path(root)
            payload, reader = self._build(root)
            with patch.dict(os.environ, {
                "STOCKBOT_EXPOSURE_HMAC_KEY": "test-key",
                "STOCKBOT_EXPOSURE_LEDGER_PATH": str(Path(ledger_root).resolve()),
            }, clear=False):
                permit = self._permit(ledger_root, payload["dataset_hash"])
                bundle = PointInTimeParquetBundle(root, reader=reader)
                incomplete = bundle.begin_read_session(
                    permit=permit,
                    strategy_family="normal",
                )
                incomplete.read_table(
                    "daily_bars",
                    start=date(2019, 1, 1),
                    end=date(2019, 12, 31),
                )
                with self.assertRaisesRegex(PermissionError, "missing required tables"):
                    incomplete.seal(required_tables=NORMAL_H1_REQUIRED_TABLES)

                complete = bundle.begin_read_session(
                    permit=permit,
                    strategy_family="normal",
                )
                for table in sorted(NORMAL_H1_REQUIRED_TABLES):
                    complete.read_table(
                        table,
                        start=date(2019, 1, 1),
                        end=date(2019, 12, 31),
                    )
                evidence = complete.seal(required_tables=NORMAL_H1_REQUIRED_TABLES)

                self.assertEqual(evidence.dataset_hash, payload["dataset_hash"])
                self.assertEqual(evidence.permit_record_hash, permit.record_hash)
                self.assertEqual(evidence.tables, NORMAL_H1_REQUIRED_TABLES)
                with self.assertRaisesRegex(PermissionError, "already sealed"):
                    complete.read_table(
                        "daily_bars",
                        start=date(2019, 1, 1),
                        end=date(2019, 12, 31),
                    )

    def test_normal_read_evidence_rejects_required_table_receipt_gaps(self):
        fixture_day = date(2019, 6, 28)
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as ledger_root:
            root = Path(root)
            payload, reader = self._build(root)
            with patch.dict(os.environ, {
                "STOCKBOT_EXPOSURE_HMAC_KEY": "test-key",
                "STOCKBOT_EXPOSURE_LEDGER_PATH": str(Path(ledger_root).resolve()),
            }, clear=False):
                permit = self._permit(ledger_root, payload["dataset_hash"])
                session = PointInTimeParquetBundle(
                    root,
                    reader=reader,
                ).begin_read_session(permit=permit, strategy_family="normal")
                for table in sorted(NORMAL_H1_REQUIRED_TABLES):
                    session.read_table(table, start=fixture_day, end=fixture_day)

                with self.assertRaisesRegex(
                    PermissionError,
                    "does not cover the complete permitted interval",
                ):
                    session.seal(required_tables=NORMAL_H1_REQUIRED_TABLES)


if __name__ == "__main__":
    unittest.main()
