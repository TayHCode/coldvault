import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

import coldvault

KIT = Path(coldvault.__file__).resolve()
SCRATCH = KIT.parent / ".scratch"
SCRATCH.mkdir(exist_ok=True)
TOKEN = "ghp_" + "Q" * 36


def stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class StatusTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.transcripts = self.base / "input"
        self.transcripts.mkdir()
        record = {"type": "user", "cwd": "/work/demo", "message": {"role": "user", "content": "Synthetic"}}
        (self.transcripts / "session.jsonl").write_text(json.dumps(record), encoding="utf-8")
        self.vault = self.base / "vault"

    def kit(self, *argv, urlopen=None, env=None):
        out, err = io.StringIO(), io.StringIO()
        network = patch.object(coldvault, "urlopen", side_effect=urlopen or URLError("network is unreachable"))
        code = 0
        with patch.dict(os.environ, env or {}, clear=True), patch.object(sys, "argv", ["coldvault", *argv]), network, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                coldvault.main()
            except SystemExit as exit:
                code = exit.code
        return code, out.getvalue(), err.getvalue()

    def sync(self, username="", **options):
        return self.kit("sync", "--transcripts", str(self.transcripts), "--username", username, "--vault", str(self.vault), **options)

    def status(self):
        return self.kit("status", "--vault", str(self.vault))

    def receipt(self):
        return json.loads((self.vault / ".coldvault" / "last-run.json").read_text(encoding="utf-8"))

    def test_offline_github_fails_the_run_and_status(self):
        code, _, err = self.sync("synthetic-user")
        self.assertEqual(3, code)
        self.assertIn("github: failed", err)
        self.assertTrue((self.vault / "Index.md").is_file())
        receipt = self.receipt()
        self.assertFalse(receipt["ok"])
        self.assertIsNone(receipt["last_success"])
        self.assertEqual({"state": "ok", "count": 1}, receipt["sources"]["transcripts"])
        self.assertEqual("failed", receipt["sources"]["github"]["state"])
        self.assertIn("network is unreachable", receipt["sources"]["github"]["reason"])

        code, out, _ = self.status()
        self.assertNotEqual(0, code)
        self.assertIn("github: failed (", out)
        self.assertIn("transcripts: ok (1)", out)
        self.assertIn("last success: never", out)

    def test_clean_run_exits_zero_and_status_is_ok(self):
        code, _, err = self.sync()
        self.assertEqual(0, code, err)
        receipt = self.receipt()
        self.assertTrue(receipt["ok"])
        self.assertEqual(receipt["finished"], receipt["last_success"])
        self.assertEqual({"state": "skipped"}, receipt["sources"]["github"])
        self.assertEqual({"state": "skipped"}, receipt["sources"]["notes"])
        self.assertEqual({"state": "skipped"}, receipt["sources"]["notion"])

        code, out, _ = self.status()
        self.assertEqual(0, code, out)
        self.assertIn("transcripts: ok (1)", out)
        self.assertIn("github: skipped", out)
        self.assertIn("schedule: not installed", out)

    def test_every_run_appends_one_history_line(self):
        self.sync()
        self.sync("synthetic-user")
        lines = (self.vault / ".coldvault" / "runs.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual([True, False], [json.loads(line)["ok"] for line in lines])
        self.assertEqual(json.loads(lines[0])["finished"], self.receipt()["last_success"])

    def write_config(self, **extra):
        config = {"version": 1, "transcripts": [str(self.transcripts)], "notes": [], "notion_exports": [], "username": "", **extra}
        (self.vault / ".coldvault" / "config.json").write_text(json.dumps(config), encoding="utf-8")

    def age_receipt(self, hours):
        receipt = self.receipt()
        then = datetime.now(timezone.utc) - timedelta(hours=hours)
        receipt.update(started=stamp(then), finished=stamp(then), last_success=stamp(then))
        (self.vault / ".coldvault" / "last-run.json").write_text(json.dumps(receipt), encoding="utf-8")
        (self.vault / ".coldvault" / "runs.jsonl").write_text(json.dumps(receipt) + "\n", encoding="utf-8")

    def test_old_success_with_an_hourly_schedule_is_stale(self):
        self.assertEqual(0, self.sync()[0])
        self.write_config(schedule_hours=1)
        self.age_receipt(3)
        code, out, _ = self.status()
        self.assertNotEqual(0, code)
        self.assertIn("stale", out)
        self.assertIn("schedule: installed, next run due ", out)
        self.assertIn("(every 1h)", out)

    def test_recent_success_with_an_hourly_schedule_is_fresh(self):
        self.assertEqual(0, self.sync()[0])
        self.write_config(schedule_hours=1)
        self.age_receipt(1.5)
        code, out, _ = self.status()
        self.assertEqual(0, code, out)
        self.assertNotIn("stale", out)

    def test_invalid_schedule_is_rejected(self):
        self.assertEqual(0, self.sync()[0])
        for bad in (0, -1, True, 1.5, "1"):
            self.write_config(schedule_hours=bad)
            code, _, err = self.status()
            self.assertEqual(2, code, bad)
            self.assertIn("not a Coldvault config", err)

    def test_status_without_a_run_fails(self):
        self.vault.mkdir()
        code, out, _ = self.status()
        self.assertNotEqual(0, code)
        self.assertIn("no run recorded", out)

    def test_malformed_receipt_fails(self):
        self.assertEqual(0, self.sync()[0])
        path = self.vault / ".coldvault" / "last-run.json"
        good = self.receipt()
        for bad in ("not json", json.dumps({**good, "finished": "yesterday"}), json.dumps({**good, "ok": "yes"}),
                    json.dumps({**good, "sources": {"github": {"state": "fine"}}}),
                    json.dumps({**good, "sources": {}}), json.dumps({**good, "last_success": None}),
                    json.dumps({**good, "sources": {"github": {"state": "ok"}}})):
            path.write_text(bad, encoding="utf-8")
            code, out, _ = self.status()
            self.assertNotEqual(0, code, bad)
            self.assertIn("cannot read", out)

    def test_missing_source_folder_is_recorded_as_failed(self):
        notes = self.base / "notes"
        notes.mkdir()
        code, _, err = self.kit("sync", "--transcripts", str(self.transcripts), "--notes", str(notes), "--username", "", "--vault", str(self.vault))
        self.assertEqual(0, code, err)
        notes.rmdir()
        code, _, err = self.kit("sync", "--transcripts", str(self.transcripts), "--notes", str(notes), "--username", "", "--vault", str(self.vault))
        self.assertEqual(2, code)
        self.assertIn("notes folder not found", err)
        code, out, _ = self.status()
        self.assertNotEqual(0, code)
        self.assertIn("sources: failed (notes folder not found", out)
        self.assertIsNotNone(self.receipt()["last_success"])

    def test_first_run_with_a_missing_source_is_recorded(self):
        self.vault.mkdir()
        code, _, err = self.kit("sync", "--transcripts", str(self.base / "absent"), "--username", "", "--vault", str(self.vault))
        self.assertEqual(2, code)
        self.assertIn("transcript folder not found", err)
        code, out, _ = self.status()
        self.assertNotEqual(0, code)
        self.assertIn("sources: failed (transcript folder not found", out)
        self.assertIn("last success: never", out)

    def test_history_is_written_without_o_nofollow(self):
        with patch.object(coldvault.os, "O_NOFOLLOW", 0, create=True):
            del coldvault.os.O_NOFOLLOW
            code, _, err = self.sync()
        self.assertEqual(0, code, err)
        self.assertEqual(1, len((self.vault / ".coldvault" / "runs.jsonl").read_text(encoding="utf-8").splitlines()))

    def test_receipt_not_replaced_after_the_history_append_fails_status(self):
        self.assertEqual(0, self.sync()[0])
        real = coldvault._atomic_write

        def full_disk(target, text):
            if target.name == "last-run.json":
                raise OSError(28, "No space left on device")
            return real(target, text)

        with patch.object(coldvault, "_atomic_write", side_effect=full_disk):
            code, _, err = self.sync("synthetic-user")
        self.assertEqual(2, code)
        self.assertIn("cannot record this run", err)
        code, out, _ = self.status()
        self.assertNotEqual(0, code)
        self.assertIn("does not match the last line", out)

    def test_unrecordable_history_fails_the_run_and_status(self):
        self.assertEqual(0, self.sync()[0])
        history = self.vault / ".coldvault" / "runs.jsonl"
        history.unlink()
        history.mkdir()
        code, _, err = self.sync()
        self.assertEqual(2, code)
        self.assertIn("cannot record this run", err)
        code, out, _ = self.status()
        self.assertNotEqual(0, code)
        self.assertIn("history: failed (", out)

    def test_symlinked_receipt_is_refused(self):
        self.assertEqual(0, self.sync()[0])
        outside = self.base / "outside.jsonl"
        outside.write_text("", encoding="utf-8")
        history = self.vault / ".coldvault" / "runs.jsonl"
        history.unlink()
        history.symlink_to(outside)
        code, _, err = self.sync()
        self.assertNotEqual(0, code)
        self.assertEqual("", outside.read_text(encoding="utf-8"))
        code, out, _ = self.status()
        self.assertNotEqual(0, code)
        self.assertIn("is a symlink", out)

    def test_no_secret_appears_in_any_receipt(self):
        def leak(request, timeout):
            raise URLError(f"refused with {request.get_header('Authorization')}")

        code, out, err = self.sync("synthetic-user", urlopen=leak, env={"GITHUB_TOKEN": TOKEN})
        self.assertEqual(3, code)
        self.assertNotIn(TOKEN, out + err)
        status_code, status_out, status_err = self.status()
        self.assertNotEqual(0, status_code)
        self.assertNotIn(TOKEN, status_out + status_err)
        state = self.vault / ".coldvault"
        for name in ("last-run.json", "runs.jsonl"):
            self.assertNotIn(TOKEN, (state / name).read_text(encoding="utf-8"))
        self.assertIn("github: failed (", status_out)

    def test_malformed_token_is_a_failure_not_a_skip(self):
        code, _, err = self.sync("synthetic-user", env={"GITHUB_TOKEN": "ghp_bad token"})
        self.assertEqual(3, code)
        self.assertEqual("failed", self.receipt()["sources"]["github"]["state"])
        self.assertNotIn("bad token", json.dumps(self.receipt()) + err)


if __name__ == "__main__":
    unittest.main()
