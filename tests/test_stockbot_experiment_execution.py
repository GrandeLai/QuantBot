import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.experiment_execution import (
    ExperimentExecutionDeniedError,
    ExperimentExecutionIdentity,
    ExperimentExecutionIntegrityError,
    ExperimentExecutionLedger,
    canonical_experiment_execution_ledger_path,
)


def _identity(**changes):
    values = {
        "strategy_family": "daytrade",
        "experiment_id": "daytrade-h9-rvol-reward-risk",
        "access_mode": "adaptive_development",
        "request_start": date(2023, 1, 1),
        "request_end": date(2024, 12, 31),
        "dataset_hash": "a" * 64,
        "config_hash": "b" * 64,
        "cost_model_hash": "c" * 64,
        "code_hash": "d" * 64,
    }
    values.update(changes)
    return ExperimentExecutionIdentity(**values)


class ExperimentExecutionLedgerTest(unittest.TestCase):
    def setUp(self):
        self.environment = mock.patch.dict(
            os.environ,
            {"STOCKBOT_EXPOSURE_HMAC_KEY": "execution-ledger-test-key"},
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_completed_identity_is_replayed_without_a_second_execution(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ledger = ExperimentExecutionLedger.for_testing(
                root / "execution.jsonl"
            )
            result_path = root / "result.json"
            identity = _identity()

            started = ledger.begin(
                identity,
                run_id="h9-adaptive-v1",
                result_path=result_path,
            )
            completed = ledger.complete(
                identity,
                run_id="h9-adaptive-v1",
                result_path=result_path,
                payload={"passed": False, "reason": "gate"},
                exit_code=1,
            )
            resumed = ledger.begin(
                identity,
                run_id="h9-adaptive-v1",
                result_path=result_path,
            )

            self.assertFalse(started.completed)
            self.assertTrue(resumed.completed)
            self.assertEqual(resumed.exit_code, 1)
            self.assertEqual(resumed.result_payload, completed)
            verified = ledger.verify_completed(
                identity,
                run_id="h9-adaptive-v1",
                result_path=result_path,
            )
            self.assertTrue(verified.completed)
            self.assertEqual(verified.result_payload, completed)
            self.assertEqual(len(verified.receipt_hash or ""), 64)
            self.assertEqual(len(verified.result_hash or ""), 64)
            self.assertEqual(len(ledger.verify()), 2)

    def test_run_id_or_identity_rotation_cannot_repeat_a_stage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ledger = ExperimentExecutionLedger.for_testing(
                root / "execution.jsonl"
            )
            result_path = root / "result.json"
            identity = _identity()
            ledger.begin(identity, run_id="original", result_path=result_path)

            with self.assertRaisesRegex(
                ExperimentExecutionDeniedError,
                "different run_id",
            ):
                ledger.begin(identity, run_id="rotated", result_path=result_path)
            with self.assertRaisesRegex(
                ExperimentExecutionDeniedError,
                "another immutable identity",
            ):
                ledger.begin(
                    replace(identity, dataset_hash="e" * 64),
                    run_id="original",
                    result_path=result_path,
                )

    def test_started_identity_can_resume_after_interruption(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ledger = ExperimentExecutionLedger.for_testing(
                root / "execution.jsonl"
            )
            identity = _identity()
            result_path = root / "result.json"

            first = ledger.begin(identity, run_id="resume", result_path=result_path)
            second = ledger.begin(identity, run_id="resume", result_path=result_path)

            self.assertEqual(first, second)
            self.assertEqual(len(ledger.verify()), 1)
            with self.assertRaisesRegex(
                ExperimentExecutionIntegrityError,
                "not completed",
            ):
                ledger.verify_completed(
                    identity,
                    run_id="resume",
                    result_path=result_path,
                )

    def test_receipt_or_result_tampering_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ledger_path = root / "execution.jsonl"
            result_path = root / "result.json"
            identity = _identity()
            ledger = ExperimentExecutionLedger.for_testing(ledger_path)
            ledger.begin(identity, run_id="tamper", result_path=result_path)
            ledger.complete(
                identity,
                run_id="tamper",
                result_path=result_path,
                payload={"passed": True},
                exit_code=0,
            )

            payload = json.loads(result_path.read_text(encoding="utf-8"))
            payload["passed"] = False
            result_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(
                ExperimentExecutionIntegrityError,
                "result hash",
            ):
                ledger.begin(identity, run_id="tamper", result_path=result_path)

            first, second = ledger_path.read_text(encoding="utf-8").splitlines()
            record = json.loads(second)
            record["identity"]["dataset_hash"] = "f" * 64
            ledger_path.write_text(first + "\n" + json.dumps(record) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(
                ExperimentExecutionIntegrityError,
                "signature",
            ):
                ledger.verify()

    def test_unreceipted_result_cannot_recover_started_as_completed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ledger = ExperimentExecutionLedger.for_testing(
                root / "execution.jsonl"
            )
            identity = _identity()
            result_path = root / "result.json"
            ledger.begin(identity, run_id="interrupted", result_path=result_path)
            result_path.write_text(
                json.dumps(
                    {
                        "passed": True,
                        "execution_identity_hash": identity.identity_hash,
                        "execution_run_id": "interrupted",
                        "execution_complete": True,
                        "execution_exit_code": 0,
                    }
                ),
                encoding="utf-8",
            )

            resumed = ledger.begin(
                identity,
                run_id="interrupted",
                result_path=result_path,
            )

            self.assertEqual(resumed.status, "started")
            self.assertFalse(resumed.completed)
            self.assertIsNone(resumed.result_payload)
            self.assertEqual(len(ledger.verify()), 1)
            with self.assertRaisesRegex(
                ExperimentExecutionIntegrityError,
                "not completed",
            ):
                ledger.verify_completed(
                    identity,
                    run_id="interrupted",
                    result_path=result_path,
                )

    def test_production_constructor_rejects_explicit_execution_path_bypass(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(TypeError):
                ExperimentExecutionLedger(
                    path=Path(tmpdir) / "bypass-execution.jsonl"
                )

    def test_canonical_execution_path_treats_exposure_file_and_dir_equally(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            exposure_file = root / "exposure-ledger.jsonl"

            from_directory = canonical_experiment_execution_ledger_path(root)
            from_file = canonical_experiment_execution_ledger_path(exposure_file)

            self.assertEqual(from_directory, from_file)
            self.assertEqual(
                from_directory,
                root.resolve() / "experiment-execution-ledger.jsonl",
            )
            self.assertEqual(
                ExperimentExecutionLedger(root).path,
                ExperimentExecutionLedger(exposure_file).path,
            )


if __name__ == "__main__":
    unittest.main()
