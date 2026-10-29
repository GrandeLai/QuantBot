import sys
import unittest
from datetime import date
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.integrations.alpaca import AlpacaDataClient, AlpacaTradingClient
from quantbot.integrations.chronos import ChronosAdapter, SelfTrainedTimeSeriesAdapter
from quantbot.integrations.fred import FredClient
from quantbot.integrations.qlib import QlibAlpha158Adapter
from quantbot.integrations.rdagent_runtime import RDAgentQlibAdapter
from quantbot.integrations.sec import SecEdgarClient
from quantbot.integrations.scheduler import APSchedulerRegistrar
from quantbot.paper import PaperTradingAcceptance, PaperTradingRun
from quantbot.preflight import RuntimePreflight


class StockBotIntegrationsTest(unittest.TestCase):
    def test_alpaca_data_client_builds_authenticated_daily_bar_request(self):
        transport = FakeTransport(
            {
                "bars": {
                    "AAPL": [
                        {
                            "t": "2024-01-02T00:00:00Z",
                            "o": 100,
                            "h": 102,
                            "l": 99,
                            "c": 101,
                            "v": 1000,
                            "vw": 100.5,
                        }
                    ]
                }
            }
        )
        client = AlpacaDataClient("key", "secret", transport=transport)

        rows = client.fetch_daily_bars(["AAPL"], date(2024, 1, 2), date(2024, 1, 3))

        self.assertEqual(rows[0]["ticker"], "AAPL")
        self.assertEqual(rows[0]["adjusted_close"], 101)
        self.assertEqual(transport.calls[0]["headers"]["APCA-API-KEY-ID"], "key")
        self.assertEqual(transport.calls[0]["params"]["timeframe"], "1Day")

    def test_alpaca_data_client_builds_authenticated_minute_bar_request(self):
        transport = FakeTransport(
            {
                "bars": {
                    "AAPL": [
                        {
                            "t": "2024-01-02T14:45:00Z",
                            "o": 100,
                            "h": 102,
                            "l": 99,
                            "c": 101,
                            "v": 1000,
                            "vw": 100.5,
                        }
                    ]
                }
            }
        )
        client = AlpacaDataClient("key", "secret", transport=transport)

        rows = client.fetch_minute_bars(["AAPL"], date(2024, 1, 2), date(2024, 1, 3))

        self.assertEqual(rows[0]["ticker"], "AAPL")
        self.assertEqual(rows[0]["timestamp"].isoformat(), "2024-01-02T14:45:00+00:00")
        self.assertEqual(rows[0]["vwap"], 100.5)
        self.assertEqual(transport.calls[0]["headers"]["APCA-API-KEY-ID"], "key")
        self.assertEqual(transport.calls[0]["params"]["timeframe"], "1Min")
        self.assertEqual(transport.calls[0]["params"]["limit"], 10_000)

    def test_alpaca_trading_client_reads_account_portfolio_value(self):
        transport = FakeTransport({"portfolio_value": "100123.45", "status": "ACTIVE"})
        client = AlpacaTradingClient(
            "key",
            "secret",
            transport=transport,
            base_url="https://paper-api.alpaca.markets",
        )

        account = client.fetch_account()
        value = client.fetch_portfolio_value()

        self.assertEqual(account["status"], "ACTIVE")
        self.assertAlmostEqual(value, 100123.45)
        self.assertEqual(transport.calls[0]["url"], "https://paper-api.alpaca.markets/v2/account")
        self.assertEqual(transport.calls[0]["headers"]["APCA-API-SECRET-KEY"], "secret")

    def test_alpaca_trading_client_fetches_and_parses_trading_calendar(self):
        transport = FakeTransport(
            [
                {"date": "2024-01-02", "open": "09:30", "close": "16:00"},
                {"date": "2024-01-03", "open": "09:30", "close": "16:00"},
            ]
        )
        client = AlpacaTradingClient(
            "key",
            "secret",
            transport=transport,
            base_url="https://paper-api.alpaca.markets",
        )

        self.assertTrue(hasattr(client, "fetch_calendar"))
        sessions = client.fetch_calendar(date(2024, 1, 2), date(2024, 1, 3))

        self.assertEqual(sessions, [date(2024, 1, 2), date(2024, 1, 3)])
        self.assertEqual(transport.calls[0]["url"], "https://paper-api.alpaca.markets/v2/calendar")
        self.assertEqual(
            transport.calls[0]["params"],
            {"start": "2024-01-02", "end": "2024-01-03"},
        )
        self.assertEqual(transport.calls[0]["headers"]["APCA-API-KEY-ID"], "key")

    def test_alpaca_trading_client_submits_and_closes_positions(self):
        transport = FakeTransport({"id": "order-1"})
        client = AlpacaTradingClient("key", "secret", transport=transport, base_url="https://paper-api.alpaca.markets")

        client.submit_order(symbol="AAPL", notional=1234.56, side="buy")
        client.close_all_positions()

        self.assertEqual(transport.calls[0]["method"], "POST")
        self.assertEqual(transport.calls[0]["url"], "https://paper-api.alpaca.markets/v2/orders")
        self.assertEqual(transport.calls[0]["payload"]["notional"], 1234.56)
        self.assertEqual(transport.calls[1]["method"], "DELETE")
        self.assertEqual(transport.calls[1]["url"], "https://paper-api.alpaca.markets/v2/positions")

    def test_fred_client_parses_observations_and_skips_missing_values(self):
        transport = FakeTransport(
            {
                "observations": [
                    {"date": "2024-01-01", "value": "3.5"},
                    {"date": "2024-01-02", "value": "."},
                ]
            }
        )
        client = FredClient(api_key="fred-key", transport=transport)

        rows = client.fetch_series("DGS10", date(2024, 1, 1), date(2024, 1, 3))

        self.assertEqual(rows, [{"date": date(2024, 1, 1), "series": "DGS10", "value": 3.5}])
        self.assertEqual(transport.calls[0]["params"]["api_key"], "fred-key")

    def test_sec_edgar_client_uses_user_agent_and_parses_company_facts(self):
        transport = FakeTransport(
            {
                "facts": {
                    "us-gaap": {
                        "Assets": {
                            "units": {
                                "USD": [
                                    {"fy": 2024, "fp": "Q1", "filed": "2024-05-01", "val": 1000}
                                ]
                            }
                        }
                    }
                }
            }
        )
        client = SecEdgarClient(user_agent="quantbot@example.com", transport=transport)

        facts = client.company_facts("0000320193")

        self.assertEqual(facts[0]["concept"], "Assets")
        self.assertEqual(facts[0]["filed"], date(2024, 5, 1))
        self.assertEqual(transport.calls[0]["headers"]["User-Agent"], "quantbot@example.com")

    def test_qlib_adapter_initializes_us_provider_and_returns_factor_frame(self):
        qlib = FakeQlib()
        alpha_factory = FakeAlphaFactory()
        adapter = QlibAlpha158Adapter(qlib_module=qlib, alpha158_factory=alpha_factory)

        result = adapter.compute("~/.qlib/us_data", "sp500", "2015-01-01", "2024-12-31")

        self.assertEqual(qlib.init_args, {"provider_uri": "~/.qlib/us_data", "region": "us"})
        self.assertEqual(alpha_factory.kwargs["instruments"], "sp500")
        self.assertEqual(result, {"factor_frame": "alpha158"})

    def test_chronos_adapter_extracts_mean_vol_and_skew_from_pipeline_forecast(self):
        adapter = ChronosAdapter(pipeline=FakeChronosPipeline([0.01, 0.02, -0.01]))

        features = adapter.predict_features([0.0, 0.01], prediction_length=3)

        self.assertAlmostEqual(features["kronos_mean"], 0.006666666666666666)
        self.assertGreater(features["kronos_vol"], 0)
        self.assertIn("kronos_skew", features)

    def test_self_trained_time_series_adapter_uses_alpha158_and_ohlcv_features(self):
        model = FakeForecastModel(prediction=0.025)
        adapter = SelfTrainedTimeSeriesAdapter(model=model)
        feature_rows = [
            {
                "alpha158": {"momentum_5": 0.03, "volatility_5": 0.12},
                "ohlcv": {"adjusted_close": 100.0, "volume": 1000},
            },
            {
                "alpha158": {"momentum_5": 0.01, "volatility_5": 0.10},
                "ohlcv": {"adjusted_close": 102.0, "volume": 1200},
            },
        ]

        adapter.fit(feature_rows, targets=[0.02, -0.01])
        prediction = adapter.predict_excess_return(
            alpha158_features={"momentum_5": 0.04, "volatility_5": 0.11},
            ohlcv={"adjusted_close": 103.0, "volume": 1300},
        )

        self.assertAlmostEqual(prediction, 0.025)
        self.assertEqual(model.fit_y, [0.02, -0.01])
        self.assertIn("alpha158.momentum_5", model.fit_x[0])
        self.assertIn("ohlcv.volume", model.fit_x[0])
        self.assertEqual(model.predict_x[0]["alpha158.momentum_5"], 0.04)

    def test_rd_agent_runtime_adapter_runs_qlib_factor_experiment(self):
        adapter = RDAgentQlibAdapter(experiment_factory=FakeExperiment)

        result = adapter.run_factor_experiment(
            "short interest pressure predicts underperformance",
            market="us_sp500",
            validation_period=("2020-01-01", "2023-12-31"),
        )

        self.assertTrue(result.accepted)
        self.assertEqual(result.metrics.name, "rd_agent_factor")
        self.assertEqual(FakeExperiment.last_kwargs["market"], "us_sp500")

    def test_apscheduler_registrar_adds_daily_pipeline_job(self):
        scheduler = FakeScheduler()
        registrar = APSchedulerRegistrar(scheduler)

        registrar.register_daily_after_close(lambda: None, timezone="US/Eastern", hour=16, minute=30)

        self.assertEqual(scheduler.jobs[0]["trigger"], "cron")
        self.assertEqual(scheduler.jobs[0]["hour"], 16)
        self.assertEqual(scheduler.jobs[0]["minute"], 30)
        self.assertEqual(scheduler.jobs[0]["timezone"], "US/Eastern")

    def test_apscheduler_registrar_adds_monthly_rd_agent_discovery_job(self):
        scheduler = FakeScheduler()
        registrar = APSchedulerRegistrar(scheduler)

        registrar.register_monthly_rd_agent_discovery(lambda: None, timezone="US/Eastern", day=1, hour=18, minute=0)

        self.assertEqual(scheduler.jobs[0]["trigger"], "cron")
        self.assertEqual(scheduler.jobs[0]["day"], 1)
        self.assertEqual(scheduler.jobs[0]["hour"], 18)
        self.assertEqual(scheduler.jobs[0]["minute"], 0)
        self.assertEqual(scheduler.jobs[0]["timezone"], "US/Eastern")

    def test_paper_trading_acceptance_requires_four_clean_weeks_and_backtest_deviation_under_30pct(self):
        acceptance = PaperTradingAcceptance(min_weeks=4, max_performance_deviation=0.30)

        accepted = acceptance.evaluate(
            [
                PaperTradingRun("week1", errors=0, paper_return=0.010, backtest_return=0.010),
                PaperTradingRun("week2", errors=0, paper_return=0.012, backtest_return=0.011),
                PaperTradingRun("week3", errors=0, paper_return=-0.004, backtest_return=-0.005),
                PaperTradingRun("week4", errors=0, paper_return=0.009, backtest_return=0.010),
            ]
        )
        rejected = acceptance.evaluate(
            [
                PaperTradingRun("week1", errors=0, paper_return=0.010, backtest_return=0.010),
                PaperTradingRun("week2", errors=1, paper_return=0.012, backtest_return=0.011),
                PaperTradingRun("week3", errors=0, paper_return=-0.004, backtest_return=-0.005),
                PaperTradingRun("week4", errors=0, paper_return=0.020, backtest_return=0.010),
            ]
        )

        self.assertTrue(accepted.passed)
        self.assertFalse(rejected.passed)
        self.assertIn("runtime_errors", rejected.reasons)
        self.assertIn("performance_deviation", rejected.reasons)

    def test_runtime_preflight_reports_missing_credentials_and_optional_packages(self):
        preflight = RuntimePreflight(
            environ={"ALPACA_API_KEY": "key"},
            import_checker=lambda name: name == "duckdb",
        )

        report = preflight.check()

        self.assertFalse(report.ready)
        self.assertIn("ALPACA_SECRET_KEY", report.missing_env)
        self.assertIn("qlib", report.missing_packages)
        self.assertNotIn("duckdb", report.missing_packages)


