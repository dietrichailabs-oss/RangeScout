from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from app.application.services import refresh_symbol_report
from app.historical_store.repository import HistoricalStore
from tests.fakes.mock_provider import MockMarketDataProvider


class TestApplicationService(unittest.TestCase):
    def test_refresh_symbol_report_happy_path(self) -> None:
        provider = MockMarketDataProvider()
        with tempfile.TemporaryDirectory() as folder:
            store = HistoricalStore(Path(folder) / "history.sqlite")
            try:
                report = refresh_symbol_report("AAPL", provider, store, range_days=30)
                self.assertEqual(report.symbol, "AAPL")
                self.assertGreater(len(report.bars), 0)
                self.assertIn("pct_change", report.metrics)
                self.assertTrue(len(report.insights) >= 1)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
