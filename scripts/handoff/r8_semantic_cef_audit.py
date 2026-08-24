"""Audit every authoritative CEF CIK against independent security/issuer semantics."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.company_data.instrument_intelligence import InstrumentReferenceSeeder, InstrumentResolver
from app.company_data.master import provision_company_master
from app.historical_store.repository import HistoricalStore
from app.research.routing import ResearchRoute, plan_research


COMMON_MARKER = re.compile(r"\bcommon\s+(?:stock|shares?|units?)(?:\s+of\s+beneficial\s+interest)?\b", re.I)
PREFERRED_MARKER = re.compile(
    r"(?:\bterm\s+preferred\b|\bseries\s+[a-z0-9-]+\s+(?:term\s+)?preferred\b|"
    r"\bpreferred\s+(?:stock|shares?)\b(?=.*(?:\bseries\b|\bdue\b|\d(?:\.\d+)?%))|"
    r"\bdepositary\s+shares?.*\bpreferred\b)", re.I,
)
ALTERNATE_MARKERS = re.compile(r"\b(warrant|right|unit|note|depositary share)", re.I)


def independent_decision(name: str) -> tuple[str, str]:
    if COMMON_MARKER.search(name):
        return "primary_common", "explicit common-share marker in listed security name"
    if PREFERRED_MARKER.search(name):
        return "preferred_security", "explicit preferred series/rate/term marker in listed security name"
    alternate = ALTERNATE_MARKERS.search(name)
    if alternate:
        return "alternate_security", f"explicit {alternate.group(1).lower()} marker in listed security name"
    return "primary_common", "authoritative CEF issuer with no alternate-security marker"


def build_database() -> Path:
    database = Path(tempfile.mkdtemp(prefix="rangescout-r8-cef-audit-")) / "history.sqlite"
    with HistoricalStore(database):
        pass
    provision_company_master(database)
    InstrumentReferenceSeeder(database).apply()
    return database


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    database = build_database()
    resource_path = Path("resources/RangeScout_Instrument_Classifications.json")
    payload = json.loads(resource_path.read_text(encoding="utf-8"))
    records = sorted(payload["classifications"], key=lambda row: str(row["cik"]).zfill(10))
    resolver = InstrumentResolver(database)
    cik_records: list[dict[str, object]] = []
    rows_out: list[dict[str, object]] = []
    failures: list[dict[str, object]] = []
    role_counts: Counter[str] = Counter()
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        for record in records:
            cik = str(record["cik"]).zfill(10)
            db_rows = connection.execute(
                """SELECT instrument_id,canonical_symbol,security_name,security_type,asset_class,
                          instrument_subtype,issuer_entity_type,security_role,cik
                   FROM rs_instruments WHERE cik=? AND is_active=1 ORDER BY canonical_symbol,instrument_id""",
                (cik,),
            ).fetchall()
            cik_record = {
                "cik": cik,
                "official_asset_class": record["asset_class"],
                "official_classification_source": record["source_id"],
                "official_source_url": record["source_url"],
                "official_evidence_forms": record.get("evidence_forms", []),
                "active_security_count": len(db_rows),
                "active_symbols": [str(row["canonical_symbol"]) for row in db_rows],
            }
            cik_records.append(cik_record)
            for row in db_rows:
                symbol = str(row["canonical_symbol"])
                name = str(row["security_name"] or symbol)
                expected_role, rationale = independent_decision(name)
                role_counts[expected_role] += 1
                expected_asset = "closed_end_fund" if expected_role == "primary_common" else (
                    "preferred" if expected_role == "preferred_security" else str(row["asset_class"])
                )
                match = resolver.by_id(int(row["instrument_id"]))
                plan = plan_research(
                    match.asset_class, match.subtype, match.issuer_type, match.security_role
                ) if match else None
                row_failures: list[str] = []
                if match is None:
                    row_failures.append("canonical_identity")
                else:
                    if match.symbol != symbol:
                        row_failures.append("canonical_symbol")
                    if match.asset_class != expected_asset:
                        row_failures.append("normalized_security_type")
                    if match.issuer_type != "closed_end_fund":
                        row_failures.append("issuer_entity_type")
                    if match.security_role != expected_role:
                        row_failures.append("security_role")
                    if plan is None or plan.route is not ResearchRoute.FUND:
                        row_failures.append("research_route")
                    if plan is not None and plan.analyst_applicable:
                        row_failures.append("ordinary_corporate_analyst_route")
                output = {
                    "canonical_symbol": symbol,
                    "security_name": name,
                    "raw_listing_security_type": str(row["security_type"] or ""),
                    "normalized_security_type": match.asset_class if match else None,
                    "issuer_entity_type": match.issuer_type if match else None,
                    "security_role": match.security_role if match else None,
                    "cik": cik,
                    "authoritative_cef_status": True,
                    "official_classification_source": record["source_id"],
                    "official_source_url": record["source_url"],
                    "primary_common_vs_series_decision": expected_role,
                    "research_route": plan.route.value if plan else None,
                    "research_analyst_applicable": plan.analyst_applicable if plan else None,
                    "decision_rationale": rationale,
                    "failures": row_failures,
                    "result": "PASS" if not row_failures else "FAIL",
                }
                rows_out.append(output)
                if row_failures:
                    failures.append(output)
    summary = {
        "official_cef_ciks": len(records),
        "ciks_with_active_securities": sum(item["active_security_count"] > 0 for item in cik_records),
        "active_securities": len(rows_out),
        "primary_common": role_counts["primary_common"],
        "preferred_security": role_counts["preferred_security"],
        "alternate_security": role_counts["alternate_security"],
        "ordinary_corporate_research_misroutes": sum(
            row["research_route"] == "corporate" for row in rows_out
        ),
        "failures": len(failures),
    }
    result = {
        "schema": "rangescout.r8-authoritative-cef-semantic-audit.v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_database": str(database),
        "classification_resource": str(resource_path),
        "summary": summary,
        "ciks": cik_records,
        "rows": rows_out,
        "failed_rows": failures,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if (
        summary["official_cef_ciks"] == 355
        and summary["ordinary_corporate_research_misroutes"] == 0
        and summary["failures"] == 0
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())