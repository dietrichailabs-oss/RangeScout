from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from app.notes import NoteStore


class TestNotes(unittest.TestCase):
    def test_note_store_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = NoteStore(Path(folder) / "notes.json")
            note = store.add("AAPL", "Watch quarterly earnings")
            notes = store.list_for("aapl")
            self.assertEqual(len(notes), 1)
            self.assertEqual(notes[0].text, note.text)


if __name__ == "__main__":
    unittest.main()
