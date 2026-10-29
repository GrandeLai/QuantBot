"""有界 JSON HTTP 请求；不重试下单，不跟随携带凭据的重定向。"""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .domain import strict_json


class ProviderError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        self.status = status
        super().__init__(message)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class JsonTransport:
    def __init__(self, timeout=45):
        self.timeout = timeout
        self.opener = build_opener(_NoRedirect())

    def _request(self, method, url, headers=None, params=None, payload=None):
        if params:
            url += "?" + urlencode(params)
        body = None if payload is None else json.dumps(payload, allow_nan=False).encode()
        request_headers = {"Accept": "application/json", **(headers or {})}
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        request = Request(url, data=body, headers=request_headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                encoded = response.read(1_048_577)
                if len(encoded) > 1_048_576:
                    raise ProviderError("接口响应超过 1 MiB，停止处理")
                return strict_json(encoded.decode("utf-8")) if encoded else {}
        except HTTPError as error:
            # Do not log a response body, headers, URL query, or exception containing credentials.
            raise ProviderError(f"接口 HTTP {error.code}", status=error.code) from None
        except (URLError, TimeoutError, OSError):
            raise ProviderError("接口连接失败或超时，未自动重试") from None
        except (UnicodeError, ValueError):
            raise ProviderError("接口返回了无效 JSON") from None

    def get_json(self, url, headers=None, params=None):
        return self._request("GET", url, headers, params)

    def post_json(self, url, headers=None, payload=None):
        return self._request("POST", url, headers, payload=payload)

    def delete_json(self, url, headers=None):
        return self._request("DELETE", url, headers)
