import contextlib
import copy
import hashlib
import io
import json
import re
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import vault_kit


HERE = Path(__file__).resolve().parent
SIGNED_S3_QUERY = "&".join([
    "X-Amz-Algorithm=AWS4-HMAC-SHA256",
    "X-Amz-Content-Sha256=" + "UNSIGNED-PAYLOAD",
    "X-Amz-Credential=ASIAEXAMPLEEXAMPLE%2F20261108%2Fus-west-2%2Fs3%2Faws4_request",
    "X-Amz-Date=20261108T120000Z",
    "X-Amz-Expires=3600",
    "X-Amz-Signature=" + "8f2c0e5b1d9a7c3e",
    "X-Amz-SignedHeaders=host",
    "x-id=GetObject",
])
RECORDED = json.loads((HERE / "fixtures" / "notion-api.json").read_text(encoding="utf-8").replace("{signed-s3-query}", SIGNED_S3_QUERY))
SCRATCH = HERE.parent / ".scratch"
TOKEN = "ntn_" + "Q7f3K9x2" * 6
ROADMAP = "Imported/Notion/roadmap-1f2e3d4c.md"
MEETING = "Imported/Notion/meeting-notes-2a3b4c5d.md"
READING = "Imported/Notion/reading-list-3c4d5e6f.md"
READING_ID = "3c4d5e6f7a8b4c9d8e0f1a2b3c4d5e6f"
ROADMAP_ID = "1f2e3d4c5b6a49788695a4b3c2d1e0f1"


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class RecordedNotion(BaseHTTPRequestHandler):
    """Replays the recorded Notion API responses in fixtures/notion-api.json."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.answer("GET")

    def do_POST(self):
        self.answer("POST")

    def reply(self, status, body, headers=()):
        data = json.dumps(body).encode()
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        pause = self.server.api.trickle_at.get(self.index)
        for start in range(0, len(data), 1 if pause else len(data)):
            if pause:
                time.sleep(pause)
            self.wfile.write(data[start:start + 1] if pause else data)
            self.wfile.flush()

    def answer(self, method):
        api = self.server.api
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        index = self.index = len(api.log)
        api.log.append({"at": api.clock.now, "method": method, "path": self.path, "headers": dict(self.headers), "body": body})
        if index in api.redirect_at:
            return self.reply(307, {}, [("Location", api.redirect_at[index] + self.path)])
        if index in api.delay_at:
            time.sleep(api.delay_at[index])
        if index in api.throttle_at:
            return self.reply(429, {"object": "error", "status": 429, "code": "rate_limited", "message": "Rate limited"}, [("Retry-After", api.throttle_at[index])])
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            return self.reply(401, {"object": "error", "status": 401, "code": "unauthorized", "message": "API token is invalid."})
        if self.headers.get("Notion-Version") != "2026-03-11":
            return self.reply(400, {"object": "error", "status": 400, "code": "missing_version", "message": "Notion-Version header failed validation"})
        parts = urlsplit(self.path)
        if method == "POST" and parts.path == "/v1/search" and body.get("filter") == {"property": "object", "value": "page"}:
            cursor = body.get("start_cursor") or "0"
            if not cursor.isdigit():
                return self.reply(400, {"object": "error", "status": 400, "code": "validation_error", "message": "start_cursor is invalid"})
            start = int(cursor)
            more = start + 2 < len(api.pages)
            return self.reply(200, {"object": "list", "results": api.pages[start:start + 2], "next_cursor": str(start + 2) if more else None, "has_more": more, "type": "page_or_data_source", "page_or_data_source": {}})
        found = re.fullmatch(r"/v1/pages/([0-9a-f]{32})/markdown", parts.path)
        if method == "GET" and found and parse_qs(parts.query) == {"markdown_version": ["v2"]} and found.group(1) in api.markdown:
            return self.reply(200, api.markdown[found.group(1)])
        self.reply(404, {"object": "error", "status": 404, "code": "object_not_found", "message": "Not found"})


class FakeNotionAPI:
    def __init__(self, clock):
        self.clock = clock
        self.pages = copy.deepcopy(RECORDED["search_results"])
        self.markdown = copy.deepcopy(RECORDED["markdown"])
        self.log = []
        self.throttle_at = {}
        self.delay_at = {}
        self.trickle_at = {}
        self.redirect_at = {}
        self.server = ThreadingHTTPServer(("localhost", 0), RecordedNotion)
        self.server.handle_error = lambda request, address: None
        self.server.api = self
        self.url = f"http://localhost:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def page(self, page_id):
        return next(page for page in self.pages if page["id"].replace("-", "") == page_id)

    def markdown_requests(self, since=0):
        return [entry["path"].split("/")[3] for entry in self.log[since:] if entry["method"] == "GET"]


class ClosedPipe(io.StringIO):
    def flush(self):
        raise BrokenPipeError(32, "Broken pipe")


class NotionSyncTests(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=SCRATCH))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.token_file = self.root / "notion-token"
        self.token_file.write_text(TOKEN + "\n", encoding="utf-8")
        self.token_file.chmod(0o600)
        self.clock = FakeClock()
        self.api = FakeNotionAPI(self.clock)
        self.addCleanup(self.api.close)
        self.state = self.root / "state"
        self.runs = 0

    def sync(self, out=None, budget=300, token_file=None, folder=None, stdout=None):
        self.runs += 1
        self.out = out or f"run-{self.runs}"
        argv = ["--token-file", str(token_file or self.token_file), "--out", str(self.root / self.out), "--state", str(self.state), "--budget-seconds", str(budget)]
        argv += ["--folder", folder] if folder else []
        stdout, stderr = stdout or io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                return vault_kit.notion_main(argv, api=self.api.url, clock=self.clock, sleep=self.clock.sleep)
        finally:
            self.stdout = stdout.getvalue()
            self.output = self.stdout + stderr.getvalue()

    def read(self, relative, out=None):
        return (self.root / (out or self.out) / relative).read_text(encoding="utf-8")

    def manifest(self, out=None):
        return [json.loads(line) for line in self.read("manifest.jsonl", out).splitlines()]

    def receipt(self, out=None):
        return json.loads(self.read("receipt.json", out))

    def files(self, folder):
        return {path: path.read_bytes() for path in folder.rglob("*") if path.is_file()}

    def test_first_sync_reads_v2_markdown_with_token_and_version(self):
        self.assertEqual(0, self.sync())
        self.assertEqual(["POST", "POST", "GET", "GET", "GET"], [entry["method"] for entry in self.api.log])
        for entry in self.api.log:
            self.assertEqual(f"Bearer {TOKEN}", entry["headers"]["Authorization"])
            self.assertEqual("2026-03-11", entry["headers"]["Notion-Version"])
        self.assertEqual({"markdown_version": ["v2"]}, parse_qs(urlsplit(self.api.log[2]["path"]).query))
        self.assertEqual(
            "---\nsource: notion\nnotion-id: 1f2e3d4c5b6a49788695a4b3c2d1e0f1\nlast-edited: 2026-11-01T10:15:00.000Z\nauthorship: other\n---\n\n"
            "# Roadmap\n\n## Quarter goals\n\n- [ ] Ship the importer\n- [x] Write the plan\n\n"
            "<callout icon=\"💡\">Review on Fridays.</callout>\n\n![Logo](https://example.com/logo.png)\n",
            self.read(ROADMAP),
        )
        rows = self.manifest()
        self.assertEqual([MEETING, READING, ROADMAP], [row["relative_path"] for row in rows])
        for row in rows:
            data = (self.root / self.out / row["relative_path"]).read_bytes()
            self.assertEqual("sha256:" + hashlib.sha256(data).hexdigest(), row["file_hash"])
        self.assertEqual("notion:3c4d5e6f7a8b4c9d8e0f1a2b3c4d5e6f", rows[1]["id"])
        self.assertFalse((self.root / self.out / "Imported/Notion/old-draft-4d5e6f7a.md").exists())
        receipt = self.receipt()
        self.assertEqual({"seen": 3, "written": 3, "updated": 0, "unchanged": 0, "duplicate": 0, "failed": 0, "deferred": 0, "retired": 0}, receipt["counts"])
        self.assertTrue(receipt["reconciled"])
        self.assertFalse(receipt["partial"])
        self.assertTrue(receipt["verification"]["ok"])
        self.assertEqual("2026-11-03T08:45:00.000Z", receipt["upstream"]["newest_source_event_at"])
        self.assertEqual(5, receipt["upstream"]["frontier"]["requests"])
        self.assertEqual(receipt, json.loads(self.stdout))

    def test_unchanged_rerun_fetches_no_markdown_and_exits_10(self):
        self.assertEqual(0, self.sync("first"))
        before = len(self.api.log)
        self.assertEqual(10, self.sync("second"))
        self.assertEqual([], self.api.markdown_requests(before))
        for name in ("manifest.jsonl", ROADMAP, MEETING, READING):
            self.assertEqual(self.read(name, "first"), self.read(name, "second"))
        self.assertEqual(3, self.receipt("second")["counts"]["unchanged"])
        self.assertEqual(0, self.receipt("second")["counts"]["written"] + self.receipt("second")["counts"]["updated"])

    def test_changes_from_a_failed_run_are_still_reported_by_the_retry(self):
        self.assertEqual(0, self.sync())
        self.api.page(READING_ID)["last_edited_time"] = "2026-11-08T09:00:00.000Z"
        self.api.markdown[READING_ID] = dict(self.api.markdown[READING_ID], markdown="1. Notes on the Synthesis of Form")
        self.assertEqual(1, self.sync(stdout=ClosedPipe()))
        self.assertEqual(0, self.sync())
        self.assertIn("Notes on the Synthesis of Form", self.read(READING))
        self.assertEqual(10, self.sync())

    def test_last_edited_time_cursor_fetches_only_edited_and_newly_shared_pages(self):
        self.assertEqual(0, self.sync())
        before = len(self.api.log)
        self.api.page(READING_ID)["last_edited_time"] = "2026-11-08T09:00:00.000Z"
        self.api.markdown[READING_ID] = dict(self.api.markdown[READING_ID], markdown="1. Notes on the Synthesis of Form")
        shared = copy.deepcopy(self.api.page(READING_ID))
        shared["id"] = "5e6f7a8b-9c0d-4e1f-8a2b-3c4d5e6f7a8b"
        shared["last_edited_time"] = "2025-01-01T00:00:00.000Z"
        shared["properties"]["Name"]["title"][0]["plain_text"] = "Archive"
        self.api.pages.append(shared)
        self.api.markdown["5e6f7a8b9c0d4e1f8a2b3c4d5e6f7a8b"] = {"object": "page_markdown", "id": shared["id"], "markdown": "Older page, shared today.", "truncated": False, "unknown_block_ids": []}
        self.assertEqual(0, self.sync())
        self.assertEqual(["5e6f7a8b9c0d4e1f8a2b3c4d5e6f7a8b", READING_ID], self.api.markdown_requests(before))
        self.assertIn("Notes on the Synthesis of Form", self.read(READING))
        self.assertIn("Older page, shared today.", self.read("Imported/Notion/archive-5e6f7a8b.md"))
        counts = self.receipt()["counts"]
        self.assertEqual((1, 1, 2), (counts["written"], counts["updated"], counts["unchanged"]))

    def test_requests_never_exceed_three_per_second(self):
        self.assertEqual(0, self.sync())
        times = [entry["at"] for entry in self.api.log]
        self.assertTrue(self.clock.sleeps)
        for earlier, later in zip(times, times[1:]):
            self.assertGreaterEqual(later - earlier, 1 / 3 - 1e-9)
        for first, fourth in zip(times, times[3:]):
            self.assertGreaterEqual(fourth - first, 1 - 1e-9)

    def test_429_waits_for_retry_after_then_retries_the_same_request(self):
        self.api.throttle_at = {2: "7"}
        self.assertEqual(0, self.sync())
        throttled, retried = self.api.log[2], self.api.log[3]
        self.assertEqual(throttled["path"], retried["path"])
        self.assertGreaterEqual(retried["at"] - throttled["at"], 7)
        self.assertIn(7.0, self.clock.sleeps)
        self.assertEqual(3, self.receipt()["counts"]["written"])

    def test_budget_spent_exits_20_and_next_run_resumes_without_refetching(self):
        self.assertEqual(20, self.sync(budget=1))
        receipt = self.receipt()
        self.assertTrue(receipt["partial"])
        self.assertTrue(receipt["reconciled"])
        written, deferred = receipt["counts"]["written"], receipt["counts"]["deferred"]
        self.assertGreaterEqual(deferred, 1)
        self.assertEqual(3, written + deferred)
        self.assertEqual(written, len(self.manifest()))
        fetched = set(self.api.markdown_requests())
        before = len(self.api.log)
        self.assertEqual(0, self.sync(out="resumed"))
        resumed = self.api.markdown_requests(before)
        self.assertEqual(deferred, len(resumed))
        self.assertFalse(fetched & set(resumed))
        self.assertEqual([MEETING, READING, ROADMAP], [row["relative_path"] for row in self.manifest("resumed")])

    def test_retry_after_beyond_budget_exits_20_without_retiring(self):
        self.assertEqual(0, self.sync())
        self.api.throttle_at = {len(self.api.log): "600"}
        self.api.pages = self.api.pages[:1]
        self.assertEqual(20, self.sync(out="later"))
        receipt = self.receipt("later")
        self.assertFalse(receipt["reconciled"])
        self.assertEqual([], receipt["retired"])
        self.assertEqual([MEETING, READING, ROADMAP], [row["relative_path"] for row in self.manifest("later")])

    def test_rename_and_delete_retire_old_paths_without_deleting_anything(self):
        self.assertEqual(0, self.sync("first"))
        roadmap = self.api.page("1f2e3d4c5b6a49788695a4b3c2d1e0f1")
        roadmap["properties"]["title"]["title"][0]["plain_text"] = "Product roadmap"
        roadmap["last_edited_time"] = "2026-11-08T10:00:00.000Z"
        self.api.page("2a3b4c5d6e7f4a8b9c0d1e2f3a4b5c6d")["in_trash"] = True
        self.api.pages.remove(self.api.page(READING_ID))
        self.assertEqual(0, self.sync())
        self.assertEqual(["Imported/Notion/product-roadmap-1f2e3d4c.md"], [row["relative_path"] for row in self.manifest()])
        receipt = self.receipt()
        self.assertEqual([MEETING, READING, ROADMAP], receipt["retired"])
        self.assertEqual(3, receipt["counts"]["retired"])
        for relative in (MEETING, READING, ROADMAP):
            self.assertFalse((self.root / self.out / relative).exists(), relative)
            self.assertTrue((self.root / "first" / relative).is_file(), relative)
        self.assertEqual(["1f2e3d4c5b6a49788695a4b3c2d1e0f1.json"], sorted(path.name for path in (self.state / "pages").iterdir()))

    def test_truncated_page_fetches_its_unloaded_blocks(self):
        loaded = "7a8b9c0d-1e2f-4a3b-8c4d-5e6f7a8b9c0d"
        tag = '<unknown url="https://www.notion.so/{}#{}" alt="toggle"/>'
        self.api.markdown[ROADMAP_ID] = dict(
            self.api.markdown[ROADMAP_ID], markdown="Start\n\n" + tag.format(ROADMAP_ID, loaded.replace("-", "")) + "\n\nEnd", truncated=True, unknown_block_ids=[loaded]
        )
        self.api.markdown[loaded.replace("-", "")] = {"object": "page_markdown", "id": loaded, "markdown": "Loaded later", "truncated": False, "unknown_block_ids": []}
        self.assertEqual(0, self.sync())
        self.assertIn(loaded.replace("-", ""), self.api.markdown_requests())
        self.assertIn("Start\n\nLoaded later\n\nEnd", self.read(ROADMAP))
        self.assertIn("1 pages hold blocks Notion did not return; they appear as <unknown> tags.", self.receipt()["upstream"]["coverage_notes"])

    def test_unreadable_block_fails_the_run_and_keeps_the_page_for_the_retry(self):
        self.assertEqual(0, self.sync())
        cached = (self.state / "pages" / f"{ROADMAP_ID}.json").read_bytes()
        hidden = "8b9c0d1e2f3a4b4c9d5e6f7a8b9c0d1e"
        self.api.page(ROADMAP_ID)["last_edited_time"] = "2026-11-08T09:00:00.000Z"
        self.api.markdown[ROADMAP_ID] = dict(
            self.api.markdown[ROADMAP_ID], markdown=f'Start\n\n<unknown url="https://www.notion.so/{ROADMAP_ID}#{hidden}" alt="toggle"/>', truncated=True, unknown_block_ids=[hidden]
        )
        for _ in range(2):
            before = len(self.api.log)
            self.assertEqual(1, self.sync())
            self.assertIn("the Notion API returned HTTP 404", self.output)
            self.assertIn(hidden, self.api.markdown_requests(before))
            self.assertEqual(cached, (self.state / "pages" / f"{ROADMAP_ID}.json").read_bytes())
        self.api.markdown[hidden] = {"object": "page_markdown", "id": hidden, "markdown": "Shared now", "truncated": False, "unknown_block_ids": []}
        self.assertEqual(0, self.sync())
        self.assertIn("Start\n\nShared now\n", self.read(ROADMAP))

    def test_slow_request_stops_at_the_budget_instead_of_running_past_it(self):
        self.api.delay_at = {2: 1.5}
        started = time.monotonic()
        self.assertEqual(20, self.sync(budget=1))
        self.assertLess(time.monotonic() - started, 1.4)
        receipt = self.receipt()
        self.assertTrue(receipt["partial"])
        self.assertEqual(3, receipt["counts"]["deferred"])

    def test_trickling_reply_stops_at_the_budget(self):
        self.api.trickle_at = {2: 0.05}
        started = time.monotonic()
        self.assertEqual(20, self.sync(budget=1))
        self.assertLess(time.monotonic() - started, 1.4)
        self.assertEqual(3, self.receipt()["counts"]["deferred"])

    def test_large_truncated_page_continues_its_blocks_on_the_next_run(self):
        blocks = [f"{n}a8b9c0d1e2f4a3b8c4d5e6f7a8b9c0{n}" for n in range(1, 5)]
        tag = '<unknown url="https://www.notion.so/{}#{}" alt="toggle"/>'
        self.api.markdown[ROADMAP_ID] = dict(
            self.api.markdown[ROADMAP_ID], markdown="\n\n".join(tag.format(ROADMAP_ID, block) for block in blocks), truncated=True, unknown_block_ids=blocks
        )
        for n, block in enumerate(blocks, 1):
            self.api.markdown[block] = {"object": "page_markdown", "id": block, "markdown": f"Part {n}", "truncated": False, "unknown_block_ids": []}
        self.api.throttle_at = {6: "600"}
        self.assertEqual(20, self.sync())
        self.assertEqual([ROADMAP_ID] + blocks, self.api.markdown_requests())
        self.assertIn("Part 3", self.read(ROADMAP))
        before = len(self.api.log)
        self.assertEqual(0, self.sync())
        self.assertEqual([blocks[3], "2a3b4c5d6e7f4a8b9c0d1e2f3a4b5c6d", READING_ID], self.api.markdown_requests(before))
        self.assertIn("Part 1\n\nPart 2\n\nPart 3\n\nPart 4\n", self.read(ROADMAP))
        self.assertNotIn("pending", json.loads((self.state / "pages" / f"{ROADMAP_ID}.json").read_text(encoding="utf-8")))

    def test_failed_run_drops_the_saved_listing_so_an_upstream_fix_unblocks_the_retry(self):
        blocks = [f"{n}a8b9c0d1e2f4a3b8c4d5e6f7a8b9c0{n}" for n in range(1, 5)]
        tag = '<unknown url="https://www.notion.so/{}#{}" alt="toggle"/>'
        self.api.markdown[ROADMAP_ID] = dict(
            self.api.markdown[ROADMAP_ID], markdown="\n\n".join(tag.format(ROADMAP_ID, block) for block in blocks), truncated=True, unknown_block_ids=blocks
        )
        for n, block in enumerate(blocks[:3], 1):
            self.api.markdown[block] = {"object": "page_markdown", "id": block, "markdown": f"Part {n}", "truncated": False, "unknown_block_ids": []}
        self.api.throttle_at = {6: "600"}
        self.assertEqual(20, self.sync())
        self.api.page(ROADMAP_ID)["last_edited_time"] = "2026-11-08T09:00:00.000Z"
        self.api.markdown[ROADMAP_ID] = dict(self.api.markdown[ROADMAP_ID], markdown="Fixed upstream", truncated=False, unknown_block_ids=[])
        self.assertEqual(1, self.sync())
        self.assertFalse((self.state / "inventory.json").exists())
        self.assertEqual(0, self.sync())
        self.assertIn("Fixed upstream", self.read(ROADMAP))
        self.assertEqual([MEETING, READING, ROADMAP], [row["relative_path"] for row in self.manifest()])

    def test_listed_page_that_is_no_longer_readable_is_retired(self):
        self.assertEqual(0, self.sync())
        self.api.page(READING_ID)["last_edited_time"] = "2026-11-08T09:00:00.000Z"
        del self.api.markdown[READING_ID]
        self.assertEqual(0, self.sync())
        self.assertEqual([READING], self.receipt()["retired"])
        self.assertFalse((self.root / self.out / READING).exists())
        self.assertFalse((self.state / "pages" / f"{READING_ID}.json").exists())
        self.assertIn("1 listed pages could not be read (no longer shared) and were left out.", self.receipt()["upstream"]["coverage_notes"])

    def test_listing_cut_short_by_the_budget_resumes_from_its_cursor(self):
        self.api.throttle_at = {1: "600"}
        self.assertEqual(20, self.sync())
        self.assertTrue((self.state / "inventory.json").is_file())
        before = len(self.api.log)
        self.assertEqual(0, self.sync())
        searches = [entry["body"] for entry in self.api.log[before:] if entry["method"] == "POST"]
        self.assertEqual(["2"], [body.get("start_cursor") for body in searches])
        self.assertEqual([MEETING, READING, ROADMAP], [row["relative_path"] for row in self.manifest()])
        self.assertFalse((self.state / "inventory.json").exists())
        self.assertIn("The page list was gathered over more than one run; a page unshared meanwhile is retired by the next complete listing.", self.receipt()["upstream"]["coverage_notes"])

    def test_listing_finished_by_a_partial_run_is_kept_until_its_pages_are_fetched(self):
        self.api.throttle_at = {1: "600"}
        self.assertEqual(20, self.sync())
        self.api.throttle_at = {len(self.api.log) + 1: "600"}
        self.assertEqual(20, self.sync())
        self.assertEqual(3, self.receipt()["counts"]["deferred"])
        before = len(self.api.log)
        self.assertEqual(0, self.sync())
        self.assertEqual([], [entry for entry in self.api.log[before:] if entry["method"] == "POST"])
        self.assertEqual([MEETING, READING, ROADMAP], [row["relative_path"] for row in self.manifest()])
        self.assertFalse((self.state / "inventory.json").exists())
        self.assertIn("The page list was gathered over more than one run; a page unshared meanwhile is retired by the next complete listing.", self.receipt()["upstream"]["coverage_notes"])
        before = len(self.api.log)
        self.assertEqual(10, self.sync())
        self.assertEqual([None, "2"], [entry["body"].get("start_cursor") for entry in self.api.log[before:] if entry["method"] == "POST"])

    def test_listing_finished_in_one_run_is_not_called_multi_run_by_the_retry(self):
        self.api.throttle_at = {2: "600"}
        self.assertEqual(20, self.sync())
        self.assertEqual(0, self.sync())
        self.assertEqual([MEETING, READING, ROADMAP], [row["relative_path"] for row in self.manifest()])
        self.assertNotIn("The page list was gathered over more than one run; a page unshared meanwhile is retired by the next complete listing.", self.receipt()["upstream"]["coverage_notes"])

    def test_listing_resumed_after_a_page_of_trashed_entries_still_says_it_spanned_runs(self):
        for number in range(2):
            trashed = copy.deepcopy(self.api.pages[0])
            trashed["id"] = f"0000000{number}-0000-4000-8000-000000000000"
            trashed["in_trash"] = True
            self.api.pages.insert(0, trashed)
        self.api.throttle_at = {1: "600"}
        self.assertEqual(20, self.sync())
        self.assertEqual(0, self.sync())
        self.assertEqual([MEETING, READING, ROADMAP], [row["relative_path"] for row in self.manifest()])
        self.assertIn("The page list was gathered over more than one run; a page unshared meanwhile is retired by the next complete listing.", self.receipt()["upstream"]["coverage_notes"])

    def test_listing_cut_short_before_its_first_page_is_not_called_multi_run(self):
        self.api.throttle_at = {0: "600"}
        self.assertEqual(20, self.sync())
        self.assertEqual(0, self.sync())
        self.assertEqual([MEETING, READING, ROADMAP], [row["relative_path"] for row in self.manifest()])
        self.assertNotIn("The page list was gathered over more than one run; a page unshared meanwhile is retired by the next complete listing.", self.receipt()["upstream"]["coverage_notes"])

    def test_retirement_in_a_partial_run_is_reported_by_the_retry(self):
        self.assertEqual(0, self.sync("first"))
        self.api.pages = [page for page in self.api.pages if not page["id"].startswith("2a3b4c5d")]
        self.api.page(READING_ID)["last_edited_time"] = "2026-11-08T09:00:00.000Z"
        self.api.throttle_at = {len(self.api.log) + (len(self.api.pages) + 1) // 2: "600"}
        self.assertEqual(20, self.sync("partial"))
        self.assertEqual([MEETING], self.receipt("partial")["retired"])
        self.assertEqual(1, self.receipt("partial")["counts"]["deferred"])
        self.assertEqual(0, self.sync("retry"))
        self.assertEqual([MEETING], self.receipt("retry")["retired"])
        self.assertFalse((self.root / "retry" / MEETING).exists())
        self.assertEqual([READING, ROADMAP], [row["relative_path"] for row in self.manifest("retry")])

    def test_retirement_survives_a_receipt_that_could_not_be_printed(self):
        self.assertEqual(0, self.sync("first"))
        self.api.pages = [page for page in self.api.pages if not page["id"].startswith("2a3b4c5d")]
        self.assertEqual(1, self.sync("closed", stdout=ClosedPipe()))
        self.assertEqual(0, self.sync("retry"))
        self.assertEqual([MEETING], self.receipt("retry")["retired"])
        self.assertEqual(10, self.sync("after"))
        self.assertEqual([], self.receipt("after")["retired"])

    def test_retirement_survives_an_interruption_while_the_run_finishes(self):
        self.assertEqual(0, self.sync("first"))
        self.api.pages = [page for page in self.api.pages if not page["id"].startswith("2a3b4c5d")]
        unlink = Path.unlink

        def interrupted(path, *args, **kwargs):
            unlink(path, *args, **kwargs)
            if path.name in ("unreported", "retired.jsonl"):
                raise OSError("interrupted")

        with mock.patch.object(Path, "unlink", interrupted):
            self.assertEqual(1, self.sync("cut"))
        self.assertEqual(0, self.sync("retry"))
        self.assertEqual([MEETING], self.receipt("retry")["retired"])
        self.assertEqual(10, self.sync("after"))

    def test_changing_the_folder_reports_its_retirements_with_exit_0(self):
        self.assertEqual(0, self.sync("first"))
        self.assertEqual(0, self.sync("moved", folder="Notion"))
        self.assertEqual([MEETING, READING, ROADMAP], self.receipt()["retired"])
        self.assertEqual(10, self.sync("again", folder="Notion"))
        self.assertEqual([], self.receipt()["retired"])

    def test_stale_listing_cursor_restarts_the_listing(self):
        self.state.mkdir()
        (self.state / "inventory.json").write_text(json.dumps({"cursor": "expired", "pages": {}}), encoding="utf-8")
        self.assertEqual(0, self.sync())
        searches = [entry["body"].get("start_cursor") for entry in self.api.log if entry["method"] == "POST"]
        self.assertEqual(["expired", None, "2"], searches)
        self.assertEqual([MEETING, READING, ROADMAP], [row["relative_path"] for row in self.manifest()])

    def test_folder_notion_sync_did_not_start_is_refused_before_any_request(self):
        self.assertEqual(0, self.sync("first"))
        (self.root / "mine").mkdir()
        (self.root / "mine" / "note.md").write_text("mine", encoding="utf-8")
        (self.root / "fake").mkdir()
        (self.root / "fake" / "note.md").write_text("mine", encoding="utf-8")
        (self.root / "fake" / ".notion-sync").symlink_to(self.root / "first" / ".notion-sync")
        (self.root / "file").write_text("mine", encoding="utf-8")
        requests, state, files = len(self.api.log), self.files(self.state), self.files(self.root)
        for out in ("mine", "fake", "file"):
            with self.assertRaises(SystemExit) as raised:
                self.sync(out)
            self.assertEqual(2, raised.exception.code)
            self.assertIn("--out must be a new or empty folder, or one notion-sync started", self.output)
        self.assertEqual(requests, len(self.api.log))
        self.assertEqual(state, self.files(self.state))
        self.assertEqual(files, self.files(self.root))
        (self.root / "empty").mkdir()
        self.assertEqual(10, self.sync("empty"))
        self.assertEqual([MEETING, READING, ROADMAP], [row["relative_path"] for row in self.manifest()])

    def test_signed_file_urls_are_removed(self):
        self.assertEqual(0, self.sync())
        text = self.read(MEETING)
        for signed in ("X-Amz-", "signature=", "expirationTimestamp", "prod-files-secure", "file.notion.so"):
            self.assertNotIn(signed, text)
        self.assertIn("Whiteboard photo (file not imported)", text)
        self.assertIn("Budget.pdf (file not imported)", text)
        self.assertIn("Raw link: (file link not imported)", text)
        self.assertIn('<unknown url="https://www.notion.so/2a3b4c5d6e7f4a8b9c0d1e2f3a4b5c6d#6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c" alt="embed"/>', text)
        self.assertIn("![Logo](https://example.com/logo.png)", self.read(ROADMAP))
        self.assertIn("1 pages hold blocks Notion did not return; they appear as <unknown> tags.", self.receipt()["upstream"]["coverage_notes"])

    def test_strip_signed_urls_keeps_captions_and_unsigned_links(self):
        self.assertEqual(
            "a (file not imported) [b](https://example.com/b.pdf?page=2) c (file not imported) see (file link not imported)",
            vault_kit.strip_signed_urls(
                "![a](https://s3.amazonaws.com/x.png?X-Amz-Signature=1) [b](https://example.com/b.pdf?page=2) "
                '<pdf source="https://file.notion.so/f/c.pdf?expirationTimestamp=1&signature=2">c</pdf> '
                "see https://s3.amazonaws.com/y.txt?Expires=1&Signature=2"
            ),
        )

    def test_signed_urls_with_parentheses_in_their_path_are_removed(self):
        self.assertEqual(
            "x (file not imported) [y](https://example.com/a(b).png) see (file link not imported)",
            vault_kit.strip_signed_urls(
                "![x](https://example.com/a(b).png?X-Amz-Signature=secret) [y](https://example.com/a(b).png) "
                "see https://s3.amazonaws.com/a(b).txt?Signature=secret"
            ),
        )

    def test_signed_urls_with_nested_parentheses_are_removed(self):
        text = vault_kit.strip_signed_urls(
            "![r](https://example.com/report(a(b)).pdf?Signature=secret) (see https://example.com/report(a(b)).pdf?Signature=secret)"
        )
        self.assertEqual("r (file not imported) (see (file link not imported))", text)

    def test_signed_urls_with_html_escaped_separators_are_removed(self):
        text = vault_kit.strip_signed_urls('<file source="https://example.com/x?foo=1&amp;signature=secret">x</file> see https://example.com/y?a=1&amp;X-Amz-Signature=secret')
        self.assertEqual("x (file not imported) see (file link not imported)", text)

    def test_signed_url_next_to_another_link_removes_only_itself(self):
        text = vault_kit.strip_signed_urls(
            "![photo](https://example.com/a?Signature=secret)[next](https://example.com/ok) "
            "[ok](https://example.com/ok)https://s3.amazonaws.com/y?Signature=secret"
        )
        self.assertEqual("photo (file not imported)[next](https://example.com/ok) [ok](https://example.com/ok)(file link not imported)", text)

    def test_signed_url_used_as_a_link_caption_is_removed(self):
        signed = "https://file.notion.so/f/f/photo.png?signature=secret"
        text = vault_kit.strip_signed_urls(f"[{signed}]({signed}) ![{signed}](https://example.com/ok.png) [see {signed} here](https://example.com/ok)")
        self.assertEqual("(file link not imported) (file not imported) ![(file link not imported)](https://example.com/ok.png) [see (file link not imported) here](https://example.com/ok)", text)

    def test_signed_url_behind_a_markdown_escape_is_removed(self):
        text = vault_kit.strip_signed_urls(
            r"![x](https://example.com/a\)?signature=secret) [y](https://example.com/b?X\-Amz\-Signature\=secret) https://s3.amazonaws.com/c?Signature\=secret end"
        )
        self.assertEqual("x (file not imported) y (file not imported) (file link not imported) end", text)

    def test_signed_url_with_a_percent_encoded_parameter_name_is_removed(self):
        text = vault_kit.strip_signed_urls("https://s3.amazonaws.com/a?%73ignature=secret [b](https://s3.amazonaws.com/b?X%2DAmz%2DSignature=secret) https://example.com/c?q=%73ign")
        self.assertEqual("(file link not imported) b (file not imported) https://example.com/c?q=%73ign", text)

    def test_signed_url_in_a_page_title_never_reaches_notes_or_state(self):
        self.api.page(ROADMAP_ID)["properties"]["title"]["title"][0]["plain_text"] = "Scan https://s3.amazonaws.com/x.png?X-Amz-Signature=secret"
        self.assertEqual(0, self.sync())
        files = [path for path in self.root.rglob("*") if path.is_file() and path != self.token_file]
        self.assertTrue(any(path.parent == self.state / "pages" for path in files))
        for path in files:
            self.assertNotIn(b"Signature=secret", path.read_bytes(), path.name)
            self.assertNotIn("Signature", str(path))
        self.assertIn("# Scan (file link not imported)\n", self.read("Imported/Notion/scan-file-link-not-imported-1f2e3d4c.md"))

    def test_redirect_is_refused_so_the_token_stays_with_the_api(self):
        other = FakeNotionAPI(self.clock)
        self.addCleanup(other.close)
        self.api.redirect_at = {2: other.url}
        self.assertEqual(1, self.sync())
        self.assertIn("the Notion API returned HTTP 307", self.output)
        self.assertEqual([], other.log)

    def test_symlinked_folder_under_out_is_never_written_through(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (self.root / "out").mkdir()
        (self.root / "out" / "Imported").symlink_to(elsewhere)
        with self.assertRaises(SystemExit) as raised:
            self.sync("out")
        self.assertEqual(2, raised.exception.code)
        self.assertEqual([], list(elsewhere.rglob("*")))
        self.assertEqual([], self.api.log)
        self.assertFalse(self.state.exists())

    def test_out_symlinked_to_a_used_folder_is_refused(self):
        (self.root / "mine").mkdir()
        (self.root / "mine" / "note.md").write_text("mine", encoding="utf-8")
        (self.root / "link").symlink_to(self.root / "mine")
        with self.assertRaises(SystemExit) as raised:
            self.sync("link")
        self.assertEqual(2, raised.exception.code)
        self.assertEqual([], self.api.log)
        self.assertEqual(["note.md"], [path.name for path in (self.root / "mine").iterdir()])

    def test_rerun_into_the_folder_of_a_partial_run_resumes_it(self):
        self.assertEqual(20, self.sync("staging", budget=1))
        deferred = self.receipt()["counts"]["deferred"]
        before = len(self.api.log)
        self.assertEqual(0, self.sync("staging"))
        self.assertEqual(deferred, len(self.api.markdown_requests(before)))
        self.assertEqual([MEETING, READING, ROADMAP], [row["relative_path"] for row in self.manifest()])
        self.assertTrue(self.receipt()["verification"]["ok"])
        self.assertEqual(1, self.sync("staging", stdout=ClosedPipe()))
        self.assertEqual(10, self.sync("staging"))

    def test_folder_symlinked_inside_a_folder_notion_sync_started_is_refused(self):
        self.assertEqual(0, self.sync("first"))
        elsewhere = self.root / "elsewhere"
        (self.root / "first" / "Imported").rename(elsewhere)
        (self.root / "first" / "Imported").symlink_to(elsewhere)
        files = self.files(elsewhere)
        with self.assertRaises(SystemExit) as raised:
            self.sync("first")
        self.assertEqual(2, raised.exception.code)
        self.assertIn("--folder must not pass through a symlink inside --out", self.output)
        self.assertEqual(files, self.files(elsewhere))

    def test_token_file_readable_by_others_is_refused_before_any_request(self):
        self.token_file.chmod(0o644)
        self.assertEqual(1, self.sync())
        self.assertIn("readable only by you (chmod 600)", self.output)
        self.assertEqual([], self.api.log)
        self.assertFalse((self.root / self.out).exists())

    def test_token_never_reaches_receipts_notes_state_or_output(self):
        self.api.markdown[READING_ID] = dict(self.api.markdown[READING_ID], markdown=f"Pasted by mistake: {TOKEN} and x{TOKEN}")
        self.api.page(ROADMAP_ID)["properties"]["title"]["title"][0]["plain_text"] = f"Key x{TOKEN}"
        self.assertEqual(0, self.sync())
        self.assertIn("[REDACTED]", self.read(READING))
        self.assertTrue((self.state / "pages" / f"{ROADMAP_ID}.json").is_file())
        files = [path for path in self.root.rglob("*") if path.is_file() and path != self.token_file]
        self.assertNotIn(TOKEN, "".join(str(path) for path in files))
        self.assertIn(self.root / self.out / "receipt.json", files)
        for path in files:
            self.assertNotIn(TOKEN.encode(), path.read_bytes(), path.name)
        self.assertNotIn(TOKEN, self.output)

    def test_rejected_token_fails_without_printing_it(self):
        wrong = self.root / "wrong-token"
        wrong.write_text("ntn_" + "W" * 46, encoding="utf-8")
        wrong.chmod(0o600)
        self.assertEqual(1, self.sync(token_file=wrong))
        self.assertIn("the Notion API returned HTTP 401", self.output)
        self.assertNotIn("W" * 46, self.output)

    def test_state_inside_out_is_refused(self):
        with self.assertRaises(SystemExit) as raised, contextlib.redirect_stderr(io.StringIO()):
            vault_kit.notion_main(["--token-file", str(self.token_file), "--out", str(self.root), "--state", str(self.state)], api=self.api.url)
        self.assertEqual(2, raised.exception.code)


if __name__ == "__main__":
    unittest.main()
