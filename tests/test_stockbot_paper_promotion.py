import sys
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.promotion import (
    PaperPromotionGate,
    PaperReconciliationRecord,
    build_paper_promotion_evidence,
)
from quantbot.audit import CompletionAudit
from quantbot.trading import RunManifest


def sessions(count=60):
    start = date(2026, 7, 1)
    values = []
    cursor = start
    while len(values) < count:
        if cursor.weekday() < 5:
            values.append(
                PaperReconciliationRecord(
                    strategy_family="daytrade",
                    session_date=cursor,
                    run_type="paper_trading",
                    dataset_hash="a" * 64,
                    config_hash="b" * 64,
                    code_hash="c" * 64,
                    orders_reconciled=True,
                    ledger_reconciled=True,
                    broker_reconciled=True,
                    intent_contract_verified=True,
                )
            )
        cursor += timedelta(days=1)
    return tuple(values)


class PaperPromotionTest(unittest.TestCase):
    def test_sixty_unique_reconciled_paper_sessions_allow_small_live_promotion(self):
        decision = PaperPromotionGate().evaluate(
            sessions(),
            strategy_family="daytrade",
            strategy_admitted=True,
        )

        self.assertTrue(decision.eligible_for_small_live, decision.reasons)
        self.assertEqual(decision.unique_sessions, 60)

    def test_duplicates_historical_replay_and_reconciliation_failure_fail_closed(self):
        records = sessions()
        duplicate = (records[0],) * 60
        replay = tuple(replace(item, run_type="historical_replay") for item in records)
        broken = records[:-1] + (replace(records[-1], broker_reconciled=False),)

        duplicate_decision = PaperPromotionGate().evaluate(
            duplicate,
            strategy_family="daytrade",
            strategy_admitted=True,
        )
        replay_decision = PaperPromotionGate().evaluate(
            replay,
            strategy_family="daytrade",
            strategy_admitted=True,
        )
        broken_decision = PaperPromotionGate().evaluate(
            broken,
            strategy_family="daytrade",
            strategy_admitted=True,
        )

        self.assertIn("duration", duplicate_decision.reasons)
        self.assertIn("paper_run_type", replay_decision.reasons)
        self.assertIn("reconciliation", broken_decision.reasons)

    def test_strategy_admission_and_stable_artifact_identity_are_required(self):
        records = sessions()
        changed_config = records[:-1] + (
            replace(records[-1], config_hash="d" * 64),
        )

        not_admitted = PaperPromotionGate().evaluate(
            records,
            strategy_family="daytrade",
            strategy_admitted=False,
        )
        drifted = PaperPromotionGate().evaluate(
            changed_config,
            strategy_family="daytrade",
            strategy_admitted=True,
        )

        self.assertIn("strategy_admission", not_admitted.reasons)
        self.assertIn("artifact_drift", drifted.reasons)

    def test_promotion_evidence_is_formal_current_policy_paper_evidence(self):
        decision = PaperPromotionGate().evaluate(
            sessions(),
            strategy_family="daytrade",
            strategy_admitted=True,
        )
        manifest = RunManifest(
            "paper_trading",
            datetime(2026, 10, 1, tzinfo=timezone.utc),
            "a" * 64,
            "b" * 64,
            "c" * 64,
        )
        evidence = build_paper_promotion_evidence(decision, manifest)

        self.assertTrue(
            CompletionAudit(["paper_reconciliation_60_sessions"]).evaluate(
                {"paper_reconciliation_60_sessions": evidence}
            ).complete
        )


if __name__ == "__main__":
    unittest.main()
