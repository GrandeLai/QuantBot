"""SEC 13F information table client."""

import xml.etree.ElementTree as ET

from .http import UrllibJsonTransport


class Sec13FClient:
    def __init__(self, user_agent: str, transport=None):
        self.user_agent = user_agent
        self.transport = transport or UrllibJsonTransport()

    def fetch_infotable(self, url: str):
        text = self.transport.get_text(url, headers={"User-Agent": self.user_agent})
        root = ET.fromstring(text)
        holdings = []
        for info_table in root.findall(".//infoTable"):
            holdings.append(
                {
                    "issuer": _text(info_table, "nameOfIssuer"),
                    "cusip": _text(info_table, "cusip"),
                    "shares": int(float(_text(info_table, "sshPrnamt") or 0)),
                    "value": float(_text(info_table, "value") or 0),
                }
            )
        return holdings


def _text(element, tag):
    child = element.find(tag)
    return child.text.strip() if child is not None and child.text else None
