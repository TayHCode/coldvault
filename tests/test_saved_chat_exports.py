import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

KIT = Path(__file__).resolve().parents[1] / "coldvault.py"


class SavedChatExportTests(unittest.TestCase):
    def kit(self, home, *argv, answers=""):
        env = {**os.environ, "HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"}
        env.pop("GITHUB_TOKEN", None)
        return subprocess.run([sys.executable, str(KIT), *argv], input=answers, env=env,
                              capture_output=True, text=True, timeout=60)

    def write_export(self, path, records):
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("conversations.json", json.dumps(records))

    def write_chatgpt(self, path, message):
        self.write_export(path, [{"id": "chatgpt-demo", "mapping": {"user": {"message": {
            "author": {"role": "user"}, "content": {"content_type": "text", "parts": [message]},
        }}}}])

    def write_claude(self, path, message):
        self.write_export(path, [{"uuid": "claude-demo", "chat_messages": [
            {"sender": "human", "text": message},
        ]}])

    def test_setup_saves_both_exports_and_unattended_sync_refreshes_them(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            vault = home / "vault"
            chatgpt, claude, alternate = (home / name for name in ("chatgpt.zip", "claude.zip", "other.zip"))
            self.write_chatgpt(chatgpt, "Original ChatGPT message")
            self.write_claude(claude, "Original Claude message")
            setup = self.kit(home, "setup", answers=f"{vault}\n\n\n\n\n~/{chatgpt.name}\n\n{claude}\n\n")
            self.assertEqual(0, setup.returncode, setup.stderr)
            config_path = vault / ".coldvault" / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual([str(chatgpt)], config.get("chatgpt_exports"))
            self.assertEqual([str(claude)], config.get("claude_exports"))
            sessions = vault / "Sessions"
            chatgpt_note, = sessions.glob("*-chatgpt-*.md")
            claude_note, = sessions.glob("*-claude-ai-*.md")
            self.assertIn("Original ChatGPT message", chatgpt_note.read_text(encoding="utf-8"))
            self.assertIn("Original Claude message", claude_note.read_text(encoding="utf-8"))

            self.write_chatgpt(chatgpt, "Updated ChatGPT message")
            self.write_claude(claude, "Updated Claude message")
            sync = self.kit(home, "sync", "--vault", str(vault))
            self.assertEqual(0, sync.returncode, sync.stderr)
            self.assertIn("Updated ChatGPT message", chatgpt_note.read_text(encoding="utf-8"))
            self.assertIn("Updated Claude message", claude_note.read_text(encoding="utf-8"))

            self.write_chatgpt(alternate, "Override ChatGPT message")
            self.write_claude(claude, "Saved Claude message still refreshes")
            overridden = self.kit(home, "sync", "--vault", str(vault), "--chatgpt-export", str(alternate))
            self.assertEqual(0, overridden.returncode, overridden.stderr)
            self.assertIn("Override ChatGPT message", chatgpt_note.read_text(encoding="utf-8"))
            self.assertIn("Saved Claude message still refreshes", claude_note.read_text(encoding="utf-8"))
            self.assertEqual(config, json.loads(config_path.read_text(encoding="utf-8")))

            chatgpt.unlink()
            receipt_path = vault / ".coldvault" / "last-run.json"
            for export, diagnostic, restore in (
                (chatgpt, "No such file", self.write_chatgpt),
                (claude, "File is not a zip file", self.write_claude),
            ):
                with self.subTest(export=export.name):
                    if export == claude:
                        claude.write_text("Invalid ZIP", encoding="utf-8")
                    sync = self.kit(home, "sync", "--vault", str(vault))
                    self.assertEqual(1, sync.returncode, sync.stderr)
                    self.assertIn(f"{export.name}:", sync.stderr)
                    self.assertIn(diagnostic, sync.stderr)
                    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                    self.assertEqual("failed", receipt["sources"]["chat-exports"]["state"])
                    self.assertIn("Override ChatGPT message", chatgpt_note.read_text(encoding="utf-8"))
                    restore(export, "Restored export message")

            recovered = self.kit(home, "sync", "--vault", str(vault))
            self.assertEqual(0, recovered.returncode, recovered.stderr)
            self.assertIn("Restored export message", chatgpt_note.read_text(encoding="utf-8"))
            self.assertIn("Restored export message", claude_note.read_text(encoding="utf-8"))
