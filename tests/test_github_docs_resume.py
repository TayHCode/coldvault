import json
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_github_sync as github


README = github.REPO + "/readme"
DOCS = github.REPO + "/contents/docs"
GUIDE = DOCS + "/guide.md"
SETUP = DOCS + "/setup.md"
PREFIX = [github.LIST, README, DOCS, GUIDE]


class BudgetedGitHub(github.FakeGitHub):
    def routes(self):
        routes = super().routes()
        names = [{"name": name, "type": "file"} for name in ("guide.md", "setup.md")]
        routes[DOCS] = (200, json.dumps(names), {})
        routes[GUIDE] = (200, f"Guide for {self.pushed}", {})
        routes[SETUP] = (200, "Setup text", {})
        return routes

    def __call__(self, request, timeout):
        if len(self.requests) == 4:
            self.fail[request.full_url] = (429, {})
        return super().__call__(request, timeout)


class GitHubDocsResumeTests(unittest.TestCase):
    def test_budgeted_runs_resume_at_the_interrupted_file(self):
        with tempfile.TemporaryDirectory(dir=github.SCRATCH) as temporary:
            cli = github.GitHubSyncTests()
            base, first = cli.base(temporary), BudgetedGitHub()
            receipt = base / "vault" / ".coldvault" / "last-run.json"
            cli.sync(base, first, expected_exit=3)
            self.assertEqual(PREFIX + [SETUP], [req.full_url for req in first.requests])
            attempt = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertFalse(attempt["ok"])
            self.assertIsNone(attempt["last_success"])
            self.assertEqual("failed", attempt["sources"]["github"]["state"])

            second = BudgetedGitHub()
            cli.sync(base, second)
            self.assertEqual([github.LIST, SETUP, github.ISSUES], [req.full_url for req in second.requests])
            note = (base / "vault" / "Projects" / "demo.md").read_text(encoding="utf-8")
            self.assertIn("## README\n\n# Demo", note)
            self.assertIn("## docs/guide.md\n\nGuide for 2026-11-01T10:00:00Z", note)
            self.assertIn("## docs/setup.md\n\nSetup text", note)
            attempt = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertTrue(attempt["ok"])
            self.assertEqual(attempt["finished"], attempt["last_success"])

    def test_new_push_discards_the_pending_prefix(self):
        with tempfile.TemporaryDirectory(dir=github.SCRATCH) as temporary:
            cli = github.GitHubSyncTests()
            base = cli.base(temporary)
            cli.sync(base, BudgetedGitHub(), expected_exit=3)

            new_push = BudgetedGitHub(pushed="2026-11-02T10:00:00Z")
            cli.sync(base, new_push, expected_exit=3)
            self.assertEqual(PREFIX + [SETUP], [req.full_url for req in new_push.requests])

            retry = BudgetedGitHub(pushed=new_push.pushed)
            cli.sync(base, retry)
            note = (base / "vault" / "Projects" / "demo.md").read_text(encoding="utf-8")
            self.assertIn("## docs/guide.md\n\nGuide for 2026-11-02T10:00:00Z", note)
