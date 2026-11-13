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

import coldvault


SCRATCH = Path(__file__).resolve().parents[1] / ".scratch"
SCRATCH.mkdir(exist_ok=True)
TOKEN = "ghp_" + "T" * 36
API = "https://api.github.com/"
LIST = API + "users/synthetic-user/repos?per_page=100&sort=updated"
REPO = API + "repos/synthetic-user/demo"
ISSUES = REPO + "/issues?state=open&per_page=100"


class FakeGitHub:
    def __init__(self, pushed="2026-11-01T10:00:00Z"):
        self.requests = []
        self.pushed = pushed
        self.limited = False
        self.fail = {}
        self.open_issues = 2

    def routes(self):
        return {
            LIST: (200, json.dumps([{"name": "demo", "full_name": "synthetic-user/demo", "html_url": "https://github.com/synthetic-user/demo",
                                     "description": "A demo", "pushed_at": self.pushed, "open_issues_count": self.open_issues}]), {}),
            REPO + "/readme": (200, "# Demo\n\nInstall with pip.\n", {}),
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


class GitHubSyncTests(unittest.TestCase):
    def sync(self, base, fake):
        argv = ["coldvault.py", "--username", "synthetic-user", "--transcripts", str(base / "input"), "--output", str(base / "vault")]
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"GITHUB_TOKEN": TOKEN}), patch.object(sys, "argv", argv), \
                patch.object(coldvault, "urlopen", side_effect=fake), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            coldvault.main()
        return out.getvalue() + err.getvalue()

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
            output = self.sync(base, fake)
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
            self.assertIn("rate limit", self.sync(base, fake))
            self.assertIn("## README", (base / "vault" / "Projects" / "demo.md").read_text(encoding="utf-8"))
            fake.fail, fake.requests = {}, []
            self.sync(base, fake)
            self.assertEqual([LIST, ISSUES], [request.full_url for request in fake.requests])
            self.assertIn("- PR #8", (base / "vault" / "Projects" / "demo.md").read_text(encoding="utf-8"))

    def test_rate_limit_inside_docs_keeps_the_readme_and_retries(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            base, fake = self.base(temporary), FakeGitHub()
            fake.fail = {REPO + "/contents/docs": (429, {})}
            self.assertIn("rate limit", self.sync(base, fake))
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
            fake.fail = {REPO + "/contents/docs": (429, {})}
            self.sync(base, fake)
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
            output = self.sync(base, fake)
            self.assertIn("synthetic-user/demo", output)
            self.assertIn("HTTP Error 500", output)
            self.assertNotIn(TOKEN, output)
            note = (base / "vault" / "Projects" / "demo.md").read_text(encoding="utf-8")
            self.assertIn("A demo", note)
            self.assertNotIn("## README", note)
            receipt = json.loads((base / "vault" / ".coldvault" / "last-run.json").read_text(encoding="utf-8"))
            self.assertEqual("ok", receipt["sources"]["github"]["state"])

    def test_unsafe_repo_names_get_no_content_requests(self):
        for name in ("../demo", "synthetic-user/..", "a/b/c", "a b/c"):
            self.assertIsNone(coldvault._REPO_NAME.fullmatch(name), name)
        self.assertIsNotNone(coldvault._REPO_NAME.fullmatch("synthetic-user/.github"))


if __name__ == "__main__":
    unittest.main()
