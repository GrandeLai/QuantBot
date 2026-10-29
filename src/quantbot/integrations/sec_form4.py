"""SEC Form 4 ownership transaction parser."""

from datetime import date
from typing import List
from xml.etree import ElementTree


class SecForm4Client:
    def __init__(self, user_agent: str, transport):
        self.user_agent = user_agent
        self.transport = transport

    def fetch_transactions(self, url: str) -> List[dict]:
        text = self.transport.get_text(url, headers={"User-Agent": self.user_agent})
        root = ElementTree.fromstring(text)
        ticker = _find_text(root, ".//issuerTradingSymbol")
        owner = _find_text(root, ".//rptOwnerName")
        rows = []
        for node in root.findall(".//nonDerivativeTransaction"):
            shares = _find_float(node, ".//transactionShares/value")
            price = _find_float(node, ".//transactionPricePerShare/value")
            acquired_disposed = _find_text(node, ".//transactionAcquiredDisposedCode/value")
            transaction_code = _find_text(node, ".//transactionCode")
            rows.append(
                {
                    "ticker": ticker,
                    "owner": owner,
                    "transaction_date": date.fromisoformat(_find_text(node, ".//transactionDate/value")),
                    "transaction_code": transaction_code,
                    "acquired_disposed": acquired_disposed,
                    "side": "buy" if acquired_disposed == "A" else "sell",
                    "shares": shares,
                    "price": price,
                    "value": shares * price,
                }
            )
        return rows


def _find_text(node, path: str) -> str:
    found = node.find(path)
    return (found.text or "").strip() if found is not None else ""


def _find_float(node, path: str) -> float:
    value = _find_text(node, path)
    return float(value) if value else 0.0
