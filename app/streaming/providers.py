"""Finnhub and Alpaca streaming wire protocols."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.security.credentials import ProviderCredentials
from app.streaming.events import TradeEvent


def finnhub_authentication(credentials: ProviderCredentials) -> str | None:
    del credentials
    return None


def finnhub_url(credentials: ProviderCredentials) -> str:
    # The token is required in the WebSocket URL by Finnhub. Callers must never log this URL.
    return "wss://ws.finnhub.io?token=" + credentials.values["api_key"]


def alpaca_url(credentials: ProviderCredentials) -> str:
    del credentials
    return "wss://stream.data.alpaca.markets/v2/iex"


def alpaca_authentication(credentials: ProviderCredentials) -> str:
    return json.dumps({"action": "auth", "key": credentials.values["key_id"], "secret": credentials.values["secret_key"]})


def finnhub_subscribe(symbols: tuple[str, ...]) -> str | None:
    # Finnhub accepts one subscription command per symbol; the connection sends
    # a compact internal batch that Qt transport expands without logging.
    return json.dumps({"_rangescout": "finnhub_subscribe", "symbols": list(symbols)}) if symbols else None


def finnhub_unsubscribe(symbols: tuple[str, ...]) -> str | None:
    return json.dumps({"_rangescout": "finnhub_unsubscribe", "symbols": list(symbols)}) if symbols else None


def alpaca_subscribe(symbols: tuple[str, ...]) -> str | None:
    return json.dumps({"action": "subscribe", "trades": list(symbols)}) if symbols else None


def alpaca_unsubscribe(symbols: tuple[str, ...]) -> str | None:
    return json.dumps({"action": "unsubscribe", "trades": list(symbols)}) if symbols else None


def decode_finnhub(payload: str, received_at: datetime) -> tuple[list[TradeEvent], bool]:
    message = _object(payload)
    if message.get("type") == "ping":
        return [], False
    events: list[TradeEvent] = []
    for raw in message.get("data", []) if isinstance(message.get("data"), list) else []:
        if not isinstance(raw, dict):
            continue
        event = _trade("finnhub", raw.get("s"), raw.get("p"), raw.get("v"), raw.get("t"), received_at, raw.get("c"))
        if event:
            events.append(event)
    return events, False


def decode_alpaca(payload: str, received_at: datetime) -> tuple[list[TradeEvent], bool]:
    decoded = json.loads(payload)
    messages = decoded if isinstance(decoded, list) else [decoded]
    events: list[TradeEvent] = []
    authenticated = False
    for raw in messages:
        if not isinstance(raw, dict):
            continue
        authenticated = authenticated or (raw.get("T") == "success" and raw.get("msg") == "authenticated")
        if raw.get("T") != "t":
            continue
        timestamp = _iso_timestamp(raw.get("t"))
        event = _trade("alpaca", raw.get("S"), raw.get("p"), raw.get("s"), timestamp, received_at, raw.get("c"), raw.get("i"))
        if event:
            events.append(event)
    return events, authenticated


def _object(payload: str) -> dict[str, Any]:
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("Expected object.")
    return value


def _trade(provider: str, symbol: Any, price: Any, size: Any, timestamp: Any, received_at: datetime, conditions: Any = None, event_id: Any = None) -> TradeEvent | None:
    try:
        parsed_price = Decimal(str(price))
        parsed_size = Decimal(str(size))
        parsed_timestamp = datetime.fromtimestamp(float(timestamp) / 1000.0, tz=received_at.tzinfo) if isinstance(timestamp, (int, float)) else timestamp
        if not isinstance(symbol, str) or not isinstance(parsed_timestamp, datetime) or parsed_price <= 0 or parsed_size < 0:
            return None
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        return None
    return TradeEvent(provider, symbol.upper(), parsed_price, parsed_size, parsed_timestamp, str(event_id) if event_id is not None else None, tuple(map(str, conditions or ())), received_at)


def _iso_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
