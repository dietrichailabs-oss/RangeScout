from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.exports import sanitize_csv_field, sanitize_filename, export_bars_csv
from app.models.schemas import InstrumentIdentifier, OhlcvBar
from decimal import Decimal
from datetime import date


class TestExports(unittest.TestCase):
    def test_sanitize_filename(self) -> None:
        self.assertEqual(sanitize_filename("../bad/name.csv"), ".._bad_name.csv")

    def test_sanitize_csv_field_blocks_formula(self) -> None:
        self.assertEqual(sanitize_csv_field("=2+2"), "'=2+2")

    def test_export_writes_csv_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            bars = [
                OhlcvBar(
                    instrument=InstrumentIdentifier(symbol="AAPL"),
                    date=date(2026, 1, 1),
                    open=Decimal("100"),
                    high=Decimal("101"),
                    low=Decimal("99"),
                    close=Decimal("100.5"),
                    volume=10,
                    provider="mock",
                )
            ]
            result = export_bars_csv("AAPL", bars, Path(folder))
            self.assertTrue(Path(result.path).exists())
            self.assertEqual(result.row_count, 1)


if __name__ == "__main__":
    unittest.main()
