"""Security helpers for redaction."""

import re


_SECRET_PATTERNS = (
    re.compile(
        r"(?:api[_-]?key|secret[_-]?key|token|x-finnhub-token|apca-api-key-id|apca-api-secret-key)"
        r"['\"]?[\s:=]+['\"]?([A-Za-z0-9._-]{8,})",
        flags=re.IGNORECASE,
    ),
    re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]{19,})"),
)


def redact_secrets(text: str) -> str:
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted
