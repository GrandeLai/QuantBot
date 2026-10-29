"""跨季度、修订、盘前盘后和假日的显式合成事件数据。"""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import math

from quantbot.ai.feeds import stock_calendar

from .archive import EASTERN, SEC, parse_submission, quarters


def example_submission(accession, accepted, *, filed=None, form="8-K", body="A synthetic operating update.", changed=None):
    day = filed or accepted[:8]
    return f"""<SEC-DOCUMENT>{accession}.txt
<SEC-HEADER>
<ACCEPTANCE-DATETIME>{accepted}
ACCESSION NUMBER: {accession}
CONFORMED SUBMISSION TYPE: {form}
CONFORMED PERIOD OF REPORT: {day}
FILED AS OF DATE: {day}
DATE AS OF CHANGE: {changed or day}
FILER:
 COMPANY DATA:
  COMPANY CONFORMED NAME: DEMO SYNTHETIC
  CENTRAL INDEX KEY: 0001234567
</SEC-HEADER>
<DOCUMENT>
<TYPE>EX-99.1
<FILENAME>exhibit.htm
<TEXT><html><body>Exhibit, not the main filing.</body></html></TEXT>
</DOCUMENT>
<DOCUMENT>
<TYPE>{form}
<FILENAME>main.htm
<TEXT><html><body><h2>Item 2.02. Results of Operations and Financial Condition.</h2><p>{body}</p><h2>Item 9.01. Financial Statements and Exhibits.</h2><p>Synthetic annex.</p></body></html></TEXT>
</DOCUMENT>
</SEC-DOCUMENT>"""


def example_input():
    start, end = "2024-12-02", "2025-03-31"
    calendar = stock_calendar(start, end)
    days = [day.date() for day in calendar.sessions_in_range(start, end)]
    captured = "2025-04-02T00:00:00+00:00"
    prices = {"schema_version": 1, "source": "QuantBot explicit synthetic event prices", "data_kind": "synthetic",
              "captured_at": captured, "price_basis": "raw", "bars": [], "dividends": [], "actions": []}
    for i, day in enumerate(days):
        for symbol in ("DEMO", "SPY"):
            opening = round(100 + i * (0.04 if symbol == "SPY" else 0.12) + (0 if symbol == "SPY" else 2 * math.sin(i / 4)), 4)
            close = round(opening * (1.0003 if symbol == "SPY" else 1 + 0.003 * math.sin(i)), 4)
            prices["bars"].append({"date": str(day), "symbol": symbol, "open": opening, "close": close,
                "high": round(max(opening, close) + 1, 4), "low": round(min(opening, close) - 1, 4),
                "volume": 1000000, "available_at": calendar.session_close(str(day)).isoformat()})
    filings = []
    moments = ["20241206080000", "20241227163000", "20241231180000", "20250103100000",
               "20250117160000", "20250214120000", "20250307120000", "20250328160000"]
    for i, accepted in enumerate(moments):
        accession = f"0009999999-{accepted[2:4]}-{i + 1:06d}"
        filed = "20250102" if i == 2 else accepted[:8]
        raw = example_submission(accession, accepted, filed=filed)
        seen = datetime.strptime(accepted, "%Y%m%d%H%M%S").replace(tzinfo=EASTERN).astimezone(UTC) + timedelta(minutes=2)
        if i == 2:
            seen = datetime(2025, 1, 2, 13, tzinfo=UTC)
        index = {"cik": "0001234567", "company": "DEMO SYNTHETIC", "form": "8-K", "filing_date": datetime.strptime(filed, "%Y%m%d").date().isoformat(),
                 "accession": accession, "source_url": f"{SEC}edgar/data/1234567/{accession}.txt"}
        filings.append(parse_submission(raw, index, seen.isoformat()))
    amendment_acc = "0009999999-25-000009"
    amendment = parse_submission(example_submission(amendment_acc, "20250121160000", form="8-K/A"),
        {"cik": "0001234567", "company": "DEMO SYNTHETIC", "form": "8-K/A", "filing_date": "2025-01-21",
         "accession": amendment_acc, "source_url": f"{SEC}edgar/data/1234567/{amendment_acc}.txt"}, "2025-01-21T21:02:00+00:00")
    amendment["amends_accession"] = filings[4]["accession"]
    revision = filings[0]
    revision = parse_submission(example_submission(revision["accession"], revision["accepted_raw"], body="Later corrected synthetic content.", changed="20250203"),
        {key: revision[key] for key in ("cik", "company", "form", "filing_date", "accession", "source_url")}, "2025-02-03T14:00:00+00:00")
    filings += [amendment, revision, deepcopy(filings[0])]
    bundle = {"schema_version": 1, "source": "QuantBot explicit synthetic SEC filings", "data_kind": "synthetic", "captured_at": captured,
              "coverage": {"start": start, "end": end, "ciks": ["0001234567"], "quarters": quarters(start, end),
                           "complete": True, "universe_basis": "explicit_ciks_not_historical_population"}, "filings": filings}
    return {"bundle": bundle, "prices": prices, "universe": [{"cik": "0001234567", "symbol": "DEMO", "start": start, "end": end}],
            "policy": {"horizons": [1, 3, 5], "benchmark": "SPY"}}
