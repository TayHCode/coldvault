# Changelog

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
- `tests/acceptance.sh` follows the README as a new user on a fresh Ubuntu machine.
