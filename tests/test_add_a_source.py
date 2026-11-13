import importlib.util
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import coldvault

KIT = Path(coldvault.__file__).resolve().parent
SCRATCH = KIT / ".scratch"
SCRATCH.mkdir(exist_ok=True)
EXAMPLE = KIT / "examples" / "sources" / "bookmarks.py"
FIXTURE = KIT / "examples" / "sources" / "bookmarks.json"
GUIDE = KIT / "docs" / "add-a-source.md"

spec = importlib.util.spec_from_file_location("bookmarks_example", EXAMPLE)
bookmarks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bookmarks)


class AddASourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.vault = self.base / "vault"

    def build(self, env):
        sources = [*coldvault.SOURCES, ("bookmarks", bookmarks.bookmarks_source)]
        with patch.dict(os.environ, env, clear=True), patch.object(coldvault, "SOURCES", sources), \
                contextlib.redirect_stdout(io.StringIO()):
            return coldvault.build_from_files([], self.vault, "")

    def test_example_script_adds_bookmark_notes_to_the_vault(self):
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(self.base), "PYTHONPATH": str(KIT),
               "COLDVAULT_BOOKMARKS": str(FIXTURE)}
        run = subprocess.run(
            [sys.executable, str(EXAMPLE), "--username", "", "--transcripts", str(KIT / "examples" / "transcripts"),
             "--output", str(self.vault)],
            cwd=self.base, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        note = (self.vault / "Imported" / "Bookmarks" / "tomato-spacing.md").read_text(encoding="utf-8")
        self.assertIn("<https://example.org/tomatoes>", note)
        self.assertIn("45 cm between plants.", note)
        self.assertIn("Imported/Bookmarks/raised-bed-soil-mix", (self.vault / "Index.md").read_text(encoding="utf-8"))
        receipt = json.loads((self.vault / ".coldvault" / "last-run.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["sources"]["bookmarks"], {"state": "ok", "count": 2})

    def test_source_is_skipped_without_a_bookmarks_file(self):
        self.assertEqual(self.build({})["bookmarks"], {"state": "skipped"})
        self.assertFalse((self.vault / "Imported" / "Bookmarks").exists())

    def test_invalid_bookmarks_fail_the_source_and_write_no_notes(self):
        cases = {
            "not-json.json": "[",
            "not-a-list.json": json.dumps({"title": "A", "url": "https://example.com"}),
            "no-url.json": json.dumps([{"title": "A"}]),
            "bad-scheme.json": json.dumps([{"title": "A", "url": "javascript:alert(1)"}]),
            "no-host.json": json.dumps([{"title": "A", "url": "https://"}]),
            "spaces.json": json.dumps([{"title": "A", "url": "https:// not a url"}]),
            "blank-title.json": json.dumps([{"title": " ", "url": "https://example.com"}]),
            "bad-note.json": json.dumps([{"title": "A", "url": "https://example.com", "note": 3}]),
            "repeat.json": json.dumps([{"title": "A", "url": "https://example.com"}, {"title": "a", "url": "https://example.org"}]),
        }
        for name, text in cases.items():
            with self.subTest(name):
                path = self.base / name
                path.write_text(text, encoding="utf-8")
                result = self.build({"COLDVAULT_BOOKMARKS": str(path)})["bookmarks"]
                self.assertEqual(result["state"], "failed")
                self.assertIn(name, result["reason"])
                self.assertFalse((self.vault / "Imported" / "Bookmarks").exists())

    def test_bookmark_text_is_redacted(self):
        path = self.base / "secret.json"
        token = "ghp_" + "Q" * 36
        path.write_text(json.dumps([{"title": f"Token {token}", "url": "https://example.com", "note": token}]), encoding="utf-8")
        self.build({"COLDVAULT_BOOKMARKS": str(path)})
        self.assertEqual([p.name for p in (self.vault / "Imported" / "Bookmarks").iterdir()], ["token-redacted.md"])
        note = (self.vault / "Imported" / "Bookmarks" / "token-redacted.md").read_text(encoding="utf-8")
        self.assertNotIn(token, note)
        self.assertIn("[REDACTED]", note)
        self.assertNotIn(token.lower(), (self.vault / "Index.md").read_text(encoding="utf-8").lower())

    def test_guide_shows_the_tested_example_and_fixture(self):
        guide = GUIDE.read_text(encoding="utf-8")
        self.assertIn(EXAMPLE.read_text(encoding="utf-8"), guide)
        self.assertIn(FIXTURE.read_text(encoding="utf-8"), guide)


if __name__ == "__main__":
    unittest.main()
