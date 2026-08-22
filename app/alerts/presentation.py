"""Human-facing market-event naming and filtering."""

from __future__ import annotations

import re


EVENT_LABELS = {
    "TRADE_HALT": "Trading Halted",
    "RESUMPTION_PENDING": "Resumption Pending",
    "TRADING_RESUMED": "Trading Resumed",
    "NEWS_PENDING": "News Pending",
    "VOLATILITY_PAUSE": "Volatility Pause",
    "REGULATORY_HALT": "Regulatory Halt",
}


def humanize_event_code(value: str) -> str:
    code = re.sub(r"[^A-Z0-9]+", "_", str(value or "").strip().upper()).strip("_")
    if not code:
        return "Market Event"
    return EVENT_LABELS.get(code, code.replace("_", " ").title())


def humanize_status_text(value: str) -> str:
    """Humanize enum-like status phrases while preserving ordinary prose."""

    text = str(value or "").strip()
    if not text:
        return "Market Event"
    parts = re.split(r"(\s+[—-]\s+)", text, maxsplit=1)
    head = parts[0]
    if head == head.upper() and any(character.isalpha() for character in head):
        parts[0] = humanize_event_code(head)
    return "".join(parts)


def market_event_filter(code: str) -> str:
    normalized = str(code or "").upper()
    if "RESUM" in normalized:
        return "Resumptions"
    if "REGULATORY" in normalized:
        return "Regulatory"
    if "HALT" in normalized or "PAUSE" in normalized:
        return "Trading Halts"
    return "All"
