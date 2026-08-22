"""Provider-result validation: fastest valid beats fastest raw."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from app.market_calendar.us_equities import NEW_YORK, _is_session_day, market_session_status
from app.market_data.contracts import Capability, DelayClass, FabricRequest, FabricResult, FreshnessPolicy


class ResultValidationError(ValueError):
    def __init__(self, message: str, kind: str = "validation") -> None:
        super().__init__(message)
        self.kind = kind


def _numeric_invariants(payload: Any) -> None:
    if not isinstance(payload, dict):
        return
    for field in ("price", "last", "open", "high", "low", "close"):
        if field in payload and payload[field] is not None:
            try:
                value = Decimal(str(payload[field]))
            except (InvalidOperation, ValueError) as exc:
                raise ResultValidationError(f"Invalid numeric field: {field}", "parse") from exc
            if not value.is_finite() or value < 0:
                raise ResultValidationError(f"Invalid numeric invariant: {field}")
    if all(field in payload for field in ("low", "high")):
        if Decimal(str(payload["low"])) > Decimal(str(payload["high"])):
            raise ResultValidationError("Low exceeds high.")


def validate_result(request: FabricRequest, result: FabricResult) -> None:
    if result.request_id != request.request_id:
        raise ResultValidationError("Request identity mismatch.")
    if result.canonical_instrument_id != request.canonical_instrument_id:
        raise ResultValidationError("Instrument identity mismatch.")
    if result.canonical_symbol.upper() != request.canonical_symbol.upper():
        raise ResultValidationError("Symbol identity mismatch.")
    if result.capability != request.capability:
        raise ResultValidationError("Capability mismatch.")
    if request.venue and result.venue and request.venue.upper() != result.venue.upper():
        raise ResultValidationError("Venue mapping mismatch.")
    if result.payload is None:
        raise ResultValidationError("Required payload is missing.", "parse")
    if not _accepts_freshness(request, result):
        raise ResultValidationError("Provider result is stale or has an incompatible delay class.", "stale")
    _numeric_invariants(result.payload)


def _accepts_freshness(request: FabricRequest, result: FabricResult) -> bool:
    if request.freshness is not None:
        return request.freshness.accepts(result.provider_timestamp, result.delay_class, result.received_at)
    source = _as_utc(result.provider_timestamp)
    received = _as_utc(result.received_at)
    if source > received + timedelta(minutes=5):
        return False
    if request.capability in {Capability.HISTORICAL, Capability.CANDLES}:
        if _is_daily_interval(request.interval):
            return _daily_history_is_current(source, received)
        policy = FreshnessPolicy(_intraday_max_age(request.interval), allow_delayed=True)
        return policy.accepts(source, result.delay_class, received)
    if request.capability == Capability.INTRADAY:
        return FreshnessPolicy(_intraday_max_age(request.interval), allow_delayed=True).accepts(
            source, result.delay_class, received
        )
    if request.capability == Capability.MACRO_SERIES:
        return FreshnessPolicy(timedelta(days=45), allow_delayed=True, allow_end_of_day=True).accepts(
            source, result.delay_class, received
        )
    if result.delay_class == DelayClass.END_OF_DAY:
        return _daily_history_is_current(source, received)
    quote_policy = FreshnessPolicy(timedelta(minutes=20), allow_delayed=True)
    if quote_policy.accepts(source, result.delay_class, received):
        return True
    if request.capability == Capability.QUOTE and not market_session_status(received).is_open:
        return _daily_history_is_current(source, received)
    return False


def _daily_history_is_current(source: datetime, received: datetime) -> bool:
    # Daily providers commonly encode a trading date as UTC midnight rather
    # than a market-close instant, so preserve the provider's stated date.
    source_date = source.date()
    cursor = received.astimezone(NEW_YORK).date() - timedelta(days=1)
    while not _is_session_day(cursor):
        cursor -= timedelta(days=1)
    return source_date >= cursor


def _is_daily_interval(interval: str | None) -> bool:
    if interval is None:
        return True
    return interval.strip().lower() in {"1d", "1day", "day", "daily", "86400", "1440"}


def _intraday_max_age(interval: str | None) -> timedelta:
    value = (interval or "5m").strip().lower()
    multiplier = 1
    if value.endswith("m") and value[:-1].isdigit():
        multiplier = int(value[:-1]) * 60
    elif value.endswith("s") and value[:-1].isdigit():
        multiplier = int(value[:-1])
    elif value.isdigit():
        multiplier = int(value)
    return timedelta(seconds=max(120, multiplier * 3))


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
