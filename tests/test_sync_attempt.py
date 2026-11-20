import contextlib
import errno
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

KIT_FOLDER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT_FOLDER))
try:
    import coldvault
finally:
    sys.path.pop(0)


class SyncAttemptTests(unittest.TestCase):
    def setUp(self):
        scratch = KIT_FOLDER / ".scratch"
        scratch.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.transcripts = self.base / "input"
        self.transcripts.mkdir()
        record = {"type": "user", "cwd": "/work/demo", "message": {"role": "user", "content": "Synthetic"}}
        (self.transcripts / "session.jsonl").write_text(json.dumps(record), encoding="utf-8")
        self.vault = self.base / "vault"

    def kit(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with patch.dict(os.environ, {"HOME": str(self.base)}, clear=True), patch.object(sys, "argv", ["coldvault", *argv]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                coldvault.main()
            except SystemExit as exit:
                code = exit.code
        return code, out.getvalue(), err.getvalue()

    def sync(self):
        return self.kit("sync", "--transcripts", str(self.transcripts), "--username", "", "--vault", str(self.vault))

    def status(self):
        return self.kit("status", "--vault", str(self.vault))

    def receipt(self):
        return json.loads((self.vault / ".coldvault" / "last-run.json").read_text(encoding="utf-8"))

    def previous_success(self):
        old_time = "2000-01-01T00:00:00Z"
        with patch.object(coldvault, "_utc_now", return_value=old_time):
            self.assertEqual(0, self.sync()[0])
        return old_time

    def test_full_vault_leaves_the_attempt_visible(self):
        previous = self.previous_success()
        open_file = os.open

        def full_vault(path, flags, *args, **kwargs):
            if flags & os.O_CREAT and self.vault in Path(path).parents:
                raise OSError(errno.ENOSPC, "No space left on device", path)
            return open_file(path, flags, *args, **kwargs)

        with patch.object(os, "open", side_effect=full_vault):
            code, _, err = self.sync()
        self.assertEqual(2, code, err)
        self.assertIn("No space left on device", err)
        self.assertEqual(previous, self.receipt()["last_success"])
        self.assert_interrupted_then_recovers(previous)

    def test_process_exit_during_sync_leaves_the_attempt_visible(self):
        previous = self.previous_success()
        result = subprocess.run(
            [sys.executable, "-c", "import coldvault, os; coldvault._write_vault = lambda *args: os._exit(42); coldvault.main()",
             "sync", "--transcripts", str(self.transcripts), "--username", "", "--vault", str(self.vault)],
            cwd=KIT_FOLDER, env={"HOME": str(self.base)}, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(42, result.returncode, result.stderr)
        self.assert_interrupted_then_recovers(previous)

    def test_sync_recovers_after_a_partial_history_write(self):
        self.assertEqual(0, self.sync()[0])
        history = self.vault / ".coldvault" / "runs.jsonl"
        with history.open("ab") as stream:
            stream.write(b'{"reason":"\xc3')
        self.assertEqual(1, self.status()[0])
        code, _, err = self.sync()
        self.assertEqual(0, code, err)
        code, out, _ = self.status()
        self.assertEqual(0, code, out)

    def test_sync_refuses_to_write_when_it_cannot_start_an_attempt(self):
        self.assertEqual(0, self.sync()[0])
        history = self.vault / ".coldvault" / "runs.jsonl"
        previous = history.read_bytes()
        open_file = os.open

        def full_home(path, flags, *args, **kwargs):
            if flags & os.O_CREAT and self.base / ".cache" in Path(path).parents:
                raise OSError(errno.ENOSPC, "No space left on device", path)
            return open_file(path, flags, *args, **kwargs)

        with patch.object(os, "open", side_effect=full_home):
            code, _, err = self.sync()
        self.assertEqual(2, code, err)
        self.assertIn("cannot record the start of this run", err)
        self.assertIn("No space left on device", err)
        self.assertEqual(previous, history.read_bytes())

    def assert_interrupted_then_recovers(self, previous):
        code, out, _ = self.status()
        self.assertEqual(1, code, out)
        self.assertIn("sync attempt did not finish", out)
        self.assertIn(f"last success: {previous}", out)
        code, _, err = self.sync()
        self.assertEqual(0, code, err)
        code, out, _ = self.status()
        self.assertEqual(0, code, out)
        self.assertNotIn(f"last success: {previous}", out)
