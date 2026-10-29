import sys
import unittest
from datetime import date
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.normal.protocol import (
    NORMAL_ADAPTIVE_DEVELOPMENT_WINDOW,
    NORMAL_RESEARCH_WINDOW,
    NORMAL_SEALED_WINDOW,
    require_exact_normal_experiment_window,
)
from quantbot.validation_protocol import ValidationWindowManifest


class NormalValidationProtocolTest(unittest.TestCase):
    def test_all_three_modes_use_exact_frozen_request_and_warmup_windows(self):
        for mode, window in (
            ("research", NORMAL_RESEARCH_WINDOW),
            ("adaptive_development", NORMAL_ADAPTIVE_DEVELOPMENT_WINDOW),
            ("sealed", NORMAL_SEALED_WINDOW),
        ):
            with self.subTest(mode=mode):
                self.assertEqual(
                    require_exact_normal_experiment_window(
                        mode=mode,
                        request_start=window.request_start,
                        request_end=window.request_end,
                        warmup_start=window.warmup_start,
                        warmup_end=window.warmup_end,
                    ),
                    window,
                )

    def test_partial_window_warmup_drift_and_fixed_holdout_fail_closed(self):
        window = NORMAL_RESEARCH_WINDOW
        with self.assertRaisesRegex(ValueError, "window is frozen"):
            require_exact_normal_experiment_window(
                mode="research",
                request_start=date(2011, 1, 1),
                request_end=window.request_end,
                warmup_start=window.warmup_start,
                warmup_end=window.warmup_end,
            )
        with self.assertRaisesRegex(ValueError, "warmup 2007-01-01"):
            require_exact_normal_experiment_window(
                mode="research",
                request_start=window.request_start,
                request_end=window.request_end,
                warmup_start=date(2008, 1, 1),
                warmup_end=window.warmup_end,
            )
        with self.assertRaisesRegex(PermissionError, "only research"):
            require_exact_normal_experiment_window(
                mode="fixed_holdout",
                request_start=window.request_start,
                request_end=window.request_end,
                warmup_start=window.warmup_start,
                warmup_end=window.warmup_end,
            )

    def test_read_interval_includes_warmup_but_holdout_protection_is_request_only(self):
        window = NORMAL_SEALED_WINDOW
        manifest = ValidationWindowManifest(
            strategy_family="normal",
            mode="sealed",
            request_start=window.request_start,
            request_end=window.request_end,
            warmup_start=window.warmup_start,
            warmup_end=window.warmup_end,
            dataset_hash="a" * 64,
            config_hash="b" * 64,
            cost_model_hash="c" * 64,
            code_hash="d" * 64,
        )

        self.assertEqual(manifest.read_interval.start, window.warmup_start)
        self.assertEqual(manifest.read_interval.end, window.request_end)
        self.assertEqual(
            manifest.protected_holdout_interval.start,
            window.request_start,
        )
        self.assertEqual(
            manifest.protected_holdout_interval.end,
            window.request_end,
        )


if __name__ == "__main__":
    unittest.main()
