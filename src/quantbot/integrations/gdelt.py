"""GDELT document search client."""

from .http import UrllibJsonTransport


class GDELTNewsClient:
    def __init__(self, transport=None, base_url: str = "https://api.gdeltproject.org/api/v2/doc/doc"):
        self.transport = transport or UrllibJsonTransport()
        self.base_url = base_url

    def search(self, query: str, max_records: int = 50):
        payload = self.transport.get_json(
            self.base_url,
            params={
                "query": query,
                "mode": "artlist",
                "format": "json",
                "maxrecords": max_records,
            },
        )
        return [
            {
                "title": item.get("title"),
                "url": item.get("url"),
                "published_at": item.get("seendate"),
            }
            for item in payload.get("articles", [])
        ]
