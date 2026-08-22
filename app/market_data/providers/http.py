"""Bounded JSON transport shared by official public provider adapters."""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.market_data.contracts import FabricProviderError, RateLimited


class JsonTransport:
    def __init__(self, timeout_seconds: float = 6.0, max_response_bytes: int = 8 * 1024 * 1024) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes

    def get_json(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        *,
        timeout_seconds: float | None = None,
    ):
        safe_headers = {"User-Agent": "RangeScout/1.3 (Dietrich AI Labs; market-data client)"}
        safe_headers.update(headers or {})
        request = Request(url, headers=safe_headers, method="GET")
        try:
            with urlopen(
                request, timeout=self.timeout_seconds if timeout_seconds is None else timeout_seconds
            ) as response:
                length = response.headers.get("Content-Length")
                if length and int(length) > self.max_response_bytes:
                    raise FabricProviderError("Provider response exceeds the configured safety limit.")
                payload = response.read(self.max_response_bytes + 1)
                if len(payload) > self.max_response_bytes:
                    raise FabricProviderError("Provider response exceeds the configured safety limit.")
        except HTTPError as exc:
            if exc.code == 429:
                raw_retry = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    retry = float(raw_retry) if raw_retry else None
                except ValueError:
                    retry = None
                raise RateLimited(retry) from None
            raise FabricProviderError(f"Provider HTTP request failed with status {exc.code}.") from None
        except (URLError, TimeoutError, OSError):
            raise FabricProviderError("Provider network request failed.") from None
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise FabricProviderError("Provider returned malformed JSON.") from None
