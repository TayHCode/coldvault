import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    import coldvault
finally:
    sys.path.pop(0)


SCRATCH = Path(__file__).resolve().parents[1] / ".scratch"
SCRATCH.mkdir(exist_ok=True)
TOKEN = "ghp_" + "T" * 36
API = "https://api.github.com/"
LIST = API + "users/synthetic-user/repos?per_page=100&sort=updated"
REPO = API + "repos/synthetic-user/demo"
ISSUES = REPO + "/issues?state=open&per_page=100"
PAGE_TWO = LIST + "&page=2"


class FakeGitHub:
    def __init__(self, pushed="2026-11-01T10:00:00Z"):
        self.requests = []
        self.pushed = pushed
        self.limited = False
        self.fail = {}
        self.open_issues = 2
        self.readme = "# Demo\n\nInstall with pip.\n"

    def routes(self):
        return {
            LIST: (200, json.dumps([{"name": "demo", "full_name": "synthetic-user/demo", "html_url": "https://github.com/synthetic-user/demo",
                                     "description": "A demo", "pushed_at": self.pushed, "open_issues_count": self.open_issues}]), {}),
            REPO + "/readme": (200, self.readme, {}),
            REPO + "/contents/docs": (200, json.dumps([{"name": "guide.md", "type": "file"}, {"name": "img.png", "type": "file"}, {"name": "sub", "type": "dir"}]), {}),
            REPO + "/contents/docs/guide.md": (200, "Guide text\n", {}),
            ISSUES: (200, json.dumps([
                {"number": 7, "title": "Crash on [start]", "html_url": "https://github.com/synthetic-user/demo/issues/7"},
                {"number": 8, "title": "Add docs", "html_url": "https://github.com/synthetic-user/demo/pull/8", "pull_request": {}},
            ]), {"ETag": 'W/"issues-1"'}),
        }

    def __call__(self, request, timeout):
        self.requests.append(request)
        url = request.full_url
        if self.limited and url != LIST:
            raise HTTPError(url, 403, "rate limit", {"X-RateLimit-Remaining": "0"}, None)
        if url in self.fail:
            code, headers = self.fail[url]
            raise HTTPError(url, code, "failed", headers, None)
        if url == ISSUES and request.get_header("If-none-match") == 'W/"issues-1"':
            raise HTTPError(url, 304, "Not Modified", {}, None)
        status, body, headers = self.routes()[url]
        response = io.BytesIO(body.encode())
        response.headers = headers
        return response


class PagedGitHub(FakeGitHub):
    def __init__(self):
        super().__init__()
        self.pages = (("first",), ("second",))

    def routes(self):
        routes = {}
        for url, next_url, names in zip((LIST, PAGE_TWO), (PAGE_TWO, None), self.pages):
            repos = []
            for name in names:
                full_name = f"synthetic-user/{name}"
                repos.append({"name": name, "full_name": full_name, "html_url": f"https://github.com/{full_name}",
                              "pushed_at": self.pushed, "open_issues_count": 0})
                api = API + f"repos/{full_name}"
                routes[api + "/readme"] = (200, f"{name} README", {})
                routes[api + "/contents/docs"] = (200, '[{"name":"guide.md","type":"file"}]', {})
                routes[api + "/contents/docs/guide.md"] = (200, f"{name} guide", {})
            headers = {"Link": f'<{next_url}>; rel="next"'} if next_url and len(self.pages) > 1 else {}
            routes[url] = (200, json.dumps(repos), headers)
        return routes


