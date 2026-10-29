import sys
import unittest
from datetime import date
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.integrations.finra import FinraShortVolumeClient
from quantbot.integrations.gdelt import GDELTNewsClient
from quantbot.integrations.nasdaq import NasdaqDataLinkClient
from quantbot.integrations.alpaca_news import AlpacaNewsClient
from quantbot.integrations.cboe import CBOEVixClient
from quantbot.integrations.polygon import PolygonClient
from quantbot.integrations.pytrends import PyTrendsClient
from quantbot.integrations.quandl import QuandlShortInterestClient
from quantbot.integrations.sec import SecEdgarClient
from quantbot.integrations.sec_form4 import SecForm4Client
from quantbot.integrations.sec13f import Sec13FClient
from quantbot.integrations.yfinance import YFinanceClient


class StockBotDataClientsTest(unittest.TestCase):
    def test_polygon_client_parses_minute_bars_for_vwap_execution(self):
        transport = FakeTransport({"results": [{"t": 1704205800000, "o": 100, "h": 101, "l": 99, "c": 100.5, "v": 1000, "vw": 100.2}]})
        client = PolygonClient(api_key="poly", transport=transport)

        rows = client.fetch_minute_bars("AAPL", date(2024, 1, 2), date(2024, 1, 2))

        self.assertEqual(rows[0]["ticker"], "AAPL")
        self.assertEqual(rows[0]["vwap"], 100.2)
        self.assertEqual(transport.calls[0]["params"]["apiKey"], "poly")

    def test_polygon_client_parses_options_snapshot_for_iv_skew_factor(self):
        transport = FakeTransport(
            {
                "results": [
                    {"details": {"ticker": "O:AAPL240119C00100000", "contract_type": "call"}, "implied_volatility": 0.35},
                    {"details": {"ticker": "O:AAPL240119P00100000", "contract_type": "put"}, "implied_volatility": 0.27},
                ]
            }
        )
        client = PolygonClient(api_key="poly", transport=transport)

        rows = client.fetch_options_snapshot("AAPL")

        self.assertEqual(rows[0]["underlying"], "AAPL")
        self.assertEqual(rows[0]["contract_type"], "call")
        self.assertAlmostEqual(rows[1]["implied_volatility"], 0.27)
        self.assertIn("/v3/snapshot/options/AAPL", transport.calls[0]["url"])

    def test_yfinance_client_parses_chart_payload_as_ohlcv_fallback(self):
        transport = FakeTransport(
            {
                "chart": {
                    "result": [
                        {
                            "timestamp": [1704153600],
                            "indicators": {
                                "quote": [{"open": [100], "high": [101], "low": [99], "close": [100.5], "volume": [1000]}],
                                "adjclose": [{"adjclose": [100.4]}],
                            },
                        }
                    ]
                }
            }
        )
        client = YFinanceClient(transport=transport)

        rows = client.fetch_daily_bars("AAPL", date(2024, 1, 2), date(2024, 1, 3))

        self.assertEqual(rows[0]["adjusted_close"], 100.4)
        self.assertEqual(transport.calls[0]["params"]["interval"], "1d")

    def test_finra_client_parses_short_volume_ratio(self):
        transport = FakeTextTransport("Date|Symbol|ShortVolume|TotalVolume\n20240102|AAPL|300|1000\n")
        client = FinraShortVolumeClient(transport=transport)

        rows = client.fetch_short_volume(date(2024, 1, 2))

        self.assertEqual(rows[0]["ticker"], "AAPL")
        self.assertAlmostEqual(rows[0]["short_ratio"], 0.3)

    def test_quandl_short_interest_client_parses_backup_short_interest_rows(self):
        transport = FakeTransport({"datatable": {"data": [["AAPL", "2024-01-15", 300, 1000]], "columns": []}})
        client = QuandlShortInterestClient(api_key="quandl-key", transport=transport)

        rows = client.fetch_short_interest("AAPL")

        self.assertEqual(rows[0]["ticker"], "AAPL")
        self.assertEqual(rows[0]["date"], date(2024, 1, 15))
        self.assertAlmostEqual(rows[0]["short_ratio"], 0.3)
        self.assertEqual(transport.calls[0]["params"]["api_key"], "quandl-key")

    def test_cboe_vix_client_parses_vix_history_csv(self):
        transport = FakeTextTransport("DATE,VIX\n2024-01-02,13.50\n2024-01-03,14.25\n")
        client = CBOEVixClient(transport=transport)

        rows = client.fetch_history()

        self.assertEqual(rows[0], {"date": date(2024, 1, 2), "series": "VIX", "value": 13.5})
        self.assertEqual(rows[1]["value"], 14.25)
        self.assertIn("vixcurrent.csv", transport.calls[0]["url"].lower())

    def test_nasdaq_data_link_client_parses_eps_revision_rows(self):
        transport = FakeTransport({"datatable": {"data": [["AAPL", "2024-01-02", 1.20, 1.10]], "columns": []}})
        client = NasdaqDataLinkClient(api_key="ndl", transport=transport)

        rows = client.fetch_eps_revisions("AAPL")

        self.assertEqual(rows[0]["ticker"], "AAPL")
        self.assertAlmostEqual(rows[0]["revision"], 1.20 / 1.10 - 1)
        self.assertEqual(transport.calls[0]["params"]["api_key"], "ndl")

    def test_gdelt_news_client_parses_article_metadata_for_sentiment_layer(self):
        transport = FakeTransport({"articles": [{"title": "Beat and raise", "url": "https://example.com", "seendate": "20240102120000"}]})
        client = GDELTNewsClient(transport=transport)

        rows = client.search("AAPL earnings", max_records=10)

        self.assertEqual(rows[0]["title"], "Beat and raise")
        self.assertEqual(rows[0]["published_at"], "20240102120000")
        self.assertEqual(transport.calls[0]["params"]["maxrecords"], 10)

    def test_alpaca_news_client_parses_authenticated_news_rows(self):
        transport = FakeTransport(
            {
                "news": [
                    {
                        "headline": "Apple beats estimates",
                        "summary": "Revenue beat and guidance raised",
                        "url": "https://example.com/aapl",
                        "created_at": "2024-01-02T14:30:00Z",
                        "symbols": ["AAPL"],
                    }
                ]
            }
        )
        client = AlpacaNewsClient(api_key="key", secret_key="secret", transport=transport)

        rows = client.search(symbols=["AAPL"], start=date(2024, 1, 2), end=date(2024, 1, 3), limit=5)

        self.assertEqual(rows[0]["ticker"], "AAPL")
        self.assertEqual(rows[0]["title"], "Apple beats estimates")
        self.assertEqual(rows[0]["summary"], "Revenue beat and guidance raised")
        self.assertEqual(transport.calls[0]["headers"]["APCA-API-KEY-ID"], "key")
        self.assertEqual(transport.calls[0]["params"]["symbols"], "AAPL")
        self.assertEqual(transport.calls[0]["params"]["limit"], 5)

    def test_pytrends_client_parses_search_interest_rows(self):
        pytrends = FakePyTrends(
            [
                {"date": "2024-01-01", "AAPL stock": 65, "isPartial": False},
                {"date": "2024-01-02", "AAPL stock": 80, "isPartial": False},
            ]
        )
        client = PyTrendsClient(pytrends)

        rows = client.fetch_interest_over_time("AAPL stock", start=date(2024, 1, 1), end=date(2024, 1, 2))

        self.assertEqual(rows[0], {"date": date(2024, 1, 1), "keyword": "AAPL stock", "search_volume": 65.0})
        self.assertEqual(rows[1]["search_volume"], 80.0)
        self.assertEqual(pytrends.payloads[0]["kw_list"], ["AAPL stock"])
        self.assertEqual(pytrends.payloads[0]["timeframe"], "2024-01-01 2024-01-02")

    def test_sec13f_client_extracts_holdings_from_infotable_xml(self):
        transport = FakeTextTransport(
            """
            <informationTable>
              <infoTable>
                <nameOfIssuer>APPLE INC</nameOfIssuer>
                <cusip>037833100</cusip>
                <sshPrnamt>1000</sshPrnamt>
                <value>150000</value>
              </infoTable>
            </informationTable>
            """
        )
        client = Sec13FClient(user_agent="quantbot@example.com", transport=transport)

        holdings = client.fetch_infotable("https://sec.example/infotable.xml")

        self.assertEqual(holdings[0]["issuer"], "APPLE INC")
        self.assertEqual(holdings[0]["shares"], 1000)
        self.assertEqual(transport.calls[0]["headers"]["User-Agent"], "quantbot@example.com")

    def test_sec_edgar_client_parses_recent_8k_and_form4_submissions(self):
        transport = FakeTransport(
            {
                "filings": {
                    "recent": {
                        "form": ["8-K", "4", "10-Q"],
                        "accessionNumber": ["0001", "0002", "0003"],
                        "filingDate": ["2024-01-02", "2024-01-03", "2024-01-04"],
                        "primaryDocument": ["a8k.htm", "form4.xml", "10q.htm"],
                        "items": ["4.02", "", ""],
                    }
                }
            }
        )
        client = SecEdgarClient(user_agent="quantbot@example.com", transport=transport)

        filings = client.recent_filings("320193", forms={"8-K", "4"})

        self.assertEqual([item["form"] for item in filings], ["8-K", "4"])
        self.assertEqual(filings[0]["filing_date"], date(2024, 1, 2))
        self.assertEqual(filings[0]["items"], "4.02")
        self.assertEqual(filings[1]["primary_document"], "form4.xml")

    def test_sec_form4_client_extracts_insider_transaction_values(self):
        transport = FakeTextTransport(
            """
            <ownershipDocument>
              <issuer><issuerTradingSymbol>AAPL</issuerTradingSymbol></issuer>
              <reportingOwner><reportingOwnerId><rptOwnerName>Jane Executive</rptOwnerName></reportingOwnerId></reportingOwner>
              <nonDerivativeTable>
                <nonDerivativeTransaction>
                  <transactionDate><value>2024-01-02</value></transactionDate>
                  <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
                  <transactionAmounts>
                    <transactionShares><value>100</value></transactionShares>
                    <transactionPricePerShare><value>150.25</value></transactionPricePerShare>
                    <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
                  </transactionAmounts>
                </nonDerivativeTransaction>
                <nonDerivativeTransaction>
                  <transactionDate><value>2024-01-03</value></transactionDate>
                  <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
                  <transactionAmounts>
                    <transactionShares><value>50</value></transactionShares>
                    <transactionPricePerShare><value>151.00</value></transactionPricePerShare>
                    <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
                  </transactionAmounts>
                </nonDerivativeTransaction>
              </nonDerivativeTable>
            </ownershipDocument>
            """
        )
        client = SecForm4Client(user_agent="quantbot@example.com", transport=transport)

        transactions = client.fetch_transactions("https://sec.example/form4.xml")

        self.assertEqual(transactions[0]["ticker"], "AAPL")
        self.assertEqual(transactions[0]["owner"], "Jane Executive")
        self.assertEqual(transactions[0]["side"], "buy")
        self.assertAlmostEqual(transactions[0]["value"], 15025.0)
        self.assertEqual(transactions[1]["side"], "sell")
        self.assertAlmostEqual(transactions[1]["value"], 7550.0)
        self.assertEqual(transport.calls[0]["headers"]["User-Agent"], "quantbot@example.com")


class FakeTransport:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get_json(self, url, headers=None, params=None):
        self.calls.append({"url": url, "headers": headers or {}, "params": params or {}})
        return self.payload


class FakeTextTransport:
    def __init__(self, text):
        self.text = text
        self.calls = []

    def get_text(self, url, headers=None, params=None):
        self.calls.append({"url": url, "headers": headers or {}, "params": params or {}})
        return self.text


class FakePyTrends:
    def __init__(self, records):
        self.records = records
        self.payloads = []

    def build_payload(self, kw_list, timeframe):
        self.payloads.append({"kw_list": kw_list, "timeframe": timeframe})

    def interest_over_time(self):
        return FakeTrendFrame(self.records)


class FakeTrendFrame:
    def __init__(self, records):
        self.records = records

    def reset_index(self):
        return self

    def to_dict(self, orient):
        if orient != "records":
            raise ValueError("expected records orient")
        return self.records


if __name__ == "__main__":
    unittest.main()
