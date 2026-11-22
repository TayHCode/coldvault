import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

import coldvault


KIT = Path(__file__).resolve().parents[1] / "coldvault.py"


class ImportOutageTests(unittest.TestCase):
    def test_saved_import_outages_keep_existing_links_and_report_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            vault = home / "vault"
            notes, notion, chat = home / "notes", home / "export", home / "chat.zip"
            notes.mkdir()
            notion.mkdir()
            (notes / "Plan.md").write_text("# Plan\n\nA saved note.\n", encoding="utf-8")
            (notion / "Page.md").write_text("# Page\n\nA Notion page.\n", encoding="utf-8")
            with zipfile.ZipFile(chat, "w") as archive:
                archive.writestr("conversations.json", json.dumps([{
                    "uuid": "chat-demo", "created_at": "2026-01-02T12:00:00Z",
                    "chat_messages": [{"sender": "human", "text": "A saved chat."}],
                }]))

            def kit(*argv, answers=""):
                env = {**os.environ, "HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"}
                env.pop("GITHUB_TOKEN", None)
                return subprocess.run([sys.executable, str(KIT), *argv], input=answers, env=env,
                                      capture_output=True, text=True, timeout=60)

            setup = kit("setup", answers=f"{vault}\n\n{notes}\n\n{notion}\n\n\n\n{chat}\n\n")
            self.assertEqual(0, setup.returncode, setup.stderr)
            plan, page = vault / "Imported/notes/Plan.md", vault / "Imported/Notion/Page.md"
            chat_note, = (vault / "Sessions").glob("2026-01-02-claude-ai-*.md")
            index, project = vault / "Index.md", vault / "Projects/claude-ai.md"
            imported_links = ("[[Imported/notes/Plan]]", "[[Imported/Notion/Page]]")
            chat_link = f"[[Sessions/{chat_note.stem}]]"
            self.assertIn("[[Projects/claude-ai]]", index.read_text(encoding="utf-8"))
            self.assertIn(chat_link, project.read_text(encoding="utf-8"))
            daily = list((vault / "Daily").glob("????-??-??.md"))
            daily_text = "\n".join(path.read_text(encoding="utf-8") for path in daily)
            for link in (*imported_links, chat_link):
                self.assertIn(link, daily_text)

            saved = {path: path.read_bytes() for path in (plan, page, chat_note, *daily)}
            for source, path in (("notes", notes), ("notion", notion), ("chat-exports", chat)):
                with self.subTest(source=source):
                    moved = path.rename(home / "unavailable")
                    if source == "notion":
                        page.write_bytes(saved[page] + b"\nMy own edit.\n")
                    try:
                        expected = {**saved, page: page.read_bytes()}
                        sync = kit("sync", "--vault", str(vault))
                        self.assertEqual(1, sync.returncode, sync.stderr)
                        status = kit("status", "--vault", str(vault))
                        self.assertEqual(1, status.returncode, status.stdout + status.stderr)
                        self.assertIn(f"{source}: failed", status.stdout + status.stderr)
                        index_text = index.read_text(encoding="utf-8")
                        for link in (*imported_links, "[[Projects/claude-ai]]"):
                            self.assertIn(link, index_text)
                        self.assertIn(chat_link, project.read_text(encoding="utf-8"))
                        for generated, content in expected.items():
                            self.assertEqual(content, generated.read_bytes(), generated.name)
                        if source == "chat-exports":
                            run = SimpleNamespace(root=vault, folders=coldvault.FOLDERS,
                                                  chatgpt_exports=[], claude_exports=[chat])
                            with self.assertRaises(coldvault.SourceError) as failure:
                                coldvault.chat_export_source(run)
                            self.assertEqual([(chat_note.relative_to(vault).as_posix(), "A saved chat.")],
                                             [(note, text) for note, text, _ in failure.exception.notes])
                    finally:
                        if source == "notion":
                            page.write_bytes(saved[page])
                        moved.rename(path)

            recovered = kit("sync", "--vault", str(vault))
            self.assertEqual(0, recovered.returncode, recovered.stderr)
            self.assertEqual(0, kit("status", "--vault", str(vault)).returncode)
