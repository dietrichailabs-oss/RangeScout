from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from app.catalysts.entities import CatalystEvent
from app.catalysts.normalization import normalize_event


API_URL = "https://api.congress.gov/v3/bill?format=json&limit=100&sort=updateDate+desc"
PROGRESSION = ("introduced", "house passed", "senate passed", "final passage", "sent to president", "signed", "vetoed")


def authentication_headers(api_key: str) -> dict[str, str]:
    if not api_key.strip():
        raise ValueError("Congress.gov API key is required.")
    return {"X-Api-Key": api_key.strip()}


def parse_bills(payload: dict[str, Any], received_at: datetime) -> list[CatalystEvent]:
    events: list[CatalystEvent] = []
    for bill in payload.get("bills", []):
        if not isinstance(bill, dict): continue
        title = str(bill.get("title") or bill.get("number") or "Congressional action")
        url = str(bill.get("url") or "https://www.congress.gov/")
        updated = _date(bill.get("updateDate"), received_at)
        latest = bill.get("latestAction") if isinstance(bill.get("latestAction"), dict) else {}
        action = str(latest.get("text", "introduced"))
        stage = next((stage for stage in reversed(PROGRESSION) if stage in action.lower()), "introduced")
        event = normalize_event("Congress.gov", url, updated, title, received_at=received_at, summary=action, retention="summary_allowed", metadata={"stage": stage})
        events.append(replace(event, category="legislation", urgency="high" if stage in {"signed", "vetoed", "final passage"} else "normal"))
    return events


def _date(value: Any, fallback: datetime) -> datetime:
    try: return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError): return fallback
