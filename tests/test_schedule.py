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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    import coldvault
finally:
    sys.path.pop(0)

KIT = Path(coldvault.__file__).resolve()
SCRATCH = KIT.parent / ".scratch"
SCRATCH.mkdir(exist_ok=True)
USER_LINES = "SHELL=/bin/sh\n# my backups\n30 2 * * * /usr/local/bin/backup --all\n"
FAKE_CRONTAB = """#!/bin/sh
if [ "$1" = "-l" ]; then
  [ -z "${FAKE_CRONTAB_READ_FAIL:-}" ] || { echo "crontab: permission denied" >&2; exit 1; }
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

    def test_doctor_reports_a_missing_expected_job_with_a_fresh_receipt(self):
        vault = str(self.vault)
        self.assertEqual(0, self.kit("sync", "--vault", vault)[0])
        self.assertEqual(0, self.kit("schedule", "install", "--vault", vault)[0])
        self.assertEqual(0, self.kit("doctor", "--vault", vault)[0])
        self.crontab.unlink()
        code, out, err = self.kit("doctor", "--vault", vault, "--json")
        self.assertEqual(1, code, err or out)
        report = json.loads(out)
        self.assertEqual("fresh", report["sources"][0]["freshness"])
        self.assertEqual({"expected_hours": 1, "state": "missing"}, report["schedule"])
        self.assertIn("schedule: missing", self.kit("doctor", "--vault", vault)[1])

    def test_doctor_checks_native_cadence_even_with_a_cron_timezone(self):
        vault = str(self.vault)
        self.assertEqual(0, self.kit("sync", "--vault", vault)[0])
        self.assertEqual(0, self.kit("schedule", "--vault", vault)[0])
        line, = self.cron_lines()
        for zone, timing, expected in (("", "0 0 * * *", "unverified"),
                                       ("CRON_TZ=UTC\n", "0 0 1 1 *", "unverified"),
                                       ("CRON_TZ=UTC\n", "0 * * * *", "installed")):
            with self.subTest(zone=zone, timing=timing):
                self.crontab.write_text(zone + line.replace("0 * * * *", timing, 1) + "\n", encoding="utf-8")
                code, out, err = self.kit("doctor", "--vault", vault, "--json")
                self.assertEqual(0 if expected == "installed" else 1, code, err or out)
                self.assertEqual(expected, json.loads(out)["schedule"]["state"])

    def test_remove_after_config_loss_preserves_other_jobs_and_config_bytes(self):
        self.crontab.write_bytes(USER_LINES.encode())
        other = self.setup_vault("other", self.base / "input")
        self.assertEqual(0, self.kit("schedule", "--vault", str(other))[0])
        survivor = self.crontab.read_bytes()
        config = self.vault / ".coldvault" / "config.json"
        saved = config.read_bytes()
        for damaged in (None, b"{", b"{}", b"\xff"):
            with self.subTest(damaged=damaged):
                config.write_bytes(saved)
                self.assertEqual(0, self.kit("schedule", "--vault", str(self.vault))[0])
                if damaged is None:
                    config.unlink()
                else:
                    config.write_bytes(damaged)
                code, out, err = self.kit("schedule", "remove", "--vault", str(self.vault))
                self.assertEqual(0, code, err or out)
                self.assertEqual(survivor, self.crontab.read_bytes())
                self.assertEqual(damaged, config.read_bytes() if config.exists() else None)

    def test_repair_and_remove_owned_job_after_moving_the_kit(self):
        self.assertEqual(0, self.kit("schedule", "--vault", str(self.vault))[0])
        current, = self.cron_lines()
        _, tail = current.split(" sync --vault ", 1)
        old = "37 9 * * 2 '/old venv/bin/python3' '/old kit/coldvault.py' sync --vault " + tail
        for action in ("install", "remove"):
            self.crontab.write_text(USER_LINES + old + "\n", encoding="utf-8")
            code, _, err = self.kit("schedule", action, "--vault", str(self.vault))
            self.assertEqual(0, code, err)
            expected = USER_LINES + (current + "\n" if action == "install" else "")
            self.assertEqual(expected.encode(), self.crontab.read_bytes())

    def test_remove_lists_ambiguous_cron_candidates_without_editing(self):
        self.assertEqual(0, self.kit("schedule", "--vault", str(self.vault))[0])
        job, = self.cron_lines()
        command, marker = job.rsplit(" #", 1)
        wrong_path = job.replace(str(self.vault), str(self.vault) + "-other")
        for candidates in ([job, job], [wrong_path], [command], [command + " #coldvault-schedule 000000000000"],
                           ["0 * * * * /bin/true #" + marker], ["# " + job],
                           [job.replace(str(KIT), "/other/backup.py")], [job.replace(sys.executable, "/bin/echo")],
                           [command + " && /bin/true #" + marker]):
            with self.subTest(candidates=candidates):
                data = ("# user job\n" + "\n".join(candidates) + "\n").encode()
                self.crontab.write_bytes(data)
                code, _, err = self.kit("schedule", "remove", "--vault", str(self.vault))
                self.assertEqual(2, code)
                for number in range(2, 2 + len(candidates)):
                    self.assertIn(f"line {number}", err)
                self.assertEqual(data, self.crontab.read_bytes())
                self.assertEqual(1, self.config()["schedule_hours"])

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
        code, _, err = self.kit("schedule", "--vault", str(self.vault), platform="win32")
        self.assertEqual(2, code)
        self.assertIn("schedule supports Linux and macOS", err)
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

    def test_status_reports_only_an_expected_interval_after_external_deletion(self):
        self.assertEqual(0, self.kit("schedule", "--vault", str(self.vault), "--hours", "6")[0])
        self.crontab.unlink()
        _, out, err = self.kit("status", "--vault", str(self.vault))
        self.assertIn("schedule: installation unverified (expected every 6h)", out)
        self.assertNotIn("next run due", out)
        self.assertEqual("", err)

    def test_status_reports_unknown_when_crontab_cannot_be_read(self):
        self.assertEqual(0, self.kit("schedule", "--vault", str(self.vault))[0])
        self.env["FAKE_CRONTAB_READ_FAIL"] = "1"
        code, out, err = self.kit("status", "--vault", str(self.vault))
        self.assertEqual(1, code)
        self.assertIn("schedule: unknown (expected every 1h)", out)
        self.assertNotIn("next run due", out)
        self.assertEqual("", err)

    def test_status_detects_the_tagged_entry_with_a_different_saved_interval(self):
        self.assertEqual(0, self.kit("schedule", "--vault", str(self.vault), "--hours", "6")[0])
        config = self.config()
        config["schedule_hours"] = 1
        (self.vault / ".coldvault" / "config.json").write_text(json.dumps(config), encoding="utf-8")
        _, out, _ = self.kit("status", "--vault", str(self.vault))
        self.assertIn("schedule: installed, next run due ", out)
        self.assertIn("(every 6h)", out)

    def test_saved_interval_and_disabled_entries_do_not_confirm_installation(self):
        self.assertEqual(0, self.kit("schedule", "--vault", str(self.vault))[0])
        line = self.cron_lines()[0]
        tag = line.rsplit("#", 1)[1]
        for disabled in ("# " + line, f"0 * * * * #{tag}"):
            with self.subTest(disabled=disabled):
                self.crontab.write_text(USER_LINES + disabled + "\n", encoding="utf-8")
                _, out, _ = self.kit("status", "--vault", str(self.vault))
                self.assertIn("schedule: installation unverified (expected every 1h)", out)
                self.assertNotIn("next run due", out)


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
        run = subprocess.run

        def edit_between_reads(command, **kwargs):
            if command[1:] == ["-l"]:
                reads.append(command)
                if len(reads) == 2:
                    self.crontab.write_text(USER_LINES + "45 * * * * /bin/true\n", encoding="utf-8")
            return run(command, **kwargs)

        with patch.object(subprocess, "run", edit_between_reads):
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
