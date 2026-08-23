"""Fund-aware Research using official SEC investment-company submissions metadata."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.research.models import Availability, CompanyProfile, ResearchSnapshot, ResearchValue


_FUND_FORM_PREFIXES = (
    "N-PORT", "N-CSR", "N-CSRS", "N-CSRA", "N-CEN", "N-2", "N-14", "24F-2NT",
)


class FundResearchService:
    """Build truthful fund/CEF Research without running corporate companyfacts calculations."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def load(self, symbol: str, generation: int = 0, period_mode: str = "annual") -> ResearchSnapshot:  # noqa: ARG002
        normalized = symbol.strip().upper()
        now = datetime.now(timezone.utc)
        company = self.client.company_map().get(normalized)
        if company is None:
            profile = CompanyProfile(
                normalized, None, None, None, None, None, source="SEC investment-company submissions"
            )
            missing = ResearchValue(
                None, "SEC investment-company submissions", availability=Availability.LOOKUP_FAILED,
                selection_reason="No official SEC ticker-to-CIK match was found for this fund.",
            )
            return ResearchSnapshot(
                normalized, generation, profile, {"Overview": {"Data state": missing}}, now,
                ("Fund identity lookup was not found in the approved SEC source.",),
            )

        cik = str(company["cik"])
        submissions = self.client.submissions(cik)
        profile = CompanyProfile(
            normalized, cik, str(submissions.get("name") or company.get("name") or "") or None,
            _first_string(submissions.get("exchanges")), str(submissions.get("sic") or "") or None,
            str(submissions.get("sicDescription") or "") or None,
            source="SEC investment-company submissions",
        )
        filings = _fund_filings(submissions)
        latest = filings[0] if filings else None
        filing_state = Availability.AVAILABLE if latest else Availability.UNAVAILABLE
        filing_reason = (
            "Latest recognized investment-company filing from SEC submissions."
            if latest else "No recognized N-PORT/N-CSR/N-CEN/N-2 fund filing was present in recent SEC submissions."
        )
        overview = {
            "Instrument structure": ResearchValue(
                "Fund / closed-end fund", "RangeScout canonical instrument catalog",
                selection_reason="Routed by canonical asset class and official listing metadata.",
            ),
            "SEC registrant": ResearchValue(profile.name, "SEC investment-company submissions"),
            "Fund filing count": ResearchValue(str(len(filings)), "SEC investment-company submissions"),
            "Latest fund filing": ResearchValue(
                latest["form"] if latest else None, "SEC investment-company submissions",
                period=latest["filing_date"] if latest else None, availability=filing_state,
                selection_reason=filing_reason,
            ),
        }
        unsupported = ResearchValue(
            None, "Configured fund-data providers", availability=Availability.PROVIDER_NOT_SUPPORTED,
            selection_reason="NAV, premium/discount, leverage, expenses and holdings are not supplied by a configured approved provider.",
        )
        unavailable = ResearchValue(
            None, "SEC investment-company submissions", availability=Availability.UNAVAILABLE,
            selection_reason="Market-price performance requires eligible quote/history data for this canonical instrument.",
        )
        filing_value = ResearchValue(
            latest["form"] if latest else None, "SEC investment-company submissions",
            period=latest["filing_date"] if latest else None, availability=filing_state,
            selection_reason=filing_reason,
        )
        sections = {
            "Overview": overview,
            "Financials": {
                "NAV / premium-discount": unsupported,
                "Distributions / leverage / expenses": unsupported,
                "Portfolio holdings / exposure": unsupported,
            },
            "Performance": {"Fund performance state": unavailable},
            "Catalysts & News": {"Latest official fund filing": filing_value},
        }
        warning = (
            "Fund Research uses official SEC investment-company submissions; unsupported fund metrics were not fabricated."
        )
        return ResearchSnapshot(normalized, generation, profile, sections, now, (warning,))


def _fund_filings(payload: dict[str, Any]) -> list[dict[str, str]]:
    recent = payload.get("filings", {}).get("recent", {})
    if not isinstance(recent, dict):
        return []
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    if not all(isinstance(values, list) for values in (forms, dates, accessions)):
        return []
    rows: list[dict[str, str]] = []
    for form, filed, accession in zip(forms, dates, accessions):
        normalized = str(form or "").upper()
        if not any(normalized == prefix or normalized.startswith(prefix + "/") for prefix in _FUND_FORM_PREFIXES):
            continue
        rows.append({"form": normalized, "filing_date": str(filed or ""), "accession": str(accession or "")})
    return sorted(rows, key=lambda item: (item["filing_date"], item["accession"]), reverse=True)


def _first_string(value: object) -> str | None:
    if isinstance(value, list):
        return next((str(item) for item in value if str(item).strip()), None)
    return None
