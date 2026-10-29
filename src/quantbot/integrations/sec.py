"""SEC EDGAR API client."""

from datetime import date
from typing import List

from .http import UrllibJsonTransport


class SecEdgarClient:
    def __init__(
        self,
        user_agent: str,
        transport=None,
        base_url: str = "https://data.sec.gov/api/xbrl/companyfacts/CIK%s.json",
        submissions_url: str = "https://data.sec.gov/submissions/CIK%s.json",
    ):
        self.user_agent = user_agent
        self.transport = transport or UrllibJsonTransport()
        self.base_url = base_url
        self.submissions_url = submissions_url

    def company_facts(self, cik: str) -> List[dict]:
        normalized_cik = str(cik).zfill(10)
        payload = self.transport.get_json(
            self.base_url % normalized_cik,
            headers={"User-Agent": self.user_agent},
        )
        rows = []
        facts = payload.get("facts", {})
        for taxonomy, concepts in facts.items():
            for concept, definition in concepts.items():
                units = definition.get("units", {})
                for unit, values in units.items():
                    for item in values:
                        if "filed" not in item or "val" not in item:
                            continue
                        rows.append(
                            {
                                "taxonomy": taxonomy,
                                "concept": concept,
                                "unit": unit,
                                "fiscal_year": item.get("fy"),
                                "fiscal_period": item.get("fp"),
                                "filed": date.fromisoformat(item["filed"]),
                                "value": item["val"],
                            }
                        )
        return rows

    def recent_filings(self, cik: str, forms=None) -> List[dict]:
        forms = set(forms or [])
        normalized_cik = str(cik).zfill(10)
        payload = self.transport.get_json(
            self.submissions_url % normalized_cik,
            headers={"User-Agent": self.user_agent},
        )
        recent = payload.get("filings", {}).get("recent", {})
        rows = []
        form_values = recent.get("form", [])
        accession_values = recent.get("accessionNumber", [])
        filing_dates = recent.get("filingDate", [])
        primary_documents = recent.get("primaryDocument", [])
        items_values = recent.get("items", [])
        for index, form in enumerate(form_values):
            if forms and form not in forms:
                continue
            rows.append(
                {
                    "form": form,
                    "accession_number": _at(accession_values, index),
                    "filing_date": date.fromisoformat(_at(filing_dates, index)),
                    "primary_document": _at(primary_documents, index),
                    "items": _at(items_values, index),
                }
            )
        return rows


def _at(values, index):
    return values[index] if index < len(values) else ""
