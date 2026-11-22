import io
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    import coldvault
finally:
    sys.path.pop(0)


class LaunchAgentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.vault = self.home / "vault & notes"
        self.config = self.vault / ".coldvault" / "config.json"
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps({
            "version": 1, "transcripts": [], "notes": [], "notion_exports": [], "username": "",
        }), encoding="utf-8")
        self.jobs = {}
        self.fail_bootstrap = False
        self.fail_print = False
        self.command = [sys.executable, str(Path(coldvault.__file__).resolve()), "sync", "--vault", str(self.vault)]

    def launchctl(self, command, stdin="", missing_ok=None):
        _, verb, target, *paths = command
        if verb == "bootstrap":
            if self.fail_bootstrap:
                self.fail_bootstrap = False
                raise OSError("Bootstrap failed")
            path = Path(paths[0])
            job = plistlib.loads(path.read_bytes())
            service = target + "/" + job["Label"]
            self.assertNotIn(service, self.jobs)
            self.jobs[service] = (path, job)
        elif verb == "bootout":
            del self.jobs[target]
        else:
            self.assertEqual("print", verb)
            if self.fail_print:
                raise OSError("Permission denied")
            if target not in self.jobs:
                self.assertEqual("could not find service", missing_ok)
                return ""
            path, job = self.jobs[target]
            arguments = "\n".join(f"\t\t{arg}" for arg in job["ProgramArguments"])
            triggers = ""
            for index, interval in enumerate(job["StartCalendarInterval"]):
                descriptor = "\n".join(f'\t\t\t\t"{key}" => {value}' for key, value in interval.items())
                triggers += (f"\t\t{job['Label']}.{index} => {{\n"
                             "\t\t\tstream = com.apple.launchd.calendarinterval\n"
                             f"\t\t\tdescriptor = {{\n{descriptor}\n\t\t\t}}\n\t\t}}\n")
            return (f"{target} = {{\n\tpath = {path}\n\tprogram = {job['ProgramArguments'][0]}\n"
                    f"\targuments = {{\n{arguments}\n\t}}\n\tevent triggers = {{\n{triggers}\t}}\n}}\n")

    def cli(self, entry, *args, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"HOME": str(self.home)}, clear=True), \
                patch.object(sys, "platform", "darwin"), patch.object(coldvault, "_run", self.launchctl), \
                redirect_stdout(out), redirect_stderr(err):
            try:
                code = entry([*args, "--vault", str(self.vault)], **kwargs)
            except SystemExit as error:
                code = error.code
        return code, out.getvalue(), err.getvalue()

    def install(self, *args):
        code, _, error = self.cli(coldvault.schedule_main, "install", *args)
        self.assertEqual(0, code, error)

    def status(self):
        with patch.object(coldvault, "_cron_zone", return_value=timezone.utc):
            return self.cli(coldvault.status_main, now=datetime(2026, 11, 20, 13, tzinfo=timezone.utc))[1]

    def test_install_replaces_one_vault_job_and_remove_keeps_the_note(self):
        note = self.vault / "Keep.md"
        note.write_text("Keep this note.\n", encoding="utf-8")
        self.install()
        service, = self.jobs
        path, job = self.jobs[service]
        self.assertEqual(self.home / "Library" / "LaunchAgents", path.parent)
        self.assertEqual(self.command, job["ProgramArguments"])
        self.assertIs(True, job["RunAtLoad"])
        self.assertEqual([{"Minute": 0}], job["StartCalendarInterval"])
        self.assertIn("schedule: installed, next run due 2026-11-20T14:00:00Z (every 1h)", self.status())
        self.install("--hours", "6")
        self.assertEqual([service], list(self.jobs))
        self.assertIn("next run due 2026-11-20T18:00:00Z (every 6h)", self.status())
        for _ in range(2):
            code, _, error = self.cli(coldvault.schedule_main, "remove")
            self.assertEqual(0, code, error)
        self.assertFalse(path.exists())
        self.assertEqual({}, self.jobs)
        self.assertIn("schedule: not installed", self.status())
        self.assertEqual("Keep this note.\n", note.read_text(encoding="utf-8"))

    def test_status_reads_loaded_job_and_distinguishes_missing_from_unreadable(self):
        self.install()
        service, = self.jobs
        path, job = self.jobs[service]
        job["StartCalendarInterval"] = [{"Minute": 23}]
        self.assertIn("schedule: installed, next run due unknown", self.status())
        del self.jobs[service]
        self.assertTrue(path.exists())
        self.assertIn("schedule: installation unverified (expected every 1h)", self.status())
        self.fail_print = True
        self.assertIn("schedule: unknown (expected every 1h)", self.status())

    def test_doctor_checks_loaded_interval_against_saved_interval(self):
        self.install()
        (_, job), = self.jobs.values()
        job["StartCalendarInterval"] = [{"Hour": 0, "Minute": 0}]
        code, out, err = self.cli(coldvault.doctor_main, "--json")
        self.assertEqual(1, code, err)
        self.assertEqual({"expected_hours": 1, "state": "unverified"}, json.loads(out)["schedule"])

    def test_bootstrap_failure_restores_previous_schedule(self):
        self.install()
        self.fail_bootstrap = True
        code, _, error = self.cli(coldvault.schedule_main, "install", "--hours", "6")
        self.assertEqual(2, code)
        self.assertIn("Bootstrap failed", error)
        service, = self.jobs
        path, job = self.jobs[service]
        self.assertEqual(job, plistlib.loads(path.read_bytes()))
        self.assertEqual(1, json.loads(self.config.read_text())["schedule_hours"])
        self.assertIn("next run due 2026-11-20T14:00:00Z (every 1h)", self.status())

    def test_installed_arguments_record_a_run_with_only_home_set(self):
        self.install()
        (_, job), = self.jobs.values()
        result = subprocess.run(job["ProgramArguments"], cwd=self.home, env={"HOME": str(self.home)}, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stderr)
        receipts = (self.config.parent / "runs.jsonl").read_text(encoding="utf-8").splitlines()
        receipt, = map(json.loads, receipts)
        self.assertIs(True, receipt["ok"])

    def test_remove_unloads_the_job_when_its_plist_is_already_gone(self):
        self.install()
        (path, _), = self.jobs.values()
        path.unlink()
        code, _, error = self.cli(coldvault.schedule_main, "remove")
        self.assertEqual(0, code, error)
        self.assertEqual({}, self.jobs)

    def test_remove_with_corrupt_config_checks_loaded_and_saved_ownership(self):
        self.install()
        service, = self.jobs
        path, job = self.jobs[service]
        self.config.write_bytes(b"{")
        for changed in ("loaded arguments", "saved arguments", "saved label", "invalid plist"):
            with self.subTest(changed=changed):
                foreign = {**job, "ProgramArguments": [*self.command[:-1], str(self.home / "other")]}
                self.jobs[service] = (path, foreign if changed == "loaded arguments" else job)
                saved = (b"{" if changed == "invalid plist" else plistlib.dumps(
                    foreign if changed == "saved arguments" else
                    {**job, "Label": "another.job"} if changed == "saved label" else job))
                path.write_bytes(saved)
                code, _, err = self.cli(coldvault.schedule_main, "remove")
                self.assertEqual(2, code)
                self.assertIn(str(path), err)
                self.assertIn(service, self.jobs)
                self.assertEqual(saved, path.read_bytes())
        path.write_bytes(plistlib.dumps(job))
        code, _, err = self.cli(coldvault.schedule_main, "remove")
        self.assertEqual(0, code, err)
        self.assertFalse(path.exists())
        self.assertFalse(self.jobs)
        self.assertEqual(b"{", self.config.read_bytes())

    def test_remove_owned_job_after_moving_the_kit(self):
        self.install()
        (path, job), = self.jobs.values()
        job["ProgramArguments"][:2] = ["/old venv/bin/python3", "/old kit/coldvault.py"]
        path.write_bytes(plistlib.dumps(job))
        self.config.unlink()
        code, _, err = self.cli(coldvault.schedule_main, "remove")
        self.assertEqual(0, code, err)
        self.assertFalse(path.exists())
        self.assertFalse(self.jobs)

    def test_remove_refuses_a_job_loaded_from_another_path(self):
        self.install()
        service, = self.jobs
        path, job = self.jobs[service]
        self.jobs[service] = (self.home / "other.plist", job)
        code, _, error = self.cli(coldvault.schedule_main, "remove")
        self.assertEqual(2, code)
        self.assertIn("was loaded from another path", error)
        self.assertTrue(path.exists())
        self.assertIn(service, self.jobs)

    def test_install_leaves_a_symlinked_plist_and_its_target_intact(self):
        self.install()
        (path, _), = self.jobs.values()
        code, _, error = self.cli(coldvault.schedule_main, "remove")
        self.assertEqual(0, code, error)
        other = self.home / "other.plist"
        other.write_text("another job\n", encoding="utf-8")
        path.symlink_to(other)
        code, _, error = self.cli(coldvault.schedule_main, "install")
        self.assertEqual(2, code)
        self.assertIn("cannot change symlink", error)
        self.assertTrue(path.is_symlink())
        self.assertEqual("another job\n", other.read_text(encoding="utf-8"))
