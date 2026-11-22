import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


CLI = Path(__file__).resolve().parents[1] / "coldvault.py"
UNSCHEDULED = {"expected_hours": None, "state": "not_configured"}


class SearchDoctorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.vault = self.home / "vault"
        self.vault.mkdir()
        self.env = {**os.environ, "HOME": str(self.home), "XDG_CACHE_HOME": str(self.home / "cache")}

    def cli(self, *args, code=0):
        result = subprocess.run([sys.executable, str(CLI), *args, "--vault", str(self.vault), "--json"],
                                env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(code, result.returncode, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_search_ranks_frontmatter_and_body_with_line_citations(self):
        for index in range(498):
            (self.vault / f"note-{index}.md").write_text(f"# Note {index}\nA synthetic orchard.\n", encoding="utf-8")
        (self.vault / "body.md").write_text("# Orchard\nMany filler words before one juniper tree.\n", encoding="utf-8")
        (self.vault / "metadata.md").write_text("---\ntags: [juniper, juniper, juniper]\n---\n", encoding="utf-8")
        expected = {"schema_version": 1, "hits": [
            {"path": "metadata.md", "line": 2, "text": "tags: [juniper, juniper, juniper]"},
            {"path": "body.md", "line": 2, "text": "Many filler words before one juniper tree."},
        ]}
        for label in ("first search", "cached search"):
            started = time.perf_counter()
            actual = self.cli("search", "juniper")
            elapsed = (time.perf_counter() - started) * 1000
            self.assertEqual(expected, actual)
            print(f"500 notes, {label} including CLI startup: {elapsed:.1f} ms", flush=True)

    def test_search_falls_back_to_plain_scan_without_fts5(self):
        (self.vault / "Tree.md").write_text("# Tree\nJuniper\n", encoding="utf-8")
        script = """import sqlite3, sys
connect = sqlite3.connect
class WithoutFts5(sqlite3.Connection):
    def execute(self, sql, parameters=()):
        if "USING fts5" in sql:
            raise sqlite3.OperationalError("no such module: fts5")
        return super().execute(sql, parameters)
sqlite3.connect = lambda *args, **kwargs: connect(*args, factory=WithoutFts5, **kwargs)
import coldvault
sys.argv = [sys.argv[0], 'search', 'juniper', '--vault', sys.argv[1], *sys.argv[2:]]
coldvault.main()
"""
        for options, expected in (
            ([], "Tree.md:2: Juniper\n"),
            (["--json"], {"schema_version": 1, "hits": [{"path": "Tree.md", "line": 2, "text": "Juniper"}]}),
        ):
            with self.subTest(options=options):
                result = subprocess.run([sys.executable, "-c", script, str(self.vault), *options], cwd=CLI.parent,
                                        env=self.env, capture_output=True, text=True, timeout=20)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertEqual(expected, json.loads(result.stdout) if options else result.stdout)
                self.assertEqual("", result.stderr)

    def test_doctor_reports_fresh_stale_and_duplicate_sources(self):
        source = self.home / "input"
        source.mkdir()
        (source / "Tree.md").write_text("# Juniper\n", encoding="utf-8")
        receipt = self.cli("sync", "--notes", str(source))
        good = {"source": "notes", "freshness": "fresh", "duplicates": 0}
        self.assertEqual({"schema_version": 1, "schedule": UNSCHEDULED, "sources": [good]}, self.cli("doctor"))

        original, = self.vault.rglob("Tree.md")
        original.with_name("copy.md").write_bytes(original.read_bytes())
        duplicate = {**good, "duplicates": 1}
        self.assertEqual({"schema_version": 1, "schedule": UNSCHEDULED, "sources": [duplicate]}, self.cli("doctor", code=1))

        old = "2000-01-01T00:00:00Z"
        receipt.update(started=old, finished=old, last_success=old)
        folder = self.vault / ".coldvault"
        for name in ("last-run.json", "runs.jsonl"):
            (folder / name).write_text(json.dumps(receipt) + "\n", encoding="utf-8")
        self.assertEqual({"schema_version": 1, "schedule": UNSCHEDULED, "sources": [{**duplicate, "freshness": "stale"}]},
                         self.cli("doctor", code=1))
        (folder / "last-run.json").write_text("{}", encoding="utf-8")
        error = self.cli("doctor", code=2)
        self.assertEqual(1, error["schema_version"])
        self.assertIn("last-run.json", error["error"])

    def test_doctor_counts_duplicate_notes_with_quoted_provenance_keys(self):
        source = self.home / "input"
        source.mkdir()
        for name, quote in (("Single", "'"), ("Double", '"')):
            (source / f"{name}.md").write_text(
                f'---\n{quote}coldvault-source-path{quote}: "earlier/{name}.md"\n---\n# {name}\n',
                encoding="utf-8",
            )
        self.cli("sync", "--notes", str(source))
        for name in ("Single", "Double"):
            note, = self.vault.rglob(f"{name}.md")
            note.with_name(f"{name} copy.md").write_bytes(note.read_bytes())
        self.assertEqual({"schema_version": 1, "schedule": UNSCHEDULED, "sources": [
            {"source": "notes", "freshness": "fresh", "duplicates": 2}
        ]}, self.cli("doctor", code=1))

    def test_doctor_counts_provenance_independently_of_unrelated_yaml(self):
        source = self.home / "input"
        source.mkdir()
        (source / "Tree.md").write_text(
            '---\ntitle: "Juniper" # a note about the title\n---\n# Tree\n', encoding="utf-8",
        )
        self.cli("sync", "--notes", str(source))
        note, = self.vault.rglob("Tree.md")
        note.with_name("Tree copy.md").write_bytes(note.read_bytes())
        self.assertEqual({"schema_version": 1, "schedule": UNSCHEDULED, "sources": [
            {"source": "notes", "freshness": "fresh", "duplicates": 1}
        ]}, self.cli("doctor", code=1))
