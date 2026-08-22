"""CSV export helpers with basic formula-injection protection."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from app.models.schemas import OhlcvBar

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_FORMULA_PREFIX = ("=", "+", "-", "@", "\t", "\r", "\n")


def sanitize_filename(name: str) -> str:
    safe = _SAFE_NAME.sub("_", name.strip())
    return safe or "rangescout_export"


def sanitize_csv_field(value: object) -> str:
    text = str(value)
    if text.startswith(_FORMULA_PREFIX):
        return f"'{text}"
    return text


@dataclass(frozen=True)
class CsvExportResult:
    path: str
    row_count: int


def export_bars_csv(symbol: str, bars: Iterable[OhlcvBar], target_dir: Path) -> CsvExportResult:
    target_dir.mkdir(parents=True, exist_ok=True)
    file_path = target_dir / f"{sanitize_filename(symbol)}.csv"
    rows = 0
    with file_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["symbol", "date", "open", "high", "low", "close", "volume", "adjusted", "provider"])
        for bar in bars:
            writer.writerow(
                [
                    sanitize_csv_field(bar.instrument.symbol),
                    sanitize_csv_field(bar.date.isoformat()),
                    sanitize_csv_field(bar.open),
                    sanitize_csv_field(bar.high),
                    sanitize_csv_field(bar.low),
                    sanitize_csv_field(bar.close),
                    sanitize_csv_field(bar.volume),
                    sanitize_csv_field(int(bar.adjusted)),
                    sanitize_csv_field(bar.provider),
                ]
            )
            rows += 1
    return CsvExportResult(path=str(file_path), row_count=rows)
