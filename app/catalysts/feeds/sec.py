from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from app.catalysts.entities import CatalystEvent
from app.catalysts.normalization import normalize_event


SEC_FORMS = frozenset({"8-K", "10-Q", "10-K", "S-3", "424B", "424B2", "424B3", "424B4", "424B5", "13D", "13G", "3", "4", "5", "144"})


def submissions_url(cik: str) -> str:
    digits = "".join(character for character in str(cik) if character.isdigit())
    if not digits:
        raise ValueError("CIK is required.")
    return f"https://data.sec.gov/submissions/CIK{int(digits):010d}.json"


def parse_submissions(payload: dict[str, Any], symbol: str, received_at: datetime) -> list[CatalystEvent]:
    recent = payload.get("filings", {}).get("recent", {})
    forms, accessions, dates = recent.get("form", []), recent.get("accessionNumber", []), recent.get("filingDate", [])
    company = str(payload.get("name", symbol))
    cik = str(payload.get("cik", ""))
    events: list[CatalystEvent] = []
    for form, accession, filed in zip(forms, accessions, dates):
        normalized_form = str(form).upper()
        if not any(normalized_form == allowed or normalized_form.startswith(allowed) for allowed in SEC_FORMS):
            continue
        published = datetime.fromisoformat(str(filed)).replace(tzinfo=timezone.utc)
        compact = str(accession).replace("-", "")
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{compact}/"
        base = normalize_event("SEC", url, published, f"{company} filed {form}", received_at=received_at, retention="metadata_only", metadata={"form": str(form), "cik": cik, "accession": str(accession)})
        events.append(replace(base, symbols=(symbol.upper(),), company_names=(company,), category="sec_filing", urgency="high"))
    return events
