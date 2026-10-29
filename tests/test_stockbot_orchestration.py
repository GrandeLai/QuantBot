import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.dashboard_rendering import StreamlitDashboardRenderer
from quantbot.data.duckdb_store import DuckDBParquetStore
from quantbot.execution import BrokerFailoverExecutor, IBKRBrokerAdapter
from quantbot.orchestration import DailyTradingWorkflow, WorkflowInputs
from quantbot.regime import Regime
from quantbot.trading import BrokerAccountSnapshot, MarginStatus


class StockBotOrchestrationTest(unittest.TestCase):
    def test_duckdb_store_writes_records_to_partitioned_parquet(self):
        connection = FakeDuckDBConnection()
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        store = DuckDBParquetStore(root=Path(tmpdir.name), connection=connection)

        path = store.write_partition("ohlcv", "2024-01-02", [{"ticker": "AAPL", "close": 100}])

        self.assertEqual(path, Path(tmpdir.name) / "ohlcv" / "date=2024-01-02" / "data.parquet")
        self.assertTrue(path.parent.exists())
        self.assertEqual(connection.registered_name, "records")
        self.assertIn("COPY records TO", connection.sql[0])
        self.assertIn("FORMAT PARQUET", connection.sql[0])

    def test_ibkr_adapter_maps_market_orders_and_account_value(self):
        ib = FakeIBKR(portfolio_value=200_000)
        adapter = IBKRBrokerAdapter(ib)

        adapter.submit_order("AAPL", notional=10_000, side="buy")
        adapter.close_all_positions()

        self.assertEqual(adapter.get_portfolio_value(), 200_000)
        self.assertEqual(ib.orders, [{"symbol": "AAPL", "notional": 10_000, "side": "buy", "order_type": "MKT"}])
        self.assertTrue(ib.closed)

    def test_broker_failover_executor_uses_backup_when_primary_prepare_fails(self):
        primary = RecordingBroker(portfolio_value=100_000, trading_blocked=True)
        backup = RecordingBroker(portfolio_value=100_000)
        executor = BrokerFailoverExecutor(primary, backup)

        result = executor.rebalance(
            target_weights={"AAPL": 0.10},
            current_holdings={},
            backup_current_holdings={},
        )

        self.assertFalse(result.primary_used)
        self.assertTrue(result.backup_used)
        self.assertIn("account_trading_blocked", result.detail)
        self.assertEqual(backup.orders[0]["symbol"], "AAPL")

    def test_streamlit_renderer_emits_dashboard_fields_to_injected_streamlit_module(self):
        streamlit = FakeStreamlit()
        renderer = StreamlitDashboardRenderer(streamlit)

        renderer.render(
            {
                "regime": "R1_BULL_LOW_VOL",
                "gross_exposure": 0.90,
                "performance": {"sharpe": 1.3},
                "factor_exposure": {"momentum": 0.2},
                "top_positions": [("AAPL", 0.05)],
            }
        )

        self.assertIn(("title", "StockBot Dashboard"), streamlit.calls)
        self.assertIn(("metric", "Regime", "R1_BULL_LOW_VOL"), streamlit.calls)
        self.assertIn(("metric", "Gross Exposure", "90.00%"), streamlit.calls)

    def test_daily_trading_workflow_generates_watchlist_weights_and_rebalance_orders(self):
        broker = RecordingBroker(portfolio_value=100_000)
        workflow = DailyTradingWorkflow(broker=broker)

        result = workflow.run(
            WorkflowInputs(
                factor_matrix={
                    "AAPL": {"momentum": 1.0, "quality": 0.8, "low_vol": 0.1},
                    "MSFT": {"momentum": 0.9, "quality": 0.7, "low_vol": 0.2},
                    "LOWADV": {"momentum": 2.0, "quality": 0.1, "low_vol": 0.1},
                },
                regime=Regime.R1_BULL_LOW_VOL,
                factor_weights={Regime.R1_BULL_LOW_VOL: {"momentum": 0.5, "quality": 0.4, "low_vol": -0.1}},
                average_dollar_volume={"AAPL": 10_000_000, "MSFT": 9_000_000, "LOWADV": 1_000_000},
                sectors={"AAPL": "Technology", "MSFT": "Technology"},
                current_holdings={"AAPL": 0.01},
                top_n=2,
            )
        )

        self.assertEqual(result.watchlist, ["AAPL", "MSFT"])
        self.assertEqual(result.target_weights, {"AAPL": 0.05, "MSFT": 0.05})
        self.assertEqual(
            broker.orders,
            [
                {"symbol": "AAPL", "notional": 4000.0, "side": "buy", "order_type": "market", "time_in_force": "day"},
                {"symbol": "MSFT", "notional": 5000.0, "side": "buy", "order_type": "market", "time_in_force": "day"},
            ],
        )


class FakeDuckDBConnection:
    def __init__(self):
        self.registered_name = None
        self.registered_records = None
        self.sql = []

    def register(self, name, records):
        self.registered_name = name
        self.registered_records = records

    def execute(self, sql):
        self.sql.append(sql)


class FakeIBKR:
    def __init__(self, portfolio_value):
        self.portfolio_value = portfolio_value
        self.orders = []
        self.closed = False

    def account_value(self):
        return self.portfolio_value

    def account_snapshot(self):
        return {
            "equity": self.portfolio_value,
            "buying_power": self.portfolio_value,
            "day_trade_buying_power": None,
            "trading_blocked": False,
            "shorting_enabled": True,
            "margin_status": "margin",
            "status": "ACTIVE",
        }

    def place_market_order(self, symbol, notional, side):
        self.orders.append({"symbol": symbol, "notional": notional, "side": side, "order_type": "MKT"})

    def close_all_positions(self):
        self.closed = True


class FakeStreamlit:
    def __init__(self):
        self.calls = []

    def title(self, value):
        self.calls.append(("title", value))

    def metric(self, label, value):
        self.calls.append(("metric", label, value))

    def write(self, value):
        self.calls.append(("write", value))


class RecordingBroker:
    def __init__(self, portfolio_value, trading_blocked=False):
        self.portfolio_value = portfolio_value
        self.trading_blocked = trading_blocked
        self.orders = []

    def get_portfolio_value(self):
        return self.portfolio_value

    def account_snapshot(self):
        return BrokerAccountSnapshot(
            broker="test",
            observed_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
            equity=self.portfolio_value,
            buying_power=self.portfolio_value,
            day_trade_buying_power=None,
            trading_blocked=self.trading_blocked,
            shorting_enabled=True,
            margin_status=MarginStatus.MARGIN,
        )

    def submit_order(self, symbol, notional, side, order_type="market", time_in_force="day"):
        self.orders.append(
            {
                "symbol": symbol,
                "notional": notional,
                "side": side,
                "order_type": order_type,
                "time_in_force": time_in_force,
            }
        )

    def close_all_positions(self):
        pass

if __name__ == "__main__":
    unittest.main()
