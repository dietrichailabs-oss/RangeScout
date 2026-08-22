from __future__ import annotations

import unittest

from app.domain.errors import ValidationError
from app.instruments.service import lookup_symbol
from tests.fakes.mock_provider import MockMarketDataProvider


class TestInstrumentService(unittest.TestCase):
    def test_lookup_symbol_uppercases(self) -> None:
        provider = MockMarketDataProvider()
        instrument = lookup_symbol(provider, "aapl")
        self.assertEqual(instrument.identifier.symbol, "AAPL")

    def test_lookup_symbol_requires_symbol(self) -> None:
        provider = MockMarketDataProvider()
        with self.assertRaises(ValidationError):
            lookup_symbol(provider, "   ")


if __name__ == "__main__":
    unittest.main()
