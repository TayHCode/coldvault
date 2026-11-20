import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone, tzinfo
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    import coldvault
finally:
    sys.path.pop(0)


class Havana2026(tzinfo):
    def fromutc(self, moment):
        utc = moment.replace(tzinfo=timezone.utc)
        spring = datetime(2026, 3, 8, 5, tzinfo=timezone.utc)
        fall = datetime(2026, 11, 1, 5, tzinfo=timezone.utc)
        offset = -4 if spring <= utc < fall else -5
        return utc.astimezone(timezone(timedelta(hours=offset)))


class ScheduleReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        home = Path(self.temp.name).resolve()
        self.vault = home / "vault"
        config = self.vault / ".coldvault" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({
            "version": 1, "transcripts": [], "notes": [], "notion_exports": [], "username": "",
        }), encoding="utf-8")
        self.table = home / "table"
        self.env = {"HOME": str(home), "USERPROFILE": str(home)}
        self.table.write_text("", encoding="utf-8")

    def crontab(self, command, stdin="", missing_ok=None):
        if command == ["crontab", "-l"]:
            return self.table.read_bytes().decode("utf-8")
        self.assertEqual(["crontab", "-"], command)
        self.table.write_bytes(stdin.encode("utf-8"))

    def schedule(self, *args):
        lock = SimpleNamespace(flock=Mock(), LOCK_EX=2)
        with patch.dict(os.environ, self.env, clear=True), patch.dict(sys.modules, fcntl=lock), \
                patch.object(sys, "platform", "linux"), patch.object(coldvault, "_run", self.crontab), \
                redirect_stdout(StringIO()):
            self.assertEqual(0, coldvault.schedule_main(["--vault", str(self.vault), *args]))
        return self.table.read_bytes()

    def status(self, at, zone):
        output = StringIO()
        now = datetime.fromisoformat(at).replace(tzinfo=timezone.utc)
        with patch.dict(os.environ, self.env, clear=True), patch.object(sys, "platform", "linux"), \
                patch.object(coldvault, "_run", self.crontab), \
                patch.object(coldvault, "_cron_zone", return_value=zone), redirect_stdout(output):
            coldvault.status_main(["--vault", str(self.vault)], now=now)
        return output.getvalue()

    def test_replacing_the_tag_keeps_its_place_between_user_entries(self):
        tagged = self.schedule()
        before = b"CRON_TZ=Asia/Kolkata\n15 3 * * * /bin/true\n"
        after = b"\nCRON_TZ=UTC\n45 4 * * * /bin/true\n"
        self.table.write_bytes(before + tagged.removesuffix(b"\n") + b" \t\n" + after)
        self.assertEqual(before + tagged.replace(b"0 * ", b"0 */6 ", 1) + after, self.schedule("--hours", "6"))
        self.assertEqual(before + after, self.schedule("remove"))

    def test_timezone_override_only_affects_following_entries(self):
        tagged = self.schedule()
        for override in (b'\tCRON_TZ = "Asia/Kolkata"\n', b"'CRON_TZ'=Asia/Kolkata\n"):
            with self.subTest(override=override):
                self.table.write_bytes(override + tagged)
                out = self.status("2026-11-12T00:00", timezone.utc)
                self.assertIn("schedule: installed, next run due unknown", out)
                self.assertIn("CRON_TZ", out)
        self.table.write_bytes(b"# CRON_TZ=Asia/Kolkata\n" + tagged + b"CRON_TZ=Asia/Kolkata\n")
        self.assertIn("next run due 2026-11-12T01:00:00Z", self.status("2026-11-12T00:00", timezone.utc))

    def test_daily_midnight_near_dst_is_unknown(self):
        self.schedule("--hours", "24")
        havana = Havana2026()
        self.assertIn("next run due unknown", self.status("2026-03-08T04:30", havana))
        self.assertIn("next run due unknown", self.status("2026-11-01T04:30", havana))
        self.assertIn("next run due 2026-03-07T05:00:00Z", self.status("2026-03-07T04:30", havana))

    def test_unreadable_daemon_timezone_is_unknown(self):
        self.schedule()
        self.assertIn("next run due unknown", self.status("2026-11-12T00:00", None))

    def test_tagged_entry_with_an_edited_command_has_unknown_due_time(self):
        tagged = self.schedule()
        self.table.write_bytes(b"0 * * * * /bin/true #" + tagged.split(b"#", 1)[1])
        out = self.status("2026-11-12T00:00", timezone.utc)
        self.assertIn("next run due unknown", out)
        self.assertIn("crontab command differs", out)
