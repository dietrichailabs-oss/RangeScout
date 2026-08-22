from __future__ import annotations

import json
import gzip
import time
import urllib.error
import urllib.request
import zlib
from typing import Callable


class FeedRequestError(RuntimeError):
    pass


class OfficialFeedClient:
    def __init__(self, user_agent: str, minimum_interval_seconds: float, *, clock: Callable[[], float] = time.monotonic, sleeper: Callable[[float], None] = time.sleep) -> None:
        if "@" not in user_agent:
            raise ValueError("Official-source User-Agent must include a contact email.")
        self.user_agent = user_agent
        self.minimum_interval_seconds = max(0.0, minimum_interval_seconds)
        self._clock, self._sleeper = clock, sleeper
        self._last_request: float | None = None

    def get(self, url: str, *, headers: dict[str, str] | None = None) -> bytes:
        now = self._clock()
        if self._last_request is not None:
            delay = self.minimum_interval_seconds - (now - self._last_request)
            if delay > 0:
                self._sleeper(delay)
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate", **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                value = response.read()
                content_encoding = str(response.headers.get("Content-Encoding", "")).lower()
                if content_encoding == "gzip":
                    value = gzip.decompress(value)
                elif content_encoding == "deflate":
                    value = zlib.decompress(value)
        except (urllib.error.URLError, TimeoutError, OSError):
            raise FeedRequestError("Official catalyst source is temporarily unavailable.") from None
        finally:
            self._last_request = self._clock()
        return value

    def get_json(self, url: str, *, headers: dict[str, str] | None = None) -> dict:
        try:
            value = json.loads(self.get(url, headers=headers).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise FeedRequestError("Official catalyst source returned an invalid response.") from None
        if not isinstance(value, dict):
            raise FeedRequestError("Official catalyst source returned an unexpected response.")
        return value
