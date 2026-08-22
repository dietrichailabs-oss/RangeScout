from __future__ import annotations

import logging
from typing import Any

from app.security.secrets import redact_secrets


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        original = super().format(record)
        return redact_secrets(original)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger
