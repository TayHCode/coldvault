# Changelog

## 0.4.0

- **Breaking:** `coldvault search --json` now returns an object with `schema_version: 1` and a `hits` array. Scripts that read the previous bare array must read `hits`.
- A sync that finishes with a failed source now exits 1 (previously 3). The JSON receipt identifies the failed source. Runtime and usage errors exit 2.
- `status --json` reports the sync receipt and schedule. `doctor --json` reports source freshness, duplicates and schedule health. See the [CLI JSON reference](docs/vault.md#cli-json).

## 0.3.0

- On macOS, `schedule install` creates a LaunchAgent for the vault. `status` reads the loaded job and shows the next scheduled run, and `schedule remove` removes the job.
- New vaults include an `Archive/` folder. `Home.md` links to conversations, projects, daily notes, notes, decisions, reference, and archives. The optional ColdStack guide covers agent-specific folders.

## 0.2.0

Sync keeps your notes and reports honestly when something goes wrong.

- A missing saved source no longer stops the sync: other sources still import, and the missing one is reported as failed.
- When a saved Markdown folder or Notion export is unavailable, its earlier notes stay in place and stay linked from `Index.md`. When a saved chat export ZIP is missing or invalid, its earlier conversations stay linked from their project notes, and existing `Daily/` pages stay in place. `status` names the failed source.
- A sync interrupted by a full disk or a killed process makes `status` exit non-zero, shows the last successful run, and turns green after a successful retry. Syncs for the same vault run one at a time under the same user account.
- GitHub: an incomplete README, docs or issues refresh keeps the earlier content and is reported in the run receipt. Projects stay listed in `Index.md` through a failed repository listing. After a request failure, the next run resumes from cached docs progress for the same repository revision.
- Sessions: transcripts that share a day, project and file name stay separate sessions, and moving or renaming a transcript reuses its existing session note. Edited continuations keep later messages on the same session in order, and project notes link every continuation part in order, even after the transcript disappears. Renaming the sessions folder keeps edited sessions linked, and a session whose transcript has expired stays linked in `Daily/`.
- `setup` saves ChatGPT and Claude.ai export ZIP paths, so a scheduled `sync --vault` rereads them. An explicit `--chatgpt-export` or `--claude-export` replaces that provider's saved list for one run.
- Imported Markdown notes record their source path in `coldvault-source-path`, and reruns keep their import time.
- On supported platforms, Coldvault refuses a generated-note write when it detects that the parent folder changed during the write; a retry writes inside the selected vault.
- On Linux, `status` reads the vault's tagged crontab line, tells a missing line apart from an unreadable crontab, and reports when the due time is uncertain. `schedule` replaces the tagged line in place, keeping the cron settings around it.
- Docs: how the saved schedule interval works, and an opt-in way to back up and restore chosen original transcripts outside the vault.

## 0.1.0

First tagged release.

- `coldvault.py` is the main script; `vault_kit.py` stays as a shim for older commands.
- `coldvault setup`: a guided first run that checks prerequisites and saves your choices.
- A starter vault template in a new or empty folder, plus project, session, imported and `Daily/` notes.
- Imports Claude Code and Codex transcripts (including archived Codex sessions), Markdown folders (`--notes`), Notion exports (`--notion-export`), and ChatGPT and Claude.ai export zips.
- Adds each public GitHub repository's README, the Markdown files at the top of its `docs/` folder, and the titles and links of its first 100 open issues and pull requests to its project note.
- Incremental sync: only changed notes are rewritten, sessions keep stable names, and duplicates are prevented.
- Notes you edit are never overwritten: session notes continue in a `(continued)` note, and other notes get a `(Coldvault)` copy.
- On Linux, `coldvault schedule` adds a crontab line that runs the sync on a schedule, and `schedule remove` deletes it. Mac and Windows steps are in the customize guide.
- `coldvault status` reports the last run, with honest exit codes.
- `coldvault search TERM --vault VAULT` searches the vault (`--json` for JSON), ranked by SQLite full-text search when your Python includes it.
- Transcript imports replace common credential formats (API keys, tokens, private keys, passwords) with `[REDACTED]`.
- Guides for adding a source, customizing the vault, syncing it to other devices, uninstalling and recovering.
- `tests/acceptance.py` follows the README as a new user on a fresh Ubuntu machine.
