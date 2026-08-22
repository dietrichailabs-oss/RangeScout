"""Qt WebSocket transport; all network work is event-driven and nonblocking."""

from __future__ import annotations

import json
from typing import Callable

from PySide6.QtCore import QUrl
from PySide6.QtWebSockets import QWebSocket


class QtWebSocketTransport:
    def __init__(self, url_factory: Callable[[], str], provider: str) -> None:
        self._url_factory = url_factory
        self._provider = provider
        self._socket = QWebSocket()
        self._opened: Callable[[], None] = lambda: None
        self._message: Callable[[str], None] = lambda value: None
        self._closed: Callable[[], None] = lambda: None
        self._failed: Callable[[str], None] = lambda value: None
        self._socket.connected.connect(lambda: self._opened())
        self._socket.textMessageReceived.connect(lambda value: self._message(value))
        self._socket.disconnected.connect(lambda: self._closed())
        self._socket.errorOccurred.connect(lambda error: self._failed("Provider WebSocket connection failed."))

    def set_callbacks(self, *, opened: Callable[[], None], message: Callable[[str], None], closed: Callable[[], None], failed: Callable[[str], None]) -> None:
        self._opened, self._message, self._closed, self._failed = opened, message, closed, failed

    def open(self) -> None:
        # URL may contain a BYO token. It is deliberately never retained in logs/errors.
        self._socket.open(QUrl(self._url_factory()))

    def send(self, payload: str) -> None:
        try:
            message = json.loads(payload)
        except json.JSONDecodeError:
            self._socket.sendTextMessage(payload)
            return
        marker = message.get("_rangescout") if isinstance(message, dict) else None
        if marker in {"finnhub_subscribe", "finnhub_unsubscribe"}:
            action = "subscribe" if marker.endswith("subscribe") and "unsubscribe" not in marker else "unsubscribe"
            for symbol in message.get("symbols", []):
                self._socket.sendTextMessage(json.dumps({"type": action, "symbol": symbol}))
            return
        self._socket.sendTextMessage(payload)

    def close(self) -> None:
        self._socket.close()
