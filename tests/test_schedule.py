import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from unittest.mock import patch

import coldvault

KIT = Path(coldvault.__file__).resolve()
SCRATCH = KIT.parent / ".scratch"
SCRATCH.mkdir(exist_ok=True)
USER_LINES = "SHELL=/bin/sh\n# my backups\n30 2 * * * /usr/local/bin/backup --all\n"
FAKE_CRONTAB = """#!/bin/sh
if [ "$1" = "-l" ]; then
  [ -f "$FAKE_CRONTAB" ] || { echo "no crontab for $(id -un)" >&2; exit 1; }
  cat "$FAKE_CRONTAB"
elif [ "$1" = "-" ]; then
  [ -z "${FAKE_CRONTAB_FAIL:-}" ] || { echo "crontab: disk full" >&2; exit 1; }
  cat > "$FAKE_CRONTAB"
else
  exit 2
fi
"""
@unittest.skipUnless(os.name == "posix", "the fake crontab is a shell script")
class ScheduleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.home = self.base / "home"
        self.home.mkdir()
        tools = self.base / "bin"
        tools.mkdir()
        (tools / "crontab").write_text(FAKE_CRONTAB, encoding="utf-8")
        (tools / "crontab").chmod(0o755)
        self.crontab = self.base / "crontab.txt"
        self.env = {"PATH": f"{tools}{os.pathsep}/usr/bin{os.pathsep}/bin", "HOME": str(self.home),
                    "FAKE_CRONTAB": str(self.crontab)}
        transcripts = self.base / "input"
        transcripts.mkdir()
        record = {"type": "user", "cwd": "/work/demo", "message": {"role": "user", "content": "Synthetic"}}
        (transcripts / "session.jsonl").write_text(json.dumps(record), encoding="utf-8")
        self.vault = self.setup_vault("vault", transcripts)

    def setup_vault(self, name, transcripts):
        vault = self.base / name
        (vault / ".coldvault").mkdir(parents=True)
        config = {"version": 1, "transcripts": [str(transcripts)], "notes": [], "notion_exports": [], "username": ""}
        (vault / ".coldvault" / "config.json").write_text(json.dumps(config), encoding="utf-8")
        return vault

    def kit(self, *argv, platform="linux"):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with patch.dict(os.environ, self.env, clear=True), patch.object(sys, "argv", ["coldvault", *argv]), \
                patch.object(sys, "platform", platform), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                coldvault.main()
            except SystemExit as exit:
                code = exit.code
        return code, out.getvalue(), err.getvalue()

    def config(self, vault=None):
        return json.loads(((vault or self.vault) / ".coldvault" / "config.json").read_text(encoding="utf-8"))

    def cron_lines(self):
        return self.crontab.read_text(encoding="utf-8").splitlines()

    def test_install_keeps_user_lines_and_status_reports_it(self):
        self.crontab.write_text(USER_LINES, encoding="utf-8")
        code, out, err = self.kit("schedule", "--vault", str(self.vault))
        self.assertEqual(0, code, err)
        self.assertIn("Added crontab line", out)
        lines = self.cron_lines()
        self.assertEqual(USER_LINES.splitlines(), lines[:3])
        self.assertEqual(4, len(lines))
        self.assertTrue(lines[3].startswith("0 * * * * "), lines[3])
        self.assertIn(f"sync --vault {self.vault}", lines[3])
        self.assertEqual(1, self.config()["schedule_hours"])

        self.assertEqual(0, self.kit("sync", "--vault", str(self.vault))[0])
        code, out, _ = self.kit("status", "--vault", str(self.vault))
        self.assertEqual(0, code, out)
        self.assertIn("schedule: installed, next run due ", out)

    def test_install_is_idempotent_and_takes_the_new_interval(self):
        self.crontab.write_text(USER_LINES, encoding="utf-8")
        self.kit("schedule", "--vault", str(self.vault))
        code, out, err = self.kit("schedule", "--vault", str(self.vault), "--hours", "6")
        self.assertEqual(0, code, err)
        self.assertIn("Replaced crontab line", out)
        lines = self.cron_lines()
        self.assertEqual(4, len(lines))
        self.assertTrue(lines[3].startswith("0 */6 * * * "), lines[3])
        self.assertEqual(6, self.config()["schedule_hours"])

    def test_remove_leaves_user_lines_exactly_and_no_entry(self):
        self.crontab.write_text(USER_LINES, encoding="utf-8")
        self.kit("schedule", "--vault", str(self.vault))
        code, out, err = self.kit("schedule", "remove", "--vault", str(self.vault))
        self.assertEqual(0, code, err)
        self.assertIn("Removed 1 crontab line(s)", out)
        self.assertEqual(USER_LINES, self.crontab.read_text(encoding="utf-8"))
        self.assertNotIn("schedule_hours", self.config())
        self.assertEqual(0, self.kit("sync", "--vault", str(self.vault))[0])
        self.assertIn("schedule: not installed", self.kit("status", "--vault", str(self.vault))[1])

    def test_remove_without_a_crontab_changes_nothing(self):
        code, out, err = self.kit("schedule", "remove", "--vault", str(self.vault))
        self.assertEqual(0, code, err)
        self.assertIn("No Coldvault schedule found", out)
        self.assertFalse(self.crontab.exists())

    def test_remove_touches_only_its_own_vault(self):
        other = self.setup_vault("other", self.base / "input")
        self.kit("schedule", "--vault", str(self.vault))
        self.kit("schedule", "--vault", str(other))
        self.kit("schedule", "remove", "--vault", str(self.vault))
        lines = self.cron_lines()
        self.assertEqual(1, len(lines))
        self.assertIn(f"--vault {other}", lines[0])
        self.assertEqual(1, self.config(other)["schedule_hours"])

    def test_schedule_needs_setup_first(self):
        empty = self.base / "empty"
        empty.mkdir()
        code, _, err = self.kit("schedule", "--vault", str(empty))
        self.assertEqual(2, code)
        self.assertIn("run: coldvault setup", err)
        self.assertFalse(self.crontab.exists())

    def test_hours_outside_one_to_24_are_rejected(self):
        for bad in ("0", "5", "25", "x"):
            self.assertEqual(2, self.kit("schedule", "--vault", str(self.vault), "--hours", bad)[0], bad)
        self.assertFalse(self.crontab.exists())

    def test_other_systems_are_refused(self):
        for platform in ("darwin", "win32"):
            code, _, err = self.kit("schedule", "--vault", str(self.vault), platform=platform)
            self.assertEqual(2, code, platform)
            self.assertIn("Linux only", err)
        self.assertFalse(self.crontab.exists())

    def test_installed_line_builds_the_vault_under_cron_like_environment(self):
        self.kit("schedule", "--vault", str(self.vault))
        line = self.cron_lines()[0]
        command = line.split(" ", 5)[5].replace("\\%", "%")
        result = subprocess.run(["/bin/sh", "-c", command], env={"HOME": str(self.home), "PATH": "/usr/bin:/bin"},
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue((self.vault / ".coldvault" / "runs.jsonl").is_file())

    def test_carriage_returns_in_user_lines_survive_byte_for_byte(self):
        original = b"MAILTO=me\r\n15 3 * * * /bin/echo a\rb\n"
        self.crontab.write_bytes(original)
        self.kit("schedule", "--vault", str(self.vault))
        self.assertTrue(self.crontab.read_bytes().startswith(original))
        self.kit("schedule", "remove", "--vault", str(self.vault))
        self.assertEqual(original, self.crontab.read_bytes())

    def test_a_failed_crontab_write_leaves_the_config_unscheduled(self):
        self.crontab.write_text(USER_LINES, encoding="utf-8")
        self.env["FAKE_CRONTAB_FAIL"] = "1"
        code, _, err = self.kit("schedule", "--vault", str(self.vault))
        self.assertEqual(2, code)
        self.assertIn("disk full", err)
        self.assertNotIn("schedule_hours", self.config())
        self.assertEqual(USER_LINES, self.crontab.read_text(encoding="utf-8"))

    def test_status_reports_the_next_firing_in_the_cron_zone(self):
        self.kit("schedule", "--vault", str(self.vault), "--hours", "6")
        out = self.kit("status", "--vault", str(self.vault))[1]
        self.assertIn("no run recorded", out)
        self.assertIn("schedule: installed, next run due ", out)
        self.assertEqual(0, self.kit("sync", "--vault", str(self.vault))[0])
        before = datetime.now(timezone.utc)
        self.env["TZ"] = "Asia/Kolkata"
        with patch.object(coldvault, "_cron_zone", return_value=timezone.utc):
            out = self.kit("status", "--vault", str(self.vault))[1]
        due = datetime.strptime(out.split("next run due ")[1].split(" ")[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        self.assertTrue(before < due <= before + timedelta(hours=6), out)
        self.assertEqual((0, 0, 0), (due.hour % 6, due.minute, due.second), out)


    def test_next_run_follows_daylight_saving_and_half_hour_zones(self):
        cases = [
            ("America/New_York", "2026-11-01T05:30Z", 6, "2026-11-01T11:00Z"),
            ("America/New_York", "2026-03-08T05:30Z", 6, "2026-03-08T10:00Z"),
            ("Asia/Kolkata", "2026-11-12T00:00Z", 1, "2026-11-12T00:30Z"),
            ("UTC", "2026-11-12T23:59Z", 24, "2026-11-13T00:00Z"),
            (timezone(timedelta(hours=5, minutes=31)), "2026-11-12T00:00Z", 1, "2026-11-12T00:29Z"),
        ]
        for zone, now, hours, expected in cases:
            zone = ZoneInfo(zone) if isinstance(zone, str) else zone
            moment = datetime.strptime(now, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
            self.assertEqual(expected.replace("Z", ":00Z"), coldvault._next_run(moment, hours, zone), (zone, now))


    def test_a_crontab_edited_meanwhile_is_not_overwritten(self):
        self.crontab.write_text(USER_LINES, encoding="utf-8")
        reads = []
        run = coldvault._run

        def edit_between_reads(parser, command, stdin="", missing_ok=None):
            if command[1:] == ["-l"]:
                reads.append(command)
                if len(reads) == 2:
                    self.crontab.write_text(USER_LINES + "45 * * * * /bin/true\n", encoding="utf-8")
            return run(parser, command, stdin, missing_ok)

        with patch.object(coldvault, "_run", edit_between_reads):
            code, _, err = self.kit("schedule", "--vault", str(self.vault))
        self.assertEqual(2, code)
        self.assertIn("crontab changed", err)
        self.assertEqual(USER_LINES + "45 * * * * /bin/true\n", self.crontab.read_text(encoding="utf-8"))
        self.assertNotIn("schedule_hours", self.config())


    def test_a_second_schedule_command_waits_for_the_first(self):
        import fcntl
        lock = self.home / ".cache" / "coldvault-schedule.lock"
        lock.parent.mkdir()
        with open(lock, "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            waiting = subprocess.Popen([sys.executable, str(KIT), "schedule", "--vault", str(self.vault)], env=self.env,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.addCleanup(waiting.kill)
            with self.assertRaises(subprocess.TimeoutExpired):
                waiting.wait(timeout=2)
            self.assertFalse(self.crontab.exists())
            self.assertNotIn("schedule_hours", self.config())
        out, err = waiting.communicate(timeout=60)
        self.assertEqual(0, waiting.returncode, err)
        self.assertEqual(1, len(self.cron_lines()))
        self.assertEqual(1, self.config()["schedule_hours"])

if __name__ == "__main__":
    unittest.main()
