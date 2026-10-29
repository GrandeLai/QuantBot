"""Small JSON transport used by optional API clients."""

import json
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class UrllibJsonTransport:
    def get_json(self, url, headers=None, params=None):
        headers = headers or {}
        params = params or {}
        if params:
            separator = "&" if "?" in url else "?"
            url = url + separator + urlencode(params)
        request = Request(url, headers=headers, method="GET")
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    def get_text(self, url, headers=None, params=None):
        headers = headers or {}
        params = params or {}
        if params:
            separator = "&" if "?" in url else "?"
            url = url + separator + urlencode(params)
        request = Request(url, headers=headers, method="GET")
        with urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8")

    def post_json(self, url, headers=None, payload=None):
        headers = dict(headers or {})
        headers.setdefault("Content-Type", "application/json")
        data = json.dumps(payload or {}).encode("utf-8")
        request = Request(url, data=data, headers=headers, method="POST")
        with urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}

    def delete_json(self, url, headers=None, params=None):
        headers = headers or {}
        params = params or {}
        if params:
            separator = "&" if "?" in url else "?"
            url = url + separator + urlencode(params)
        request = Request(url, headers=headers, method="DELETE")
        with urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}


class RateLimitError(Exception):
    def __init__(self, message: str, retry_after: float = None):
        super().__init__(message)
        self.retry_after = retry_after


class ProviderRateLimitPolicy:
    def __init__(self, provider: str):
        self.provider = provider.lower()

    def retry_after(self, headers, now: float = None):
        normalized = {
            str(key).lower(): str(value)
            for key, value in (headers or {}).items()
        }
        if "retry-after" in normalized:
            return _parse_float(normalized["retry-after"])

        for reset_header in self._reset_headers():
            if reset_header in normalized:
                reset_at = _parse_float(normalized[reset_header])
                if reset_at is None:
                    return None
                now = time.time() if now is None else now
                return max(0.0, reset_at - now)
        return None

    def raise_if_limited(self, headers, now: float = None) -> None:
        normalized = {
            str(key).lower(): str(value)
            for key, value in (headers or {}).items()
        }
        remaining = normalized.get("x-ratelimit-remaining") or normalized.get("x-rate-limit-remaining")
        if remaining == "0":
            retry_after = self.retry_after(normalized, now=now)
            raise RateLimitError("%s rate limit exhausted" % self.provider, retry_after=retry_after)

    def _reset_headers(self):
        if self.provider == "alpaca":
            return ("x-ratelimit-reset", "x-rate-limit-reset")
        if self.provider == "polygon":
            return ("x-rate-limit-reset", "x-ratelimit-reset")
        return ("x-ratelimit-reset", "x-rate-limit-reset")


class RetryingJsonTransport:
    def __init__(
        self,
        request_json=None,
        sleep=None,
        max_attempts: int = 3,
        backoff_seconds: float = 1.0,
        retryable_errors=(TimeoutError, ConnectionError),
    ):
        self.request_json = request_json or self._urllib_request_json
        self.sleep = sleep or time.sleep
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self.retryable_errors = retryable_errors

    def get_json(self, url, headers=None, params=None):
        headers = headers or {}
        params = params or {}
        attempt = 0
        while True:
            attempt += 1
            try:
                return self.request_json(url, headers, params)
            except RateLimitError as error:
                if attempt >= self.max_attempts:
                    raise
                retry_after = error.retry_after
                if retry_after is None:
                    retry_after = self.backoff_seconds * (2 ** (attempt - 1))
                self.sleep(retry_after)
            except self.retryable_errors:
                if attempt >= self.max_attempts:
                    raise
                self.sleep(self.backoff_seconds * (2 ** (attempt - 1)))

    def _urllib_request_json(self, url, headers, params):
        return UrllibJsonTransport().get_json(url, headers=headers, params=params)


def _parse_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
