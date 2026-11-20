import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1] / "coldvault.py"


class SavedSourceTests(unittest.TestCase):
    def kit(self, home, *argv, answers=""):
        env = {**os.environ, "HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"}
        env.pop("GITHUB_TOKEN", None)
        return subprocess.run([sys.executable, str(KIT), *argv], input=answers, env=env, capture_output=True, text=True, timeout=60)

    def test_sync_keeps_daily_links_after_a_transcript_expires(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            transcripts = home / "transcripts"
            transcripts.mkdir()
            vault = home / "vault"
            for name, day in (("expiring", "2026-01-02"), ("active", "2026-01-03")):
                record = {
                    "type": "user", "cwd": "/work/demo", "timestamp": f"{day}T12:00:00Z",
                    "message": {"role": "user", "content": f"{name} transcript"},
                }
                (transcripts / f"{name}.jsonl").write_text(json.dumps(record), encoding="utf-8")

            def sync():
                result = self.kit(home, "sync", "--vault", str(vault), "--username", "", "--transcripts", str(transcripts))
                self.assertEqual(0, result.returncode, result.stderr)

            sync()
            session, = (path for path in (vault / "Sessions").glob("*.md")
                        if "expiring transcript" in path.read_text(encoding="utf-8"))
            session_text = session.read_text(encoding="utf-8")
            link = f"- [[Sessions/{session.stem}]] · [[Projects/demo]]"
            daily = vault / "Daily/2026-01-02.md"
            self.assertIn(link, daily.read_text(encoding="utf-8"))

            (transcripts / "expiring.jsonl").unlink()
            sync()
            self.assertIn(link, daily.read_text(encoding="utf-8") if daily.exists() else "")

            edited = daily.read_text(encoding="utf-8") + "\nMy Daily edit.\n"
            daily.write_text(edited, encoding="utf-8")
            generated = vault / "Daily/2026-01-02 (Coldvault).md"
            for _ in range(2):
                sync()
                self.assertEqual(session_text, session.read_text(encoding="utf-8"))
                self.assertEqual(edited, daily.read_text(encoding="utf-8"))
                self.assertEqual(1, generated.read_text(encoding="utf-8").count(link))
                self.assertCountEqual([daily, generated], vault.glob("Daily/2026-01-02*.md"))

    def test_sync_imports_markdown_when_a_saved_transcript_folder_is_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            transcripts = home / ".claude" / "projects"
            transcripts.mkdir(parents=True)
            session = transcripts / "session.jsonl"
            record = {
                "type": "user", "cwd": "/work/demo", "timestamp": "2026-01-02T12:00:00Z",
                "message": {"role": "user", "content": "Transcript text"},
            }
            session.write_text(json.dumps(record), encoding="utf-8")
            notes = home / "notes"
            notes.mkdir()
            (notes / "Plan.md").write_text("# Plan\n\nGrow zucchini.\n", encoding="utf-8")
            other_transcripts = home / "other-transcripts"
            other_transcripts.mkdir()
            vault = home / "vault"
            setup = self.kit(home, "setup", answers=f"{vault}\n\n{other_transcripts}\n\n{notes}\n\n\n\n\n\n")
            self.assertEqual(0, setup.returncode, setup.stderr)
            receipt_path = vault / ".coldvault" / "last-run.json"
            previous_success = json.loads(receipt_path.read_text(encoding="utf-8"))["last_success"]
            daily = vault / "Daily/2026-01-02.md"
            daily_text = daily.read_text(encoding="utf-8")
            self.assertIn("[[Sessions/", daily_text)

            moved = transcripts.rename(home / "moved")
            (notes / "Plan.md").write_text("# Plan\n\nPlant beans.\n", encoding="utf-8")
            sync = self.kit(home, "sync", "--vault", str(vault))
            self.assertEqual(3, sync.returncode, sync.stderr)
            self.assertIn("Plant beans.", (vault / "Imported/notes/Plan.md").read_text(encoding="utf-8"))
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertFalse(receipt["ok"])
            self.assertEqual(previous_success, receipt["last_success"])
            self.assertEqual({"state": "ok", "count": 1}, receipt["sources"]["notes"])
            self.assertEqual({"state": "failed", "reason": f"transcript folder not found: {transcripts}"},
                             receipt["sources"]["transcripts"])
            self.assertEqual(daily_text, daily.read_text(encoding="utf-8") if daily.exists() else "")

            moved.rename(transcripts)
            record["message"]["content"] = "Restored transcript text"
            session.write_text(json.dumps(record), encoding="utf-8")
            sync = self.kit(home, "sync", "--vault", str(vault))
            self.assertEqual(0, sync.returncode, sync.stderr)
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertTrue(receipt["ok"])
            self.assertEqual({"state": "ok", "count": 1}, receipt["sources"]["transcripts"])
            rendered, = (vault / "Sessions").glob("*.md")
            self.assertIn("Restored transcript text", rendered.read_text(encoding="utf-8"))

    def test_sync_preserves_daily_when_a_saved_import_source_is_missing(self):
        for source, imported_folder in (("notes", "notes"), ("notion", "Notion")):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary)
                importing = home / source
                importing.mkdir()
                (importing / "Plan.md").write_text("# Plan\n\nGrow zucchini.\n", encoding="utf-8")
                vault = home / "vault"
                notes = f"{importing}\n" if source == "notes" else ""
                notion = f"{importing}\n" if source == "notion" else ""
                setup = self.kit(home, "setup", answers=f"{vault}\n\n{notes}\n{notion}\n\n\n\n")
                self.assertEqual(0, setup.returncode, setup.stderr)
                daily, = (vault / "Daily").glob("????-??-??.md")
                daily_text = daily.read_text(encoding="utf-8")
                self.assertIn(f"[[Imported/{imported_folder}/Plan]]", daily_text)

                importing.rename(home / "moved")
                sync = self.kit(home, "sync", "--vault", str(vault))
                self.assertEqual(3, sync.returncode, sync.stderr)
                receipt = json.loads((vault / ".coldvault" / "last-run.json").read_text(encoding="utf-8"))
                self.assertEqual("failed", receipt["sources"][source]["state"])
                self.assertEqual(daily_text, daily.read_text(encoding="utf-8") if daily.exists() else "")
