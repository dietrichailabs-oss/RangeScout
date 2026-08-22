from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.application.bootstrap import RangeScoutApplication
from app.configuration.settings import AppSettings
from app.providers.registry import ProviderRegistry, default_provider_registry


class TestProviderRegistry(unittest.TestCase):
    def test_default_provider_registry_lists_public_120_providers(self) -> None:
        registry = default_provider_registry()
        providers = sorted(registry.list_available())
        self.assertNotIn("mock", providers)
        self.assertIn("yahoo", providers)
        self.assertIn("finnhub", providers)
        self.assertNotIn("alpaca", providers)

    def test_range_scout_application_selects_live_default_provider(self) -> None:
        settings = AppSettings()
        with tempfile.TemporaryDirectory() as folder:
            app = RangeScoutApplication(data_dir=Path(folder), settings=settings)
            try:
                self.assertEqual(app.provider.provider_id, "yahoo")
            finally:
                app.store.close()

    def test_range_scout_application_migrates_explicit_mock_provider(self) -> None:
        settings = AppSettings(default_provider="mock", provider_policy_version=2)
        with tempfile.TemporaryDirectory() as folder:
            app = RangeScoutApplication(data_dir=Path(folder), settings=settings)
            try:
                self.assertEqual(app.provider.provider_id, "yahoo")
            finally:
                app.store.close()


if __name__ == "__main__":
    unittest.main()
