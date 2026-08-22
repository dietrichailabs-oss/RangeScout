from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from scripts.build_manifest import build_manifest


class TestManifest(unittest.TestCase):
    def test_build_manifest_includes_file_hash(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / "a.txt"
            target.write_text("hello", encoding="utf-8")
            manifest = build_manifest(root)
            self.assertIn("a.txt", manifest)
            self.assertEqual(len(manifest["a.txt"]), 64)


if __name__ == "__main__":
    unittest.main()
