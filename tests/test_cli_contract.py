import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


CLI = Path(__file__).resolve().parents[1] / "coldvault.py"
HISTORY_LIMIT = 1024 * 1024


class CliContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.vault = self.base / "vault"
        self.notes = self.base / "notes"
        self.notes.mkdir()
        (self.notes / "Example.md").write_text("Synthetic garden notes.\n", encoding="utf-8")
        self.env = {"HOME": str(self.base), "PATH": "", "XDG_CACHE_HOME": str(self.base / "cache"),
                    "PYTHONPATH": str(self.base), "PYTHONDONTWRITEBYTECODE": "1"}
        (self.base / "sitecustomize.py").write_text(
            "import socket\n"
            "def offline(*args):\n"
            "    raise OSError('network is unavailable in this test')\n"
            "socket.socket.connect = offline\n"
            "socket.socket.connect_ex = offline\n", encoding="utf-8")

    def cli(self, *args, code=0, answers=None):
        result = subprocess.run([sys.executable, str(CLI), *map(str, args)], cwd=self.base,
                                input=answers or "", env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(code, result.returncode, result.stdout + result.stderr)
        try:
            return json.loads(result.stdout)
        except ValueError:
            self.fail(f"expected one JSON document, got {result.stdout!r}")

    def sync(self):
        return self.cli("sync", "--json", "--vault", self.vault, "--notes", self.notes)

    def setup_vault(self):
        return self.cli("setup", "--json", answers=f"{self.vault}\n\n{self.notes}\n\n\n\n\n\n")

    def test_sync_and_status_share_the_record_without_cold_tools(self):
        receipt = self.sync()
        self.assertEqual({"version", "started", "finished", "ok", "last_success", "sources"}, set(receipt))
        self.assertEqual(1, receipt["version"])
        self.assertEqual(True, receipt["ok"])
        self.assertEqual({"state": "ok", "count": 1}, receipt["sources"]["notes"])
        self.assertEqual({"state": "skipped"}, receipt["sources"]["github"])
        status = self.status()
        self.assertEqual({"version", "ok", "receipt", "schedule", "messages"}, set(status))
        self.assertEqual(1, status["version"])
        self.assertTrue(status["ok"])
        self.assertEqual(receipt, status["receipt"])
        self.assertEqual("unknown", status["schedule"])
        self.assertIn("notes: ok (1)", status["messages"])
        self.assertTrue(list(self.vault.rglob("Example.md")))

    def status(self, code=0):
        return self.cli("status", "--json", "--vault", self.vault, code=code)

    def test_setup_keeps_prompts_off_stdout_and_exits_two_on_eof(self):
        self.assertEqual({"version": 1, "error": "setup ended before every question was answered"},
                         self.cli("setup", "--json", code=2))
        receipt = self.setup_vault()
        self.assertEqual({"state": "ok", "count": 1}, receipt["sources"]["notes"])
        self.assertEqual(receipt, self.status()["receipt"])

    def test_sync_findings_and_refusals_use_one_and_two(self):
        self.setup_vault()
        (self.notes / "Example.md").unlink()
        self.notes.rmdir()
        receipt = self.cli("sync", "--json", "--vault", self.vault, code=1)
        self.assertFalse(receipt["ok"])
        self.assertEqual("failed", receipt["sources"]["notes"]["state"])
        self.assertEqual(receipt, self.status(code=1)["receipt"])
        refused = self.cli("sync", "--json", "--vault", self.vault, "--notes", self.notes, code=2)
        self.assertEqual({"version": 1, "error": f"notes folder not found: {self.notes}"}, refused)

    def test_status_distinguishes_no_run_from_unreadable_receipt(self):
        status = self.status(code=1)
        self.assertFalse(status["ok"])
        self.assertIsNone(status["receipt"])
        state = self.vault / ".coldvault"
        state.mkdir(parents=True)
        (state / "last-run.json").write_text("{}", encoding="utf-8")
        broken = self.status(code=2)
        self.assertEqual({"version", "ok", "receipt", "schedule", "messages"}, set(broken))
        self.assertFalse(broken["ok"])
        self.assertIsNone(broken["receipt"])
        self.assertIn("cannot read", broken["messages"][0])

    @unittest.skipUnless(sys.platform == "linux", "uses a fake crontab")
    def test_schedule_install_remove_and_missing_scheduler(self):
        state = self.vault / ".coldvault"
        state.mkdir(parents=True)
        config = state / "config.json"
        config.write_text(json.dumps({"version": 1, "username": "", "transcripts": [], "notes": [],
                                      "notion_exports": []}), encoding="utf-8")
        arguments = ["schedule", "--vault", self.vault, "--json"]
        error = self.cli(*arguments, code=2)
        self.assertEqual({"version", "error"}, set(error))
        self.assertIn("cannot run crontab", error["error"])
        self.assertNotIn("schedule_hours", json.loads(config.read_text(encoding="utf-8")))
        cron = self.base / "crontab"
        cron.write_text(f"#!{sys.executable}\n"
                        "import sys\n"
                        "from pathlib import Path\n"
                        "table = Path.home() / 'table'\n"
                        "if sys.argv[1] == '-l':\n"
                        "    print(table.read_text() if table.exists() else '', end='')\n"
                        "else:\n"
                        "    table.write_text(sys.stdin.read())\n", encoding="utf-8")
        cron.chmod(0o700)
        self.env["PATH"] = str(self.base)
        common = {"version": 1, "vault": str(self.vault), "backend": "cron"}
        self.assertEqual({**common, "action": "install", "hours": 2}, self.cli(*arguments, "install", "--hours", 2))
        self.assertIn("0 */2 * * *", (self.base / "table").read_text(encoding="utf-8"))
        self.assertEqual({**common, "action": "remove", "hours": None}, self.cli(*arguments, "remove"))
        self.assertEqual("", (self.base / "table").read_text(encoding="utf-8"))

    def test_search_returns_versioned_hits_and_json_error(self):
        self.vault.mkdir()
        (self.vault / "Example.md").write_text("Synthetic garden\n", encoding="utf-8")
        self.assertEqual({"schema_version": 1, "hits": [{"path": "Example.md", "line": 1, "text": "Synthetic garden"}]},
                         self.cli("search", "garden", "--vault", self.vault, "--json"))
        self.assertEqual({"schema_version": 1, "hits": []}, self.cli("search", "missing", "--vault", self.vault, "--json", code=1))
        self.assertEqual({"schema_version": 1, "error": "vault folder not found"},
                         self.cli("search", "missing", "--vault", self.base / "absent", "--json", code=2))

    def test_release_version_identifies_the_search_results_format(self):
        version = self.cli("--version", "--json")["coldvault_version"]
        self.vault.mkdir()
        (self.vault / "Example.md").write_text("Garden", encoding="utf-8")
        result = self.cli("search", "Garden", "--vault", self.vault, "--json")
        if tuple(map(int, version.split("."))) < (0, 4, 0):
            self.assertIsInstance(result, list)
            hits = result
        else:
            self.assertEqual(1, result["schema_version"])
            hits = result["hits"]
        self.assertEqual(["Example.md"], [hit["path"] for hit in hits])

    def test_cli_version_supports_json(self):
        version = self.cli("--version", "--json")
        self.assertEqual({"version", "coldvault_version"}, set(version))
        self.assertEqual(1, version["version"])
        self.assertRegex(version["coldvault_version"], r"^\d+\.\d+\.\d+$")

    def test_sync_trims_large_run_history_and_preserves_last_run(self):
        args = ["sync", "--vault", self.vault, "--notes", self.notes]
        first = subprocess.run([sys.executable, str(CLI), *map(str, args)], env=self.env,
                               capture_output=True, text=True, timeout=20)
        self.assertEqual(0, first.returncode, first.stderr)
        history = self.vault / ".coldvault" / "runs.jsonl"
        original = history.read_bytes()
        history.write_bytes(original * (HISTORY_LIMIT // len(original) + 2) + b'{"partial":')
        second = subprocess.run([sys.executable, str(CLI), *map(str, args)], env=self.env,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertLessEqual(history.stat().st_size, HISTORY_LIMIT)
        records = [json.loads(line) for line in history.read_bytes().splitlines()]
        self.assertEqual(json.loads(original), records[-2])
        self.assertEqual(json.loads((history.parent / "last-run.json").read_bytes()), records[-1])

    def test_scheduled_sync_keeps_complete_records_at_history_limit(self):
        state = self.vault / ".coldvault"
        state.mkdir(parents=True)
        (state / "config.json").write_text(json.dumps({
            "version": 1, "username": "", "transcripts": [], "notes": [str(self.notes)],
            "notion_exports": [], "schedule_hours": 1,
        }), encoding="utf-8")
        command = [sys.executable, str(CLI), "sync", "--vault", str(self.vault)]
        first = subprocess.run(command, env=self.env, capture_output=True, timeout=20)
        self.assertEqual(0, first.returncode, first.stderr)
        history = state / "runs.jsonl"
        original = history.read_bytes()
        copies, padding = divmod(HISTORY_LIMIT - len(original), len(original))
        retained = b" " * padding + original * copies
        history.write_bytes(original * 2 + retained)

        previous = original
        for run in range(2):
            (self.notes / f"Run {run}.md").write_text("Scheduled sync fixture.\n", encoding="utf-8")
            result = subprocess.run(command, env=self.env, capture_output=True, timeout=20)
            self.assertEqual(0, result.returncode, result.stderr)
            receipt = (state / "last-run.json").read_bytes()
            data = history.read_bytes()
            self.assertLessEqual(len(data), HISTORY_LIMIT)
            self.assertNotEqual(previous, receipt)
            self.assertEqual([previous, receipt], data.splitlines(keepends=True)[-2:])
            previous = receipt
            if run == 0:
                self.assertEqual(HISTORY_LIMIT, len(data))
                self.assertTrue(data.startswith(retained))

    def test_successful_sync_releases_its_attempt_file(self):
        result = subprocess.run([sys.executable, str(CLI), "sync", "--vault", str(self.vault), "--notes", str(self.notes)],
                                env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([], list((self.base / ".cache" / "coldvault" / "sync").glob("[!.]*")))

    def test_refusal_before_creating_a_vault_releases_its_attempt_file(self):
        result = subprocess.run([sys.executable, str(CLI), "sync", "--vault", str(self.vault), "--notes",
                                 str(self.base / "absent")], env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        self.assertFalse(self.vault.exists())
        self.assertEqual([], list((self.base / ".cache" / "coldvault" / "sync").glob("[!.]*")))

    def test_completed_sync_succeeds_if_its_attempt_file_is_already_gone(self):
        script = """
import sys
from unittest.mock import patch
import coldvault

record_run = coldvault._record_run
def record_after_removal(root, *args):
    coldvault._attempt_path(root).unlink()
    return record_run(root, *args)

vault, notes = sys.argv[1:]
with patch.object(coldvault, "_record_run", record_after_removal):
    sys.argv = [sys.argv[0], "sync", "--vault", vault, "--notes", notes, "--json"]
    coldvault.main()
"""
        result = subprocess.run([sys.executable, "-c", script, str(self.vault), str(self.notes)],
                                cwd=CLI.parent, env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(json.loads((self.vault / ".coldvault" / "last-run.json").read_bytes()),
                         json.loads(result.stdout))

    def test_search_reuses_one_fallback_index_across_read_only_vaults(self):
        for name in ("alpha", "beta", "gamma"):
            vault = self.base / name
            vault.mkdir()
            (vault / ".coldvault").touch()
            (vault / f"{name}.md").write_text("Synthetic garden\n", encoding="utf-8")
            self.assertEqual([{"path": f"{name}.md", "line": 1, "text": "Synthetic garden"}],
                             self.cli("search", "garden", "--vault", vault, "--json")["hits"])
        self.assertEqual(1, len(list((self.base / "cache").glob("coldvault/*/search.sqlite"))))

    def test_search_uses_memory_when_an_index_hits_its_disk_limit(self):
        self.vault.mkdir()
        (self.vault / "Example.md").write_text("Synthetic garden " * 4096, encoding="utf-8")
        script = ("import coldvault, sys; "
                  "coldvault._SEARCH_INDEX_BYTES = 4096; "
                  "sys.argv = [sys.argv[0], 'search', 'garden', '--vault', sys.argv[1], '--json']; "
                  "coldvault.main()")
        result = subprocess.run([sys.executable, "-c", script, str(self.vault)], cwd=CLI.parent,
                                env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual("Example.md", json.loads(result.stdout)["hits"][0]["path"])
        indexes = [*self.vault.glob(".coldvault/search.sqlite"),
                   *(self.base / "cache").glob("coldvault/*/search.sqlite")]
        for index in indexes:
            self.assertLessEqual(index.stat().st_size, 4096)

    def test_sync_can_import_transcripts_larger_than_its_parse_cache(self):
        source = self.base / "sessions"
        source.mkdir()
        record = {"type": "user", "message": {"role": "user", "content": "Garden " * 1024}}
        (source / "session.jsonl").write_text(json.dumps(record), encoding="utf-8")
        script = ("import coldvault, sys; "
                  "coldvault._TRANSCRIPT_CACHE_BYTES = 1024; "
                  "sys.argv = [sys.argv[0], 'sync', '--username', '', '--transcripts', sys.argv[1], '--vault', sys.argv[2]]; "
                  "coldvault.main()")
        result = subprocess.run([sys.executable, "-c", script, str(source), str(self.vault)], cwd=CLI.parent,
                                env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        receipt = json.loads((self.vault / ".coldvault" / "last-run.json").read_bytes())
        self.assertEqual({"state": "ok", "count": 1}, receipt["sources"]["transcripts"])
        self.assertTrue(list((self.vault / "Sessions").glob("*.md")))
        self.assertFalse((self.vault / ".coldvault" / "transcripts.json").exists())
