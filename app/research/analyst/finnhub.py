"""Official Finnhub recommendation-trends adapter."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

from app.market_data.contracts import FabricProviderError, RateLimited
from app.market_data.providers.http import JsonTransport
from app.research.analyst.models import AnalystProviderError, AnalystState


FINNHUB_RECOMMENDATION_URL = "https://finnhub.io/api/v1/stock/recommendation"


class FinnhubRecommendationClient:
    provider_id = "finnhub"
    dataset = "recommendation_trends"

    def __init__(self, transport: JsonTransport | None = None) -> None:
        self.transport = transport or JsonTransport()

    def fetch(self, symbol: str, api_key: str) -> tuple[dict[str, Any], str | None]:
        try:
            payload = self.transport.get_json(
                FINNHUB_RECOMMENDATION_URL + "?" + urlencode({"symbol": symbol, "token": api_key})
            )
        except RateLimited:
            raise AnalystProviderError(AnalystState.RATE_LIMITED, "Finnhub recommendation data is rate limited.") from None
        except FabricProviderError as exc:
            text = str(exc).lower()
            state = AnalystState.UNAUTHORIZED if "401" in text else AnalystState.ENTITLEMENT_UNAVAILABLE if "403" in text else AnalystState.UNAVAILABLE
            raise AnalystProviderError(state, "Finnhub recommendation data is unavailable.") from None
        if isinstance(payload, dict):
            message = str(payload.get("error") or payload.get("message") or "").lower()
            if any(word in message for word in ("permission", "entitle", "access", "premium", "plan")):
                raise AnalystProviderError(AnalystState.ENTITLEMENT_UNAVAILABLE, "Finnhub recommendations are not available on the configured plan.")
            if any(word in message for word in ("api key", "token", "unauthorized", "invalid")):
                raise AnalystProviderError(AnalystState.UNAUTHORIZED, "The configured Finnhub key was not authorized.")
            raise AnalystProviderError(AnalystState.UNAVAILABLE, "Finnhub recommendation data is unavailable.")
        if not isinstance(payload, list) or not payload:
            raise AnalystProviderError(AnalystState.ENTITLEMENT_UNAVAILABLE, "Finnhub recommendations are not available on the configured plan.")
        row = next((item for item in payload if isinstance(item, dict)), None)
        if row is None:
            raise AnalystProviderError(AnalystState.UNAVAILABLE, "Finnhub returned malformed recommendation data.")
        values: dict[str, Any] = {}
        for output, source in (
            ("Strong Buy", "strongBuy"), ("Buy", "buy"), ("Hold", "hold"),
            ("Sell", "sell"), ("Strong Sell", "strongSell"),
        ):
            raw = row.get(source)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw >= 0:
                values[output] = int(raw)
        if not values:
            raise AnalystProviderError(AnalystState.UNAVAILABLE, "Finnhub returned no recommendation counts.")
        values["Total Analysts"] = sum(int(values.get(name, 0)) for name in ("Strong Buy", "Buy", "Hold", "Sell", "Strong Sell"))
        period = str(row.get("period") or "").strip() or None
        if period:
            values["Recommendation Period"] = period
        values["Retrieved At"] = datetime.now(timezone.utc).isoformat()
        return values, period
