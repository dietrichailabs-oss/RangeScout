from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.historical_store.repository import HistoricalStore
from tests.fakes.mock_provider import MockMarketDataProvider


class TestStoreAndProviderIntegration(unittest.TestCase):
    def test_store_round_trip_for_mock_historical(self) -> None:
        provider = MockMarketDataProvider()
        inst = provider.resolve_instrument("AAPL")
        result = provider.fetch_historical(inst.identifier)

        bars, _actions = result.payload
        with tempfile.TemporaryDirectory() as folder:
            db = HistoricalStore(Path(folder) / "history.sqlite")
            try:
                db.upsert_bars(bars, provider.provider_id)
                cached = db.get_bars(inst.identifier, provider.provider_id)
                self.assertGreater(len(cached), 0)
                self.assertEqual(cached[0].instrument.symbol, inst.identifier.symbol)
                self.assertEqual(cached[0].provider, provider.provider_id)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
