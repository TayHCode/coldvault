import json
import hashlib
import importlib.util
import contextlib
import io
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import quote

import vault_kit


SCRATCH = Path(__file__).resolve().parents[1] / ".scratch"
SCRATCH.mkdir(exist_ok=True)
GITHUB_USER_REPOS = "https://api.github.com/" + "users/synthetic-user/repos?per_page=100&sort=updated"


def scan_fold(text):
    return "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))


def scan_has(word, text):
    return re.search(r"(?<![^\W_])" + re.escape(scan_fold(word)), scan_fold(text)) is not None


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
            self.assertIn("coldvault-managed: true", generated_index.read_text(encoding="utf-8"))
            self.assertNotIn("stale generated content", generated_index.read_text(encoding="utf-8"))
            start = (output / "START-HERE (Coldvault).md").read_text(encoding="utf-8")
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
            self.assertIn("[[Projects/demo (Coldvault)]]", session)
            self.assertNotIn("[[Projects/demo]]", session)
            self.assertIn("[[Projects/demo (Coldvault)]]", (output / "Index.md").read_text())
            project = (output / "Projects" / "demo (Coldvault).md").read_text()
            self.assertIn(f"[[Sessions/{next((output / 'Sessions').glob('*.md')).stem}]]", project)

    def test_projects_with_the_same_slug_keep_separate_notes(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            source, output = base / "input", base / "vault"
            source.mkdir()
            for name, cwd, text in (("a", "/work/.scratch", "dot question"), ("b", "/work/scratch", "plain question")):
                record = {"type": "user", "timestamp": "2026-02-03T10:00:00Z", "cwd": cwd, "message": {"role": "user", "content": text}}
                (source / f"{name}.jsonl").write_text(json.dumps(record), encoding="utf-8")
            for _ in range(2):
                with patch.object(vault_kit, "github_repos", return_value=[]):
                    vault_kit.build(source, output)
            index = (output / "Index.md").read_text()
            targets = [line.split("[[")[1].split("]]")[0] for line in index.splitlines() if line.startswith("- [[")]
            self.assertEqual(2, len(set(targets)))
            self.assertEqual(2, len(list((output / "Projects").glob("*.md"))))
            for session in (output / "Sessions").glob("*.md"):
                text = session.read_text()
                project = text.split("Project: [[")[1].split("]]")[0]
                self.assertIn(f"[[Sessions/{session.stem}]]", (output / f"{project}.md").read_text())

    def test_hash_suffixed_slug_never_reuses_a_taken_slug(self):
        suffix = vault_kit.hashlib.sha1("scratch".encode()).hexdigest()[:8]
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            source, output = base / "input", base / "vault"
            source.mkdir()
            for number, name in enumerate([".scratch", f".scratch-{suffix}", "scratch"]):
                record = {"type": "user", "timestamp": "2026-02-03T10:00:00Z", "cwd": f"/work/{name}", "message": {"role": "user", "content": f"question {number}"}}
                (source / f"{number}.jsonl").write_text(json.dumps(record), encoding="utf-8")
            with patch.object(vault_kit, "github_repos", return_value=[]):
                vault_kit.build(source, output)
            self.assertEqual(3, len(list((output / "Projects").glob("*.md"))))

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
            copy = next(p for p in (output / "Sessions").glob("*(Coldvault).md"))
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

    def test_search_json_lists_path_line_and_text_and_exits_1_on_no_hits(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            vault = Path(temporary) / "vault"
            (vault / "Notes").mkdir(parents=True)
            (vault / "Notes" / "a.md").write_text("intro\n  Findable needle here  \n", encoding="utf-8")

            def run(term):
                argv = ["vault_kit.py", "search", term, "--vault", str(vault), "--json"]
                with patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()) as printed:
                    with self.assertRaises(SystemExit) as raised:
                        vault_kit.main()
                return raised.exception.code, json.loads(printed.getvalue())

            self.assertEqual((0, [{"path": "Notes/a.md", "line": 2, "text": "Findable needle here"}]), run("needle"))
            self.assertEqual((1, []), run("absent"))
            self.assertEqual((1, []), run("?!"))

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

    def fake_github(self, pages):
        requests = []

        def respond(request, timeout):
            requests.append(request)
            body, link = pages[request.full_url]
            response = io.BytesIO(json.dumps(body).encode())
            response.headers = {"Link": link} if link else {}
            return response

        return requests, respond

    def two_page_api(self):
        first = GITHUB_USER_REPOS
        second = "https://api.github.com/user/1/repos?per_page=100&sort=updated&page=2"
        return {
            first: ([{"name": f"repo-{n}", "html_url": f"https://github.com/synthetic-user/repo-{n}"} for n in range(100)], f'<{second}>; rel="next", <{second}>; rel="last"'),
            second: ([{"name": f"repo-{n}", "html_url": f"https://github.com/synthetic-user/repo-{n}"} for n in range(100, 150)], f'<{first}>; rel="first", <{first}>; rel="prev"'),
        }

    def test_github_lookup_follows_every_page(self):
        requests, respond = self.fake_github(self.two_page_api())
        with patch.dict(os.environ, {}, clear=True), patch.object(vault_kit, "urlopen", side_effect=respond):
            repos = vault_kit.github_repos("synthetic-user")
        self.assertEqual([f"repo-{n}" for n in range(150)], [repo["name"] for repo in repos])
        self.assertEqual(2, len(requests))
        self.assertTrue(all(request.get_header("Authorization") is None for request in requests))

    def test_github_token_is_sent_and_never_printed(self):
        token = "ghp_" + "S" * 36
        requests, respond = self.fake_github(self.two_page_api())
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            (base / "input").mkdir()
            record = {"type": "user", "cwd": "/work/demo", "message": {"role": "user", "content": "Synthetic"}}
            (base / "input" / "session.jsonl").write_text(json.dumps(record), encoding="utf-8")
            argv = ["vault_kit.py", "--username", "synthetic-user", "--transcripts", str(base / "input"), "--output", str(base / "vault")]
            out, err = io.StringIO(), io.StringIO()
            with patch.dict(os.environ, {"GITHUB_TOKEN": token}), patch.object(sys, "argv", argv), \
                    patch.object(vault_kit, "urlopen", side_effect=respond), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                vault_kit.main()
            self.assertEqual(2, len(requests))
            self.assertTrue(all(request.get_header("Authorization") == f"Bearer {token}" for request in requests))
            self.assertTrue((base / "vault" / "Projects" / "repo-149.md").is_file())
            self.assertNotIn(token, out.getvalue() + err.getvalue())
            self.assertFalse([p for p in (base / "vault").rglob("*") if p.is_file() and token in p.read_text(encoding="utf-8")])

    def test_github_token_is_never_sent_off_the_api_host(self):
        first = GITHUB_USER_REPOS
        elsewhere = "https://example.invalid/steal?page=2"
        requests, respond = self.fake_github({first: ([{"name": "only"}], f'<{elsewhere}>; rel="next"')})
        with patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_" + "S" * 36}), patch.object(vault_kit, "urlopen", side_effect=respond):
            self.assertEqual(["only"], [repo["name"] for repo in vault_kit.github_repos("synthetic-user")])
        self.assertEqual([first], [request.full_url for request in requests])

    def test_github_token_with_trailing_newline_is_trimmed(self):
        token = "ghp_" + "S" * 36
        requests, respond = self.fake_github(self.two_page_api())
        with patch.dict(os.environ, {"GITHUB_TOKEN": token + "\n"}), patch.object(vault_kit, "urlopen", side_effect=respond):
            self.assertEqual(150, len(vault_kit.github_repos("synthetic-user")))
        self.assertTrue(all(request.get_header("Authorization") == f"Bearer {token}" for request in requests))

    def test_malformed_github_token_never_reaches_error_output(self):
        token = "ghp_" + "S" * 18 + "\n" + "S" * 18
        err = io.StringIO()
        with patch.dict(os.environ, {"GITHUB_TOKEN": token}), \
                patch("socket.create_connection", side_effect=AssertionError("network called")), \
                contextlib.redirect_stderr(err):
            self.assertEqual([], vault_kit.github_repos("synthetic-user"))
        self.assertIn("GitHub lookup skipped", err.getvalue())
        self.assertNotIn("S" * 18, err.getvalue())

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
        quoted = [label + ' = "abcd(efgh)' + '123"', label + " = 'horse " + "battery staple'", label + " = hun" + "ter2", label + ' = "$ec' + 'ret123"', label + ' = "abc\\"def' + 'secret"', label + " = $ec" + "ret456", label + " = abc(" + "defsecret2)", label + " = 'multi\nline" + "secret'", label + " = `tick" + "secret`", label + " = comma," + "secret", "AWS_SECRET_" + "ACCESS_KEY=abcdef" + "0123456789"]
        kept = "token = ${TOKEN} and api" + "_key = os.environ"
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            _, output = self.build_vault(Path(temporary), "\n".join([*keys, assigned, lettered, *quoted, kept]))
            session = next((output / "Sessions").glob("*.md")).read_text(encoding="utf-8")
        for key in keys:
            self.assertNotIn(key.splitlines()[-1] if "PRIVATE" in key else key, session)
        self.assertNotIn("MIIsynthetic", session)
        self.assertNotIn("hunter2fixture9", session)
        self.assertNotIn("batterystaple", session)
        for leaked in ("efgh", "horse battery", "hunter2\n", "ret123", "defsecret", "ret456", "linesecret", "ticksecret", "commasecret", ",secret", "abcdef0123456789"):
            self.assertNotIn(leaked, session)
        self.assertIn(label + " = " + vault_kit.REDACTED, session)
        self.assertIn(kept, session)
        self.assertIn(label + " =\nplease continue", vault_kit.redact(label + " =\nplease continue"))

    def test_secret_probe_redacts_bearer_url_password_and_notion_tokens(self):
        email = "user" + "@example.com"
        secrets = {
            "anthropic": "sk-" + "ant-" + "Ef5" * 10,
            "openai": "sk-" + "proj-" + "Gh6" * 10,
            "github": "gh" + "p_" + "Ij7" * 12,
            "aws": "AK" + "IA" + "ZXCVBNMASD123456",
            "private_key": "MIIprobe" + "synthetic",
            "password": "probe" + "pass" + "9fixture",
            "bearer": "Kl8m" * 9,
            "url_password": "probe" + "Db" + "pass77",
            "notion_secret": "secret" + "_" + "Mn9o" * 11,
            "notion_assigned": "ntn" + "_" + "4821" * 3 + "Pq" * 17,
            "notion_bare": "ntn" + "_" + "7310" * 3 + "Rs" * 17,
        }
        probe = "\n".join([
            secrets["anthropic"],
            "OPENAI_KEY " + secrets["openai"],
            secrets["github"],
            secrets["aws"],
            "-----BEGIN " + "PRIVATE KEY-----\n" + secrets["private_key"] + "\n-----END " + "PRIVATE KEY-----",
            "pass" + "word: " + secrets["password"],
            "curl -H 'Authorization: Bearer " + secrets["bearer"] + "' https://api.example.test",
            "DATABASE_URL postgres://admin:" + secrets["url_password"] + "@db.example.test:5432/app",
            "invite https://ops:" + secrets["url_password"] + "@host.example.test?email=" + email,
            "notion key " + secrets["notion_secret"] + " here",
            "NOTION_" + "TOKEN=" + secrets["notion_assigned"],
            "fresh " + secrets["notion_bare"],
        ])
        kept = "the bearer of news visits https://example.test:8443/a?b=c@d and https://example.test:8443?email=" + email + "#x@y and mails " + email
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            _, output = self.build_vault(Path(temporary), probe + "\n" + kept)
            session = next((output / "Sessions").glob("*.md")).read_text(encoding="utf-8")
        leaked = [name for name, value in secrets.items() if value in session]
        self.assertEqual([], leaked)
        self.assertIn("Authorization: Bearer " + vault_kit.REDACTED, session)
        self.assertIn("postgres://admin:" + vault_kit.REDACTED + "@db.example.test", session)
        self.assertIn("https://ops:" + vault_kit.REDACTED + "@host.example.test?email=" + email, session)
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
            copy = next((output / "Sessions").glob("*(Coldvault).md"))
            self.assertIn("Synthetic", copy.read_text(encoding="utf-8"))
            self.assertNotIn("My own addition", copy.read_text(encoding="utf-8"))
            self.assertEqual([], list(output.rglob(".coldvault-*")))

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
            _, output = self.build_vault(Path(temporary), "alpha only", "alpha beta once", "beta alpha beta alpha beta", "un caf\u00e9", "die Stra\u00dfe", "foo bar baz", "\u03ba\u03cc\u03c3\u03bc\u03bf\u03c2")
            for ranked in (True, False):
                captured = io.StringIO()
                with contextlib.redirect_stdout(captured):
                    if ranked:
                        found = vault_kit.search(output, "beta alp")
                    else:
                        with patch.object(vault_kit, "_ranked", return_value=None):
                            found = vault_kit.search(output, "beta alp")
                lines = captured.getvalue().splitlines()
                with contextlib.redirect_stdout(io.StringIO()) as printed:
                    if ranked:
                        hits = [vault_kit.search(output, w) for w in ("foo-bar baz", "--")]
                    else:
                        with patch.object(vault_kit, "_ranked", return_value=None):
                            hits = [vault_kit.search(output, w) for w in ("foo-bar baz", "--")]
                self.assertEqual([1, 0], hits)
                self.assertIn("foo bar baz", printed.getvalue())
                self.assertEqual(2, found)
                self.assertFalse(any("alpha only" in line for line in lines))
                if ranked:
                    self.assertIn("beta alpha beta alpha beta", lines[0])
                with contextlib.redirect_stdout(io.StringIO()):
                    if ranked:
                        self.assertEqual([1, 0], [vault_kit.search(output, w) for w in ("cafe", "strasse")])
                        self.assertEqual(1, vault_kit.search(output, "\u03ba\u03bf\u03c3\u03bc\u03bf\u03c2"))
                    else:
                        with patch.object(vault_kit, "_ranked", return_value=None):
                            self.assertEqual([1, 0], [vault_kit.search(output, w) for w in ("cafe", "strasse")])
                with contextlib.redirect_stdout(io.StringIO()):
                    if ranked:
                        self.assertEqual(0, vault_kit.search(output, "pha"))
                    else:
                        with patch.object(vault_kit, "_ranked", return_value=None):
                            self.assertEqual(0, vault_kit.search(output, "pha"))

    def test_search_works_without_sqlite(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            _, output = self.build_vault(Path(temporary), "alpha beta")
            with patch.dict(sys.modules, {"sqlite3": None}):
                spec = importlib.util.spec_from_file_location("vault_kit_without_sqlite", vault_kit.__file__)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(1, module.search(output, "beta alp"))

    def run_notes(self, home, *argv):
        with patch.object(Path, "home", return_value=home), patch.object(sys, "argv", ["vault_kit.py", *argv]), \
                patch("builtins.input", side_effect=AssertionError("prompted")), \
                patch.object(vault_kit, "urlopen", side_effect=AssertionError("network called")), \
                contextlib.redirect_stdout(io.StringIO()):
            vault_kit.main()

    def test_notes_folder_is_imported_linked_searchable_and_idempotent(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            transcripts = base / ".claude" / "projects"
            transcripts.mkdir(parents=True)
            record = {"type": "user", "cwd": "/work/demo", "message": {"role": "user", "content": "Transcript text"}}
            (transcripts / "session.jsonl").write_text(json.dumps(record), encoding="utf-8")
            notes, output = base / "My Notes", base / "vault"
            files = {
                "Welcome.md": "---\ntags: [garden]\n---\n\n# Welcome\n\nSee [[Plan]] for the zucchini.\n",
                "Projects/Plan.md": "﻿# Plan\r\n\r\nGrow zucchini.\r\n",
                "Projects/photo.png": "not markdown",
                ".git/HEAD.md": "gitinternal",
                ".obsidian/workspace.md": "hiddenconfig",
                "Projects/.trash/Old.md": "hiddentrash",
                ".draft.md": "hiddenfile",
            }
            for name, text in files.items():
                (notes / name).parent.mkdir(parents=True, exist_ok=True)
                (notes / name).write_text(text, encoding="utf-8")
            (notes / "Link.md").symlink_to(transcripts / "session.jsonl")

            self.run_notes(base, "--notes", str(notes), "--output", str(output))
            imported = output / "Imported" / "My Notes"
            self.assertEqual(["Projects/Plan.md", "Welcome.md"], sorted(p.relative_to(imported).as_posix() for p in imported.rglob("*") if p.is_file()))
            welcome = (imported / "Welcome.md").read_text(encoding="utf-8")
            self.assertTrue(welcome.startswith("---\ncoldvault-managed: true\ncoldvault-hash: "))
            self.assertEqual(2, welcome.count("---\n"))
            self.assertIn("tags: [garden]\n---\n\n# Welcome", welcome)
            self.assertIn("Grow zucchini.\n", (imported / "Projects" / "Plan.md").read_text(encoding="utf-8"))
            for note in imported.rglob("*.md"):
                self.assertTrue(vault_kit._is_managed_note(note))
            index = (output / "Index.md").read_text(encoding="utf-8")
            self.assertIn("## Imported notes", index)
            self.assertIn("- [[Imported/My Notes/Projects/Plan]]", index)
            self.assertIn("- [[Imported/My Notes/Welcome]]", index)
            self.assertFalse((output / "Sessions").exists())
            with contextlib.redirect_stdout(io.StringIO()) as printed:
                self.assertEqual(2, vault_kit.search(output, "zucchini"))
                for hidden in ("gitinternal", "hiddenconfig", "hiddentrash", "hiddenfile", "transcript"):
                    self.assertEqual(0, vault_kit.search(output, hidden))
            self.assertIn("Imported/My Notes/Welcome.md:", printed.getvalue())

            snapshot = {p: p.read_bytes() for p in output.rglob("*") if p.is_file()}
            self.run_notes(base, "--notes", str(notes), "--output", str(output))
            self.assertEqual(snapshot, {p: p.read_bytes() for p in output.rglob("*") if p.is_file()})

    def test_imported_notes_are_redacted(self):
        key = "gh" + "p_" + "Cd4" * 12
        assigned = "pass" + "word = " + "hunter" + "2fixture9"
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            notes = base / "notes"
            notes.mkdir()
            (notes / "Keys.md").write_text(f"---\napi_key: {key}\n---\n\n{assigned}\nkept words\n", encoding="utf-8")
            self.run_notes(base, "--notes", str(notes), "--output", str(base / "vault"))
            note = (base / "vault" / "Imported" / "notes" / "Keys.md").read_text(encoding="utf-8")
        self.assertNotIn(key, note)
        self.assertNotIn("hunter2fixture9", note)
        self.assertEqual(2, note.count(vault_kit.REDACTED))
        self.assertIn("kept words", note)

    def test_notes_folders_with_the_same_name_stay_separate(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            for parent in ("a", "b"):
                (base / parent / "notes").mkdir(parents=True)
                (base / parent / "notes" / "Same.md").write_text(f"from {parent}", encoding="utf-8")
            imported = base / "vault" / "Imported"
            seen = []
            for order in (("a", "b"), ("b", "a")):
                self.run_notes(base, *[arg for parent in order for arg in ("--notes", str(base / parent / "notes"))], "--output", str(base / "vault"))
                folders = sorted(p.name for p in imported.iterdir())
                self.assertEqual(2, len(folders))
                self.assertTrue(all(name.startswith("notes (") for name in folders))
                self.assertEqual(["from a", "from b"], sorted((imported / name / "Same.md").read_text(encoding="utf-8").rstrip().split("\n")[-1] for name in folders))
                self.assertEqual([], list(imported.rglob("*(Vault Kit)*")))
                self.assertEqual([], list(imported.rglob("*(Coldvault)*")))
                seen.append({name: (imported / name / "Same.md").read_bytes() for name in folders})
            self.assertEqual(seen[0], seen[1])

    def test_imported_notes_never_share_a_destination(self):
        for label in ("Vault Kit", "Coldvault"):
            with self.subTest(label=label), tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
                base = Path(temporary)
                notes, output = base / "notes", base / "vault"
                notes.mkdir()
                (notes / "Note.md").write_text("first source note", encoding="utf-8")
                (notes / f"Note ({label}).md").write_text("second source note", encoding="utf-8")
                self.run_notes(base, "--notes", str(notes), "--output", str(output))
                imported = output / "Imported" / "notes"
                (imported / "Note.md").write_text("my edit", encoding="utf-8")
                self.run_notes(base, "--notes", str(notes), "--output", str(output))
                texts = [p.read_text(encoding="utf-8") for p in imported.glob("*.md")]
                self.assertEqual(3, len(texts))
                for expected in ("first source note", "my edit", "second source note"):
                    self.assertEqual(1, sum(expected in text for text in texts))
                index = (output / "Index.md").read_text(encoding="utf-8")
                links = [line for line in index.splitlines() if line.startswith("- [[Imported/")]
                self.assertEqual(2, len(set(links)))

    def test_markdown_folder_links_are_never_redirected_to_moved_copies(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            notes, output = base / "notes", base / "vault"
            notes.mkdir()
            (notes / "Child.md").write_text("source child", encoding="utf-8")
            (notes / "Home.md").write_text("See [[Imported/notes/Child]]", encoding="utf-8")
            mine = output / "Imported" / "notes" / "Child.md"
            mine.parent.mkdir(parents=True)
            mine.write_text("my own child", encoding="utf-8")
            self.run_notes(base, "--notes", str(notes), "--output", str(output))
            self.assertEqual("my own child", mine.read_text(encoding="utf-8"))
            self.assertIn("source child", (mine.parent / "Child (Coldvault).md").read_text(encoding="utf-8"))
            self.assertIn("See [[Imported/notes/Child]]", (mine.parent / "Home.md").read_text(encoding="utf-8"))

    def test_vault_inside_notes_folder_is_never_reimported(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            notes = base / "notes"
            notes.mkdir()
            (notes / "Note.md").write_text("only note", encoding="utf-8")
            for _ in range(2):
                self.run_notes(base, "--notes", str(notes), "--output", str(notes / "vault"))
            self.assertEqual(["Note.md"], [p.name for p in (notes / "vault" / "Imported" / "notes").rglob("*.md")])
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.run_notes(base, "--notes", str(notes / "vault" / "Imported"), "--output", str(notes / "vault"))
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.run_notes(base, "--notes", str(base / "missing"), "--output", str(base / "vault2"))

    def notion_fixture(self):
        ids = {title: hashlib.md5(title.encode()).hexdigest() for title in ("home", "child", "tasks", "row", "notes-a", "notes-b", "escape")}
        home, child, tasks, row = (f"Start {ids['home']}", f"Child (v2) {ids['child']}", f"Tasks {ids['tasks']}", f"Water plants {ids['row']}")
        key = "gh" + "p_" + "Nt7" * 12

        def url(path):
            return quote(path, safe="/()")

        attached = io.BytesIO()
        with zipfile.ZipFile(attached, "w") as archive:
            archive.writestr(f"{home}.md", "zipattachment")

        return ids, key, {
            f"{home}.md": (
                f"# Start\n\nSee [Child (v2)]({url(f'{home}/{child}.md')}) and the [Tasks]({url(f'{home}/{tasks}.csv')}) board.\n"
                f"Mention: [Child (v2)](https://www.notion.so/Child-v2-{ids['child']}?pvs=21)\n"
                f"![diagram.png]({url(f'{home}/diagram.png')})\n"
                "Keep [the docs](https://example.com/guide) and [top](#start).\n\n"
                f"| Page | Note |\n| --- | --- |\n| [Row]({url(f'{home}/{tasks}/{row}.md')}) | cell |\n"
            ).encode(),
            f"{home}/{child}.md": (
                f"# Child (v2)\n\nBack to [Start]({url(f'../{home}.md')}). See [Plain]({url('../Plain page.md')}).\n"
                f"Jump to [the board]({url(f'../{home}.md')}#tasks-board).\n"
            ).encode(),
            "Plain page.md": "# Plain page\n\nno id in its name, see [DB](Plain%20db.csv)\n".encode(),
            "Plain db.csv": "Name\nviewrow\n".encode(),
            "Plain db_all.csv": "Name\nplainrow\n".encode(),
            f"{home}/{tasks}.csv": "Name,Status\nview only,Done\n".encode(),
            f"{home}/{tasks}_all.csv": (
                "﻿Name,Status,Notes\r\nWater plants,Done,\"marigold seeds | trays\nsecond line\"\r\n"
                f"Order soil,Todo,token: {key}\r\nShort row\r\n"
            ).encode(),
            f"{home}/{tasks}/{row}.md": "# Water plants\n\nStatus: Done\n".encode(),
            f"{home}/diagram.png": b"\x89PNG not really",
            f"Notes {ids['notes-a']}.md": "# Notes\n\nfirst notes page\n".encode(),
            f"Notes {ids['notes-b']}.md": "# Notes\n\nsecond notes page\n".encode(),
            f"Notes {ids['notes-a']}/Same.md": "first same child".encode(),
            f"Notes {ids['notes-b']}/Same.md": "second same child".encode(),
            f"../escape {ids['escape']}.md": "escapedpage".encode(),
            f"{home}/../sneaky.md": "sneakypage".encode(),
            f"__MACOSX/._Start {ids['home']}.md": "resourcefork".encode(),
            "__MACOSX/macstray.md": "macstray".encode(),
            f"{home}/files.zip": attached.getvalue(),
            "attached.zip": attached.getvalue(),
            "Vacation-Part-2.zip": attached.getvalue(),
        }

    def write_zip(self, path, files):
        with zipfile.ZipFile(path, "w") as archive:
            for name, data in files.items():
                archive.writestr(name, data)

    def test_notion_export_becomes_clean_linked_searchable_notes(self):
        ids, key, files = self.notion_fixture()
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            flat, nested, inner, folder = base / "flat.zip", base / "nested.zip", base / "Part-1.zip", base / "unzipped"
            self.write_zip(flat, files)
            self.write_zip(inner, files)
            self.write_zip(nested, {
                "Export-Part-1.zip": inner.read_bytes(),
                ".hidden-Part-2.zip": files["attached.zip"],
                "__MACOSX/._Export-Part-1.zip": b"resource fork",
            })
            for name, data in files.items():
                if ".." not in name.split("/"):
                    (folder / name).parent.mkdir(parents=True, exist_ok=True)
                    (folder / name).write_bytes(data)
            vaults = {}
            for export in (flat, nested, folder):
                output = base / f"vault-{export.stem}"
                self.run_notes(base, "--notion-export", str(export), "--output", str(output))
                notion = output / "Imported" / "Notion"
                notes = {p.relative_to(notion).as_posix(): p.read_text(encoding="utf-8") for p in notion.rglob("*") if p.is_file()}
                vaults[export.stem] = notes
                same = sorted(name for name in notes if name.startswith("Notes/"))
                self.assertEqual(sorted([
                    "Start.md", "Start/Child (v2).md", "Start/Tasks.md", "Start/Tasks/Water plants.md",
                    f"Notes {ids['notes-a'][:8]}.md", f"Notes {ids['notes-b'][:8]}.md", "Plain page.md", "Plain db.md",
                ]), sorted(set(notes) - set(same)))
                self.assertIn("see [[Imported/Notion/Plain db|DB]]", notes["Plain page.md"])
                self.assertIn("| plainrow |", notes["Plain db.md"])
                self.assertIn("Jump to [[Imported/Notion/Start#tasks-board|the board]].", notes["Start/Child (v2).md"])
                self.assertEqual(2, len(same))
                self.assertTrue(all(re.fullmatch(r"Notes/Same [0-9a-f]{8}\.md", name) for name in same))
                self.assertEqual(["first same child", "second same child"], sorted(notes[name].rstrip().split("\n")[-1] for name in same))
                self.assertIn("source: notion\n---", notes["Plain page.md"])
                self.assertNotIn("notion-id", notes["Plain page.md"])
                self.assertEqual([], [p for p in output.rglob("*") if re.search(r"[0-9a-f]{32}", p.name)])
                self.assertEqual([], [p for p in base.rglob("escape*")])

                home = notes["Start.md"]
                self.assertIn("source: notion\nnotion-id: " + ids["home"], home)
                self.assertIn("See [[Imported/Notion/Start/Child (v2)|Child (v2)]] and the [[Imported/Notion/Start/Tasks|Tasks]] board.", home)
                self.assertIn("Mention: [[Imported/Notion/Start/Child (v2)|Child (v2)]]", home)
                self.assertIn("diagram.png (not imported)", home)
                self.assertIn("Keep [the docs](https://example.com/guide) and [top](#start).", home)
                self.assertIn("| [[Imported/Notion/Start/Tasks/Water plants\\|Row]] | cell |", home)
                self.assertIn("Back to [[Imported/Notion/Start|Start]]. See [[Imported/Notion/Plain page|Plain]].", notes["Start/Child (v2).md"])
                links = [target for text in notes.values() for target in re.findall(r"\[\[([^\]|\\#]+)", text)]
                self.assertEqual(8, len(links))
                for target in links:
                    self.assertTrue((output / f"{target}.md").is_file(), target)

                table = notes["Start/Tasks.md"]
                self.assertIn("# Tasks\n\n| Name | Status | Notes |\n| --- | --- | --- |\n", table)
                self.assertIn("| Water plants | Done | marigold seeds \\| trays<br>second line |", table)
                self.assertIn("| Short row |  |  |", table)
                self.assertNotIn("view only", table)
                self.assertNotIn(key, table)
                self.assertIn("token: " + vault_kit.REDACTED, table)
                for note in notion.rglob("*.md"):
                    self.assertTrue(vault_kit._is_managed_note(note))
                index = (output / "Index.md").read_text(encoding="utf-8")
                self.assertIn("- [[Imported/Notion/Start/Tasks]]", index)
                with contextlib.redirect_stdout(io.StringIO()) as printed:
                    self.assertEqual(1, vault_kit.search(output, "marigold"))
                    self.assertEqual(0, vault_kit.search(output, "resourcefork"))
                    for skipped in ("zipattachment", "sneakypage", "macstray", "escapedpage"):
                        self.assertEqual(0, vault_kit.search(output, skipped), skipped)
                self.assertTrue(printed.getvalue().startswith("Imported/Notion/Start/Tasks.md:"))

                snapshot = {p: p.read_bytes() for p in output.rglob("*") if p.is_file()}
                self.run_notes(base, "--notion-export", str(export), "--output", str(output))
                self.assertEqual(snapshot, {p: p.read_bytes() for p in output.rglob("*") if p.is_file()})
            self.assertEqual(vaults["flat"], vaults["nested"])
            self.assertEqual(vaults["flat"], vaults["unzipped"])

    def test_notion_links_follow_notes_moved_aside_for_existing_files(self):
        ids, _, files = self.notion_fixture()
        example = "```\n[[Imported/Notion/Start/Child (v2)]]\n```"
        files[f"Start {ids['home']}.md"] += f"\n{example}\n".encode()
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            export, output = base / "export.zip", base / "vault"
            self.write_zip(export, files)
            mine = output / "Imported" / "Notion" / "Start" / "Child (v2).md"
            mine.parent.mkdir(parents=True)
            mine.write_text("my own note", encoding="utf-8")
            self.run_notes(base, "--notion-export", str(export), "--output", str(output))
            notion = output / "Imported" / "Notion"
            self.assertEqual("my own note", mine.read_text(encoding="utf-8"))
            self.assertIn("Back to [[Imported/Notion/Start|Start]]", (notion / "Start" / "Child (v2) (Coldvault).md").read_text(encoding="utf-8"))
            home = (notion / "Start.md").read_text(encoding="utf-8")
            self.assertIn("See [[Imported/Notion/Start/Child (v2) (Coldvault)|Child (v2)]] and", home)
            self.assertNotIn("[[Imported/Notion/Start/Child (v2)|", home)
            self.assertIn(example, home)

    def test_notion_import_keeps_every_note_value_and_code_example(self):
        first, second = "11111111" + "a" * 24, "22222222" + "b" * 24
        nested = "(" * 32 + "x" + ")" * 32
        files = {
            "Nest.md": f"[nested]({nested}/Deeper.md)",
            "Doc.md": "# Doc file",
            "Doc.md/Inner.md": "# Inner",
            "Doc.MD.md/Twice.md": "# Twice",
            f"{nested}/Deeper.md": "# Deeper",
            f"A {first}.md": "# A one",
            f"A {second}.md": "# A two",
            "A 11111111.md": "# A eight",
            "Sales_all.csv": "Region\nnorth",
            "Keys.csv": "Name,Other\napi_key=foo,second",
            "Code.md": "```\n[example](missing.md)\n```not-a-closing-fence\n[still code](Keys.csv)\n```\n\n[bad](https://[bad/path) and inline `[x](missing.md)`, ``a ` [y](Keys.csv) ` b`` and [real](Keys.csv)\n\n"
                       "h1 | h2\n--- | ---\nrow | [cell](Keys.csv)\nAfter [after](Keys.csv) and [deep](Folder%20(one%20(two))/Deep.md)\n",
            "Folder (one (two))/Deep.md": "# Deep",
            "Rel.csv": "Name,Link\nrow,[keys](Keys.csv)",
            "D.csv": "N\nalone",
            "D_all.csv": "N\nview",
            "D_all_all.csv": "N\nfull",
            "Uses D.md": "[D all](D_all.csv)",
            "Big.csv": "Notes\n" + "w" * 200000 + " bigcellend",
            "#../#../Projects/escapee.md": "# escapee",
            "#.obsidian/workspace.md": "# hiddenwrite",
        }
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            export, output = base / "export.zip", base / "vault"
            self.write_zip(export, files)
            self.run_notes(base, "--notion-export", str(export), "--output", str(output))
            notion = output / "Imported" / "Notion"
            texts = {path.name: path.read_text(encoding="utf-8") for path in notion.glob("*.md")}
            self.assertEqual({"A 11111111.md", "A 11111111 2.md", "A 22222222.md", "Sales_all.md", "Keys.md", "Code.md",
                              "Rel.md", "D.md", "D_all.md", "Uses D.md", "Big.md", "Nest.md", "Doc.md"}, set(texts))
            self.assertIn("# Doc file", texts["Doc.md"])
            self.assertIn("# Inner", (notion / "Doc" / "Inner.md").read_text(encoding="utf-8"))
            self.assertIn("# Twice", (notion / "Doc" / "Twice.md").read_text(encoding="utf-8"))
            self.assertIn(f"[[Imported/Notion/{nested}/Deeper|nested]]", texts["Nest.md"])
            self.assertIn(" bigcellend |", texts["Big.md"])
            self.assertIn("# escapee", (notion / "Untitled" / "Untitled" / "Projects" / "escapee.md").read_text(encoding="utf-8"))
            self.assertIn("# hiddenwrite", (notion / "obsidian" / "workspace.md").read_text(encoding="utf-8"))
            outside = [path for path in output.rglob("*.md") if notion not in path.parents and "# escapee" in path.read_text(encoding="utf-8")]
            self.assertEqual([], outside)
            self.assertEqual([], list(output.rglob(".*")))
            self.assertIn("| row | [[Imported/Notion/Keys\\|keys]] |", texts["Rel.md"])
            self.assertIn("row | [[Imported/Notion/Keys\\|cell]]\nAfter [[Imported/Notion/Keys|after]] and [[Imported/Notion/Folder (one (two))/Deep|deep]]\n", texts["Code.md"])
            self.assertIn("| alone |", texts["D.md"])
            self.assertIn("| full |", texts["D_all.md"])
            self.assertIn("[[Imported/Notion/D_all|D all]]", texts["Uses D.md"])
            self.assertEqual(["# A eight", "# A one", "# A two"], sorted(text.split("---\n\n")[1].strip() for name, text in texts.items() if name.startswith("A ")))
            self.assertIn("| north |", texts["Sales_all.md"])
            self.assertIn("| api_key=" + vault_kit.REDACTED + " | second |", texts["Keys.md"])
            self.assertNotIn("foo", texts["Keys.md"])
            self.assertIn("```\n[example](missing.md)\n```not-a-closing-fence\n[still code](Keys.csv)\n```\n\n"
                          "[bad](https://[bad/path) and inline `[x](missing.md)`, ``a ` [y](Keys.csv) ` b`` and [[Imported/Notion/Keys|real]]", texts["Code.md"])

    def test_notion_export_must_be_a_zip_or_folder(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            (base / "export.zip").write_text("not a zip", encoding="utf-8")
            for export in (base / "export.zip", base / "missing.zip", base / "vault" / "inside"):
                (base / "vault" / "inside").mkdir(parents=True, exist_ok=True)
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    self.run_notes(base, "--notion-export", str(export), "--output", str(base / "vault"))
            self.assertFalse((base / "vault" / "Index.md").exists())

    def scan_search(self, vault, term):
        notes = {}
        for path in sorted(Path(vault).rglob("*.md")):
            if path.is_symlink():
                continue
            try:
                notes[path.relative_to(vault).as_posix()] = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
        words = re.findall(r"[^\W_]+", term)
        if not words:
            return 0, ""
        query = " ".join('"' + scan_fold(word).replace('"', '""') + '"*' for word in words)
        with contextlib.closing(vault_kit.sqlite3.connect(":memory:")) as database:
            database.execute("CREATE VIRTUAL TABLE notes USING fts5(path UNINDEXED, body)")
            database.executemany("INSERT INTO notes VALUES (?, ?)", ((path, scan_fold(text)) for path, text in notes.items()))
            ranked = [path for (path,) in database.execute("SELECT path FROM notes WHERE notes MATCH ? ORDER BY bm25(notes), path", (query,))]
        lines = [f"{path}:{number}: {line.strip()}" for path in ranked for number, line in enumerate(notes[path].splitlines(), 1)
                 if any(scan_has(word, line) for word in words)]
        return len(lines), "".join(line + "\n" for line in lines)

    def kit_search(self, vault, term):
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            found = vault_kit.search(vault, term)
        return found, printed.getvalue()

    def assert_same_results(self, vault, queries):
        for term in queries:
            expected = self.scan_search(vault, term)
            self.assertEqual(expected, self.kit_search(vault, term), term)
        return expected

    def test_search_index_matches_a_full_scan_and_follows_edits_and_deletes(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            texts = [f"proj{n % 7} alpha {'beta ' * (n % 5)}caf\u00e9 note {n}\nsecond line gamma{n % 3}" for n in range(40)]
            _, output = self.build_vault(Path(temporary), *texts)
            (output / "Extra").mkdir()
            (output / "Extra" / "loose.md").write_text("alpha loose proj3 gamma2\n", encoding="utf-8")
            (output / "Extra" / "bad.md").write_bytes(b"alpha \xff proj3")
            (output / "Extra" / "link.md").symlink_to(output / "Extra" / "loose.md")
            rank = output / "Extra" / "rank"
            rank.mkdir()
            (rank / "a.md").write_text("wone wone wone wtwo\n", encoding="utf-8")
            (rank / "b.md").write_text("wtwo wtwo wtwo wone\n", encoding="utf-8")
            for number in range(3):
                (rank / f"two{number}.md").write_text("wtwo filler\n", encoding="utf-8")
            for number in range(6):
                (rank / f"ghost{number}.md").write_text("wone filler\n", encoding="utf-8")
            queries = ["proj3", "alpha beta", "cafe", "gam", "proj3 gamma2", "note 17", "absent", "--", "Vault index", "wone wtwo"]
            for _ in range(2):
                self.assert_same_results(output, queries)
            self.assertTrue((output / ".coldvault" / "search.sqlite").is_file())
            self.assertTrue(self.kit_search(output, "wone wtwo")[1].startswith("Extra/rank/b.md:1:"))
            for number in range(6):
                (rank / f"ghost{number}.md").unlink()
            self.assertTrue(self.kit_search(output, "wone wtwo")[1].startswith("Extra/rank/a.md:1:"))

            session = next(p for p in sorted((output / "Sessions").glob("*.md")) if "proj3" in p.read_text(encoding="utf-8"))
            session.write_text(session.read_text(encoding="utf-8").replace("proj3", "projX"), encoding="utf-8")
            loose = output / "Extra" / "loose.md"
            before = loose.stat()
            loose.write_text("delta loose proj4 gamma2\n", encoding="utf-8")
            os.utime(loose, ns=(before.st_atime_ns, before.st_mtime_ns))
            gone = next(p for p in sorted((output / "Sessions").glob("*.md")) if "proj5" in p.read_text(encoding="utf-8"))
            gone.unlink()
            (output / "Extra" / "new.md").write_text("proj3 newcomer\n", encoding="utf-8")
            queries += ["projX", "delta", "proj5", "newcomer", "loose"]
            self.assert_same_results(output, queries)
            self.assertEqual(1, self.kit_search(output, "projX")[0])
            self.assertEqual(0, self.kit_search(output, "delta proj3")[0])
            self.assertEqual(1, self.kit_search(output, "delta proj4")[0])
            self.assertNotIn(gone.relative_to(output).as_posix(), self.kit_search(output, "proj5")[1])
            self.assertIn("Extra/new.md:1: proj3 newcomer", self.kit_search(output, "newcomer")[1])

    def test_search_index_is_gitignored_and_never_changes_notes(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            _, output = self.build_vault(Path(temporary), "alpha beta", "beta gamma")
            notes = {p: p.read_bytes() for p in output.rglob("*") if p.is_file()}
            self.kit_search(output, "beta")
            self.assertEqual("*\n", (output / ".coldvault" / ".gitignore").read_text(encoding="utf-8"))
            self.assertEqual(notes, {p: p.read_bytes() for p in output.rglob("*") if p.is_file() and ".coldvault" not in p.parts})
            if shutil.which("git"):
                subprocess.run(["git", "init", "-q", str(output)], check=True)
                status = subprocess.run(["git", "-C", str(output), "status", "--porcelain", "--untracked-files=all"],
                                        capture_output=True, text=True, check=True).stdout
                self.assertNotIn(".coldvault", status)
                self.assertIn("Index.md", status)

    def test_search_index_of_an_unwritable_vault_goes_to_the_cache(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            _, output = self.build_vault(Path(temporary), "alpha beta", "beta gamma")
            (output / ".coldvault").write_text("not a folder", encoding="utf-8")
            files = {p: p.read_bytes() for p in output.rglob("*") if p.is_file()}
            cache = Path(temporary) / "cache"
            with patch.dict(os.environ, {"XDG_CACHE_HOME": str(cache)}):
                self.assertEqual(self.scan_search(output, "beta"), self.kit_search(output, "beta"))
                self.assertEqual(self.scan_search(output, "gamma"), self.kit_search(output, "gamma"))
            self.assertEqual(files, {p: p.read_bytes() for p in output.rglob("*") if p.is_file()})
            self.assertEqual(1, len(list(cache.glob("coldvault/*/search.sqlite"))))

    def test_search_index_never_writes_through_symlinks_or_into_foreign_databases(self):
        def foreign_database(path):
            with contextlib.closing(vault_kit.sqlite3.connect(str(path))) as database:
                database.execute("CREATE TABLE notes(body TEXT)")
                database.execute("INSERT INTO notes VALUES ('irreplaceable synthetic record')")
                database.commit()

        def symlinked_folder(vault, outside, cache):
            (vault / ".coldvault").symlink_to(outside, target_is_directory=True)

        def symlinked_database(vault, outside, cache):
            foreign_database(outside / "other.sqlite")
            (vault / ".coldvault").mkdir()
            (vault / ".coldvault" / "search.sqlite").symlink_to(outside / "other.sqlite")

        def symlinked_journal(vault, outside, cache):
            (outside / "journal").write_text("unrelated outside file\n", encoding="utf-8")
            (vault / ".coldvault").mkdir()
            (vault / ".coldvault" / "search.sqlite-journal").symlink_to(outside / "journal")

        def symlinked_gitignore(vault, outside, cache):
            (vault / ".coldvault").mkdir()
            (vault / ".coldvault" / ".gitignore").symlink_to(outside / "ignore")

        def symlinked_cache_component(vault, outside, cache):
            (vault / ".coldvault").write_text("not a folder", encoding="utf-8")
            cache.mkdir()
            (cache / "coldvault").symlink_to(outside, target_is_directory=True)

        def foreign_database_in_place(vault, outside, cache):
            (vault / ".coldvault").mkdir()
            foreign_database(vault / ".coldvault" / "search.sqlite")

        needle = "syntheticconfidentialneedle"
        for setup in (symlinked_folder, symlinked_database, symlinked_journal, symlinked_gitignore,
                      symlinked_cache_component, foreign_database_in_place):
            with self.subTest(setup.__name__), tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
                base = Path(temporary)
                _, vault = self.build_vault(base, f"alpha {needle}", "beta gamma")
                outside, cache = base / "outside", base / "cache"
                outside.mkdir()
                setup(vault, outside, cache)
                existing = [p for p in [*outside.rglob("*"), *vault.glob(".coldvault/*")] if p.is_file()]
                before = {p: p.read_bytes() for p in existing}
                with patch.dict(os.environ, {"XDG_CACHE_HOME": str(cache)}):
                    for term in (needle, "beta"):
                        self.assertEqual(self.scan_search(vault, term), self.kit_search(vault, term), term)
                self.assertEqual(before, {p: p.read_bytes() if p.exists() else None for p in existing})
                self.assertEqual(sorted(p for p in existing if outside in p.parents),
                                 sorted(p for p in outside.rglob("*") if p.is_file()))

class CommandLineErrorTests(unittest.TestCase):
    def cli(self, home, *argv):
        script = Path(vault_kit.__file__).resolve()
        result = subprocess.run(
            [sys.executable, str(script), *argv], stdin=subprocess.DEVNULL, capture_output=True, text=True,
            env={**os.environ, "HOME": str(home), "USERPROFILE": str(home)}, timeout=60,
        )
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        return result

    def test_missing_answers_without_a_terminal_name_the_flags(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            for argv in ((), ("--username", ""), ("--output", str(base / "vault"))):
                with self.subTest(argv=argv):
                    result = self.cli(base, *argv)
                    self.assertEqual(2, result.returncode)
                    self.assertIn("pass --username and --output when not interactive", result.stderr)
            self.assertFalse((base / "vault").exists())

    def test_unwritable_output_is_one_error_line(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            source = base / "transcripts"
            source.mkdir()
            record = {"type": "user", "cwd": "/work/demo", "message": {"role": "user", "content": "hello"}}
            (source / "session.jsonl").write_text(json.dumps(record), encoding="utf-8")
            (base / "file").write_text("not a folder", encoding="utf-8")
            outputs = [base / "file" / "vault"]
            if hasattr(os, "geteuid") and os.geteuid() != 0:
                locked = base / "locked"
                locked.mkdir(mode=0o500)
                outputs.append(locked / "vault")
            for output in outputs:
                with self.subTest(output=output):
                    result = self.cli(base, "--username", "", "--transcripts", str(source), "--output", str(output))
                    self.assertEqual(2, result.returncode)
                    self.assertEqual(1, len(result.stderr.splitlines()), result.stderr)
                    self.assertIn(str(base), result.stderr)
                    self.assertEqual("", result.stdout)

    def test_missing_or_empty_transcripts_fail_instead_of_building_an_empty_vault(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            (base / "empty").mkdir()
            cases = (
                (("--transcripts", str(base / "nope")), "transcript folder not found"),
                (("--transcripts", str(base / "empty")), "no transcripts (*.jsonl) found in"),
                ((), "no transcripts found in ~/.claude/projects or ~/.codex/sessions"),
            )
            for argv, message in cases:
                with self.subTest(argv=argv):
                    result = self.cli(base, "--username", "", "--output", str(base / "vault"), *argv)
                    self.assertEqual(2, result.returncode)
                    self.assertIn(message, result.stderr)
                    self.assertEqual("", result.stdout)
            self.assertFalse((base / "vault").exists())

    def test_version_flag_prints_the_coldvault_version(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            result = self.cli(Path(temporary), "--version")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertRegex(vault_kit.__version__, r"^\d+\.\d+\.\d+$")
        self.assertEqual(f"coldvault {vault_kit.__version__}\n", result.stdout)


class PackagingTests(unittest.TestCase):
    def test_package_installs_a_coldvault_command_with_the_module_version(self):
        pyproject = (Path(vault_kit.__file__).resolve().parent / "pyproject.toml").read_text(encoding="utf-8")
        self.assertRegex(pyproject, r'(?m)^name = "coldvault"$')
        self.assertRegex(pyproject, r'(?m)^dynamic = \["version"\]$')
        self.assertRegex(pyproject, r'(?m)^version = \{attr = "vault_kit.__version__"\}$')
        self.assertRegex(pyproject, r'(?m)^py-modules = \["vault_kit"\]$')
        self.assertRegex(pyproject, r'(?ms)^\[project\.scripts\]\ncoldvault = "vault_kit:main"$')


class ManagedMarkerTests(unittest.TestCase):
    def legacy_note(self, body):
        unhashed = f"---\nvault-kit-managed: true\n---\n\n{body}"
        lines = unhashed.split("\n")
        lines.insert(2, "vault-kit-hash: " + hashlib.sha256(unhashed.encode("utf-8")).hexdigest())
        return "\n".join(lines)

    def rebuild(self, source, output):
        with patch.object(vault_kit, "github_repos", return_value=[]), contextlib.redirect_stdout(io.StringIO()):
            vault_kit.build(source, output)

    def test_new_notes_use_the_coldvault_marker(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            source, output = base / "input", base / "vault"
            source.mkdir()
            record = {"type": "user", "cwd": "/work/demo", "message": {"role": "user", "content": "Synthetic"}}
            (source / "session.jsonl").write_text(json.dumps(record), encoding="utf-8")
            self.rebuild(source, output)
            notes = list(output.rglob("*.md"))
            self.assertTrue(notes)
            for note in notes:
                text = note.read_text(encoding="utf-8")
                self.assertTrue(text.startswith("---\ncoldvault-managed: true\ncoldvault-hash: "), note)
                self.assertNotIn("vault-kit", text, note)

    def test_old_marker_notes_are_still_managed_and_refreshed(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            source, output = base / "input", base / "vault"
            source.mkdir()
            output.mkdir()
            (output / "Index.md").write_text("my own index\n", encoding="utf-8")
            record = {"type": "user", "cwd": "/work/demo", "message": {"role": "user", "content": "Synthetic"}}
            (source / "session.jsonl").write_text(json.dumps(record), encoding="utf-8")
            self.rebuild(source, output)
            session = next((output / "Sessions").glob("*.md"))
            old_copy = output / "Index (Vault Kit).md"
            for note in (session, old_copy):
                note.write_text(self.legacy_note("stale generated content\n"), encoding="utf-8")
                self.assertTrue(vault_kit._is_managed_note(note))
            (output / "Index (Coldvault).md").unlink(missing_ok=True)

            self.rebuild(source, output)

            self.assertEqual("my own index\n", (output / "Index.md").read_text(encoding="utf-8"))
            for note in (session, old_copy):
                text = note.read_text(encoding="utf-8")
                self.assertTrue(text.startswith("---\ncoldvault-managed: true\ncoldvault-hash: "), note)
                self.assertNotIn("stale generated content", text)
                self.assertTrue(vault_kit._is_managed_note(note))
            self.assertIn("Synthetic", session.read_text(encoding="utf-8"))
            self.assertEqual([session], list((output / "Sessions").glob("*.md")))
            self.assertFalse((output / "Index (Coldvault).md").exists())
            self.assertIn("[[Index (Vault Kit)]]", (output / "START-HERE.md").read_text(encoding="utf-8"))

    def test_edited_or_mixed_marker_notes_are_not_managed(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            note = Path(temporary) / "note.md"
            legacy = self.legacy_note("body\n")
            cases = {
                "edited legacy": legacy + "my edit\n",
                "new marker, old hash": legacy.replace("vault-kit-managed", "coldvault-managed"),
                "old marker, new hash": legacy.replace("vault-kit-hash", "coldvault-hash"),
                "both markers": legacy.replace("---\n", "---\ncoldvault-managed: true\n", 1),
            }
            for name, text in cases.items():
                with self.subTest(name):
                    note.write_text(text, encoding="utf-8")
                    self.assertFalse(vault_kit._is_managed_note(note))
            note.write_text(legacy, encoding="utf-8")
            self.assertTrue(vault_kit._is_managed_note(note))

    def test_a_kept_old_copy_gets_a_coldvault_copy_beside_it(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            source, output = base / "input", base / "vault"
            source.mkdir()
            output.mkdir()
            (output / "Index.md").write_text("my own index\n", encoding="utf-8")
            (output / "Index (Vault Kit).md").write_text("my edited old copy\n", encoding="utf-8")
            (output / "Index (Vault Kit 2).md").write_text(self.legacy_note("stale\n"), encoding="utf-8")
            record = {"type": "user", "cwd": "/work/demo", "message": {"role": "user", "content": "Synthetic"}}
            (source / "session.jsonl").write_text(json.dumps(record), encoding="utf-8")
            self.rebuild(source, output)
            self.assertEqual("my own index\n", (output / "Index.md").read_text(encoding="utf-8"))
            self.assertEqual("my edited old copy\n", (output / "Index (Vault Kit).md").read_text(encoding="utf-8"))
            self.assertIn("coldvault-managed: true", (output / "Index (Vault Kit 2).md").read_text(encoding="utf-8"))
            self.assertFalse((output / "Index (Coldvault).md").exists())

    def test_a_symlinked_old_copy_is_never_written_through(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            source, output, outside = base / "input", base / "vault", base / "outside.md"
            source.mkdir()
            output.mkdir()
            outside.write_text(self.legacy_note("outside\n"), encoding="utf-8")
            before = outside.read_bytes()
            (output / "Index.md").write_text("my own index\n", encoding="utf-8")
            (output / "Index (Vault Kit).md").symlink_to(outside)
            record = {"type": "user", "cwd": "/work/demo", "message": {"role": "user", "content": "Synthetic"}}
            (source / "session.jsonl").write_text(json.dumps(record), encoding="utf-8")
            self.rebuild(source, output)
            self.assertEqual(before, outside.read_bytes())
            self.assertTrue((output / "Index (Vault Kit).md").is_symlink())
            self.assertIn("coldvault-managed: true", (output / "Index (Coldvault).md").read_text(encoding="utf-8"))



class IdentityTests(unittest.TestCase):
    def write_transcripts(self, source, sessions):
        for relative, cwd, text in sessions:
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            record = {"type": "user", "timestamp": "2026-11-01T10:00:00Z", "cwd": cwd, "message": {"role": "user", "content": text}}
            path.write_text(json.dumps(record), encoding="utf-8")

    def rebuild(self, source, output):
        with patch.object(vault_kit, "github_repos", return_value=[]), contextlib.redirect_stdout(io.StringIO()):
            vault_kit.build(source, output)

    def test_copied_transcript_tree_keeps_the_session_count(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            source, output = base / "first", base / "vault"
            self.write_transcripts(source, [
                ("claude/-work-proja/303e70a2-session.jsonl", "/work/proja", "first session"),
                ("codex/2026/11/01/rollout-2026-11-01-synthetic.jsonl", "/work/proja", "second session"),
                ("claude/-work-projb/session.jsonl", "/work/projb", "third session"),
            ])
            self.rebuild(source, output)
            before = sorted(p.name for p in (output / "Sessions").iterdir())
            copy = base / "elsewhere" / "backup"
            shutil.copytree(source, copy)
            self.rebuild(copy, output)
            self.assertEqual(3, len(before))
            self.assertEqual(before, sorted(p.name for p in (output / "Sessions").iterdir()))

    def test_projects_with_the_same_folder_name_get_separate_notes(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            source, output = base / "input", base / "vault"
            self.write_transcripts(source, [
                ("one/session.jsonl", "/other/claude2/app", "claude two app"),
                ("two/session.jsonl", "/other/claude3/app", "claude three app"),
            ])
            self.rebuild(source, output)
            projects = sorted((output / "Projects").glob("*.md"))
            self.assertEqual(2, len(projects))
            self.assertIn("app.md", [p.name for p in projects])
            for session in (output / "Sessions").glob("*.md"):
                text = session.read_text(encoding="utf-8")
                project = text.split("Project: [[")[1].split("]]")[0]
                linked = [p for p in projects if f"[[Sessions/{session.stem}]]" in p.read_text(encoding="utf-8")]
                self.assertEqual([output / f"{project}.md"], linked)

    def test_many_projects_with_the_same_folder_name_each_get_a_note(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            source, output = base / "input", base / "vault"
            self.write_transcripts(source, [(f"{n}/session.jsonl", f"/work/{n}/app", f"app {n}") for n in range(45)])
            self.rebuild(source, output)
            self.assertEqual(45, len(list((output / "Projects").glob("*.md"))))

    def test_vault_built_with_path_keyed_names_keeps_its_notes_without_duplicates(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            source, output = base / "input", base / "vault"
            self.write_transcripts(source, [
                ("a/session.jsonl", "/other/claude2/app", "claude two app"),
                ("b/session.jsonl", "/other/claude3/app", "claude three app"),
                ("c/session.jsonl", "/work/kept", "edited session"),
            ])
            legacy = {}
            for folder, project in (("a", "app"), ("b", "app"), ("c", "kept")):
                key = vault_kit.hashlib.sha1(str((source / folder / "session.jsonl").resolve()).encode()).hexdigest()[:8]
                legacy[folder] = f"Sessions/2026-11-01-{project}-{key}.md"
                vault_kit.write_note(output, legacy[folder], f"# {project} — 2026-11-01\n\nProject: [[Projects/{project}]]\n\nold text")
            vault_kit.write_note(output, "Projects/app.md", "# app\n\n## Sessions\n\n" + "\n".join(f"- {vault_kit.link(legacy[f])} (2026-11-01)" for f in "ab"))
            edited = (output / legacy["c"]).read_text(encoding="utf-8") + "\nMy own addition\n"
            (output / legacy["c"]).write_text(edited, encoding="utf-8")

            self.rebuild(source, output)
            self.rebuild(source, output)

            sessions = sorted(p.relative_to(output).as_posix() for p in (output / "Sessions").glob("*.md"))
            self.assertNotIn(legacy["a"], sessions)
            self.assertNotIn(legacy["b"], sessions)
            self.assertEqual(edited, (output / legacy["c"]).read_text(encoding="utf-8"))
            self.assertEqual(4, len(sessions))
            self.assertEqual(3, len(list((output / "Projects").glob("*.md"))))
            linked = "\n".join(p.read_text(encoding="utf-8") for p in (output / "Projects").glob("*.md"))
            for note in sessions:
                if note != legacy["c"]:
                    self.assertIn(vault_kit.link(note), linked)
            for text in ("claude two app", "claude three app", "edited session"):
                self.assertEqual(1, sum(text in (output / note).read_text(encoding="utf-8") for note in sessions if note != legacy["c"]))



KIT = Path(__file__).resolve().parents[1]


class ReadmeTests(unittest.TestCase):
    def setUp(self):
        self.readme = (KIT / "README.md").read_text(encoding="utf-8")
        self.lines = self.readme.splitlines()
        self.commands = [line[4:] for line in self.lines if line.startswith("    ")]

    def section(self, heading):
        self.assertTrue(heading in self.lines, f"README has no {heading!r} heading")
        start = self.lines.index(heading)
        end = next((i for i in range(start + 1, len(self.lines)) if self.lines[i].startswith("## ")), len(self.lines))
        return self.lines[start:end]

    def scheduled(self):
        cron = [command for command in self.commands if command.startswith("0 * * * * ")]
        self.assertEqual(1, len(cron), cron)
        start = self.commands.index("cat > ~/Library/LaunchAgents/coldvault.rebuild.plist <<'EOF'")
        end = self.commands.index("EOF", start)
        plist = plistlib.loads("\n".join(self.commands[start + 1:end]).encode("utf-8"))
        return cron[0][len("0 * * * * "):], plist, self.commands[end + 1]

    def test_quickstart_downloads_the_script_with_curl_before_any_git_step(self):
        curl = f"curl -O https://raw.githubusercontent.com/TayHCode/coldvault/main/{Path(vault_kit.__file__).name}"
        self.assertIn(curl, self.commands)
        first_git = next(i for i, command in enumerate(self.commands) if command.startswith("git "))
        self.assertLess(self.commands.index(curl), first_git)
        self.assertEqual("mkdir -p ~/coldvault && cd ~/coldvault", self.commands[self.commands.index(curl) - 1])

    def test_readme_says_how_to_open_the_vault_in_obsidian(self):
        self.assertIn("choose **Open folder as vault**", "\n".join(self.section("## Open it in Obsidian")))

    def test_cron_and_launchd_lines_run_the_same_hourly_rebuild(self):
        command, plist, load = self.scheduled()
        self.assertEqual({"Label", "ProgramArguments", "StartInterval"}, set(plist))
        self.assertEqual(["/bin/sh", "-c", command], plist["ProgramArguments"])
        self.assertEqual(3600, plist["StartInterval"])
        self.assertEqual("launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/coldvault.rebuild.plist", load)
        for verb in ("kickstart", "bootout"):
            self.assertIn(f"`launchctl {verb} gui/$(id -u)/{plist['Label']}`", self.readme)

    @unittest.skipUnless(os.name == "posix" and shutil.which("python3"), "the scheduled line runs python3 under /bin/sh")
    def test_scheduled_command_builds_the_vault_without_a_terminal(self):
        command, _, _ = self.scheduled()
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            home = Path(temporary)
            (home / "coldvault").mkdir()
            shutil.copy2(KIT / "vault_kit.py", home / "coldvault" / "vault_kit.py")
            env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}

            def run():
                result = subprocess.run(["/bin/sh", "-c", command], stdin=subprocess.DEVNULL, capture_output=True,
                                        text=True, env=env, timeout=120)
                self.assertNotIn("Traceback", result.stdout + result.stderr)
                return result

            missing = run()
            self.assertEqual(2, missing.returncode, missing.stderr)
            self.assertIn("no transcripts found in ~/.claude/projects or ~/.codex/sessions", missing.stderr)
            vault = home / "ColdvaultVault"
            self.assertFalse(vault.exists())

            shutil.copytree(KIT / "examples" / "transcripts" / "claude", home / ".claude" / "projects" / "demo")
            shutil.copytree(KIT / "examples" / "transcripts" / "codex", home / ".codex" / "sessions" / "2026")
            listings = []
            for _ in range(2):
                result = run()
                self.assertEqual(0, result.returncode, result.stderr)
                listings.append(sorted(str(p.relative_to(vault)) for p in vault.rglob("*.md")))
            self.assertEqual(listings[0], listings[1])
            self.assertIn("START-HERE.md", listings[0])
            for term in ("checklist", "offline"):
                self.assertTrue(list(vault_kit.hits(str(vault), term)), term)

    def test_sync_recipes_follow_a_privacy_warning(self):
        text = "\n".join(self.section("## Sync it to other devices"))
        self.assertIn("**Privacy warning:**", text)
        warning = text.index("**Privacy warning:**")
        recipes = (
            ("**A private git repository.**", "git push -u origin main"),
            ("**Obsidian Sync.**", "ob sync"),
            ("**iCloud on a Mac.**", "python3 vault_kit.py --username '' --output \"$HOME/Library/Mobile Documents/iCloud~md~obsidian/Documents/Coldvault\""),
            ("**S3 with rclone.**", "rclone sync ~/ColdvaultVault s3:YOUR-BUCKET/coldvault --exclude '.coldvault/**'"),
        )
        for heading, command in recipes:
            with self.subTest(heading=heading):
                self.assertLess(warning, text.index(heading))
                self.assertLess(text.index(heading), text.index(f"\n    {command}\n"))

    def test_release_check_quickstart_lines_match_the_readme(self):
        manifest = json.loads((KIT / "release-check.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["quickstart"])
        for entry in manifest["quickstart"]:
            with self.subTest(entry=entry):
                self.assertEqual("README.md", entry["file"])
                self.assertEqual(entry["argv"], shlex.split(self.lines[entry["line"] - 1]))


if __name__ == "__main__":
    unittest.main()
