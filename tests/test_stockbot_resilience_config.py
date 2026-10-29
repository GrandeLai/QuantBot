import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.research_cli import main
from quantbot.config_loader import load_runtime_config
from quantbot.integrations.alpaca import AlpacaDataClient
from quantbot.integrations.http import RetryingJsonTransport
from quantbot.live_smoke import LiveSmokeCheck, LiveSmokeGate


class StockBotResilienceConfigTest(unittest.TestCase):
    def test_retrying_transport_retries_transient_failures_with_backoff(self):
        calls = []
        sleeps = []

        def request_json(_url, _headers, _params):
            calls.append("call")
            if len(calls) < 3:
                raise TimeoutError("temporary timeout")
            return {"ok": True}

        transport = RetryingJsonTransport(
            request_json=request_json,
            sleep=sleeps.append,
            max_attempts=3,
            backoff_seconds=0.5,
        )

        payload = transport.get_json("https://example.com", headers={"h": "v"}, params={"p": "1"})

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [0.5, 1.0])

    def test_retrying_transport_does_not_retry_non_retryable_client_error(self):
        calls = []

        def request_json(_url, _headers, _params):
            calls.append("call")
            raise ValueError("401 unauthorized")

        transport = RetryingJsonTransport(request_json=request_json, max_attempts=3)

        with self.assertRaises(ValueError):
            transport.get_json("https://example.com")
        self.assertEqual(calls, ["call"])

    def test_alpaca_data_client_follows_next_page_token(self):
        transport = PagedTransport(
            [
                {
                    "bars": {"AAPL": [{"t": "2024-01-02T00:00:00Z", "c": 100, "v": 10}]},
                    "next_page_token": "next-token",
                },
                {
                    "bars": {"AAPL": [{"t": "2024-01-03T00:00:00Z", "c": 101, "v": 11}]},
                    "next_page_token": None,
                },
            ]
        )
        client = AlpacaDataClient("key", "secret", transport=transport)

        rows = client.fetch_daily_bars(["AAPL"], __import__("datetime").date(2024, 1, 2), __import__("datetime").date(2024, 1, 4))

        self.assertEqual([row["date"].isoformat() for row in rows], ["2024-01-02", "2024-01-03"])
        self.assertEqual(transport.calls[1]["params"]["page_token"], "next-token")

    def test_runtime_config_loads_toml_and_env_overrides(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "quantbot.toml"
            config_path.write_text(
                """
[broker]
primary = "Alpaca"
base_url = "https://paper-api.alpaca.markets"

[data]
root = "/tmp/quantbot-data"

[risk]
max_single_stock_weight = 0.04
""",
                encoding="utf-8",
            )

            config = load_runtime_config(
                config_path,
                environ={"ALPACA_API_KEY": "env-key", "ALPACA_SECRET_KEY": "env-secret"},
            )

        self.assertEqual(config.broker.primary, "Alpaca")
        self.assertEqual(config.broker.api_key, "env-key")
        self.assertEqual(config.data.root, Path("/tmp/quantbot-data"))
        self.assertEqual(config.risk.max_single_stock_weight, 0.04)

    def test_live_smoke_gate_requires_all_checks_to_pass(self):
        gate = LiveSmokeGate(
            [
                LiveSmokeCheck("alpaca", lambda: True),
                LiveSmokeCheck("fred", lambda: False),
            ]
        )

        result = gate.run()

        self.assertFalse(result.ready)
        self.assertEqual(result.failed, ["fred"])

    def test_cli_live_smoke_outputs_json_summary(self):
        stdout = io.StringIO()
        checks_json = json.dumps(
            [
                {"name": "alpaca", "passed": True},
                {"name": "fred", "passed": False},
            ]
        )

        exit_code = main(["live-smoke", "--checks-json", checks_json], stdout=stdout)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["failed"], ["fred"])

    def test_cli_live_smoke_only_accepts_actual_boolean_true(self):
        stdout = io.StringIO()

        exit_code = main(
            [
                "live-smoke",
                "--checks-json",
                json.dumps([{"name": "alpaca", "passed": "false"}]),
            ],
            stdout=stdout,
        )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertFalse(payload["ready"])
        self.assertEqual(payload["passed"], [])
        self.assertEqual(payload["failed"], ["alpaca"])


class PagedTransport:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get_json(self, url, headers=None, params=None):
        self.calls.append({"url": url, "headers": headers or {}, "params": dict(params or {})})
        return self.pages.pop(0)


if __name__ == "__main__":
    unittest.main()
