import json
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

import vault_kit


SCRATCH = Path(__file__).resolve().parents[1] / ".scratch"
SCRATCH.mkdir(exist_ok=True)


class VaultKitTests(unittest.TestCase):
    def test_imports_conversation_and_skips_tool_outputs(self):
        self.assertEqual("hello", vault_kit.conversation({
            "type": "user", "message": {"role": "user", "content": "hello"}
        }))
        self.assertEqual("visible", vault_kit.conversation({
            "type": "response_item", "payload": {
                "type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": "visible"},
                    {"type": "tool_output", "text": "hidden"},
                ]
            }
        }))
        self.assertIsNone(vault_kit.conversation({
            "type": "response_item", "payload": {"type": "function_call", "name": "private"}
        }))

    def test_build_creates_linked_notes_from_synthetic_fixture(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            source, output = base / "input", base / "vault"
            source.mkdir()
            records = [
                {"type": "user", "timestamp": "2026-02-03T10:00:00Z", "cwd": "/work/demo", "message": {"role": "user", "content": "Synthetic question"}},
                {"type": "assistant", "timestamp": "2026-02-03T10:00:01Z", "message": {"role": "assistant", "content": "Synthetic answer"}},
                {"type": "tool_result", "content": "Synthetic tool output"},
            ]
            (source / "session.jsonl").write_text("\n".join(map(json.dumps, records)), encoding="utf-8")
            with patch.object(vault_kit, "github_repos", return_value=[]):
                vault_kit.build(source, output)
            self.assertTrue((output / "START-HERE.md").is_file())
            self.assertIn("[[Index]]", (output / "START-HERE.md").read_text())
            self.assertIn("vault_kit.py search TERM --vault DIR", (output / "START-HERE.md").read_text())
            self.assertIn("[[Projects/demo]]", (output / "Index.md").read_text())
            project = (output / "Projects" / "demo.md").read_text()
            self.assertIn("[[Sessions/2026-02-03-demo-", project)
            session = next((output / "Sessions").glob("*.md")).read_text()
            self.assertIn("[[Projects/demo]]", session)
            self.assertIn("Synthetic question", session)
            self.assertIn("Synthetic answer", session)
            self.assertNotIn("Synthetic tool output", session)
            self.assertEqual(["Index.md", "Projects", "START-HERE.md", "Sessions"], sorted(p.name for p in output.iterdir()))

    def test_existing_notes_survive_and_managed_notes_refresh(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            source, output = base / "input", base / "vault"
            source.mkdir()
            output.mkdir()
            existing = {
                "Index.md": b"# My index\n\nKeep these exact bytes.\n",
                "START-HERE.md": b"My own start note\r\n",
            }
            for name, content in existing.items():
                (output / name).write_bytes(content)
            record = {"type": "user", "cwd": "/work/demo", "message": {"role": "user", "content": "Synthetic"}}
            (source / "session.jsonl").write_text(json.dumps(record), encoding="utf-8")

            with patch.object(vault_kit, "github_repos", return_value=[]):
                vault_kit.build(source, output)
                generated_index = output / "Index (Vault Kit).md"
                stale = "---\nvault-kit-managed: true\n---\n\nstale generated content\n"
                generated_index.write_text(stale.replace("true\n", f"true\nvault-kit-hash: {vault_kit._digest(stale)}\n", 1), encoding="utf-8")
                vault_kit.build(source, output)

            for name, content in existing.items():
                self.assertEqual(content, (output / name).read_bytes())
            self.assertIn("vault-kit-managed: true", generated_index.read_text(encoding="utf-8"))
            self.assertNotIn("stale generated content", generated_index.read_text(encoding="utf-8"))
            start = (output / "START-HERE (Vault Kit).md").read_text(encoding="utf-8")
            self.assertIn("[[Index (Vault Kit)]]", start)

    def test_links_point_at_notes_vault_kit_wrote(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            source, output = base / "input", base / "vault"
            source.mkdir()
            (output / "Projects").mkdir(parents=True)
            (output / "Projects" / "demo.md").write_text("mine", encoding="utf-8")
            record = {"type": "user", "timestamp": "2026-02-03T10:00:00Z", "cwd": "/work/demo", "message": {"role": "user", "content": "Synthetic"}}
            (source / "session.jsonl").write_text(json.dumps(record), encoding="utf-8")
            with patch.object(vault_kit, "github_repos", return_value=[]):
                vault_kit.build(source, output)
            self.assertEqual("mine", (output / "Projects" / "demo.md").read_text())
            session = next((output / "Sessions").glob("*.md")).read_text()
            self.assertIn("[[Projects/demo (Vault Kit)]]", session)
            self.assertNotIn("[[Projects/demo]]", session)
            self.assertIn("[[Projects/demo (Vault Kit)]]", (output / "Index.md").read_text())
            project = (output / "Projects" / "demo (Vault Kit).md").read_text()
            self.assertIn(f"[[Sessions/{next((output / 'Sessions').glob('*.md')).stem}]]", project)

    def test_refresh_updates_grown_transcript_in_place(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            source, output = base / "input", base / "vault"
            source.mkdir()
            transcript = source / "session.jsonl"
            first = {"type": "user", "timestamp": "2026-02-03T10:00:00Z", "cwd": "/work/demo", "message": {"role": "user", "content": "First message"}}
            second = {"type": "assistant", "timestamp": "2026-02-03T10:00:01Z", "cwd": "/work/other", "message": {"role": "assistant", "content": "Second message"}}
            transcript.write_text(json.dumps(first), encoding="utf-8")
            with patch.object(vault_kit, "github_repos", return_value=[]):
                vault_kit.build(source, output)
                transcript.write_text(json.dumps(first) + "\n" + json.dumps(second), encoding="utf-8")
                vault_kit.build(source, output)
            notes = list((output / "Sessions").glob("*.md"))
            self.assertEqual(1, len(notes))
            self.assertIn("Second message", notes[0].read_text())
            self.assertEqual(["demo.md"], [p.name for p in (output / "Projects").iterdir()])

    def test_refresh_never_touches_unmanaged_session_note(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            source, output = base / "input", base / "vault"
            source.mkdir()
            record = {"type": "user", "timestamp": "2026-02-03T10:00:00Z", "cwd": "/work/demo", "message": {"role": "user", "content": "Synthetic"}}
            (source / "session.jsonl").write_text(json.dumps(record), encoding="utf-8")
            with patch.object(vault_kit, "github_repos", return_value=[]):
                vault_kit.build(source, output)
                managed = next((output / "Sessions").glob("*.md"))
                managed.write_text("my own edit", encoding="utf-8")
                vault_kit.build(source, output)
            self.assertEqual("my own edit", managed.read_text())
            copy = next(p for p in (output / "Sessions").glob("*(Vault Kit).md"))
            self.assertIn("Synthetic", copy.read_text())

    def test_search_finds_terms_in_built_vault(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            source, output = base / "input", base / "vault"
            source.mkdir()
            record = {"type": "user", "cwd": "/work/demo", "message": {"role": "user", "content": "Findable Needle here"}}
            (source / "session.jsonl").write_text(json.dumps(record), encoding="utf-8")
            with patch.object(vault_kit, "github_repos", return_value=[]):
                vault_kit.build(source, output)
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                self.assertEqual(1, vault_kit.search(output, "needle"))
                self.assertEqual(0, vault_kit.search(output, "absent-term"))
            self.assertIn("Findable Needle here", captured.getvalue())
            with patch.object(sys, "argv", ["vault_kit.py", "search", "needle", "--vault", str(output)]):
                with contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        vault_kit.main()
            self.assertEqual(0, raised.exception.code)

    def test_write_refuses_to_follow_path_outside_vault(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            output = base / "vault"
            output.mkdir()
            with self.assertRaises(ValueError):
                vault_kit.write_note(output, "../outside.md", "no")
            self.assertFalse((base / "outside.md").exists())

    def test_empty_username_never_calls_github(self):
        with patch.object(vault_kit, "urlopen", side_effect=AssertionError("network called")):
            self.assertEqual([], vault_kit.github_repos(""))

    def test_github_failure_is_skipped(self):
        with patch.object(vault_kit, "urlopen", side_effect=URLError("offline")):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual([], vault_kit.github_repos("synthetic-user"))

    def test_default_finds_both_tools_standard_folders(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            for folder in (home / ".claude" / "projects", home / ".codex" / "sessions"):
                folder.mkdir(parents=True)
                record = {"type": "user", "cwd": f"/work/{folder.name}", "message": {"role": "user", "content": "Synthetic"}}
                (folder / "session.jsonl").write_text(json.dumps(record), encoding="utf-8")
            with patch.object(Path, "home", return_value=home), patch.object(sys, "argv", ["vault_kit.py", "--username", "synthetic-user", "--output", str(home / "vault")]), patch.object(vault_kit, "github_repos", return_value=[]):
                with contextlib.redirect_stdout(io.StringIO()):
                    vault_kit.main()
            notes = list((home / "vault" / "Sessions").glob("*.md"))
            self.assertEqual(2, len(notes))


    def build_vault(self, base, *texts):
        source, output = base / "input", base / "vault"
        source.mkdir()
        for number, text in enumerate(texts):
            record = {"type": "user", "cwd": f"/work/demo{number}", "message": {"role": "user", "content": text}}
            (source / f"session{number}.jsonl").write_text(json.dumps(record), encoding="utf-8")
        with patch.object(vault_kit, "github_repos", return_value=[]), contextlib.redirect_stdout(io.StringIO()):
            vault_kit.build(source, output)
        return source, output

    def test_imported_transcripts_redact_credentials(self):
        keys = [
            "sk-" + "ant-" + "Ab3" * 10,
            "gh" + "p_" + "Cd4" * 12,
            "AK" + "IA" + "QWERTYUIOP123456",
            "-----BEGIN " + "PRIVATE KEY-----\nMIIsynthetic\n-----END " + "PRIVATE KEY-----",
        ]
        label = "pass" + "word"
        assigned = label + " = " + "hunter" + "2fixture9"
        lettered = "token: " + "correcthorse" + "batterystaple"
        quoted = [label + ' = "abcd(efgh)' + '123"', label + " = 'horse " + "battery staple'", label + " = hun" + "ter2", label + ' = "$ec' + 'ret123"', label + ' = "abc\\"def' + 'secret"', label + " = $ec" + "ret456"]
        kept = "token_count = tokenize(text) and api" + "_key = os.environ"
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            _, output = self.build_vault(Path(temporary), "\n".join([*keys, assigned, lettered, *quoted, kept]))
            session = next((output / "Sessions").glob("*.md")).read_text(encoding="utf-8")
        for key in keys:
            self.assertNotIn(key.splitlines()[-1] if "PRIVATE" in key else key, session)
        self.assertNotIn("MIIsynthetic", session)
        self.assertNotIn("hunter2fixture9", session)
        self.assertNotIn("batterystaple", session)
        for leaked in ("efgh", "horse battery", "hunter2\n", "ret123", "defsecret", "ret456"):
            self.assertNotIn(leaked, session)
        self.assertIn(label + " = " + vault_kit.REDACTED, session)
        self.assertIn(kept, session)

    def test_edited_managed_note_is_kept_and_a_copy_is_written(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            source, output = self.build_vault(Path(temporary), "Synthetic")
            note = next((output / "Sessions").glob("*.md"))
            edited = note.read_text(encoding="utf-8") + "\nMy own addition\n"
            note.write_text(edited, encoding="utf-8")
            with patch.object(vault_kit, "github_repos", return_value=[]), contextlib.redirect_stdout(io.StringIO()):
                vault_kit.build(source, output)
            self.assertEqual(edited, note.read_text(encoding="utf-8"))
            copy = next((output / "Sessions").glob("*(Vault Kit).md"))
            self.assertIn("Synthetic", copy.read_text(encoding="utf-8"))
            self.assertNotIn("My own addition", copy.read_text(encoding="utf-8"))
            self.assertEqual([], list(output.rglob(".vault-kit-*")))

    def test_notes_vault_kit_cannot_verify_are_kept(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            source, output = self.build_vault(Path(temporary), "Synthetic")
            note = next((output / "Sessions").glob("*.md"))
            original = note.read_text(encoding="utf-8")
            legacy = "---\nvault-kit-managed: true\n---\n\nMy edit to an older note\n"
            header_edit = original.replace("---\n\n", "tags: mine\n---\n\n", 1)
            for edited in (legacy, header_edit):
                note.write_text(edited, encoding="utf-8")
                with patch.object(vault_kit, "github_repos", return_value=[]), contextlib.redirect_stdout(io.StringIO()):
                    vault_kit.build(source, output)
                self.assertEqual(edited, note.read_text(encoding="utf-8"))

    def test_search_requires_every_word_and_ranks_the_best_note_first(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            _, output = self.build_vault(Path(temporary), "alpha only", "alpha beta once", "beta alpha beta alpha beta", "un caf\u00e9", "die Stra\u00dfe")
            for ranked in (True, False):
                captured = io.StringIO()
                with contextlib.redirect_stdout(captured):
                    if ranked:
                        found = vault_kit.search(output, "beta alp")
                    else:
                        with patch.object(vault_kit, "_ranked", return_value=None):
                            found = vault_kit.search(output, "beta alp")
                lines = captured.getvalue().splitlines()
                self.assertEqual(2, found)
                self.assertFalse(any("alpha only" in line for line in lines))
                if ranked:
                    self.assertIn("beta alpha beta alpha beta", lines[0])
                with contextlib.redirect_stdout(io.StringIO()):
                    if ranked:
                        self.assertEqual([1, 0], [vault_kit.search(output, w) for w in ("cafe", "strasse")])
                    else:
                        with patch.object(vault_kit, "_ranked", return_value=None):
                            self.assertEqual([1, 0], [vault_kit.search(output, w) for w in ("cafe", "strasse")])
                with contextlib.redirect_stdout(io.StringIO()):
                    if ranked:
                        self.assertEqual(0, vault_kit.search(output, "pha"))
                    else:
                        with patch.object(vault_kit, "_ranked", return_value=None):
                            self.assertEqual(0, vault_kit.search(output, "pha"))

if __name__ == "__main__":
    unittest.main()
