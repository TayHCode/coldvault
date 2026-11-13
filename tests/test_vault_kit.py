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
from urllib.error import HTTPError, URLError
from urllib.parse import quote

import coldvault


SCRATCH = Path(__file__).resolve().parents[1] / ".scratch"
SCRATCH.mkdir(exist_ok=True)
TEMPLATE_TOP = [".obsidian", "Agent-Space", "Attachments", "Capabilities", "Context", "Daily", "Home.md", "Templates", "Writing"]
GITHUB_USER_REPOS = "https://api.github.com/" + "users/synthetic-user/repos?per_page=100&sort=updated"


def scan_fold(text):
    return "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))


def scan_has(word, text):
    return re.search(r"(?<![^\W_])" + re.escape(scan_fold(word)), scan_fold(text)) is not None



def notes_and_caches(output):
    receipts = {output / ".coldvault" / "last-run.json", output / ".coldvault" / "runs.jsonl"}
    return {p: p.read_bytes() for p in output.rglob("*") if p.is_file() and p not in receipts}

class VaultKitTests(unittest.TestCase):
    def test_imports_conversation_and_skips_tool_outputs(self):
        self.assertEqual("hello", coldvault.conversation({
            "type": "user", "message": {"role": "user", "content": "hello"}
        }))
        self.assertEqual("visible", coldvault.conversation({
            "type": "response_item", "payload": {
                "type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": "visible"},
                    {"type": "tool_output", "text": "hidden"},
                ]
            }
        }))
        self.assertIsNone(coldvault.conversation({
            "type": "response_item", "payload": {"type": "function_call", "name": "private"}
        }))

    def test_a_registered_source_adds_notes_and_reports_its_count(self):
        def bookmarks(run):
            return [("Imported/Bookmarks/reading.md", "# Reading\n\nSynthetic bookmark", {})]

        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            output = Path(temporary) / "vault"
            with patch.object(coldvault, "SOURCES", [*coldvault.SOURCES, ("bookmarks", bookmarks)]), contextlib.redirect_stdout(io.StringIO()):
                sources = coldvault.build_from_files([], output, "")
            self.assertEqual({"state": "ok", "count": 1}, sources["bookmarks"])
            self.assertEqual("skipped", sources["transcripts"]["state"])
            self.assertIn("Synthetic bookmark", (output / "Imported" / "Bookmarks" / "reading.md").read_text())
            self.assertIn("[[Imported/Bookmarks/reading]]", (output / "Index.md").read_text())

    def test_a_run_without_transcripts_forgets_cached_transcripts(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            source, output = base / "input", base / "vault"
            source.mkdir()
            (source / "session.jsonl").write_text(json.dumps({"type": "user", "cwd": "/work/demo", "message": {"role": "user", "content": "Cached question"}}), encoding="utf-8")
            with patch.object(coldvault, "github_repos", return_value=[]), contextlib.redirect_stdout(io.StringIO()):
                coldvault.build(source, output)
                self.assertIn("Cached question", (output / ".coldvault" / "transcripts.json").read_text())
                sources = coldvault.build_from_files([], output, "")
            self.assertEqual({"state": "skipped"}, sources["transcripts"])
            self.assertEqual({}, json.loads((output / ".coldvault" / "transcripts.json").read_text())["files"])

    def test_a_source_that_fails_while_listing_is_recorded_as_failed(self):
        def broken(run):
            yield ("Imported/Broken/first.md", "first", {})
            raise coldvault.SourceError("synthetic outage")

        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            output = Path(temporary) / "vault"
            with patch.object(coldvault, "SOURCES", [*coldvault.SOURCES, ("broken", broken)]), contextlib.redirect_stdout(io.StringIO()):
                sources = coldvault.build_from_files([], output, "")
            self.assertEqual({"state": "failed", "reason": "synthetic outage"}, sources["broken"])
            self.assertFalse((output / "Imported" / "Broken").exists())

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
            with patch.object(coldvault, "github_repos", return_value=[]):
                coldvault.build(source, output)
            self.assertTrue((output / "START-HERE.md").is_file())
            self.assertIn("[[Index]]", (output / "START-HERE.md").read_text())
            self.assertIn("coldvault.py search TERM --vault DIR", (output / "START-HERE.md").read_text())
            self.assertIn("[[Projects/demo]]", (output / "Index.md").read_text())
            project = (output / "Projects" / "demo.md").read_text()
            self.assertIn("[[Sessions/2026-02-03-demo-", project)
            session = next((output / "Sessions").glob("*.md")).read_text()
            self.assertIn("[[Projects/demo]]", session)
            self.assertIn("Synthetic question", session)
            self.assertIn("Synthetic answer", session)
            self.assertNotIn("Synthetic tool output", session)
            self.assertEqual(sorted([".coldvault", "Index.md", "Projects", "START-HERE.md", "Sessions", *TEMPLATE_TOP]), sorted(p.name for p in output.iterdir()))

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

            with patch.object(coldvault, "github_repos", return_value=[]):
                coldvault.build(source, output)
                generated_index = output / "Index (Vault Kit).md"
                stale = "---\nvault-kit-managed: true\n---\n\nstale generated content\n"
                generated_index.write_text(stale.replace("true\n", f"true\nvault-kit-hash: {coldvault._digest(stale)}\n", 1), encoding="utf-8")
                coldvault.build(source, output)

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
            with patch.object(coldvault, "github_repos", return_value=[]):
                coldvault.build(source, output)
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
                with patch.object(coldvault, "github_repos", return_value=[]):
                    coldvault.build(source, output)
            index = (output / "Index.md").read_text()
            targets = [line.split("[[")[1].split("]]")[0] for line in index.splitlines() if line.startswith("- [[")]
            self.assertEqual(2, len(set(targets)))
            self.assertEqual(2, len(list((output / "Projects").glob("*.md"))))
            for session in (output / "Sessions").glob("*.md"):
                text = session.read_text()
                project = text.split("Project: [[")[1].split("]]")[0]
                self.assertIn(f"[[Sessions/{session.stem}]]", (output / f"{project}.md").read_text())

    def test_hash_suffixed_slug_never_reuses_a_taken_slug(self):
        suffix = coldvault.hashlib.sha1("scratch".encode()).hexdigest()[:8]
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            source, output = base / "input", base / "vault"
            source.mkdir()
            for number, name in enumerate([".scratch", f".scratch-{suffix}", "scratch"]):
                record = {"type": "user", "timestamp": "2026-02-03T10:00:00Z", "cwd": f"/work/{name}", "message": {"role": "user", "content": f"question {number}"}}
                (source / f"{number}.jsonl").write_text(json.dumps(record), encoding="utf-8")
            with patch.object(coldvault, "github_repos", return_value=[]):
                coldvault.build(source, output)
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
            with patch.object(coldvault, "github_repos", return_value=[]):
                coldvault.build(source, output)
                transcript.write_text(json.dumps(first) + "\n" + json.dumps(second), encoding="utf-8")
                coldvault.build(source, output)
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
            with patch.object(coldvault, "github_repos", return_value=[]):
                coldvault.build(source, output)
                managed = next((output / "Sessions").glob("*.md"))
                managed.write_text("my own edit", encoding="utf-8")
                coldvault.build(source, output)
            self.assertEqual("my own edit", managed.read_text())
            continued = managed.with_name(f"{managed.stem} (continued).md")
            self.assertEqual(sorted([managed.name, continued.name]), sorted(p.name for p in (output / "Sessions").iterdir()))
            self.assertIn("Synthetic", continued.read_text())

    def test_search_finds_terms_in_built_vault(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            source, output = base / "input", base / "vault"
            source.mkdir()
            record = {"type": "user", "cwd": "/work/demo", "message": {"role": "user", "content": "Findable Needle here"}}
            (source / "session.jsonl").write_text(json.dumps(record), encoding="utf-8")
            with patch.object(coldvault, "github_repos", return_value=[]):
                coldvault.build(source, output)
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                self.assertEqual(1, coldvault.search(output, "needle"))
                self.assertEqual(0, coldvault.search(output, "absent-term"))
            self.assertIn("Findable Needle here", captured.getvalue())
            with patch.object(sys, "argv", ["coldvault.py", "search", "needle", "--vault", str(output)]):
                with contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        coldvault.main()
            self.assertEqual(0, raised.exception.code)

    def test_search_json_lists_path_line_and_text_and_exits_1_on_no_hits(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            vault = Path(temporary) / "vault"
            (vault / "Notes").mkdir(parents=True)
            (vault / "Notes" / "a.md").write_text("intro\n  Findable needle here  \n", encoding="utf-8")

            def run(term):
                argv = ["coldvault.py", "search", term, "--vault", str(vault), "--json"]
                with patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()) as printed:
                    with self.assertRaises(SystemExit) as raised:
                        coldvault.main()
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
                coldvault.write_note(output, "../outside.md", "no")
            self.assertFalse((base / "outside.md").exists())

    def test_empty_username_never_calls_github(self):
        with patch.object(coldvault, "urlopen", side_effect=AssertionError("network called")):
            self.assertEqual([], coldvault.github_repos(""))

    def test_github_failure_is_reported(self):
        with patch.object(coldvault, "urlopen", side_effect=URLError("offline")):
            with self.assertRaisesRegex(coldvault.SourceError, "offline"):
                coldvault.github_repos("synthetic-user")

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
        with patch.dict(os.environ, {}, clear=True), patch.object(coldvault, "urlopen", side_effect=respond):
            repos = coldvault.github_repos("synthetic-user")
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
            argv = ["coldvault.py", "--username", "synthetic-user", "--transcripts", str(base / "input"), "--output", str(base / "vault")]
            out, err = io.StringIO(), io.StringIO()
            with patch.dict(os.environ, {"GITHUB_TOKEN": token}), patch.object(sys, "argv", argv), \
                    patch.object(coldvault, "urlopen", side_effect=respond), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                coldvault.main()
            self.assertEqual(2, len(requests))
            self.assertTrue(all(request.get_header("Authorization") == f"Bearer {token}" for request in requests))
            self.assertTrue((base / "vault" / "Projects" / "repo-149.md").is_file())
            self.assertNotIn(token, out.getvalue() + err.getvalue())
            self.assertFalse([p for p in (base / "vault").rglob("*") if p.is_file() and token in p.read_text(encoding="utf-8")])

    def test_github_token_is_never_sent_off_the_api_host(self):
        first = GITHUB_USER_REPOS
        elsewhere = "https://example.invalid/steal?page=2"
        requests, respond = self.fake_github({first: ([{"name": "only"}], f'<{elsewhere}>; rel="next"')})
        with patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_" + "S" * 36}), patch.object(coldvault, "urlopen", side_effect=respond):
            self.assertEqual(["only"], [repo["name"] for repo in coldvault.github_repos("synthetic-user")])
        self.assertEqual([first], [request.full_url for request in requests])

    def test_github_token_with_trailing_newline_is_trimmed(self):
        token = "ghp_" + "S" * 36
        requests, respond = self.fake_github(self.two_page_api())
        with patch.dict(os.environ, {"GITHUB_TOKEN": token + "\n"}), patch.object(coldvault, "urlopen", side_effect=respond):
            self.assertEqual(150, len(coldvault.github_repos("synthetic-user")))
        self.assertTrue(all(request.get_header("Authorization") == f"Bearer {token}" for request in requests))

    def test_malformed_github_token_never_reaches_error_output(self):
        token = "ghp_" + "S" * 18 + "\n" + "S" * 18
        with patch.dict(os.environ, {"GITHUB_TOKEN": token}), \
                patch("socket.create_connection", side_effect=AssertionError("network called")), \
                self.assertRaisesRegex(coldvault.SourceError, "GITHUB_TOKEN contains spaces") as raised:
            coldvault.github_repos("synthetic-user")
        self.assertNotIn("S" * 18, str(raised.exception))

    def test_default_finds_both_tools_standard_folders(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            for folder in (home / ".claude" / "projects", home / ".codex" / "sessions", home / ".codex" / "archived_sessions"):
                folder.mkdir(parents=True)
                record = {"type": "user", "cwd": f"/work/{folder.name}", "message": {"role": "user", "content": "Synthetic"}}
                (folder / "session.jsonl").write_text(json.dumps(record), encoding="utf-8")
            with patch.object(Path, "home", return_value=home), patch.object(sys, "argv", ["coldvault.py", "--username", "synthetic-user", "--output", str(home / "vault")]), patch.object(coldvault, "github_repos", return_value=[]):
                with contextlib.redirect_stdout(io.StringIO()):
                    coldvault.main()
            notes = list((home / "vault" / "Sessions").glob("*.md"))
            self.assertEqual(3, len(notes))


    def test_sessions_stay_linked_after_their_transcript_is_deleted(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            source, output = self.build_vault(Path(temporary), "kept question", "live question")
            first = {note.read_text(encoding="utf-8"): note for note in (output / "Sessions").glob("*.md")}
            gone = next(note for text, note in first.items() if "kept question" in text)
            snapshot = gone.read_bytes()
            (source / "session0.jsonl").unlink()
            with patch.object(coldvault, "github_repos", return_value=[]), contextlib.redirect_stdout(io.StringIO()):
                coldvault.build(source, output)
            self.assertEqual(snapshot, gone.read_bytes())
            index = (output / "Index.md").read_text(encoding="utf-8")
            for number in range(2):
                project = output / "Projects" / f"demo{number}.md"
                self.assertIn(f"[[Projects/demo{number}]]", index)
                self.assertEqual(1, len(re.findall(r"\[\[Sessions/", project.read_text(encoding="utf-8"))))
            self.assertIn(f"[[{gone.relative_to(output).with_suffix('').as_posix()}]]", (output / "Projects" / "demo0.md").read_text(encoding="utf-8"))
            self.assertEqual(2, len(list((output / "Sessions").glob("*.md"))))

    def test_a_kept_session_stays_linked_when_another_project_reuses_its_file_name(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            source, output = base / "input", base / "vault"
            for project in ("alpha", "beta"):
                (source / project).mkdir(parents=True)
                record = {"type": "user", "cwd": f"/work/{project}", "message": {"role": "user", "content": f"{project} question"}}
                (source / project / "session.jsonl").write_text(json.dumps(record), encoding="utf-8")
            with patch.object(coldvault, "github_repos", return_value=[]), contextlib.redirect_stdout(io.StringIO()):
                coldvault.build(source, output)
                (source / "alpha" / "session.jsonl").unlink()
                coldvault.build(source, output)
            self.assertIn("[[Projects/alpha]]", (output / "Index.md").read_text(encoding="utf-8"))
            self.assertEqual(1, len(re.findall(r"\[\[Sessions/", (output / "Projects" / "alpha.md").read_text(encoding="utf-8"))))

    def test_a_kept_note_from_an_older_version_joins_its_project_when_it_reappears(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            source, output = self.build_vault(Path(temporary), "older question")
            old = next((output / "Sessions").glob("*.md"))
            text = old.read_text(encoding="utf-8")
            lines = [line for line in text.split("\n") if not line.startswith(("project-key: ", "coldvault-hash: "))]
            lines.insert(2, "coldvault-hash: " + hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest())
            old.write_text("\n".join(lines), encoding="utf-8")
            (source / "session0.jsonl").unlink()

            def build():
                with patch.object(coldvault, "github_repos", return_value=[]), contextlib.redirect_stdout(io.StringIO()):
                    coldvault.build_from_files(sorted(source.rglob("*.jsonl")), output, "")

            build()
            self.assertIn("[[Projects/demo0]]", (output / "Index.md").read_text(encoding="utf-8"))
            record = {"type": "user", "cwd": "/work/demo0", "message": {"role": "user", "content": "newer question"}}
            (source / "later.jsonl").write_text(json.dumps(record), encoding="utf-8")
            build()
            self.assertEqual(["demo0.md"], sorted(note.name for note in (output / "Projects").glob("*.md")))
            self.assertEqual(2, len(re.findall(r"\[\[Sessions/", (output / "Projects" / "demo0.md").read_text(encoding="utf-8"))))

    def test_a_kept_session_stays_linked_beside_a_live_session_with_the_same_file_name(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            source, output = base / "input", base / "vault"
            for folder in ("a", "b"):
                (source / folder).mkdir(parents=True)
                record = {"type": "user", "cwd": "/work/shared", "message": {"role": "user", "content": f"{folder} question"}}
                (source / folder / "session.jsonl").write_text(json.dumps(record), encoding="utf-8")
            with patch.object(coldvault, "github_repos", return_value=[]), contextlib.redirect_stdout(io.StringIO()):
                coldvault.build(source, output)
                (source / "b" / "session.jsonl").unlink()
                coldvault.build(source, output)
            self.assertEqual(2, len(re.findall(r"\[\[Sessions/", (output / "Projects" / "shared.md").read_text(encoding="utf-8"))))

    def test_kept_session_notes_join_live_sessions_of_the_same_project(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            source, output = base / "input", base / "vault"
            source.mkdir()
            for name, text in (("a", "older question"), ("b", "newer question")):
                record = {"type": "user", "sessionId": name, "cwd": "/work/shared", "message": {"role": "user", "content": text}}
                (source / f"{name}.jsonl").write_text(json.dumps(record), encoding="utf-8")

            def build():
                with patch.object(coldvault, "github_repos", return_value=[]), contextlib.redirect_stdout(io.StringIO()):
                    coldvault.build(source, output)

            build()
            (source / "a.jsonl").unlink()
            build()
            self.assertEqual(["shared.md"], sorted(note.name for note in (output / "Projects").glob("*.md")))
            self.assertEqual(2, len(re.findall(r"\[\[Sessions/", (output / "Projects" / "shared.md").read_text(encoding="utf-8"))))

    def build_vault(self, base, *texts):
        source, output = base / "input", base / "vault"
        source.mkdir()
        for number, text in enumerate(texts):
            record = {"type": "user", "cwd": f"/work/demo{number}", "message": {"role": "user", "content": text}}
            (source / f"session{number}.jsonl").write_text(json.dumps(record), encoding="utf-8")
        with patch.object(coldvault, "github_repos", return_value=[]), contextlib.redirect_stdout(io.StringIO()):
            coldvault.build(source, output)
        return source, output

    def rebuild_and_list_rewritten(self, source, output, notes=()):
        for note in output.rglob("*.md"):
            os.utime(note, ns=(10**18, 10**18))
        with patch.object(coldvault, "github_repos", return_value=[]), contextlib.redirect_stdout(io.StringIO()):
            coldvault.build_from_files(sorted(source.rglob("*.jsonl")), output, "", notes)
        return sorted(note.relative_to(output).as_posix() for note in output.rglob("*.md") if note.stat().st_mtime_ns != 10**18)

    def test_rerun_on_unchanged_inputs_rewrites_no_note_and_parses_no_transcript(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            notes = base / "notes"
            notes.mkdir()
            (notes / "Plan.md").write_text("# Plan\n", encoding="utf-8")
            source, output = self.build_vault(base, "first", "second")
            with patch.object(coldvault, "github_repos", return_value=[]), contextlib.redirect_stdout(io.StringIO()):
                coldvault.build_from_files(sorted(source.rglob("*.jsonl")), output, "", [notes])
            before = {note: note.read_bytes() for note in output.rglob("*.md")}
            self.assertEqual(8 + sum(name.endswith(".md") for name in coldvault.VAULT_TEMPLATE), len(before))
            with patch.object(coldvault, "transcript_info", side_effect=AssertionError("re-parsed an unchanged transcript")):
                self.assertEqual([], self.rebuild_and_list_rewritten(source, output, [notes]))
            self.assertEqual(before, {note: note.read_bytes() for note in output.rglob("*.md")})

    def test_grown_transcript_rewrites_only_its_session_note(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            source, output = self.build_vault(Path(temporary), "first", "second")
            grown = source / "session0.jsonl"
            record = {"type": "assistant", "message": {"role": "assistant", "content": "grown reply"}}
            grown.write_text(grown.read_text(encoding="utf-8") + "\n" + json.dumps(record), encoding="utf-8")
            rewritten = self.rebuild_and_list_rewritten(source, output)
            self.assertEqual(1, len(rewritten))
            self.assertTrue(rewritten[0].startswith("Sessions/undated-demo0-"))
            self.assertIn("grown reply", (output / rewritten[0]).read_text(encoding="utf-8"))
            self.assertIn(f"[[{rewritten[0].removesuffix('.md')}]]", (output / "Projects" / "demo0.md").read_text(encoding="utf-8"))

    def test_corrupt_transcript_cache_is_ignored(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            source, output = self.build_vault(Path(temporary), "first")
            for broken in ("not json", '{"tag": "x", "files": {}}', json.dumps({"tag": coldvault._CACHE_TAG, "files": {"a": ["s", "text"]}})):
                (output / ".coldvault" / "transcripts.json").write_text(broken, encoding="utf-8")
                self.assertEqual([], self.rebuild_and_list_rewritten(source, output))
                self.assertEqual(coldvault._CACHE_TAG, json.loads((output / ".coldvault" / "transcripts.json").read_text(encoding="utf-8"))["tag"])

    def test_build_never_writes_its_transcript_cache_through_a_symlink(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            source, output = self.build_vault(base, "first")
            outside = base / "outside"
            outside.mkdir()
            shutil.rmtree(output / ".coldvault")
            (output / ".coldvault").symlink_to(outside, target_is_directory=True)
            (source / "session0.jsonl").write_text(json.dumps({"type": "user", "cwd": "/work/demo0", "message": {"role": "user", "content": "changed"}}), encoding="utf-8")
            self.assertEqual(1, len(self.rebuild_and_list_rewritten(source, output)))
            self.assertEqual([], list(outside.iterdir()))

    def test_unwritable_transcript_cache_does_not_stop_the_build(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            source, output = self.build_vault(Path(temporary), "first")
            cache = output / ".coldvault" / "transcripts.json"
            cache.unlink()
            cache.mkdir()
            (source / "session0.jsonl").write_text(json.dumps({"type": "user", "cwd": "/work/demo0", "message": {"role": "user", "content": "changed"}}), encoding="utf-8")
            rewritten = self.rebuild_and_list_rewritten(source, output)
            self.assertEqual(1, len(rewritten))
            self.assertIn("changed", (output / rewritten[0]).read_text(encoding="utf-8"))
            self.assertTrue(cache.is_dir())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs named pipes")
    def test_build_never_opens_a_named_pipe_at_a_note_path(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            source, output = self.build_vault(Path(temporary), "first")
            session = next((output / "Sessions").glob("*.md"))
            session.unlink()
            os.mkfifo(session)
            self.rebuild_and_list_rewritten(source, output)
            self.assertFalse(session.is_file())
            self.assertEqual(1, len(list((output / "Sessions").glob("* (continued).md"))))

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs named pipes")
    def test_build_never_opens_a_named_pipe_as_its_transcript_cache(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            source, output = self.build_vault(Path(temporary), "first")
            cache = output / ".coldvault" / "transcripts.json"
            cache.unlink()
            os.mkfifo(cache)
            self.assertEqual([], self.rebuild_and_list_rewritten(source, output))

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
        self.assertIn(label + " = " + coldvault.REDACTED, session)
        self.assertIn(kept, session)
        self.assertIn(label + " =\nplease continue", coldvault.redact(label + " =\nplease continue"))

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
        self.assertIn("Authorization: Bearer " + coldvault.REDACTED, session)
        self.assertIn("postgres://admin:" + coldvault.REDACTED + "@db.example.test", session)
        self.assertIn("https://ops:" + coldvault.REDACTED + "@host.example.test?email=" + email, session)
        self.assertIn(kept, session)

    def test_edited_managed_note_is_kept_and_linked_without_a_copy(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            source, output = self.build_vault(Path(temporary), "Synthetic")
            note = next((output / "Sessions").glob("*.md"))
            edited = note.read_text(encoding="utf-8") + "\nMy own addition\n"
            note.write_text(edited, encoding="utf-8")
            with patch.object(coldvault, "github_repos", return_value=[]), contextlib.redirect_stdout(io.StringIO()):
                coldvault.build(source, output)
            self.assertEqual(edited, note.read_text(encoding="utf-8"))
            self.assertEqual([note.name], [p.name for p in (output / "Sessions").iterdir()])
            self.assertIn(f"- [[Sessions/{note.stem}]]", (output / "Projects" / "demo0.md").read_text(encoding="utf-8"))
            self.assertEqual([], list(output.rglob(".coldvault-*")))

    def test_edited_session_note_is_frozen_and_new_messages_go_to_one_continuation(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            source, output = base / "input", base / "vault"
            source.mkdir()
            transcript = source / "session.jsonl"

            def say(*texts):
                with transcript.open("a", encoding="utf-8") as stream:
                    for text in texts:
                        stream.write(json.dumps({"type": "user", "timestamp": "2026-02-03T10:00:00Z", "cwd": "/work/demo", "message": {"role": "user", "content": text}}) + "\n")
                with patch.object(coldvault, "github_repos", return_value=[]), contextlib.redirect_stdout(io.StringIO()):
                    coldvault.build(source, output)

            say("First question", "First answer")
            note = next((output / "Sessions").glob("*.md"))
            mine = note.read_text(encoding="utf-8").replace("First answer", "First answer, which I rewrote") + "\nMy own summary\n"
            note.write_text(mine, encoding="utf-8")
            say("Second question")
            say("Third question")

            self.assertEqual(mine, note.read_text(encoding="utf-8"))
            self.assertEqual(sorted([note.name, f"{note.stem} (continued).md"]), sorted(p.name for p in (output / "Sessions").iterdir()))
            continued = (output / "Sessions" / f"{note.stem} (continued).md").read_text(encoding="utf-8")
            self.assertIn(f"Continues [[Sessions/{note.stem}]]", continued)
            self.assertIn("Second question\n\nThird question", continued)
            self.assertNotIn("First", continued)
            project = (output / "Projects" / "demo.md").read_text(encoding="utf-8")
            self.assertIn(f"- [[Sessions/{note.stem}]] (2026-02-03) · [[Sessions/{note.stem} (continued)]]", project)
            self.assertIn(f"[[Sessions/{note.stem}]]", (output / "Daily" / "2026-02-03.md").read_text(encoding="utf-8"))
            transcript.write_text("".join(transcript.read_text(encoding="utf-8").splitlines(keepends=True)[:2]), encoding="utf-8")
            with patch.object(coldvault, "github_repos", return_value=[]), contextlib.redirect_stdout(io.StringIO()):
                coldvault.build(source, output)
            self.assertEqual(continued, (output / "Sessions" / f"{note.stem} (continued).md").read_text(encoding="utf-8"))
            for text in ("First question", "Third question"):
                self.assertEqual(1, sum(text in p.read_text(encoding="utf-8") for p in (output / "Sessions").iterdir()), text)

    def test_an_edited_session_and_its_continuation_stay_linked_and_untouched_after_the_transcript_is_deleted(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            source, output = base / "input", base / "vault"
            source.mkdir()
            transcript = source / "session.jsonl"

            def say(*texts):
                with transcript.open("a", encoding="utf-8") as stream:
                    for text in texts:
                        stream.write(json.dumps({"type": "user", "timestamp": "2026-02-03T10:00:00Z", "cwd": "/work/demo", "message": {"role": "user", "content": text}}) + "\n")
                build()

            def build():
                with patch.object(coldvault, "github_repos", return_value=[]), contextlib.redirect_stdout(io.StringIO()):
                    coldvault.build(source, output)

            say("First question")
            note = next((output / "Sessions").glob("*.md"))
            generated = note.read_text(encoding="utf-8")
            mine = generated + "\nMy own summary\n"
            note.write_text(mine, encoding="utf-8")
            say("Second question")
            continued = note.with_name(f"{note.stem} (continued).md")
            later = continued.read_text(encoding="utf-8")
            transcript.unlink()
            build()
            self.assertEqual(mine, note.read_text(encoding="utf-8"))
            self.assertEqual(later, continued.read_text(encoding="utf-8"))
            self.assertIn("[[Projects/demo]]", (output / "Index.md").read_text(encoding="utf-8"))
            self.assertIn(f"- [[Sessions/{note.stem}]] (2026-02-03) · [[Sessions/{continued.stem}]]", (output / "Projects" / "demo.md").read_text(encoding="utf-8"))
            note.write_text(generated, encoding="utf-8")
            build()
            self.assertEqual(later, continued.read_text(encoding="utf-8"))

    def test_older_edited_note_without_paragraphs_never_skips_a_new_message(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            source, output = self.build_vault(Path(temporary), "First")
            note = next((output / "Sessions").glob("*.md"))
            note.write_text(re.sub(r"(?m)^paragraphs: .*\n", "", note.read_text(encoding="utf-8")) + "\nDone\n", encoding="utf-8")
            with (source / "session0.jsonl").open("a", encoding="utf-8") as stream:
                for text in ("New question", "Done"):
                    stream.write("\n" + json.dumps({"type": "user", "cwd": "/work/demo0", "message": {"role": "user", "content": text}}))
            with patch.object(coldvault, "github_repos", return_value=[]), contextlib.redirect_stdout(io.StringIO()):
                coldvault.build(source, output)
            continued = note.with_name(f"{note.stem} (continued).md")
            self.assertEqual(sorted([note.name, continued.name]), sorted(p.name for p in (output / "Sessions").iterdir()))
            self.assertTrue(continued.read_text(encoding="utf-8").endswith("\n\nNew question\n\nDone\n"))

    def test_old_copy_of_an_edited_session_note_is_retired_and_older_notes_still_continue(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            source, output = self.build_vault(Path(temporary), "Synthetic")
            note = next((output / "Sessions").glob("*.md"))
            copy = note.with_name(f"{note.stem} (Coldvault).md")
            copy.write_text(coldvault._render("old full copy\n"), encoding="utf-8")
            note.with_name(f"{note.stem} (Coldvault 2).md").write_text(coldvault._render("older full copy\n"), encoding="utf-8")
            mine = re.sub(r"(?m)^paragraphs: .*$", "paragraphs: 0", note.read_text(encoding="utf-8")) + "\nMy own addition\n"
            note.write_text(mine, encoding="utf-8")
            with patch.object(coldvault, "github_repos", return_value=[]), contextlib.redirect_stdout(io.StringIO()):
                coldvault.build(source, output)
            self.assertEqual(mine, note.read_text(encoding="utf-8"))
            self.assertEqual([note.name], [p.name for p in (output / "Sessions").iterdir()])
            with (source / "session0.jsonl").open("a", encoding="utf-8") as stream:
                stream.write("\n" + json.dumps({"type": "user", "cwd": "/work/demo0", "message": {"role": "user", "content": "Later"}}))
            with patch.object(coldvault, "github_repos", return_value=[]), contextlib.redirect_stdout(io.StringIO()):
                coldvault.build(source, output)
            continued = note.with_name(f"{note.stem} (continued).md")
            self.assertEqual(sorted([note.name, continued.name]), sorted(p.name for p in (output / "Sessions").iterdir()))
            self.assertTrue(continued.read_text(encoding="utf-8").endswith("\n\nLater\n"))
            self.assertNotIn("Synthetic", continued.read_text(encoding="utf-8"))

    def test_notes_vault_kit_cannot_verify_are_kept(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            source, output = self.build_vault(Path(temporary), "Synthetic")
            note = next((output / "Sessions").glob("*.md"))
            original = note.read_text(encoding="utf-8")
            legacy = "---\nvault-kit-managed: true\n---\n\nMy edit to an older note\n"
            header_edit = original.replace("---\n\n", "tags: mine\n---\n\n", 1)
            for edited in (legacy, header_edit):
                note.write_text(edited, encoding="utf-8")
                with patch.object(coldvault, "github_repos", return_value=[]), contextlib.redirect_stdout(io.StringIO()):
                    coldvault.build(source, output)
                self.assertEqual(edited, note.read_text(encoding="utf-8"))

    def test_search_requires_every_word_and_ranks_the_best_note_first(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            _, output = self.build_vault(Path(temporary), "alpha only", "alpha beta once", "beta alpha beta alpha beta", "un caf\u00e9", "die Stra\u00dfe", "foo bar baz", "\u03ba\u03cc\u03c3\u03bc\u03bf\u03c2")
            for ranked in (True, False):
                captured = io.StringIO()
                with contextlib.redirect_stdout(captured):
                    if ranked:
                        found = coldvault.search(output, "beta alp")
                    else:
                        with patch.object(coldvault, "_ranked", return_value=None):
                            found = coldvault.search(output, "beta alp")
                lines = captured.getvalue().splitlines()
                with contextlib.redirect_stdout(io.StringIO()) as printed:
                    if ranked:
                        hits = [coldvault.search(output, w) for w in ("foo-bar baz", "--")]
                    else:
                        with patch.object(coldvault, "_ranked", return_value=None):
                            hits = [coldvault.search(output, w) for w in ("foo-bar baz", "--")]
                self.assertEqual([1, 0], hits)
                self.assertIn("foo bar baz", printed.getvalue())
                self.assertEqual(2, found)
                self.assertFalse(any("alpha only" in line for line in lines))
                if ranked:
                    self.assertIn("beta alpha beta alpha beta", lines[0])
                with contextlib.redirect_stdout(io.StringIO()):
                    if ranked:
                        self.assertEqual([1, 0], [coldvault.search(output, w) for w in ("cafe", "strasse")])
                        self.assertEqual(1, coldvault.search(output, "\u03ba\u03bf\u03c3\u03bc\u03bf\u03c2"))
                    else:
                        with patch.object(coldvault, "_ranked", return_value=None):
                            self.assertEqual([1, 0], [coldvault.search(output, w) for w in ("cafe", "strasse")])
                with contextlib.redirect_stdout(io.StringIO()):
                    if ranked:
                        self.assertEqual(0, coldvault.search(output, "pha"))
                    else:
                        with patch.object(coldvault, "_ranked", return_value=None):
                            self.assertEqual(0, coldvault.search(output, "pha"))

    def test_search_works_without_sqlite(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            _, output = self.build_vault(Path(temporary), "alpha beta")
            with patch.dict(sys.modules, {"sqlite3": None}):
                spec = importlib.util.spec_from_file_location("vault_kit_without_sqlite", coldvault.__file__)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(1, module.search(output, "beta alp"))

    def run_notes(self, home, *argv):
        with patch.object(Path, "home", return_value=home), patch.object(sys, "argv", ["coldvault.py", *argv]), \
                patch("builtins.input", side_effect=AssertionError("prompted")), \
                patch.object(coldvault, "urlopen", side_effect=AssertionError("network called")), \
                contextlib.redirect_stdout(io.StringIO()):
            coldvault.main()

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
                self.assertTrue(coldvault._is_managed_note(note))
            index = (output / "Index.md").read_text(encoding="utf-8")
            self.assertIn("## Imported notes", index)
            self.assertIn("- [[Imported/My Notes/Projects/Plan]]", index)
            self.assertIn("- [[Imported/My Notes/Welcome]]", index)
            self.assertFalse((output / "Sessions").exists())
            with contextlib.redirect_stdout(io.StringIO()) as printed:
                self.assertEqual(2, coldvault.search(output, "zucchini"))
                for hidden in ("gitinternal", "hiddenconfig", "hiddentrash", "hiddenfile", "transcript"):
                    self.assertEqual([], [path for path, _, _ in coldvault.hits(output, hidden) if path not in coldvault.VAULT_TEMPLATE])
            self.assertIn("Imported/My Notes/Welcome.md:", printed.getvalue())

            snapshot = notes_and_caches(output)
            self.run_notes(base, "--notes", str(notes), "--output", str(output))
            self.assertEqual(snapshot, notes_and_caches(output))

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
        self.assertEqual(2, note.count(coldvault.REDACTED))
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

    @patch.object(coldvault, "_utc_now", lambda: "2026-03-01T12:00:00Z")
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
                self.assertIn("token: " + coldvault.REDACTED, table)
                for note in notion.rglob("*.md"):
                    self.assertTrue(coldvault._is_managed_note(note))
                index = (output / "Index.md").read_text(encoding="utf-8")
                self.assertIn("- [[Imported/Notion/Start/Tasks]]", index)
                with contextlib.redirect_stdout(io.StringIO()) as printed:
                    self.assertEqual(1, coldvault.search(output, "marigold"))
                    self.assertEqual(0, coldvault.search(output, "resourcefork"))
                    for skipped in ("zipattachment", "sneakypage", "macstray", "escapedpage"):
                        self.assertEqual(0, coldvault.search(output, skipped), skipped)
                self.assertTrue(printed.getvalue().startswith("Imported/Notion/Start/Tasks.md:"))

                snapshot = notes_and_caches(output)
                self.run_notes(base, "--notion-export", str(export), "--output", str(output))
                self.assertEqual(snapshot, notes_and_caches(output))
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
            self.assertEqual([output / ".obsidian"], [p for p in output.rglob(".*") if ".coldvault" not in p.relative_to(output).parts])
            self.assertIn("| row | [[Imported/Notion/Keys\\|keys]] |", texts["Rel.md"])
            self.assertIn("row | [[Imported/Notion/Keys\\|cell]]\nAfter [[Imported/Notion/Keys|after]] and [[Imported/Notion/Folder (one (two))/Deep|deep]]\n", texts["Code.md"])
            self.assertIn("| alone |", texts["D.md"])
            self.assertIn("| full |", texts["D_all.md"])
            self.assertIn("[[Imported/Notion/D_all|D all]]", texts["Uses D.md"])
            self.assertEqual(["# A eight", "# A one", "# A two"], sorted(text.split("---\n\n")[1].strip() for name, text in texts.items() if name.startswith("A ")))
            self.assertIn("| north |", texts["Sales_all.md"])
            self.assertIn("| api_key=" + coldvault.REDACTED + " | second |", texts["Keys.md"])
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
        with contextlib.closing(coldvault.sqlite3.connect(":memory:")) as database:
            database.execute("CREATE VIRTUAL TABLE notes USING fts5(path UNINDEXED, body)")
            database.executemany("INSERT INTO notes VALUES (?, ?)", ((path, scan_fold(text)) for path, text in notes.items()))
            ranked = [path for (path,) in database.execute("SELECT path FROM notes WHERE notes MATCH ? ORDER BY bm25(notes), path", (query,))]
        lines = [f"{path}:{number}: {line.strip()}" for path in ranked for number, line in enumerate(notes[path].splitlines(), 1)
                 if any(scan_has(word, line) for word in words)]
        return len(lines), "".join(line + "\n" for line in lines)

    def kit_search(self, vault, term):
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            found = coldvault.search(vault, term)
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
            shutil.rmtree(output / ".coldvault")
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
            shutil.rmtree(output / ".coldvault")
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
            with contextlib.closing(coldvault.sqlite3.connect(str(path))) as database:
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
                shutil.rmtree(vault / ".coldvault")
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

def chatgpt_conversation(conversation_id, title, user, assistant, created=1767225600.0):
    def node(key, role, text, offset, content_type="text", metadata=None, **extra):
        return key, {"id": key, "message": {"id": key, "author": {"role": role}, "create_time": created + offset,
                                            "content": {"content_type": content_type, "parts": [text]}, "metadata": metadata or {}, **extra}}
    return {"id": conversation_id, "conversation_id": conversation_id, "title": title, "create_time": created, "mapping": dict([
        node(conversation_id + "-a", "assistant", assistant, 3),
        node(conversation_id + "-s", "system", "hidden system prompt", 0),
        node(conversation_id + "-u", "user", user, 1),
        node(conversation_id + "-t", "tool", "tool output text", 2),
        node(conversation_id + "-h", "user", "hidden context text", 0.5, metadata={"is_visually_hidden_from_conversation": True}),
        node(conversation_id + "-c", "assistant", "tool call text", 1.5, recipient="python"),
        node(conversation_id + "-r", "assistant", "analysis channel text", 1.7, channel="analysis"),
        (conversation_id + "-root", {"id": conversation_id + "-root", "message": None}),
    ])}


def claude_conversation(conversation_id, name, user, assistant):
    return {"uuid": conversation_id, "name": name, "created_at": "2026-01-01T09:00:00.000000Z", "chat_messages": [
        {"uuid": conversation_id + "-u", "sender": "human", "text": user, "content": [{"type": "text", "text": user}]},
        {"uuid": conversation_id + "-a", "sender": "assistant", "text": "legacy flattened text", "content": [
            {"type": "thinking", "thinking": "private reasoning text"}, {"type": "text", "text": assistant},
            {"type": "tool_result", "content": "tool output text"}]},
    ]}


def branched_chatgpt_conversation(conversation_id, title, user, discarded, kept, created=1767225600.0):
    def node(key, parent, role, text):
        message = None if role is None else {"id": key, "author": {"role": role}, "create_time": created,
                                             "content": {"content_type": "text", "parts": [text]}}
        return key, {"id": key, "parent": parent, "message": message}
    return {"id": conversation_id, "title": title, "create_time": created, "current_node": "kept", "mapping": dict([
        node("kept", "question", "assistant", kept), node("discarded", "question", "assistant", discarded),
        node("question", "root", "user", user), node("root", None, None, ""),
    ])}


def write_export(path, conversations, split=False):
    with zipfile.ZipFile(path, "w") as archive:
        if split:
            for number, conversation in enumerate(conversations):
                archive.writestr(f"conversations-{number:03d}.json", json.dumps([conversation]))
        else:
            archive.writestr("conversations.json", json.dumps(conversations))
        archive.writestr("user.json", "{}")
    return path


class ChatExportTests(unittest.TestCase):
    def run_kit(self, home, *argv):
        with patch.object(Path, "home", return_value=home), patch.object(sys, "argv", ["coldvault.py", *argv]), \
                patch("builtins.input", side_effect=AssertionError("prompted")), \
                patch.object(coldvault, "urlopen", side_effect=AssertionError("network called")), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            coldvault.main()

    def test_exports_become_one_session_note_per_conversation_and_reruns_add_only_new_ones(self):
        secret = "sk-proj-" + "a" * 24
        first = [chatgpt_conversation("c1", "Garden plan", f"Plan my garden, key {secret}", "Plant tomatoes"),
                 branched_chatgpt_conversation("c2", "Bread", "How do I bake bread", "Discarded answer", "Use flour")]
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            chatgpt = write_export(base / "chatgpt.zip", first, split=True)
            claude = write_export(base / "claude.zip", [claude_conversation("k1", "Trip notes", "Pack for a trip", "Bring boots")])
            output = base / "vault"
            argv = ("--chatgpt-export", str(chatgpt), "--claude-export", str(claude), "--output", str(output))
            self.run_kit(base, *argv)
            sessions = sorted((output / "Sessions").glob("*.md"))
            self.assertEqual(3, len(sessions))
            text = "\n".join(path.read_text(encoding="utf-8") for path in sessions)
            for expected in ("Plan my garden", "Plant tomatoes", "## Garden plan", "Use flour", "Pack for a trip", "Bring boots",
                             "source: chatgpt", "source: claude-ai", 'session-id: "k1"', "date: 2026-01-01", coldvault.REDACTED):
                self.assertIn(expected, text)
            for hidden in (secret, "tool output text", "hidden system prompt", "hidden context text", "private reasoning text",
                           "tool call text", "analysis channel text", "legacy flattened text", "Discarded answer"):
                self.assertNotIn(hidden, text)
            self.assertIn("[[Sessions/", (output / "Projects" / "chatgpt.md").read_text(encoding="utf-8"))
            self.assertIn("[[Sessions/", (output / "Projects" / "claude-ai.md").read_text(encoding="utf-8"))
            receipt = json.loads((output / ".coldvault" / "last-run.json").read_text(encoding="utf-8"))
            self.assertEqual({"state": "ok", "count": 3}, receipt["sources"]["chat-exports"])

            before = notes_and_caches(output)
            self.run_kit(base, *argv)
            self.assertEqual(before, notes_and_caches(output))

            newer = write_export(base / "chatgpt-newer.zip", [*first, chatgpt_conversation("c3", "Bikes", "Fix a flat tyre", "Patch the tube")])
            self.run_kit(base, "--chatgpt-export", str(newer), "--claude-export", str(claude), "--output", str(output))
            notes = {path for path in output.rglob("*.md")}
            added = notes - {path for path in before if path.suffix == ".md"}
            self.assertEqual(1, len(added))
            self.assertEqual("Sessions", added.pop().parent.name)
            self.assertEqual(4, len(list((output / "Sessions").glob("*.md"))))

    def test_export_that_is_not_a_chat_export_zip_is_refused(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            (base / "plain.zip").write_text("not a zip", encoding="utf-8")
            for flag in ("--chatgpt-export", "--claude-export"):
                for export in (base / "plain.zip", base / "missing.zip"):
                    with self.subTest(flag=flag, export=export.name), self.assertRaises(SystemExit) as raised:
                        self.run_kit(base, flag, str(export), "--output", str(base / "vault"))
                    self.assertEqual(2, raised.exception.code)
            self.assertFalse((base / "vault" / "Index.md").exists())
            empty = base / "empty.zip"
            with zipfile.ZipFile(empty, "w") as archive:
                archive.writestr("readme.txt", "no conversations")
            broken = write_export(base / "broken.zip", [{"uuid": "k1", "chat_messages": "not a list"}])
            for flag, export in (("--chatgpt-export", empty), ("--claude-export", broken), ("--chatgpt-export", write_export(base / "noid.zip", [{"mapping": {}}])),
                                 ("--chatgpt-export", write_export(base / "nomapping.zip", [{"id": "c1"}])),
                                 ("--chatgpt-export", write_export(base / "badnode.zip", [{**branched_chatgpt_conversation("c1", "T", "q", "old", "new"), "current_node": "missing"}])),
                                 ("--chatgpt-export", write_export(base / "stringparts.zip", [{"id": "c1", "mapping": {"m": {"message": {
                                     "author": {"role": "user"}, "content": {"content_type": "text", "parts": "hello"}}}}}]))):
                with self.subTest(export=export.name), self.assertRaises(SystemExit) as raised:
                    self.run_kit(base, flag, str(export), "--output", str(base / "vault"))
                self.assertEqual(3, raised.exception.code)
                receipt = json.loads((base / "vault" / ".coldvault" / "last-run.json").read_text(encoding="utf-8"))
                self.assertEqual("failed", receipt["sources"]["chat-exports"]["state"])


class CommandLineErrorTests(unittest.TestCase):
    def cli(self, home, *argv):
        script = Path(coldvault.__file__).resolve()
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
                ((), "no transcripts found in ~/.claude/projects, ~/.codex/sessions or ~/.codex/archived_sessions"),
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
        self.assertRegex(coldvault.__version__, r"^\d+\.\d+\.\d+$")
        self.assertEqual(f"coldvault {coldvault.__version__}\n", result.stdout)


class PackagingTests(unittest.TestCase):
    def test_package_installs_a_coldvault_command_with_the_module_version(self):
        pyproject = (Path(coldvault.__file__).resolve().parent / "pyproject.toml").read_text(encoding="utf-8")
        self.assertRegex(pyproject, r'(?m)^name = "coldvault"$')
        self.assertRegex(pyproject, r'(?m)^dynamic = \["version"\]$')
        self.assertRegex(pyproject, r'(?m)^version = \{attr = "coldvault.__version__"\}$')
        self.assertRegex(pyproject, r'(?m)^py-modules = \["coldvault", "vault_kit"\]$')
        self.assertRegex(pyproject, r'(?ms)^\[project\.scripts\]\ncoldvault = "coldvault:main"$')


class ManagedMarkerTests(unittest.TestCase):
    def legacy_note(self, body):
        unhashed = f"---\nvault-kit-managed: true\n---\n\n{body}"
        lines = unhashed.split("\n")
        lines.insert(2, "vault-kit-hash: " + hashlib.sha256(unhashed.encode("utf-8")).hexdigest())
        return "\n".join(lines)

    def rebuild(self, source, output):
        with patch.object(coldvault, "github_repos", return_value=[]), contextlib.redirect_stdout(io.StringIO()):
            coldvault.build(source, output)

    def test_new_notes_use_the_coldvault_marker(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            source, output = base / "input", base / "vault"
            source.mkdir()
            record = {"type": "user", "cwd": "/work/demo", "message": {"role": "user", "content": "Synthetic"}}
            (source / "session.jsonl").write_text(json.dumps(record), encoding="utf-8")
            self.rebuild(source, output)
            notes = [note for note in output.rglob("*.md") if note.relative_to(output).as_posix() not in coldvault.VAULT_TEMPLATE]
            self.assertTrue(notes)
            for name in coldvault.VAULT_TEMPLATE:
                self.assertFalse(coldvault._is_managed_note(output / name), name)
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
                self.assertTrue(coldvault._is_managed_note(note))
            (output / "Index (Coldvault).md").unlink(missing_ok=True)

            self.rebuild(source, output)

            self.assertEqual("my own index\n", (output / "Index.md").read_text(encoding="utf-8"))
            for note in (session, old_copy):
                text = note.read_text(encoding="utf-8")
                self.assertTrue(text.startswith("---\ncoldvault-managed: true\ncoldvault-hash: "), note)
                self.assertNotIn("stale generated content", text)
                self.assertTrue(coldvault._is_managed_note(note))
            self.assertIn("Synthetic", session.read_text(encoding="utf-8"))
            self.assertEqual([session], list((output / "Sessions").glob("*.md")))
            self.assertFalse((output / "Index (Coldvault).md").exists())
            self.assertIn("[[Index (Vault Kit)]]", (output / "START-HERE.md").read_text(encoding="utf-8"))

    def frontmatter(self, note):
        lines = note.read_text(encoding="utf-8").split("\n")
        fields = {}
        for line in lines[1:lines.index("---", 1)]:
            key, value = line.split(": ", 1)
            fields[key] = json.loads(value) if value.startswith('"') else value
        return fields

    def test_generated_notes_carry_source_metadata_and_keep_a_valid_hash(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            source, output = base / "input", base / "vault"
            (source / "claude").mkdir(parents=True)
            (source / "codex").mkdir()
            claude = [
                {"type": "user", "sessionId": "claude-session-1", "timestamp": "2026-02-03T10:00:00Z", "cwd": "/work/demo: two", "message": {"role": "user", "content": "Claude question"}},
            ]
            codex = [
                {"type": "session_meta", "timestamp": "2026-02-04T10:00:00Z", "payload": {"id": "codex-session-1", "cwd": "/work/other"}},
                {"type": "response_item", "timestamp": "2026-02-04T10:00:01Z", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Codex question"}]}},
            ]
            (source / "claude" / "a.jsonl").write_text("\n".join(map(json.dumps, claude)), encoding="utf-8")
            (source / "codex" / "rollout.jsonl").write_text("\n".join(map(json.dumps, codex)), encoding="utf-8")
            (source / "odd").mkdir()
            (source / "odd" / "b.jsonl").write_text(json.dumps({"type": "user", "sessionId": "odd-session", "timestamp": "2026-99-99T10:00:00Z", "cwd": "/work/odd", "message": {"role": "user", "content": "Odd question"}}), encoding="utf-8")
            repo = {"name": "other", "full_name": "synthetic-user/other", "html_url": "https://github.com/synthetic-user/other", "pushed_at": "2026-02-05T09:00:00Z"}

            def build():
                with patch.object(coldvault, "github_repos", return_value=[repo]), contextlib.redirect_stdout(io.StringIO()), \
                        patch.object(coldvault, "urlopen", side_effect=HTTPError("https://api.github.com/", 404, "Not Found", {}, None)):
                    coldvault.build(source, output, "synthetic-user")

            build()
            sessions = {self.frontmatter(note).get("session-id"): note for note in (output / "Sessions").glob("*.md")}
            self.assertEqual({"claude-session-1", "codex-session-1", "odd-session"}, set(sessions))
            self.assertIn('\ndate: "2026-99-99"\n', sessions["odd-session"].read_text(encoding="utf-8"))
            self.assertIn('\ndate: 2026-02-03\n', sessions["claude-session-1"].read_text(encoding="utf-8"))
            claude_meta = self.frontmatter(sessions["claude-session-1"])
            codex_meta = self.frontmatter(sessions["codex-session-1"])
            self.assertEqual(("claude-code", "2026-02-03", "demo: two"), (claude_meta["source"], claude_meta["date"], claude_meta["project"]))
            self.assertEqual(("codex", "2026-02-04", "other"), (codex_meta["source"], codex_meta["date"], codex_meta["project"]))
            for meta in (claude_meta, codex_meta):
                self.assertRegex(meta["imported-at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
            github = self.frontmatter(output / "Projects" / "other.md")
            self.assertEqual(("synthetic-user/other", "2026-02-05T09:00:00Z"), (github["repo"], github["pushed-at"]))
            for note in [*sessions.values(), output / "Projects" / "other.md"]:
                self.assertTrue(coldvault._is_managed_note(note), note)

            session = sessions["claude-session-1"]
            unhashed = f"---\ncoldvault-managed: true\n---\n\n# demo: two — 2026-02-03\n\nold body\n"
            lines = unhashed.split("\n")
            lines.insert(2, "coldvault-hash: " + hashlib.sha256(unhashed.encode("utf-8")).hexdigest())
            session.write_text("\n".join(lines), encoding="utf-8")
            self.assertTrue(coldvault._is_managed_note(session))
            build()
            self.assertEqual(sorted(sessions.values()), sorted((output / "Sessions").glob("*.md")))
            self.assertEqual("claude-session-1", self.frontmatter(session)["session-id"])
            self.assertIn("Claude question", session.read_text(encoding="utf-8"))

            first = {note: note.read_text(encoding="utf-8") for note in output.rglob("*.md")}
            build()
            self.assertEqual(first, {note: note.read_text(encoding="utf-8") for note in output.rglob("*.md")})

            for bad in ("2026-99-99T99:99:99Z", "2026-02-05 09:00:00", None):
                with self.subTest(pushed_at=bad):
                    repo["pushed_at"] = bad
                    build()
                    github = self.frontmatter(output / "Projects" / "other.md")
                    self.assertEqual("synthetic-user/other", github["repo"])
                    self.assertNotIn("pushed-at", github)
            repo["pushed_at"] = "2026-02-05T09:00:00Z"
            build()
            self.assertEqual(first, {note: note.read_text(encoding="utf-8") for note in output.rglob("*.md")})

    def test_daily_notes_list_each_days_sessions_and_imported_notes_and_reruns_change_nothing(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            source, notes, output = base / "input", base / "notes", base / "vault"
            source.mkdir()
            notes.mkdir()
            for name, stamp, cwd in (("a", "2026-02-03T10:00:00Z", "/work/alpha"), ("b", "2026-02-03T23:00:00Z", "/work/beta"), ("c", "2026-02-04T09:00:00Z", "/work/alpha"), ("d", None, "/work/alpha")):
                record = {"type": "user", "cwd": cwd, "message": {"role": "user", "content": f"Question {name}"}}
                if stamp:
                    record["timestamp"] = stamp
                (source / f"{name}.jsonl").write_text(json.dumps(record), encoding="utf-8")
            (notes / "Plan.md").write_text("# Plan\n", encoding="utf-8")
            (notes / "Tagged.md").write_text("---\ntags: [garden]\n---\n\n# Tagged\n", encoding="utf-8")
            (notes / "Stamped.md").write_text('---\nimported-at: "2025-12-31T08:00:00Z"\n---\n\n# Stamped\n', encoding="utf-8")

            def build(now):
                with patch.object(coldvault, "github_repos", return_value=[]), patch.object(coldvault, "_utc_now", return_value=now), \
                        contextlib.redirect_stdout(io.StringIO()):
                    coldvault.build_from_files(sorted(source.glob("*.jsonl")), output, "", [notes])

            build("2026-03-01T12:00:00Z")
            daily = output / "Daily"
            self.assertEqual(["2025-12-31.md", "2026-02-03.md", "2026-02-04.md", "2026-03-01.md", "README.md"], sorted(p.name for p in daily.iterdir()))
            sessions = {self.frontmatter(note)["session-id"]: note.relative_to(output).as_posix().removesuffix(".md") for note in (output / "Sessions").glob("*.md")}
            first = (daily / "2026-02-03.md").read_text(encoding="utf-8")
            self.assertEqual({"record_type": "daily_index", "date": "2026-02-03"}, {k: v for k, v in self.frontmatter(daily / "2026-02-03.md").items() if not k.startswith("coldvault-")})
            self.assertIn(f"## Sessions\n\n- [[{sessions['a']}]] · [[Projects/alpha]]\n- [[{sessions['b']}]] · [[Projects/beta]]", first)
            self.assertNotIn("## Imported notes", first)
            self.assertIn(f"- [[{sessions['c']}]] · [[Projects/alpha]]", (daily / "2026-02-04.md").read_text(encoding="utf-8"))
            imported = (daily / "2026-03-01.md").read_text(encoding="utf-8")
            self.assertIn("## Imported notes\n\n- [[Imported/notes/Plan]]\n- [[Imported/notes/Tagged]]", imported)
            self.assertNotIn("## Sessions", imported)
            self.assertIn("- [[Imported/notes/Stamped]]", (daily / "2025-12-31.md").read_text(encoding="utf-8"))
            self.assertNotIn(sessions["d"], "".join(p.read_text(encoding="utf-8") for p in daily.glob("*.md")))
            self.assertEqual("2026-03-01T12:00:00Z", self.frontmatter(output / "Imported" / "notes" / "Tagged.md")["imported-at"])
            self.assertEqual("[garden]", self.frontmatter(output / "Imported" / "notes" / "Tagged.md")["tags"])
            for note in [*daily.glob("20*.md"), *(output / "Imported").rglob("*.md")]:
                self.assertTrue(coldvault._is_managed_note(note), note)

            snapshot = notes_and_caches(output)
            build("2026-03-02T12:00:00Z")
            self.assertEqual(snapshot, notes_and_caches(output))

            (daily / "2026-02-04.md").write_text("# My own day\n", encoding="utf-8")
            (notes / "Later.md").write_text("# Later\n", encoding="utf-8")
            (output / "Imported" / "notes" / "Later.md").write_text("---\nimported-at: 2020-01-01T00:00:00Z\n---\n\n# My unrelated note\n", encoding="utf-8")
            build("2026-03-02T12:00:00Z")
            self.assertEqual("# My own day\n", (daily / "2026-02-04.md").read_text(encoding="utf-8"))
            self.assertIn(f"- [[{sessions['c']}]]", (daily / "2026-02-04 (Coldvault).md").read_text(encoding="utf-8"))
            self.assertIn("- [[Imported/notes/Later (Coldvault)]]", (daily / "2026-03-02.md").read_text(encoding="utf-8"))
            self.assertFalse((daily / "2020-01-01.md").exists())
            self.assertNotIn("Later", (daily / "2026-03-01.md").read_text(encoding="utf-8"))

            tagged = output / "Imported" / "notes" / "Tagged.md"
            tagged.write_text(tagged.read_text(encoding="utf-8") + "\nMy edit\n", encoding="utf-8")
            (notes / "Plan.md").unlink()
            (source / "a.jsonl").unlink()
            build("2026-03-03T12:00:00Z")
            self.assertIn("My edit", tagged.read_text(encoding="utf-8"))
            self.assertEqual("2026-03-01T12:00:00Z", self.frontmatter(output / "Imported" / "notes" / "Tagged (Coldvault).md")["imported-at"])
            self.assertIn("- [[Imported/notes/Tagged (Coldvault)]]", (daily / "2026-03-01.md").read_text(encoding="utf-8"))
            self.assertNotIn("Plan", (daily / "2026-03-01.md").read_text(encoding="utf-8"))
            self.assertFalse((daily / "2026-03-03.md").exists())
            self.assertNotIn(sessions["a"], (daily / "2026-02-03.md").read_text(encoding="utf-8"))
            (source / "b.jsonl").unlink()
            build("2026-03-03T12:00:00Z")
            self.assertFalse((daily / "2026-02-03.md").exists())
            self.assertEqual("# My own day\n", (daily / "2026-02-04.md").read_text(encoding="utf-8"))
            self.assertTrue((daily / "2026-02-04 (Coldvault).md").exists())
            self.assertTrue((daily / "README.md").exists())

            other = coldvault.write_note(output, "Daily/2026-01-01.md", "# A managed note that is not a daily index\n")
            build("2026-03-03T12:00:00Z")
            self.assertTrue((output / other).exists())

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
                    self.assertFalse(coldvault._is_managed_note(note))
            note.write_text(legacy, encoding="utf-8")
            self.assertTrue(coldvault._is_managed_note(note))

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
        with patch.object(coldvault, "github_repos", return_value=[]), contextlib.redirect_stdout(io.StringIO()):
            coldvault.build(source, output)

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
                key = coldvault.hashlib.sha1(str((source / folder / "session.jsonl").resolve()).encode()).hexdigest()[:8]
                legacy[folder] = f"Sessions/2026-11-01-{project}-{key}.md"
                coldvault.write_note(output, legacy[folder], f"# {project} — 2026-11-01\n\nProject: [[Projects/{project}]]\n\nold text")
            coldvault.write_note(output, "Projects/app.md", "# app\n\n## Sessions\n\n" + "\n".join(f"- {coldvault.link(legacy[f])} (2026-11-01)" for f in "ab"))
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
                    self.assertIn(coldvault.link(note), linked)
            for text in ("claude two app", "claude three app", "edited session"):
                self.assertEqual(1, sum(text in (output / note).read_text(encoding="utf-8") for note in sessions if note != legacy["c"]))



KIT = Path(__file__).resolve().parents[1]


class ChangelogTests(unittest.TestCase):
    def test_latest_entry_is_the_current_version(self):
        changelog = (KIT / "CHANGELOG.md").read_text(encoding="utf-8")
        headings = re.findall(r"^## (\S+)", changelog, re.M)
        self.assertEqual(headings[:1], [coldvault.__version__])


class ReadmeTests(unittest.TestCase):
    def setUp(self):
        self.readme = (KIT / "README.md").read_text(encoding="utf-8")
        self.lines = self.readme.splitlines()
        self.commands = [line[4:] for line in self.lines if line.startswith("    ")]
        self.customize = (KIT / "docs" / "customize.md").read_text(encoding="utf-8")

    def section(self, heading):
        self.assertTrue(heading in self.lines, f"README has no {heading!r} heading")
        start = self.lines.index(heading)
        end = next((i for i in range(start + 1, len(self.lines)) if self.lines[i].startswith("## ")), len(self.lines))
        return self.lines[start:end]

    def scheduled(self):
        commands = [line[4:] for line in self.customize.splitlines() if line.startswith("    ")]
        cron = [command for command in commands if command.startswith("0 * * * * ")]
        self.assertEqual(1, len(cron), cron)
        start = commands.index("cat > ~/Library/LaunchAgents/coldvault.rebuild.plist <<'EOF'")
        end = commands.index("EOF", start)
        plist = plistlib.loads("\n".join(commands[start + 1:end]).encode("utf-8"))
        return cron[0][len("0 * * * * "):], plist, commands[end + 1]

    def test_quickstart_downloads_the_script_with_curl_before_any_git_step(self):
        curl = f"curl -O https://raw.githubusercontent.com/TayHCode/coldvault/main/{Path(coldvault.__file__).name}"
        self.assertIn(curl, self.commands)
        first_git = next(i for i, command in enumerate(self.commands) if command.startswith("git "))
        self.assertLess(self.commands.index(curl), first_git)
        self.assertEqual("mkdir -p ~/coldvault && cd ~/coldvault", self.commands[self.commands.index(curl) - 1])

    def test_main_path_is_install_setup_obsidian_schedule_status_in_800_words(self):
        path = ["## Install", "## Set up", "## Open it in Obsidian", "## Schedule", "## Check that it is working"]
        headings = [line for line in self.lines if line.startswith("## ")]
        self.assertEqual(path, headings[:len(path)])
        start = self.lines.index(path[0])
        end = self.lines.index(headings[len(path)])
        main = "\n".join(self.lines[start:end])
        self.assertLessEqual(len(main.split()), 800)
        self.assertIn("    python3 ~/coldvault/coldvault.py schedule --vault ~/ColdvaultVault", main)
        self.assertNotIn("notion-sync", main)
        self.assertNotIn("rclone", main)

    def test_readme_links_the_moved_guides(self):
        for guide in ("docs/sources.md", "docs/sync.md", "docs/vault.md", "docs/customize.md#schedule"):
            self.assertIn(f"]({guide})", self.readme)
        for target in re.findall(r"\]\((docs/[^)#]+)", self.readme):
            self.assertTrue((KIT / target).is_file(), target)

    def test_readme_says_how_to_open_the_vault_in_obsidian(self):
        self.assertIn("choose **Open folder as vault**", "\n".join(self.section("## Open it in Obsidian")))

    def test_cron_and_launchd_lines_run_the_same_hourly_rebuild(self):
        command, plist, load = self.scheduled()
        self.assertEqual({"Label", "ProgramArguments", "StartInterval"}, set(plist))
        self.assertEqual(["/bin/sh", "-c", command], plist["ProgramArguments"])
        self.assertEqual(3600, plist["StartInterval"])
        self.assertEqual("launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/coldvault.rebuild.plist", load)
        for verb in ("kickstart", "bootout"):
            self.assertIn(f"`launchctl {verb} gui/$(id -u)/{plist['Label']}`", self.customize)

    @unittest.skipUnless(os.name == "posix" and shutil.which("python3"), "the scheduled line runs python3 under /bin/sh")
    def test_scheduled_command_builds_the_vault_without_a_terminal(self):
        command, _, _ = self.scheduled()
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            home = Path(temporary)
            (home / "coldvault").mkdir()
            shutil.copy2(KIT / "coldvault.py", home / "coldvault" / "coldvault.py")
            env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}

            def run():
                result = subprocess.run(["/bin/sh", "-c", command], stdin=subprocess.DEVNULL, capture_output=True,
                                        text=True, env=env, timeout=120)
                self.assertNotIn("Traceback", result.stdout + result.stderr)
                return result

            missing = run()
            self.assertEqual(2, missing.returncode, missing.stderr)
            self.assertIn("no transcripts found in ~/.claude/projects, ~/.codex/sessions or ~/.codex/archived_sessions", missing.stderr)
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
                self.assertTrue(list(coldvault.hits(str(vault), term)), term)

    def test_sync_recipes_follow_a_privacy_warning(self):
        text = (KIT / "docs" / "sync.md").read_text(encoding="utf-8")
        self.assertIn("**Privacy warning:**", text)
        warning = text.index("**Privacy warning:**")
        recipes = (
            ("**A private git repository.**", "git push -u origin main"),
            ("**Obsidian Sync.**", "ob sync"),
            ("**iCloud on a Mac.**", "python3 coldvault.py --username '' --output \"$HOME/Library/Mobile Documents/iCloud~md~obsidian/Documents/Coldvault\""),
            ("**S3 with rclone.**", "rclone sync ~/ColdvaultVault s3:YOUR-BUCKET/coldvault --exclude '.coldvault/**'"),
        )
        for heading, command in recipes:
            with self.subTest(heading=heading):
                self.assertLess(warning, text.index(heading))
                self.assertLess(text.index(heading), text.index(f"\n    {command}\n"))

    def test_uninstall_keeps_the_notes_and_names_every_step(self):
        text = "\n".join(self.section("## Uninstall"))
        for step in ("schedule remove --vault", "crontab -e", "launchctl bootout gui/$(id -u)/coldvault.rebuild", "pipx uninstall coldvault",
                     "rm -rf ~/ColdvaultVault/.coldvault", "notes are yours"):
            self.assertIn(step, text)

    def test_recover_covers_each_case(self):
        text = "\n".join(self.section("## Recover"))
        for case in ("**An interrupted run.**", "**An edited note and its `(Coldvault)` copy.**", "**Rebuilding the search index.**",
                     "**Moving the vault.**", "**Restoring from the sync target.**"):
            self.assertIn(case, text)

    def test_release_check_quickstart_lines_match_the_readme(self):
        manifest = json.loads((KIT / "release-check.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["quickstart"])
        for entry in manifest["quickstart"]:
            with self.subTest(entry=entry):
                self.assertEqual("README.md", entry["file"])
                self.assertEqual(entry["argv"], shlex.split(self.lines[entry["line"] - 1]))


class LegacyNameTests(unittest.TestCase):
    def test_vault_kit_py_is_a_one_line_shim_for_coldvault_py(self):
        self.assertEqual("coldvault.py", Path(coldvault.__file__).name)
        self.assertEqual(1, len((KIT / "vault_kit.py").read_text(encoding="utf-8").splitlines()))

    @unittest.skipUnless(os.name == "posix" and shutil.which("python3"), "the scheduled line runs python3 under /bin/sh")
    def test_an_old_vault_kit_cron_line_still_builds_the_vault(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            home = Path(temporary)
            (home / "coldvault").mkdir()
            for name in ("coldvault.py", "vault_kit.py"):
                shutil.copy2(KIT / name, home / "coldvault" / name)
            env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}

            def run(flags):
                command = f"""python3 {flags} "$HOME/coldvault/vault_kit.py" --username '' --output "$HOME/ColdvaultVault\""""
                result = subprocess.run(["/bin/sh", "-c", command], stdin=subprocess.DEVNULL, capture_output=True,
                                        text=True, env=env, timeout=120)
                self.assertNotIn("Traceback", result.stdout + result.stderr)
                return result

            missing = run("")
            self.assertEqual(2, missing.returncode, missing.stderr)
            self.assertIn("no transcripts found", missing.stderr)
            shutil.copytree(KIT / "examples" / "transcripts" / "claude", home / ".claude" / "projects" / "demo")
            for flags in ("", "-I"):
                with self.subTest(flags=flags):
                    result = run(flags)
                    self.assertEqual(0, result.returncode, result.stderr)
                    self.assertTrue(list(coldvault.hits(str(home / "ColdvaultVault"), "checklist")))

    def test_import_vault_kit_still_exposes_the_coldvault_api(self):
        script = f"import sys; sys.path.insert(0, {str(KIT)!r}); import vault_kit, coldvault; vault_kit.SOURCES = []; print(vault_kit is coldvault, coldvault.SOURCES)"
        result = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True, timeout=60)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("True []\n", result.stdout)

    def test_security_and_contributing_name_coldvault_and_a_public_contact_path(self):
        issues = "https://github.com/TayHCode/coldvault/issues"
        for name in ("SECURITY.md", "CONTRIBUTING.md"):
            with self.subTest(name=name):
                text = (KIT / name).read_text(encoding="utf-8")
                self.assertNotRegex(text, r"(?i)vault[- ]?kit")
                self.assertNotRegex(text, r"[\w.+-]+@[\w-]+\.[\w.]+")
                self.assertIn(issues, text)


class VaultTemplateTests(unittest.TestCase):
    def build(self, output):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as source:
            record = {"type": "user", "cwd": "/work/demo", "message": {"role": "user", "content": "Synthetic"}}
            (Path(source) / "session.jsonl").write_text(json.dumps(record), encoding="utf-8")
            with patch.object(coldvault, "github_repos", return_value=[]), contextlib.redirect_stdout(io.StringIO()):
                coldvault.build(source, output)

    @staticmethod
    def tree(root):
        return sorted(p.relative_to(root).as_posix() + ("/" if p.is_dir() else "") for p in root.rglob("*") if ".coldvault" not in p.relative_to(root).parts)

    def test_new_vault_gets_the_template_tree_and_a_working_search(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            output = Path(temporary) / "vault"
            self.build(output)
            tree = self.tree(output)
            session = next(p for p in tree if p.startswith("Sessions/") and p.endswith(".md"))
            self.assertEqual(sorted([
                ".obsidian/", ".obsidian/app.json", ".obsidian/core-plugins.json", ".obsidian/daily-notes.json", ".obsidian/templates.json",
                "Agent-Space/", "Agent-Space/README.md", "Attachments/", "Capabilities/", "Capabilities/README.md",
                "Context/", "Context/README.md", "Daily/", "Daily/README.md", "Home.md", "Index.md",
                "Projects/", "Projects/demo.md", "START-HERE.md", "Sessions/", session,
                "Templates/", "Templates/Capability.md", "Templates/Context.md", "Templates/Daily.md", "Templates/Note.md", "Templates/README.md",
                "Writing/", "Writing/README.md",
            ]), tree)
            app = json.loads((output / ".obsidian/app.json").read_text(encoding="utf-8"))
            self.assertEqual("Attachments", app["attachmentFolderPath"])
            plugins = json.loads((output / ".obsidian/core-plugins.json").read_text(encoding="utf-8"))
            self.assertTrue(plugins["daily-notes"] and plugins["templates"])
            daily = json.loads((output / ".obsidian/daily-notes.json").read_text(encoding="utf-8"))
            self.assertTrue((output / f"{daily['template']}.md").is_file())
            self.assertTrue((output / daily["folder"]).is_dir())
            for note in output.rglob("*.md"):
                for target in re.findall(r"\[\[([^\]|#]+)", note.read_text(encoding="utf-8")):
                    with self.subTest(note=note.name, link=target):
                        self.assertTrue((output / f"{target}.md").is_file())
            result = subprocess.run([sys.executable, str(KIT / "coldvault.py"), "search", "--json", "frontmatter", "--vault", str(output)], capture_output=True, text=True, timeout=60)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("Home.md", [hit["path"] for hit in json.loads(result.stdout)])

    def test_rebuilding_keeps_user_edits_and_deletions_in_template_notes(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            output = Path(temporary) / "vault"
            self.build(output)
            (output / "Home.md").write_bytes(b"My own home\r\n")
            (output / "Writing" / "README.md").unlink()
            self.build(output)
            self.assertEqual(b"My own home\r\n", (output / "Home.md").read_bytes())
            self.assertFalse((output / "Writing" / "README.md").exists())

    def test_existing_vault_and_its_obsidian_settings_stay_byte_identical(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            output = Path(temporary) / "vault"
            (output / ".obsidian").mkdir(parents=True)
            mine = {".obsidian/app.json": b'{"theme": "mine"}', "My note.md": b"# Mine\n"}
            for name, content in mine.items():
                (output / name).write_bytes(content)
            self.build(output)
            self.build(output)
            for name, content in mine.items():
                self.assertEqual(content, (output / name).read_bytes())
            self.assertEqual(["app.json"], [p.name for p in (output / ".obsidian").iterdir()])
            for name in ("Home.md", "Daily", "Templates", "Attachments"):
                self.assertFalse((output / name).exists(), name)

    def test_template_never_replaces_or_follows_paths_that_appear_after_the_empty_check(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            root, outside = Path(temporary) / "vault", Path(temporary) / "outside"
            root.mkdir()
            outside.mkdir()
            (root / ".obsidian").symlink_to(outside, target_is_directory=True)
            (root / "Home.md").write_bytes(b"raced in\n")
            (root / "Daily").mkdir()
            (outside / "target.md").write_bytes(b"outside\n")
            (root / "Daily" / "README.md").symlink_to(outside / "target.md")
            coldvault._apply_template(root)
            self.assertEqual(["target.md"], [p.name for p in outside.iterdir()])
            self.assertEqual(b"outside\n", (outside / "target.md").read_bytes())
            self.assertEqual(b"raced in\n", (root / "Home.md").read_bytes())
            self.assertEqual(coldvault.VAULT_TEMPLATE["Writing/README.md"], (root / "Writing" / "README.md").read_text(encoding="utf-8"))

    def test_setup_config_alone_still_gets_the_template_and_sync_builds_it(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = Path(temporary)
            transcripts, output = base / "transcripts", base / "vault"
            transcripts.mkdir()
            record = {"type": "user", "cwd": "/work/demo", "message": {"role": "user", "content": "Synthetic"}}
            (transcripts / "session.jsonl").write_text(json.dumps(record), encoding="utf-8")
            config = {"version": 1, "transcripts": [str(transcripts)], "notes": [], "notion_exports": [], "username": ""}
            coldvault._owned_folder(output, (".coldvault",))
            (output / ".coldvault" / "config.json").write_text(json.dumps(config), encoding="utf-8")
            result = subprocess.run([sys.executable, str(KIT / "coldvault.py"), "sync", "--vault", str(output)], capture_output=True, text=True, timeout=60, env={**os.environ, "HOME": str(base)})
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(coldvault.VAULT_TEMPLATE["Home.md"], (output / "Home.md").read_text(encoding="utf-8"))
            self.assertTrue(next((output / "Sessions").glob("*.md")).is_file())

    def test_a_built_vault_emptied_by_its_user_does_not_get_the_template_again(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            output = Path(temporary) / "vault"
            self.build(output)
            for path in output.iterdir():
                if path.name != ".coldvault":
                    shutil.rmtree(path) if path.is_dir() else path.unlink()
            self.build(output)
            for name in ("Home.md", "Daily", "Templates", ".obsidian", "Attachments"):
                self.assertFalse((output / name).exists(), name)

    def test_template_is_placeholder_only(self):
        text = "\n".join(coldvault.VAULT_TEMPLATE.values())
        self.assertIsNone(re.search(r"[\w.+-]+@[\w-]+\.[\w.]+|https?://|(?<![\w/])@\w", text))


if __name__ == "__main__":
    unittest.main()
