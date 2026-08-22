"""Quota-aware composition of cached Finnhub and Alpha Vantage analyst data."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import RLock
from typing import Any, Callable

from app.research.analyst.alpha_vantage import AlphaVantageEarningsEstimatesClient
from app.research.analyst.cache import AnalystCacheEntry, AnalystCacheRepository
from app.research.analyst.finnhub import FinnhubRecommendationClient
from app.research.analyst.models import AnalystProviderError, AnalystResult, AnalystState
from app.research.models import Availability, ResearchValue
from app.security.credentials import CredentialStore


_TTL = {
    "finnhub": timedelta(hours=6),
    "alpha_vantage": timedelta(hours=24),
}
_DATASET = {
    "finnhub": "recommendation_trends",
    "alpha_vantage": "earnings_estimates",
}
_DISPLAY = {
    "finnhub": "Finnhub recommendation trends",
    "alpha_vantage": "Alpha Vantage EARNINGS_ESTIMATES",
}


class AnalystService:
    def __init__(
        self,
        database_path: Path | str,
        credential_store: CredentialStore,
        *,
        finnhub: FinnhubRecommendationClient | None = None,
        alpha_vantage: AlphaVantageEarningsEstimatesClient | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.cache = AnalystCacheRepository(database_path)
        self.credential_store = credential_store
        self.clients = {
            "finnhub": finnhub or FinnhubRecommendationClient(),
            "alpha_vantage": alpha_vantage or AlphaVantageEarningsEstimatesClient(),
        }
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._inflight: set[tuple[str, str, str]] = set()
        self._lock = RLock()

    def load(self, symbol: str, generation: int = 0, *, force: bool = False) -> AnalystResult:
        normalized = str(symbol).strip().upper()
        values: dict[str, ResearchValue] = {}
        states: dict[str, AnalystState] = {}
        messages: list[str] = []
        configured = 0
        for provider_id in ("finnhub", "alpha_vantage"):
            try:
                credentials = self.credential_store.load(provider_id)
            except Exception:
                credentials = None
                states[provider_id] = AnalystState.UNAVAILABLE
                messages.append(f"{_DISPLAY[provider_id]}: secure credential storage is unavailable.")
                continue
            if credentials is None:
                states[provider_id] = AnalystState.NOT_CONFIGURED
                continue
            configured += 1
            provider_values, state, message = self._load_provider(
                provider_id, normalized, credentials.values["api_key"], force=force
            )
            values.update(provider_values)
            states[provider_id] = state
            if message:
                messages.append(message)
        if configured == 0 and all(state is AnalystState.NOT_CONFIGURED for state in states.values()):
            messages.append(
                "Analyst data not configured — add a free Finnhub and/or Alpha Vantage API key in Settings."
            )
        return AnalystResult(normalized, generation, values, states, self.now_fn(), tuple(messages))

    def invalidate_provider(self, provider_id: str) -> None:
        if provider_id in self.clients:
            self.cache.clear(provider_id)

    def _load_provider(
        self, provider_id: str, symbol: str, api_key: str, *, force: bool
    ) -> tuple[dict[str, ResearchValue], AnalystState, str | None]:
        dataset = _DATASET[provider_id]
        entry = self.cache.get(provider_id, symbol, dataset)
        if entry is not None and entry.expires_at_utc > self.now_fn() and not force:
            return self._from_cache(entry, stale=False)
        key = (provider_id, symbol, dataset)
        with self._lock:
            if key in self._inflight:
                if entry is not None:
                    return self._from_cache(entry, stale=True)
                return {}, AnalystState.UNAVAILABLE, f"{_DISPLAY[provider_id]}: request already in progress."
            self._inflight.add(key)
        try:
            try:
                payload, provider_timestamp = self.clients[provider_id].fetch(symbol, api_key)
            except AnalystProviderError as exc:
                if entry is not None and entry.payload and not entry.payload.get("_message"):
                    cached_values, _, _ = self._from_cache(entry, stale=True)
                    return cached_values, AnalystState.STALE_CACHED, f"{_DISPLAY[provider_id]}: {exc}; showing stale cached data."
                now = self.now_fn()
                safe = {"_message": str(exc), "_state": exc.state.value}
                self.cache.put(
                    provider_id, symbol, dataset, safe,
                    retrieved_at_utc=now,
                    expires_at_utc=now + min(_TTL[provider_id], timedelta(minutes=30)),
                    status=exc.state.value,
                )
                return {}, exc.state, str(exc)
            now = self.now_fn()
            self.cache.put(
                provider_id, symbol, dataset, payload,
                retrieved_at_utc=now,
                expires_at_utc=now + _TTL[provider_id],
                provider_timestamp_utc=provider_timestamp,
            )
            return self._values(provider_id, payload, now, AnalystState.FRESH), AnalystState.FRESH, None
        finally:
            with self._lock:
                self._inflight.discard(key)

    def _from_cache(
        self, entry: AnalystCacheEntry, *, stale: bool
    ) -> tuple[dict[str, ResearchValue], AnalystState, str | None]:
        if entry.status != "ok" or entry.payload.get("_message"):
            raw_state = str(entry.payload.get("_state") or entry.status)
            try:
                state = AnalystState(raw_state)
            except ValueError:
                state = AnalystState.UNAVAILABLE
            return {}, state, str(entry.payload.get("_message") or f"{_DISPLAY[entry.provider_id]} is unavailable.")
        state = AnalystState.STALE_CACHED if stale or entry.stale else AnalystState.CACHED
        message = f"{_DISPLAY[entry.provider_id]}: showing {'stale ' if state is AnalystState.STALE_CACHED else ''}cached data."
        return self._values(entry.provider_id, entry.payload, entry.retrieved_at_utc, state), state, message

    @staticmethod
    def _values(
        provider_id: str, payload: dict[str, Any], retrieved: datetime, state: AnalystState
    ) -> dict[str, ResearchValue]:
        source = _DISPLAY[provider_id]
        if state in {AnalystState.CACHED, AnalystState.STALE_CACHED}:
            source += " (cached)"
        availability = Availability.STALE if state is AnalystState.STALE_CACHED else Availability.AVAILABLE
        output: dict[str, ResearchValue] = {}
        for metric, raw in payload.items():
            if str(metric).startswith("_") or metric == "Retrieved At":
                continue
            value: Decimal | str
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                value = Decimal(str(raw))
            elif isinstance(raw, str) and metric not in {"Recommendation Period"} and not metric.endswith("Period"):
                try:
                    value = Decimal(raw)
                except InvalidOperation:
                    value = raw
            else:
                value = str(raw)
            units = "analysts" if "Analyst" in metric or metric in {"Strong Buy", "Buy", "Hold", "Sell", "Strong Sell"} else None
            if "Revenue Estimate" in metric:
                units = "USD"
            if "EPS Estimate" in metric:
                units = "USD/shares"
            output[metric] = ResearchValue(
                value,
                source,
                period=str(payload.get("Recommendation Period") or payload.get(metric.replace("Estimate", "Period")) or "") or None,
                units=units,
                retrieved_at=retrieved,
                availability=availability,
                selection_reason=f"Provider-supplied analyst data; cache state {state.value}.",
            )
        return output
