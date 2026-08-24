"""Shared, evidence-first classification for official listing records."""

from __future__ import annotations

from dataclasses import dataclass
import re

_COMMON = re.compile(r"\b(?:COMMON\s+(?:STOCK|SHARES?)|ORDINARY\s+SHARES?)\b", re.I)
_ETN = re.compile(r"\b(?:ETN|EXCHANGE[- ]TRADED\s+NOTES?)\b", re.I)
_ETF = re.compile(r"\b(?:ETF|EXCHANGE[- ]TRADED\s+FUNDS?)\b", re.I)
_WARRANT = re.compile(r"\bWARRANTS?\b", re.I)
_RIGHT = re.compile(r"\b(?:SUBSCRIPTION\s+)?RIGHTS?\b", re.I)
_UNIT = re.compile(r"\bUNITS?\b", re.I)
_UNIT_SECURITY = re.compile(r"\bUNITS?\s*,?\s+(?:EACH|CONSISTING|COMPRISED)\b|\bCOMMON\s+UNITS?\b", re.I)
_PREFERRED = re.compile(
    r"(?:\bTERM\s+PREFERRED\b|\bSERIES\s+[A-Z0-9-]+\s+(?:TERM\s+)?PREFERRED\b|"
    r"\bPREFERRED\s+(?:STOCK|SHARES?|SECURIT(?:Y|IES))\b|\bPFD\b)", re.I,
)
_DEPOSITARY = re.compile(r"\b(?:AMERICAN\s+)?DEPOSITARY\s+(?:SHARES?|RECEIPTS?)\b|\bADR\b", re.I)


@dataclass(frozen=True, slots=True)
class SecurityClassification:
    asset_class: str
    security_type: str


def has_explicit_common_marker(name: object) -> bool:
    return bool(_COMMON.search(str(name or "")))


def has_explicit_etn_marker(name: object) -> bool:
    return bool(_ETN.search(str(name or "")))


def has_explicit_etf_marker(name: object) -> bool:
    return bool(_ETF.search(str(name or "")))


def classify_official_security(name: object, *, provider_etp_flag: bool = False) -> SecurityClassification:
    """Classify from explicit description evidence; a ticker suffix is never evidence."""
    text = str(name or "").strip()
    if has_explicit_etn_marker(text):
        return SecurityClassification("etn", "Exchange Traded Note")
    if has_explicit_etf_marker(text):
        return SecurityClassification("etf", "Exchange Traded Fund")
    if _UNIT_SECURITY.search(text):
        return SecurityClassification("unit", "Unit")
    if _WARRANT.search(text):
        return SecurityClassification("warrant", "Warrant")
    if _RIGHT.search(text):
        return SecurityClassification("right", "Right")
    if has_explicit_common_marker(text):
        return SecurityClassification("equity", "Common Stock")
    if _PREFERRED.search(text):
        return SecurityClassification("preferred", "Preferred Stock")
    if _DEPOSITARY.search(text):
        return SecurityClassification("adr", "Depositary Share")
    if _UNIT.search(text):
        return SecurityClassification("unit", "Unit")
    if provider_etp_flag:
        return SecurityClassification("etf", "Exchange Traded Fund")
    return SecurityClassification("equity", "Listed Security")
