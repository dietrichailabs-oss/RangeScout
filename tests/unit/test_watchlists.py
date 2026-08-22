from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from app.domain.errors import ValidationError
from app.watchlists.manager import WatchlistStore


class TestWatchlistStore(unittest.TestCase):
    def test_watchlist_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = WatchlistStore.from_path(Path(folder) / "watchlists.json")
            created = store.create("primary", "Default")
            self.assertEqual(created.id, "primary")
            with self.assertRaises(ValidationError):
                store.create("primary", "Duplicate")

            store.add_symbol("primary", "aapl")
            store.add_symbol("primary", "AAPL")
            self.assertEqual(store.watchlists["primary"].symbols, ["AAPL"])
            store.remove_symbol("primary", "AAPL")
            self.assertEqual(store.watchlists["primary"].symbols, [])
            store.delete("primary")
            self.assertEqual(len(store.list()), 0)


if __name__ == "__main__":
    unittest.main()
