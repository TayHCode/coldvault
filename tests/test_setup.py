import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import coldvault

KIT = Path(coldvault.__file__).resolve()
SCRATCH = KIT.parent / ".scratch"
SCRATCH.mkdir(exist_ok=True)


class SetupTests(unittest.TestCase):
    def make_home(self, base):
        transcripts = base / ".claude" / "projects"
        transcripts.mkdir(parents=True)
        record = {"type": "user", "cwd": "/work/demo", "message": {"role": "user", "content": "Transcript text"}}
        (transcripts / "session.jsonl").write_text(json.dumps(record), encoding="utf-8")
        notes = base / "My Notes"
        notes.mkdir()
        (notes / "Plan.md").write_text("# Plan\n\nGrow zucchini.\n", encoding="utf-8")
        return transcripts, notes

    def kit(self, home, *argv, answers=""):
        env = {**os.environ, "HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"}
        env.pop("GITHUB_TOKEN", None)
        return subprocess.run([sys.executable, str(KIT), *argv], input=answers, env=env, capture_output=True, text=True, timeout=60)

    def test_setup_then_sync_matches_a_run_with_every_flag(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            home = Path(temporary)
            transcripts, notes = self.make_home(home)
            vault, flagged = home / "vault", home / "flagged"
            answers = f"{vault}\n\n\n{notes}\n\n\n\n"

            setup = self.kit(home, "setup", answers=answers)
            self.assertEqual(0, setup.returncode, setup.stderr)
            self.assertIn(f"{transcripts}: 1 sessions", setup.stdout)
            config = json.loads((vault / ".coldvault" / "config.json").read_text(encoding="utf-8"))
            self.assertEqual({"version": 1, "transcripts": [str(transcripts)], "notes": [str(notes.resolve())], "notion_exports": [], "username": ""}, config)

            sync = self.kit(home, "sync", "--vault", str(vault))
            self.assertEqual(0, sync.returncode, sync.stderr)
            every_flag = self.kit(home, "--transcripts", str(transcripts), "--notes", str(notes), "--username", "", "--output", str(flagged))
            self.assertEqual(0, every_flag.returncode, every_flag.stderr)
            index = (vault / "Index.md").read_text(encoding="utf-8")
            self.assertEqual((flagged / "Index.md").read_text(encoding="utf-8"), index)
            self.assertIn("- [[Projects/demo]]", index)
            self.assertIn("- [[Imported/My Notes/Plan]]", index)

            again = self.kit(home, "sync", "--vault", str(vault))
            self.assertEqual(0, again.returncode, again.stderr)
            self.assertEqual(index, (vault / "Index.md").read_text(encoding="utf-8"))
            plain = self.kit(home, "--vault", str(vault))
            self.assertEqual(0, plain.returncode, plain.stderr)
            self.assertEqual(index, (vault / "Index.md").read_text(encoding="utf-8"))

    def run_setup(self, home, answers):
        with patch.object(Path, "home", return_value=home), patch.object(sys, "argv", ["coldvault", "setup"]), \
                patch("builtins.input", side_effect=answers), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            coldvault.main()
        return out.getvalue(), err.getvalue()

    def test_setup_without_fts5_warns_in_one_line(self):
        def no_fts5(*args, **kwargs):
            raise sqlite3.OperationalError("no such module: fts5")

        for missing in ({"sqlite3": None}, {"sqlite3": type("NoFts5", (), {"connect": staticmethod(no_fts5), "Error": sqlite3.Error})}):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
                home = Path(temporary)
                self.make_home(home)
                with patch.multiple(coldvault, **missing):
                    out, err = self.run_setup(home, [str(home / "vault"), "", "", "", "", ""])
                warnings = [line for line in (out + err).splitlines() if "FTS5" in line]
                self.assertEqual(["warning: SQLite FTS5 is not available; search will scan every note instead of using an index"], warnings)
                self.assertNotIn("Traceback", out + err)
                self.assertTrue((home / "vault" / ".coldvault" / "config.json").is_file())
                self.assertTrue((home / "vault" / "Index.md").is_file())

    def test_setup_rejects_a_missing_notes_folder_before_saving(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            home = Path(temporary)
            self.make_home(home)
            result = self.kit(home, "setup", answers=f"{home / 'vault'}\n\n\n{home / 'missing'}\n\n\n\n")
            self.assertEqual(2, result.returncode)
            self.assertIn("notes folder not found", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertFalse((home / "vault" / ".coldvault" / "config.json").exists())

    def test_sync_rejects_a_malformed_config(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            home = Path(temporary)
            self.make_home(home)
            config = home / "vault" / ".coldvault" / "config.json"
            config.parent.mkdir(parents=True)
            valid = {"version": 1, "transcripts": [], "notes": [], "notion_exports": [], "username": ""}
            texts = ["not json", json.dumps({**valid, "transcripts": "/x"}), json.dumps({**valid, "version": True}),
                     json.dumps({**valid, "notes": [""]}), json.dumps({**valid, "notes": ["a\0b"]}),
                     *(json.dumps({**valid, "folders": folders}) for folders in (
                         "Work", {"bogus": "Work"}, {"projects": ""}, {"projects": ".."}, {"projects": ".hidden"},
                         {"projects": "a/b"}, {"projects": "a\\b"}, {"projects": " Work"}, {"projects": 1},
                         {"projects": "a|b"}, {"projects": "a]b"}, {"projects": "a#b"}, {"projects": "a^b"},
                         {"projects": "Same", "sessions": "same"}, {"imported": "Projects"}))]
            for text in [*texts, "directory", "symlink"]:
                with self.subTest(text=text):
                    if config.is_dir() and not config.is_symlink():
                        config.rmdir()
                    else:
                        config.unlink(missing_ok=True)
                    if text == "directory":
                        config.mkdir()
                    elif text == "symlink":
                        (home / "elsewhere.json").write_text(json.dumps(valid), encoding="utf-8")
                        config.symlink_to(home / "elsewhere.json")
                    else:
                        config.write_text(text, encoding="utf-8")
                    result = self.kit(home, "sync", "--vault", str(home / "vault"))
                    self.assertEqual(2, result.returncode, result.stderr)
                    self.assertIn(str(config), result.stderr)
                    self.assertNotIn("Traceback", result.stderr)
                    self.assertFalse((home / "vault" / "Index.md").exists())

    def write_config(self, vault, transcripts, notes, **extra):
        config = vault / ".coldvault" / "config.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({"version": 1, "transcripts": [str(transcripts)], "notes": notes, "notion_exports": [], "username": "", **extra}), encoding="utf-8")

    def test_config_folders_name_the_generated_folders_and_their_links(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            home = Path(temporary)
            transcripts, notes = self.make_home(home)
            vault = home / "vault"
            self.write_config(vault, transcripts, [str(notes)], folders={"projects": "Work", "sessions": "Conversations", "imported": "Library"})

            result = self.kit(home, "sync", "--vault", str(vault))
            self.assertEqual(0, result.returncode, result.stderr)
            for default in ("Projects", "Sessions", "Imported"):
                self.assertFalse((vault / default).exists())
            [session] = (vault / "Conversations").iterdir()
            name = session.stem
            self.assertIn("Project: [[Work/demo]]", session.read_text(encoding="utf-8"))
            self.assertIn(f"- [[Conversations/{name}]]", (vault / "Work" / "demo.md").read_text(encoding="utf-8"))
            index = (vault / "Index.md").read_text(encoding="utf-8")
            self.assertIn("- [[Work/demo]]", index)
            self.assertIn("- [[Library/My Notes/Plan]]", index)
            self.assertIn("Grow zucchini.", (vault / "Library" / "My Notes" / "Plan.md").read_text(encoding="utf-8"))
            home_note = (vault / "Home.md").read_text(encoding="utf-8")
            self.assertIn("`Work/`", home_note)
            self.assertIn("`Conversations/`", home_note)
            self.assertIn("`Library/`", home_note)

    def test_renaming_folders_later_moves_only_managed_unedited_notes(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            home = Path(temporary)
            transcripts, notes = self.make_home(home)
            vault = home / "vault"
            (notes / "Other.md").write_text("See [[Imported/My Notes/Plan|the plan]] and `[[Imported/x]]`\n", encoding="utf-8")
            self.write_config(vault, transcripts, [str(notes)])
            self.assertEqual(0, self.kit(home, "sync", "--vault", str(vault)).returncode)
            (transcripts / "second.jsonl").write_text(json.dumps({"type": "user", "cwd": "/work/other", "message": {"role": "user", "content": "Second text"}}), encoding="utf-8")
            self.assertEqual(0, self.kit(home, "sync", "--vault", str(vault)).returncode)
            sessions = sorted((vault / "Sessions").iterdir())
            edited = next(s for s in sessions if "demo" in s.name)
            unedited = next(s for s in sessions if "other" in s.name)
            edited.write_text(edited.read_text(encoding="utf-8") + "\nMy own line.\n", encoding="utf-8")
            edited_bytes = edited.read_bytes()
            (vault / "Projects" / "mine.md").write_text("My own note\n", encoding="utf-8")
            moved_session = unedited.read_text(encoding="utf-8")

            renamed = {"projects": "Work", "sessions": "Conversations", "imported": "Library"}
            self.write_config(vault, transcripts, [], folders=renamed)
            result = self.kit(home, "sync", "--vault", str(vault))
            self.assertEqual(0, result.returncode, result.stderr)

            self.assertEqual(edited_bytes, edited.read_bytes())
            self.assertEqual("My own note\n", (vault / "Projects" / "mine.md").read_text(encoding="utf-8"))
            self.assertEqual(["mine.md"], sorted(path.name for path in (vault / "Projects").iterdir()))
            self.assertEqual([edited.name], sorted(path.name for path in (vault / "Sessions").iterdir()))
            self.assertFalse((vault / "Imported").exists())
            self.assertTrue(coldvault._is_managed_note(vault / "Library" / "My Notes" / "Plan.md"))
            other = vault / "Library" / "My Notes" / "Other.md"
            self.assertTrue(coldvault._is_managed_note(other))
            self.assertIn("See [[Library/My Notes/Plan|the plan]] and `[[Imported/x]]`", other.read_text(encoding="utf-8"))
            new_session = vault / "Conversations" / unedited.name
            self.assertIn("imported-at: " + moved_session.split("imported-at: ")[1].split("\n")[0], new_session.read_text(encoding="utf-8"))
            self.assertIn("Project: [[Work/other]]", new_session.read_text(encoding="utf-8"))
            self.assertIn(f"[[Conversations/{edited.stem}]]", (vault / "Work" / "demo.md").read_text(encoding="utf-8"))
            self.assertIn("- [[Work/other]]", (vault / "Index.md").read_text(encoding="utf-8"))
            self.assertIn("`Conversations/`", (vault / "Home.md").read_text(encoding="utf-8"))

            again = self.kit(home, "setup", answers=f"{vault}\n\n\n\n\n\n")
            self.assertEqual(0, again.returncode, again.stderr)
            self.assertEqual(renamed, json.loads((vault / ".coldvault" / "config.json").read_text(encoding="utf-8"))["folders"])
            self.assertFalse((vault / "Projects" / "demo.md").exists())

    def test_a_rename_that_cannot_finish_is_retried_and_a_swap_is_refused(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            home = Path(temporary)
            transcripts, notes = self.make_home(home)
            vault = home / "vault"
            self.write_config(vault, transcripts, [str(notes)])
            self.assertEqual(0, self.kit(home, "sync", "--vault", str(vault)).returncode)
            plan = (vault / "Imported" / "My Notes" / "Plan.md").read_bytes()
            (vault / "Home.md").write_text("My own home\n", encoding="utf-8")

            swapped = {"projects": "Sessions", "sessions": "Projects"}
            self.write_config(vault, transcripts, [str(notes)], folders=swapped)
            refused = self.kit(home, "sync", "--vault", str(vault))
            self.assertEqual(2, refused.returncode, refused.stderr)
            self.assertIn("rename it to a new name first", refused.stderr)
            self.assertTrue((vault / "Projects" / "demo.md").exists())

            self.write_config(vault, transcripts, [str(notes)], username="someone", folders={"imported": "Library"})
            env_proxy = {"https_proxy": "http://localhost:9", "HTTPS_PROXY": "http://localhost:9"}
            with patch.dict(os.environ, env_proxy):
                failed = self.kit(home, "sync", "--vault", str(vault))
            self.assertEqual(3, failed.returncode, failed.stderr)
            self.assertEqual(plan, (vault / "Imported" / "My Notes" / "Plan.md").read_bytes())

            self.write_config(vault, transcripts, [], folders={"imported": "Archive"})
            pending = self.kit(home, "sync", "--vault", str(vault))
            self.assertEqual(2, pending.returncode, pending.stderr)
            self.assertIn("has not finished", pending.stderr)

            self.write_config(vault, transcripts, [], folders={"imported": "Library"})
            outside = home / "outside"
            outside.mkdir()
            shutil.rmtree(vault / "Library" / "My Notes")
            (vault / "Library" / "My Notes").symlink_to(outside, target_is_directory=True)
            self.assertEqual(0, self.kit(home, "sync", "--vault", str(vault)).returncode)
            self.assertEqual([], list(outside.iterdir()))
            self.assertEqual(plan, (vault / "Imported" / "My Notes" / "Plan.md").read_bytes())

            (vault / "Library" / "My Notes").unlink()
            (vault / "Library" / "My Notes").mkdir()
            (vault / "Library" / "My Notes" / "Plan.md").write_text("Mine\n", encoding="utf-8")
            self.assertEqual(0, self.kit(home, "sync", "--vault", str(vault)).returncode)
            self.assertEqual(plan, (vault / "Imported" / "My Notes" / "Plan.md").read_bytes())
            self.write_config(vault, transcripts, [], folders={"imported": "Archive"})
            self.assertEqual(2, self.kit(home, "sync", "--vault", str(vault)).returncode)
            self.write_config(vault, transcripts, [], folders={"imported": "Library"})

            (vault / "Library" / "My Notes" / "Plan.md").unlink()
            self.assertEqual(0, self.kit(home, "sync", "--vault", str(vault)).returncode)
            self.assertFalse((vault / "Imported").exists())
            self.assertEqual(plan, (vault / "Library" / "My Notes" / "Plan.md").read_bytes())
            self.assertEqual("My own home\n", (vault / "Home.md").read_text(encoding="utf-8"))

    def test_a_rename_interrupted_by_an_error_stays_pending(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            home = Path(temporary)
            transcripts, notes = self.make_home(home)
            (notes / "Second.md").write_text("Second\n", encoding="utf-8")
            vault = home / "vault"
            with contextlib.redirect_stdout(io.StringIO()):
                coldvault.build_from_files([], vault, "", [str(notes)])
            calls = []

            def flaky(source, target):
                calls.append(source)
                if len(calls) == 2:
                    raise PermissionError("denied")
                os.rename(source, target)

            with patch.object(coldvault.os, "replace", flaky), contextlib.redirect_stdout(io.StringIO()), self.assertRaises(PermissionError):
                coldvault.build_from_files([], vault, "", [str(notes)], folders={"imported": "Library"})
            previous, pending = coldvault._previous_folders(vault.resolve())
            self.assertEqual("Imported", previous["imported"])
            self.assertIsNotNone(pending)
            self.assertEqual("Library", pending["imported"])

    def test_rerunning_setup_keeps_the_schedule_interval(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            home = Path(temporary)
            transcripts, _ = self.make_home(home)
            vault = home / "vault"
            self.write_config(vault, transcripts, [], schedule_hours=6)
            again = self.kit(home, "setup", answers=f"{vault}\n\n\n\n\n\n")
            self.assertEqual(0, again.returncode, again.stderr)
            self.assertEqual(6, json.loads((vault / ".coldvault" / "config.json").read_text(encoding="utf-8"))["schedule_hours"])

    def test_setup_never_replaces_a_config_it_did_not_write(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            home = Path(temporary)
            self.make_home(home)
            config = home / "vault" / ".coldvault" / "config.json"
            config.parent.mkdir(parents=True)
            config.write_text('{"theme": "mine"}', encoding="utf-8")
            result = self.kit(home, "setup", answers=f"{home / 'vault'}\n\n\n\n\n\n")
            self.assertEqual(2, result.returncode)
            self.assertIn("is not a Coldvault config", result.stderr)
            self.assertEqual('{"theme": "mine"}', config.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
