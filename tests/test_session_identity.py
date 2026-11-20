import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))

import coldvault


SCRATCH = KIT / ".scratch"
SCRATCH.mkdir(exist_ok=True)


class SessionIdentityTests(unittest.TestCase):
    def sync(self, source, output, source_option="--transcripts"):
        home = source.parent / "home"
        home.mkdir(exist_ok=True)
        with patch.dict(os.environ, HOME=str(home), USERPROFILE=str(home)), contextlib.redirect_stdout(io.StringIO()):
            coldvault.sync_main(["--vault", str(output), source_option, str(source), "--username", ""])
        return sorted((output / "Sessions").glob("*.md"))

    def test_distinct_ids_survive_source_replacement_and_rename(self):
        cases = [
            ("different IDs", "first-id", "second-id", "claude-code"),
            ("different providers", "shared-id", "shared-id", "codex"),
            ("filename fallback", None, "session", "claude-code"),
            ("divergent copies", "shared-id", "shared-id", "claude-code"),
            ("successive divergent copies", "shared-id", "shared-id", "claude-code"),
        ]
        for case, first_id, second_id, provider in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
                base = Path(temporary)
                source, output = base / "input", base / "vault"
                source.mkdir()
                transcript = source / ("first.jsonl" if case == "successive divergent copies" else "session.jsonl")
                stamp = "2026-11-01T10:00:00Z"
                claude = {"type": "user", "timestamp": stamp, "cwd": "/work/demo",
                          "sessionId": first_id, "message": {"role": "user", "content": "First session"}}

                transcript.write_text(json.dumps(claude), encoding="utf-8")
                first, = self.sync(source, output)
                legacy = first
                if case == "divergent copies":
                    shutil.copyfile(transcript, source / "copy.jsonl")
                elif case != "successive divergent copies":
                    legacy_key = hashlib.sha1(b"/work/demo\nsession").hexdigest()[:8]
                    legacy = first.with_name(f"2026-11-01-demo-{legacy_key}.md")
                    legacy_body = re.sub(rb"(?m)^session-id-fallback: .*\n", b"", first.read_bytes())
                    unhashed = re.sub(rb"(?m)^coldvault-hash: .*\n", b"", legacy_body)
                    legacy_body = re.sub(rb"(?m)^coldvault-hash: .*", b"coldvault-hash: " + hashlib.sha256(unhashed).hexdigest().encode(), legacy_body)
                    first.unlink()
                    legacy.write_bytes(legacy_body)
                first_body = legacy.read_bytes()
                transcript.unlink()
                if case == "successive divergent copies":
                    transcript = source / "second.jsonl"
                if provider == "codex":
                    records = [
                        {"type": "session_meta", "timestamp": stamp, "payload": {"id": second_id, "cwd": "/work/demo"}},
                        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": "Second session"}},
                    ]
                else:
                    records = [{**claude, "sessionId": second_id, "message": {"role": "user", "content": "Second session"}}]
                transcript.write_text("\n".join(map(json.dumps, records)), encoding="utf-8")

                notes = self.sync(source, output)
                with self.subTest(stage="replacement"):
                    self.assertEqual(2, len(notes))
                    self.assertEqual(first_body, legacy.read_bytes())
                    second, = [note for note in notes if note != legacy]
                    self.assertIn("Second session", second.read_text(encoding="utf-8"))
                if case == "divergent copies":
                    (source / "copy.jsonl").unlink()
                moved = source / "moved"
                moved.mkdir()
                transcript.rename(moved / "renamed.jsonl")
                with self.subTest(stage="rename"):
                    self.assertEqual(notes, self.sync(source, output))
                    self.assertEqual(notes, self.sync(source, output))
                    project = (output / "Projects" / "demo.md").read_text(encoding="utf-8")
                    for note in notes:
                        self.assertIn(f"[[Sessions/{note.stem}]]", project)
                if first_id is not None:
                    with self.subTest(stage="legacy path reuse"):
                        (moved / "first-renamed.jsonl").write_text(json.dumps(claude), encoding="utf-8")
                        self.assertEqual(notes, self.sync(source, output))
                        self.assertIn("First session", legacy.read_text(encoding="utf-8"))

    def test_edited_legacy_session_continues_after_upgrade(self):
        for session_id in ("original-id", None):
            with self.subTest(session_id=session_id), tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
                base = Path(temporary)
                source, output = base / "input", base / "vault"
                source.mkdir()
                record = {"type": "user", "timestamp": "2026-11-01T10:00:00Z", "cwd": "/work/demo",
                          "sessionId": session_id, "message": {"role": "user", "content": "First message"}}
                transcript = source / "session.jsonl"
                transcript.write_text(json.dumps(record), encoding="utf-8")
                first, = self.sync(source, output)
                key = hashlib.sha1(b"/work/demo\nsession").hexdigest()[:8]
                legacy = first.with_name(f"2026-11-01-demo-{key}.md")
                original = re.sub(r"(?m)^session-id-fallback: .*\n", "", first.read_text(encoding="utf-8"))
                edited = original.replace("First message", "My rewrite")
                first.unlink()
                legacy.write_text(edited, encoding="utf-8")
                added = {**record, "message": {"role": "user", "content": "Later message"}}
                transcript.write_text("\n".join(map(json.dumps, [record, added])), encoding="utf-8")

                self.sync(source, output)
                self.sync(source, output)

                self.assert_continuation(legacy, edited)

    def test_edited_chat_export_keeps_its_continuation(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            source, output = base / "export.zip", base / "vault"
            record = {"uuid": "export-id", "chat_messages": [{"sender": "human", "text": "First message"}]}

            def write_export():
                with zipfile.ZipFile(source, "w") as archive:
                    archive.writestr("conversations.json", json.dumps([record]))

            write_export()
            first, = self.sync(source, output, "--claude-export")
            edited = first.read_text(encoding="utf-8").replace("First message", "My rewrite")
            first.write_text(edited, encoding="utf-8")
            record["chat_messages"].append({"sender": "assistant", "text": "Later message"})
            write_export()

            self.sync(source, output, "--claude-export")
            self.sync(source, output, "--claude-export")

            self.assert_continuation(first, edited)

    def assert_continuation(self, note, edited):
        continued = note.with_name(f"{note.stem} (continued).md")
        self.assertEqual({note, continued}, set(note.parent.glob("*.md")))
        self.assertEqual(edited, note.read_text(encoding="utf-8"))
        body = continued.read_text(encoding="utf-8").split(f"Continues [[Sessions/{note.stem}]]\n\n")[1]
        self.assertEqual("Later message\n", body)
