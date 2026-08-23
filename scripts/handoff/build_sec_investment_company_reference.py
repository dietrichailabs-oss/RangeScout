"""Build the frozen SEC investment-company classification overlay.

This engineering tool queries official SEC company-submissions JSON for
candidate entities selected generically from the bundled company master. It
never classifies by a code-level ticker allowlist.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


_CANDIDATE = re.compile(
    r"\b(fund|trust|income|municipal|credit|opportunit|enhanced|dividend|strategy|portfolio)\b",
    re.IGNORECASE,
)
_CEF_FORMS = frozenset({"N-2", "N-2/A", "N-2ASR", "N-2MEF"})
_REGISTRATION_REPORTS = frozenset({"N-CEN", "N-CEN/A"})
_PORTFOLIO_REPORTS = frozenset({"N-CSR", "N-CSR/A", "N-CSRS", "N-Q", "N-Q/A", "NPORT-P", "NPORT-P/A"})


def fetch_json(url: str, user_agent: str, attempts: int = 4) -> dict[str, object]:
    for attempt in range(attempts):
        try:
            request = Request(url, headers={"User-Agent": user_agent, "Accept-Encoding": "identity"})
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except (HTTPError, URLError, TimeoutError):
            if attempt + 1 == attempts:
                raise
            time.sleep(1.0 + attempt * 2.0)
    raise AssertionError("unreachable")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("master", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--user-agent", required=True)
    parser.add_argument("--delay", type=float, default=0.12)
    args = parser.parse_args()
    with sqlite3.connect(args.master) as connection:
        rows = connection.execute(
            "SELECT canonical_symbol,security_name,cik FROM seed_instruments "
            "WHERE asset_class='stock' AND cik IS NOT NULL AND is_active=1 ORDER BY cik,canonical_symbol"
        ).fetchall()
    candidates: dict[str, list[str]] = {}
    for symbol, name, cik in rows:
        if _CANDIDATE.search(str(name or "")):
            candidates.setdefault(str(cik).zfill(10), []).append(str(symbol))
    verified = datetime.now(timezone.utc).isoformat()
    classifications: list[dict[str, object]] = []
    audit: list[dict[str, object]] = []
    for index, (cik, symbols) in enumerate(sorted(candidates.items()), 1):
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        payload = fetch_json(url, args.user_agent)
        forms = sorted(set(payload.get("filings", {}).get("recent", {}).get("form", ())))
        # Exchange-listed stock plus recurring SEC investment-company reports is authoritative even when N-2 aged out of the recent window.
        is_cef = bool(_REGISTRATION_REPORTS.intersection(forms) and _PORTFOLIO_REPORTS.intersection(forms))
        evidence = sorted((_CEF_FORMS | _REGISTRATION_REPORTS | _PORTFOLIO_REPORTS).intersection(forms))
        audit.append({"cik": cik, "entity_name": payload.get("name"), "symbols": symbols,
                      "is_closed_end_fund": is_cef, "evidence_forms": evidence, "source_url": url})
        if is_cef:
            classifications.append({
                "cik": cik, "asset_class": "closed_end_fund", "instrument_subtype": "closed_end_fund",
                "source_id": "sec_company_submissions_investment_company_forms", "source_url": url,
                "verified_at_utc": verified, "evidence_forms": evidence,
            })
        if index != len(candidates):
            time.sleep(max(0.1, args.delay))
    result = {
        "schema": "rangescout.sec-investment-company-classifications.v1",
        "generated_at_utc": verified,
        "source_policy": "Official SEC company-submissions JSON; CIK identity; N-2 plus recurring investment-company report form.",
        "candidate_rule": _CANDIDATE.pattern,
        "candidates_checked": len(candidates),
        "classifications": classifications,
        "audit": audit,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output), "candidates_checked": len(candidates),
        "closed_end_fund_entities": len(classifications),
        "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