class FakeTransport:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get_json(self, url, headers=None, params=None):
        self.calls.append({"method": "GET", "url": url, "headers": headers or {}, "params": params or {}})
        return self.payload

    def post_json(self, url, headers=None, payload=None):
        self.calls.append({"method": "POST", "url": url, "headers": headers or {}, "payload": payload or {}})
        return self.payload

    def delete_json(self, url, headers=None, params=None):
        self.calls.append({"method": "DELETE", "url": url, "headers": headers or {}, "params": params or {}})
        return self.payload


class FakeQlib:
    def init(self, provider_uri, region):
        self.init_args = {"provider_uri": provider_uri, "region": region}


class FakeAlphaFactory:
    def __call__(self, **kwargs):
        self.kwargs = kwargs
        return FakeAlphaHandler()


class FakeAlphaHandler:
    def fetch(self):
        return {"factor_frame": "alpha158"}


class FakeChronosPipeline:
    def __init__(self, forecast):
        self.forecast = forecast

    def predict(self, context, prediction_length):
        self.context = context
        self.prediction_length = prediction_length
        return self.forecast


class FakeForecastModel:
    def __init__(self, prediction):
        self.prediction = prediction

    def fit(self, x_values, y_values):
        self.fit_x = x_values
        self.fit_y = y_values

    def predict(self, x_values):
        self.predict_x = x_values
        return [self.prediction]


class FakeExperiment:
    last_kwargs = None

    def __init__(self, **kwargs):
        FakeExperiment.last_kwargs = kwargs

    def run(self):
        return type("Result", (), {
            "ic_mean": 0.03, "icir": 0.5, "accepted": True,
            "ic_positive_rate": 0.55, "long_short_return_annual": 0.08,
            "decay_days": 3, "oos_ic": 0.025, "turnover_annual": 2.0,
        })()


class FakeScheduler:
    def __init__(self):
        self.jobs = []

    def add_job(self, func, trigger, **kwargs):
        self.jobs.append({"func": func, "trigger": trigger, **kwargs})


if __name__ == "__main__":
    unittest.main()
