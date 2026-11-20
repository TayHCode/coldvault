import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import coldvault


class SessionContinuationTests(unittest.TestCase):
    def test_edited_continuations_stay_in_order_across_syncs(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source, vault = base / "input", base / "vault"
            source.mkdir()
            transcript = source / "session.jsonl"
            sessions = vault / "Sessions"

            def sync(*messages):
                if messages:
                    with transcript.open("a", encoding="utf-8") as stream:
                        for message in messages:
                            record = {"type": "user", "timestamp": "2026-11-19T10:00:00Z",
                                      "cwd": "/work/demo", "sessionId": "demo-session",
                                      "message": {"role": "user", "content": message}}
                            stream.write(json.dumps(record) + "\n")
                environment = {"HOME": str(base), "USERPROFILE": str(base), "XDG_CACHE_HOME": str(base / "cache")}
                with patch.dict(os.environ, environment), contextlib.redirect_stdout(io.StringIO()):
                    coldvault.sync_main(["--vault", str(vault), "--transcripts", str(source), "--username", ""])

            sync("Original paragraph")
            first, = sessions.glob("*.md")
            original = first.read_bytes() + b"\nMy summary\n"
            first.write_bytes(original)
            sync("First continuation paragraph")
            second = first.with_name(f"{first.stem} (continued).md")
            edited = second.read_bytes().replace(b"First continuation paragraph", b"My rewrite")
            second.write_bytes(edited)

            expected = {first: original, second: edited}
            sync()
            self.assertEqual(set(expected), set(sessions.glob("*.md")))
            self.assertEqual(expected, {path: path.read_bytes() for path in sessions.glob("*.md")})

            sync("Next paragraph")
            third = first.with_name(f"{first.stem} (continued 2).md")
            self.assertEqual({first, second, third}, set(sessions.glob("*.md")))
            self.assertTrue(third.read_text(encoding="utf-8").endswith(
                f"Continues [[Sessions/{second.stem}]]\n\nNext paragraph\n"))
            self.assertNotIn(b"First continuation paragraph", third.read_bytes())
            self.assertNotIn(b"Original paragraph", third.read_bytes())

            sync("Latest paragraph")
            self.assertTrue(third.read_bytes().endswith(b"Next paragraph\n\nLatest paragraph\n"))
            expected[third] = third.read_bytes().replace(b"Latest paragraph", b"Second rewrite")
            third.write_bytes(expected[third])
            sync("Final paragraph")
            fourth = first.with_name(f"{first.stem} (continued 3).md")
            self.assertTrue(fourth.read_text(encoding="utf-8").endswith(
                f"Continues [[Sessions/{third.stem}]]\n\nFinal paragraph\n"))
            expected[fourth] = fourth.read_bytes()
            links = (f"- [[Sessions/{first.stem}]] (2026-11-19) · [[Sessions/{second.stem}]]"
                     f" · [[Sessions/{third.stem}]] · [[Sessions/{fourth.stem}]]")
            sync()
            self.assertEqual(expected, {path: path.read_bytes() for path in sessions.glob("*.md")})
            project = vault / "Projects" / "demo.md"
            self.assertIn(links, project.read_text(encoding="utf-8"))

            second.unlink()
            del expected[second]
            links = links.replace(f" · [[Sessions/{second.stem}]]", "")
            for remove_transcript in (False, True):
                if remove_transcript:
                    transcript.unlink()
                    (source / "empty.jsonl").touch()
                sync()
                self.assertEqual(expected, {path: path.read_bytes() for path in sessions.glob("*.md")})
                self.assertIn(links, project.read_text(encoding="utf-8"))