class GitHubSyncTests(unittest.TestCase):
    def sync(self, base, fake, expected_exit=0):
        argv = ["coldvault.py", "sync", "--username", "synthetic-user", "--transcripts", str(base / "input"), "--output", str(base / "vault")]
        out, err = io.StringIO(), io.StringIO()
        exit_code = 0
        with patch.dict(os.environ, {"HOME": str(base), "GITHUB_TOKEN": TOKEN}), patch.object(sys, "argv", argv), \
                patch.object(coldvault, "urlopen", side_effect=fake), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                coldvault.main()
            except SystemExit as error:
                exit_code = error.code
        self.assertEqual(expected_exit, exit_code, out.getvalue() + err.getvalue())
        return out.getvalue() + err.getvalue()

    def test_doctor_distinguishes_github_summaries_for_different_local_projects(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base = self.base(temporary)
            record = {"type": "user", "cwd": "/other/demo", "message": {"role": "user", "content": "Another project"}}
            (base / "input" / "another.jsonl").write_text(json.dumps(record), encoding="utf-8")
            self.sync(base, FakeGitHub())
            vault = base / "vault"
            projects = sorted((vault / "Projects").glob("*.md"))
            self.assertEqual(2, len(projects))
            for path in projects:
                self.assertIn('repo: "synthetic-user/demo"', path.read_text(encoding="utf-8"))

            argv = ["coldvault.py", "doctor", "--vault", str(vault), "--json"]
            for copies in (0, 1):
                with patch.dict(os.environ, HOME=str(base)), patch.object(sys, "argv", argv), \
                        contextlib.redirect_stdout(io.StringIO()) as printed:
                    with self.assertRaises(SystemExit) as raised:
                        coldvault.main()
                self.assertEqual(copies, raised.exception.code, printed.getvalue())
                self.assertEqual({"schema_version": 1, "schedule": {"expected_hours": None, "state": "not_configured"},
                                  "sources": [
                    {"source": "transcripts", "freshness": "fresh", "duplicates": 0},
                    {"source": "github", "freshness": "fresh", "duplicates": copies},
                ]}, json.loads(printed.getvalue()))
                if not copies:
                    projects[0].with_name("duplicate.md").write_bytes(projects[0].read_bytes())

    def base(self, temporary):
        base = Path(temporary)
        (base / "input").mkdir()
        record = {"type": "user", "cwd": "/work/demo", "message": {"role": "user", "content": "Synthetic"}}
        (base / "input" / "session.jsonl").write_text(json.dumps(record), encoding="utf-8")
        return base

    def test_repo_note_has_readme_docs_and_open_issues(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base, fake = self.base(temporary), FakeGitHub()
            output = self.sync(base, fake)
            note = (base / "vault" / "Projects" / "demo.md").read_text(encoding="utf-8")
            self.assertIn("## README\n\n# Demo\n\nInstall with pip.", note)
            self.assertIn("## docs/guide.md\n\nGuide text", note)
            self.assertNotIn("img.png", note)
            self.assertIn("- Issue #7: [Crash on \\[start\\]](https://github.com/synthetic-user/demo/issues/7)", note)
            self.assertIn("- PR #8: [Add docs](https://github.com/synthetic-user/demo/pull/8)", note)
            self.assertNotIn(TOKEN, output)
            self.assertFalse([p for p in (base / "vault").rglob("*") if p.is_file() and TOKEN in p.read_text(encoding="utf-8", errors="replace")])

    def test_token_goes_only_to_the_api_host_and_never_follows_a_redirect(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base, fake = self.base(temporary), FakeGitHub()
            self.sync(base, fake)
            self.assertEqual(5, len(fake.requests))
            for request in fake.requests:
                self.assertTrue(request.full_url.startswith(API))
                self.assertEqual(f"Bearer {TOKEN}", request.get_header("Authorization"))
                self.assertNotIn("Authorization", request.headers)

    def test_unchanged_pushed_at_makes_no_readme_request(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base, fake = self.base(temporary), FakeGitHub()
            self.sync(base, fake)
            first = (base / "vault" / "Projects" / "demo.md").read_text(encoding="utf-8")
            fake.requests.clear()
            self.sync(base, fake)
            self.assertEqual([LIST, ISSUES], [request.full_url for request in fake.requests])
            self.assertEqual(first, (base / "vault" / "Projects" / "demo.md").read_text(encoding="utf-8"))

    def test_new_push_fetches_the_readme_again(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base, fake = self.base(temporary), FakeGitHub()
            self.sync(base, fake)
            fake.requests.clear()
            fake.pushed = "2026-11-02T10:00:00Z"
            self.sync(base, fake)
            self.assertIn(REPO + "/readme", [request.full_url for request in fake.requests])

    def test_rate_limit_keeps_the_listing_and_retries_next_run(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base, fake = self.base(temporary), FakeGitHub()
            fake.limited = True
            output = self.sync(base, fake, expected_exit=1)
            self.assertIn("rate limit", output)
            note = (base / "vault" / "Projects" / "demo.md").read_text(encoding="utf-8")
            self.assertIn("A demo", note)
            self.assertNotIn("## README", note)
            fake.limited = False
            fake.requests.clear()
            self.sync(base, fake)
            self.assertIn(REPO + "/readme", [request.full_url for request in fake.requests])
            self.assertIn("## README", (base / "vault" / "Projects" / "demo.md").read_text(encoding="utf-8"))

    def test_rate_limit_on_issues_keeps_the_readme_already_fetched(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base, fake = self.base(temporary), FakeGitHub()
            fake.fail = {ISSUES: (403, {"X-RateLimit-Remaining": "0"})}
            self.assertIn("rate limit", self.sync(base, fake, expected_exit=1))
            self.assertIn("## README", (base / "vault" / "Projects" / "demo.md").read_text(encoding="utf-8"))
            fake.fail, fake.requests = {}, []
            self.sync(base, fake)
            self.assertEqual([LIST, ISSUES], [request.full_url for request in fake.requests])
            self.assertIn("- PR #8", (base / "vault" / "Projects" / "demo.md").read_text(encoding="utf-8"))

    def test_rate_limit_inside_docs_keeps_the_readme_and_retries(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base, fake = self.base(temporary), FakeGitHub()
            fake.fail = {REPO + "/contents/docs": (429, {})}
            self.assertIn("rate limit", self.sync(base, fake, expected_exit=1))
            self.assertIn("## README", (base / "vault" / "Projects" / "demo.md").read_text(encoding="utf-8"))
            fake.fail, fake.requests = {}, []
            self.sync(base, fake)
            self.assertIn(REPO + "/contents/docs", [request.full_url for request in fake.requests])
            self.assertIn("## docs/guide.md", (base / "vault" / "Projects" / "demo.md").read_text(encoding="utf-8"))

    def test_failed_refresh_keeps_saved_docs_and_empty_refresh_clears_them(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base, fake = self.base(temporary), FakeGitHub()
            self.sync(base, fake)
            fake.pushed = "2026-11-02T10:00:00Z"
            fake.fail = {REPO + "/readme": (404, {}), REPO + "/contents/docs": (429, {})}
            self.sync(base, fake, expected_exit=1)
            note = (base / "vault" / "Projects" / "demo.md").read_text(encoding="utf-8")
            self.assertIn("## README", note)
            self.assertIn("## docs/guide.md", note)
            fake.fail = {REPO + "/readme": (404, {}), REPO + "/contents/docs": (404, {})}
            self.sync(base, fake)
            note = (base / "vault" / "Projects" / "demo.md").read_text(encoding="utf-8")
            self.assertNotIn("## README", note)
            self.assertNotIn("## docs/guide.md", note)

    def test_no_open_issues_skips_the_request_and_says_none_open(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base, fake = self.base(temporary), FakeGitHub()
            fake.open_issues = 0
            self.sync(base, fake)
            self.assertNotIn(ISSUES, [request.full_url for request in fake.requests])
            self.assertIn("## Open issues and pull requests\n\nNone open.", (base / "vault" / "Projects" / "demo.md").read_text(encoding="utf-8"))

    def test_one_repo_failure_keeps_the_repo_note_and_reports_it(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base, fake = self.base(temporary), FakeGitHub()
            fake.fail = {REPO + "/readme": (500, {})}
            output = self.sync(base, fake, expected_exit=1)
            self.assertIn("synthetic-user/demo", output)
            self.assertIn("HTTP Error 500", output)
            self.assertNotIn(TOKEN, output)
            note = (base / "vault" / "Projects" / "demo.md").read_text(encoding="utf-8")
            self.assertIn("A demo", note)
            self.assertNotIn("## README", note)
            receipt = json.loads((base / "vault" / ".coldvault" / "last-run.json").read_text(encoding="utf-8"))
            self.assertEqual("failed", receipt["sources"]["github"]["state"])

    def test_incomplete_refresh_preserves_content_and_last_success(self):
        cases = [
            (REPO + "/readme", 503, "HTTP Error 503"),
            (ISSUES, 503, "HTTP Error 503"),
            (ISSUES, 429, "rate limit"),
        ]
        for url, code, reason in cases:
            with self.subTest(url=url, code=code), tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
                base, fake = self.base(temporary), FakeGitHub()
                vault = base / "vault"
                receipt_path = vault / ".coldvault" / "last-run.json"
                note = vault / "Projects" / "demo.md"
                with patch.object(coldvault, "_utc_now", return_value="2026-11-01T10:00:00Z"):
                    self.sync(base, fake)
                original = note.read_text(encoding="utf-8")
                fake.pushed = "2026-11-02T10:00:00Z"
                fake.readme = "# Demo\n\nUpdated install instructions.\n"
                fake.fail[url] = (code, {})
                with patch.object(coldvault, "_utc_now", return_value="2026-11-02T10:00:00Z"):
                    self.sync(base, fake, expected_exit=1)
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                self.assertFalse(receipt["ok"])
                self.assertEqual("2026-11-01T10:00:00Z", receipt["last_success"])
                self.assertEqual("2026-11-02T10:00:00Z", receipt["finished"])
                self.assertEqual("failed", receipt["sources"]["github"]["state"])
                self.assertIn(reason, receipt["sources"]["github"]["reason"])
                self.assertEqual(original.split("## README", 1)[1], note.read_text(encoding="utf-8").split("## README", 1)[1])

                fake.fail.clear()
                with patch.object(coldvault, "_utc_now", return_value="2026-11-03T10:00:00Z"):
                    self.sync(base, fake)
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                self.assertTrue(receipt["ok"])
                self.assertEqual("2026-11-03T10:00:00Z", receipt["last_success"])
                self.assertIn("Updated install instructions.", note.read_text(encoding="utf-8"))

    def test_unsafe_repo_names_get_no_content_requests(self):
        for name in ("../demo", "synthetic-user/..", "a/b/c", "a b/c"):
            self.assertIsNone(coldvault._REPO_NAME.fullmatch(name), name)
        self.assertIsNotNone(coldvault._REPO_NAME.fullmatch("synthetic-user/.github"))

    def test_listing_outage_keeps_projects_and_docs(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base, fake = self.base(temporary), PagedGitHub()
            self.sync(base, fake)
            fake.fail[LIST] = (503, {})
            fake.requests.clear()
            self.assert_listed(base, fake, ("first", "second"), complete=False)
            self.assertEqual([LIST], [request.full_url for request in fake.requests])

    def test_page_two_failure_merges_observed_projects_until_complete_refresh(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base, fake = self.base(temporary), PagedGitHub()
            self.sync(base, fake)
            fake.pages = (("new",), ())
            fake.fail[PAGE_TWO] = (503, {})
            self.assert_listed(base, fake, ("first", "second", "new"), complete=False)
            fake.fail = {LIST: (503, {})}
            self.assert_listed(base, fake, ("first", "second", "new"), complete=False)
            fake.fail.clear()
            self.assert_listed(base, fake, ("new",), complete=True)

    def test_first_outage_after_upgrade_keeps_legacy_project_links_and_docs(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base, fake = self.base(temporary), PagedGitHub()
            self.sync(base, fake)
            cache = base / "vault" / ".coldvault" / "github.json"
            entries = json.loads(cache.read_text(encoding="utf-8"))["repos"]
            entries.pop("synthetic-user/second")
            cache.write_text(json.dumps({"tag": "previous-kit", "repos": entries}), encoding="utf-8")
            fake.fail[LIST] = (503, {})
            self.assert_listed(base, fake, ("first", "second"), complete=False)
            fake.pages = (("new", "second"), ())
            fake.pushed = "2026-11-02T10:00:00Z"
            fake.fail = {PAGE_TWO: (503, {}), API + "repos/synthetic-user/second/readme": (503, {})}
            self.assert_listed(base, fake, ("first", "second", "new"), complete=False)
            fake.fail.clear()
            fake.pages = (("new",),)
            self.assert_listed(base, fake, ("new",), complete=True)

    def assert_listed(self, base, fake, names, *, complete):
        vault = base / "vault"
        receipt_path = vault / ".coldvault" / "last-run.json"
        previous = json.loads(receipt_path.read_text(encoding="utf-8"))["last_success"]
        self.sync(base, fake, expected_exit=0 if complete else 1)
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(complete, receipt["ok"])
        self.assertEqual("ok" if complete else "failed", receipt["sources"]["github"]["state"])
        if not complete:
            self.assertEqual(previous, receipt["last_success"])
        status = io.StringIO()
        with contextlib.redirect_stdout(status):
            self.assertEqual(0 if complete else 1, coldvault.status_main(["--vault", str(vault)]))
        self.assertIn("github: ok" if complete else "github: failed", status.getvalue())
        index = (vault / "Index.md").read_text(encoding="utf-8")
        for name in ("first", "second", "new"):
            self.assertEqual(name in names, f"[[Projects/{name}]]" in index, index)
        for name in names:
            note = (vault / "Projects" / f"{name}.md").read_text(encoding="utf-8")
            self.assertIn(f"## README\n\n{name} README", note)
            self.assertIn(f"## docs/guide.md\n\n{name} guide", note)
        if not complete:
            self.assertIn("incomplete repository listing", receipt["sources"]["github"]["reason"])


if __name__ == "__main__":
    unittest.main()
