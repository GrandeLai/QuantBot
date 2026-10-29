import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.observability import AlertDispatcher, EventLogger, PipelineRunReport, RiskAlertRouter
from quantbot.quality import OHLCVSchemaValidator, quarantine_invalid_rows
from quantbot.regime import Regime
from quantbot.risk import DrawdownAction


class StockBotQualityObservabilityTest(unittest.TestCase):
    def test_ohlcv_validator_flags_missing_negative_and_inverted_prices(self):
        validator = OHLCVSchemaValidator()

        errors = validator.validate(
            {
                "date": date(2024, 1, 2),
                "ticker": "AAPL",
                "open": 100,
                "high": 99,
                "low": 101,
                "close": -1,
                "adjusted_close": None,
                "volume": -10,
            }
        )

        self.assertIn("adjusted_close_missing", errors)
        self.assertIn("negative_close", errors)
        self.assertIn("negative_volume", errors)
        self.assertIn("high_below_low", errors)

    def test_quarantine_invalid_rows_separates_clean_and_bad_records(self):
        rows = [
            {"date": date(2024, 1, 2), "ticker": "AAPL", "open": 100, "high": 101, "low": 99, "close": 100, "adjusted_close": 100, "volume": 10},
            {"date": date(2024, 1, 2), "ticker": "BAD", "open": 100, "high": 101, "low": 99, "close": 100, "adjusted_close": None, "volume": 10},
        ]

        result = quarantine_invalid_rows(rows, OHLCVSchemaValidator())

        self.assertEqual([row["ticker"] for row in result.clean_rows], ["AAPL"])
        self.assertEqual(result.quarantined[0]["ticker"], "BAD")
        self.assertEqual(result.quarantined[0]["errors"], ["adjusted_close_missing"])

    def test_event_logger_writes_jsonl_events(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "events.jsonl"
            logger = EventLogger(path)
            logger.info("data_update", records=10)
            logger.error("broker_order", error="rejected")

            events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(events[0]["level"], "INFO")
        self.assertEqual(events[0]["event"], "data_update")
        self.assertEqual(events[1]["level"], "ERROR")
        self.assertEqual(events[1]["error"], "rejected")

    def test_pipeline_run_report_summarizes_stage_statuses_and_duration(self):
        report = PipelineRunReport(run_id="2024-01-02")
        report.record_stage("data_update", started_at=0.0, finished_at=2.0, ok=True)
        report.record_stage("execute", started_at=3.0, finished_at=4.5, ok=False, error="order rejected")

        summary = report.summary()

        self.assertFalse(summary["ok"])
        self.assertEqual(summary["duration_seconds"], 3.5)
        self.assertEqual(summary["failed_stages"], ["execute"])

    def test_alert_dispatcher_sends_sms_for_critical_order_failures(self):
        sent = []
        dispatcher = AlertDispatcher(sms_sender=lambda phone, message: sent.append((phone, message)), phone_number="+15550000000")

        dispatcher.critical("broker_order_failed", symbol="AAPL", reason="Alpaca rejected order")

        self.assertEqual(sent[0][0], "+15550000000")
        self.assertIn("broker_order_failed", sent[0][1])
        self.assertIn("AAPL", sent[0][1])

    def test_risk_alert_router_notifies_drawdown_and_regime_switch_events(self):
        sent = []
        dispatcher = AlertDispatcher(sms_sender=lambda phone, message: sent.append(message), phone_number="+15550000000")
        router = RiskAlertRouter(dispatcher)

        router.notify_drawdown(0.16, DrawdownAction(manual_review=True, halt_trading=False, position_scale=0.5, reason="drawdown_review"))
        router.notify_regime_switch(Regime.R2_BULL_HIGH_VOL, Regime.R4_BEAR)
        router.notify_regime_switch(Regime.R4_BEAR, Regime.R4_BEAR)

        self.assertEqual(len(sent), 2)
        self.assertIn("risk_drawdown", sent[0])
        self.assertIn("drawdown_review", sent[0])
        self.assertIn("regime_switch", sent[1])
        self.assertIn("R2_BULL_HIGH_VOL", sent[1])
        self.assertIn("R4_BEAR", sent[1])


if __name__ == "__main__":
    unittest.main()
