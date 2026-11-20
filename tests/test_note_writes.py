import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coldvault


class NoteWrites(unittest.TestCase):
    def test_parent_change_after_target_selection_stops_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            parent = vault / "Projects"
            parent.mkdir()
            moved = vault / "Moved"
            select = coldvault.note_target

            def change_parent(root, relative):
                target = select(root, relative)
                parent.rename(moved)
                parent.mkdir()
                return target

            with patch.object(coldvault, "note_target", side_effect=change_parent):
                with self.assertRaises(OSError):
                    coldvault.write_note(vault, "Projects/example.md", "# Generated")

            self.assertEqual([], list(parent.iterdir()))
            self.assertEqual([], list(moved.iterdir()))
            self.assertEqual("Projects/example.md", coldvault.write_note(vault, "Projects/example.md", "# Retry"))
            self.assertIn("# Retry\n", (parent / "example.md").read_text(encoding="utf-8"))

    def test_parent_change_after_temp_write_cleans_up(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            parent = vault / "Projects"
            parent.mkdir()
            moved = vault / "Moved"
            fdopen = os.fdopen

            @contextmanager
            def change_parent_on_close(*args, **kwargs):
                with fdopen(*args, **kwargs) as stream:
                    yield stream
                parent.rename(moved)
                parent.mkdir()

            with patch.object(coldvault.os, "fdopen", side_effect=change_parent_on_close):
                with self.assertRaises(OSError):
                    coldvault.write_note(vault, "Projects/example.md", "# Generated")

            self.assertEqual([], list(parent.iterdir()))
            self.assertEqual([], list(moved.iterdir()))
