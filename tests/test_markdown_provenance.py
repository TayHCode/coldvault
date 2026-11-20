import contextlib
import importlib.util
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


KIT = Path(__file__).resolve().parents[1] / "coldvault.py"


class MarkdownProvenanceTests(unittest.TestCase):
    def test_sync_records_portable_markdown_provenance(self):
        spec = importlib.util.spec_from_file_location("coldvault_markdown_provenance", KIT)
        coldvault = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(coldvault)

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            notes, vault = home / "notes", home / "vault"
            originals = {
                "garden/Plan.md": b"# Garden plan\r\n",
                "work/Plan.md": b'---\r\ntags: [work]\r\n---\r\n\r\n# Work plan\r\n',
                "Saved.md": b'---\n"coldvault-source-path": "earlier/Saved.md"\nimported-at: 2025-12-31T08:00:00Z\n---\n\n# Saved\n',
            }
            for relative, data in originals.items():
                path = notes / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)

            def sync(now):
                with patch.dict(os.environ, {"HOME": str(home)}), \
                        patch.object(coldvault, "_utc_now", return_value=now), \
                        contextlib.redirect_stdout(io.StringIO()):
                    coldvault.sync_main(["--vault", str(vault), "--notes", str(notes)])

            sync("2026-03-01T12:00:00Z")
            imported = vault / "Imported" / "notes"
            rendered = {
                relative: (imported / relative).read_text(encoding="utf-8")
                for relative in originals
            }
            for relative in ("garden/Plan.md", "work/Plan.md"):
                self.assertIn(f'coldvault-source-path: "notes/{relative}"\n', rendered[relative])
                self.assertIn("imported-at: 2026-03-01T12:00:00Z\n", rendered[relative])
                self.assertNotIn(str(home), rendered[relative])
            self.assertIn("tags: [work]\n", rendered["work/Plan.md"])
            self.assertIn('"coldvault-source-path": "earlier/Saved.md"\n', rendered["Saved.md"])
            self.assertEqual(1, rendered["Saved.md"].count("coldvault-source-path"))
            self.assertIn("imported-at: 2025-12-31T08:00:00Z\n", rendered["Saved.md"])

            sync("2026-03-02T12:00:00Z")
            for relative, data in originals.items():
                self.assertEqual(rendered[relative], (imported / relative).read_text(encoding="utf-8"))
                self.assertEqual(data, (notes / relative).read_bytes())


if __name__ == "__main__":
    unittest.main()
