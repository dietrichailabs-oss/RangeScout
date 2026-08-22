"""Conservative official SEC ticker-to-CIK resolution with a minimal local cache."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.catalysts.feeds.http import OfficialFeedClient


SEC_TICKER_MAPPING_URL = "https://www.sec.gov/files/company_tickers.json"
CACHE_MAX_AGE = timedelta(hours=24)


class SecSymbolResolver:
    def __init__(self, client: OfficialFeedClient, cache_path: Path) -> None:
        self.client = client
        self.cache_path = cache_path

    def resolve(self, symbols: set[str]) -> dict[str, str]:
        wanted = {value.strip().upper() for value in symbols if value.strip()}
        cached, fetched_at = self._load()
        missing = wanted - set(cached)
        stale = fetched_at is None or datetime.now(timezone.utc) - fetched_at > CACHE_MAX_AGE
        if missing and (stale or missing):
            payload = self.client.get_json(SEC_TICKER_MAPPING_URL)
            found: dict[str, str] = {}
            for row in payload.values():
                if not isinstance(row, dict):
                    continue
                symbol = str(row.get("ticker", "")).strip().upper()
                cik = str(row.get("cik_str", "")).strip()
                if symbol in wanted and cik.isdigit():
                    found[symbol] = cik
            cached.update(found)
            self._save({key: cached[key] for key in wanted if key in cached})
        return {key: cached[key] for key in wanted if key in cached}

    def _load(self) -> tuple[dict[str, str], datetime | None]:
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            fetched_at = datetime.fromisoformat(str(payload["fetched_at"]).replace("Z", "+00:00"))
            values = {str(k).upper(): str(v) for k, v in payload.get("symbols", {}).items() if str(v).isdigit()}
            return values, fetched_at
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return {}, None

    def _save(self, values: dict[str, str]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"fetched_at": datetime.now(timezone.utc).isoformat(), "symbols": dict(sorted(values.items()))}
        temporary = self.cache_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(self.cache_path)

