import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))

import coldvault


def legacy_note(body, linesep):
    unhashed = f"---\nvault-kit-managed: true\n---\n\n{body}"
    lines = unhashed.split("\n")
    lines.insert(2, "vault-kit-hash: " + hashlib.sha256(unhashed.encode("utf-8")).hexdigest())
    return ("\n".join(lines)).replace("\n", linesep).encode("utf-8")


class LineHandlingTests(unittest.TestCase):
    def test_transcript_jsonl_preserves_unicode_line_separators(self):
        with tempfile.TemporaryDirectory() as temporary:
            transcript = Path(temporary) / "session.jsonl"
            first = json.dumps(
                {
                    "type": "user",
                    "sessionId": "session-1",
                    "timestamp": "2026-03-01T10:00:00Z",
                    "cwd": "/work/demo",
                    "message": {"role": "user", "content": "before\u2028after"},
                },
                ensure_ascii=False,
            )
            second = json.dumps(
                {
                    "type": "assistant",
                    "sessionId": "session-1",
                    "timestamp": "2026-03-01T10:01:00Z",
                    "cwd": "/work/demo",
                    "message": {"role": "assistant", "content": "para\u2029next\u0085end"},
                },
                ensure_ascii=False,
            )
            raw = (first + "\n" + second + "\r\n").encode("utf-8")
            transcript.write_bytes(raw)

            info = coldvault.transcript_info(transcript)
            self.assertIsNotNone(info)
            project, day, body, source, session_id = info
            self.assertEqual("demo", project[1])
            self.assertEqual("2026-03-01", day)
            self.assertEqual("claude-code", source)
            self.assertEqual("session-1", session_id)
            self.assertIn("before\u2028after", body)
            self.assertIn("para\u2029next\u0085end", body)
            self.assertTrue(coldvault._complete_jsonl(raw))

    def test_managed_note_rejects_line_ending_only_edits_and_keeps_legacy_notes(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            legacy = vault / "Legacy.md"
            legacy.write_bytes(legacy_note("# Legacy\n\nUntouched\n", os.linesep))
            self.assertTrue(coldvault._is_managed_note(legacy))

            body = "# Demo\n\nFirst line\nSecond line\n"
            written = coldvault.write_note(vault, "Projects/demo.md", body)
            self.assertEqual("Projects/demo.md", written)
            note = vault / written
            self.assertTrue(coldvault._is_managed_note(note))

            original = note.read_bytes()
            other_sep = b"\r\n" if os.linesep == "\n" else b"\n"
            current_sep = os.linesep.encode("ascii")
            edited = original.replace(current_sep, other_sep)
            self.assertNotEqual(original, edited)
            note.write_bytes(edited)

            self.assertFalse(coldvault._is_managed_note(note))
            rewritten = coldvault.write_note(vault, "Projects/demo.md", body)
            self.assertEqual("Projects/demo (Coldvault).md", rewritten)
            self.assertEqual(edited, note.read_bytes())

            partial = original.replace(b"First line" + current_sep, b"First line" + other_sep, 1)
            self.assertNotEqual(original, partial)
            note.write_bytes(partial)
            self.assertFalse(coldvault._is_managed_note(note))

            win_legacy = vault / "WinLegacy.md"
            win_bytes = legacy_note("# Win\n\nLine one\nLine two\n", "\r\n")
            with patch.object(coldvault.os, "linesep", "\r\n"):
                win_legacy.write_bytes(win_bytes)
                self.assertTrue(coldvault._is_managed_note(win_legacy))
                win_legacy.write_bytes(win_bytes.replace(b"\r\n", b"\n"))
                self.assertFalse(coldvault._is_managed_note(win_legacy))
                win_legacy.write_bytes(win_bytes.replace(b"Line one\r\n", b"Line one\n", 1))
                self.assertFalse(coldvault._is_managed_note(win_legacy))


if __name__ == "__main__":
    unittest.main()
