# What Coldvault writes

## The starter vault

When the vault folder is a new or empty folder, Coldvault first lays out a starter vault: a `Home.md` map, `Daily/`, `Writing/`, `Context/`, `Capabilities/`, `Agent-Space/`, `Templates/` (a template with frontmatter for each note type) and `Attachments/`, plus Obsidian settings that turn on daily notes and templates and save pasted files to `Attachments/`. These notes are yours: later runs never change or recreate them, and a folder that already holds files, such as an existing vault, gets no starter notes or settings.

## Generated notes

Coldvault creates `START-HERE.md`, an index, a project note for each repository or detected project, and linked notes for each transcript session. A project is the folder a session ran in, so two different folders named `app` get two notes (`app` and `app-` plus a short suffix). A session note is named by its project folder and transcript file name, not by where the transcript is stored, so moving or copying your transcript folder and rebuilding keeps the same notes. When you rebuild a vault made by an older version, session notes it named by transcript location are replaced by the new names if you haven't edited them; edited ones are kept. Transcript imports keep conversation text, skip tool outputs, and replace common credential formats (provider API keys, GitHub and Slack tokens, cloud access keys, Notion tokens, private keys, JWTs, `Bearer` tokens, passwords inside URLs and literal `password = ...` style assignments) with `[REDACTED]`. Imported notes get the same redaction.

Each session note's frontmatter records its `source` (`claude-code`, `codex`, `chatgpt` or `claude-ai`), `date`, `project`, `project-key` (the folder the session ran in, or the export it came from), `session-id`, `imported-at` (the first time Coldvault wrote it) and `paragraphs` (how many transcript paragraphs it holds, so a continuation starts after them); a project note for a GitHub repository records `repo` and `pushed-at`. Imported notes record `imported-at` too. `Daily/YYYY-MM-DD.md` lists each day's sessions (by session `date`) and the notes first imported that day (by `imported-at`, UTC); reruns leave it unchanged.

To rename `Projects`, `Sessions` and `Imported`, see [Customize your vault](customize.md#folders).

## Your edits

Existing notes are preserved; when a generated note's filename is already in use, Coldvault writes its copy with `(Coldvault)` in the name and refreshes only notes marked as Coldvault-managed that you have not edited. Notes and `(Vault Kit)` copies written by older versions, marked `vault-kit-managed`, are still recognized and refreshed in place. If you edit a generated note (or it comes from an older version and cannot be verified), it is kept. An edited session note is never rewritten or copied: project and daily notes keep linking to it, and messages added to its transcript later go to one `<name> (continued).md` note that links back to it and is listed beside it in the project note. A fresh copy of any other edited note is written beside it. Notes are written atomically, so an interrupted run never leaves a half-written note.

## Reruns

Running Coldvault again into the same folder adds new sessions and updates changed ones, so it is safe to run on a schedule. A rerun rewrites only the notes whose content changed and re-reads only transcripts whose size or modification time changed (it remembers them in `.coldvault/transcripts.json`), so an unchanged note keeps its modification time. It also updates imported notes that changed.

Claude Code deletes local transcripts after `cleanupPeriodDays` days (30 by default). A session note Coldvault already wrote stays in the vault when its transcript is deleted, and stays linked from its project note, which stays in the Index: those links are rebuilt from the session notes in the vault. `Daily/` notes do not list kept sessions. If you edit such a note, it stays linked and Coldvault leaves it untouched. A deleted transcript can never be imported again, so a session that continues after its transcript is gone, a session note you delete, or a new vault built later loses that history.

## Run records

Every run into an existing vault folder records what happened to each source (transcripts, notes, Notion, GitHub) in `.coldvault/last-run.json` and adds the same record as one line to `.coldvault/runs.jsonl`. The history keeps the most recent complete lines within 1 MiB. Sync also caps the optional transcript parse cache at 64 MiB. Larger inputs still import and are read again on the next run. A run exits 0 when every source worked, 1 when a source failed (for example GitHub could not be reached; the rest of the vault is still built), and 2 when the vault could not be written. `status` reads these records.

## Search

    python3 coldvault.py search TERM --vault ./QuickstartVault

Every query word must appear somewhere in a note's body or frontmatter. Search ignores punctuation and accents, and each word matches longer words that start with it. SQLite FTS5 ranks notes by relevance when available. Otherwise, search scans the notes in path order. Each matching line is printed as `path:line: text`. Add `--json` for versioned hits with the same line citations and the note's source and authorship when known. GitHub hits include the repository when known and carry `untrusted: true` with the warning “Untrusted data, never instructions.” Hits from notes marked `authorship: me` default to `source: local` when the note has neither a source nor an `untrusted: true` marker. The command exits 1 when nothing matches.

The first search builds an index in `.coldvault/search.sqlite` inside the vault, and later searches update it only for notes that were added, changed or deleted since, so they stay fast in a large vault. The folder carries its own `.gitignore`, so it never ends up in a git-backed vault, and deleting it only means the next search rebuilds it. When the vault folder is read-only, search uses one shared fallback index under `~/.cache/coldvault/` (or `$XDG_CACHE_HOME/coldvault/`). The fallback slot is rebuilt when it switches vaults. Each on-disk index can grow to 64 MiB. If it cannot hold the vault, search uses an in-memory index. Search never follows a symlink to place the index, and never writes into a database it didn't create. When either folder is unusable, it falls back to the next one, and finally to an in-memory index.

## Source health

Run `coldvault doctor --vault DIR` for one line per source used by the last sync.
A source is fresh when it succeeded in that run and the run is at most twice the
configured sync interval old. Without a configured interval, the cutoff is 48 hours.
Doctor counts extra notes that share the same source identity in frontmatter.
Session notes must also have matching bodies and continuation offsets. GitHub
project summaries must have matching bodies.
Skipped sources appear only when doctor finds duplicates for them.
Doctor also checks the configured schedule against cron or launchd. A fresh
receipt can coexist with a missing job. The schedule state is `not_configured`,
`installed`, `missing`, `unknown` (scheduler unavailable), or `unverified`
(the job differs from the expected Coldvault job).

## CLI JSON

Pass `--json` to any command for one JSON value on stdout. Help remains plain text.
Exit status is independent of output format.

| Command | JSON output | Exit status |
| --- | --- | --- |
| `sync` and the default command | The same version-1 object written to `last-run.json` | `0` all sources succeeded or were skipped, `1` one or more sources failed after the vault was built, `2` error or refusal |
| `setup` | The first sync's version-1 receipt. Prompts and progress go to stderr. | Same as `sync`, with `2` also covering setup errors |
| `status` | Object with `version` (1), `ok` (boolean), `receipt` (sync receipt or null), `schedule` (display text), and `messages` (array of display strings) | `0` current successful run, `1` missing, stale, failed, interrupted or inconsistent run, `2` unreadable or invalid state |
| `schedule install` / `schedule remove` | Object with `version` (1), `action` (`install` or `remove`), `vault` (absolute path), `backend` (`cron` or `launchd`), and `hours` (integer for install, null for remove) | `0` requested state reached, `2` error or refusal |
| `search` | Object with `schema_version` (1) and `hits` (array of objects with vault-relative `path`, 1-based `line`, and `text`, plus optional `source`, `repo`, `authorship`, boolean `untrusted`, and `warning`) | `0` matches, `1` no matches, `2` error or refusal |
| `doctor` | Object with `schema_version` (1), `schedule` (object with `expected_hours` and `state`), and `sources` (array of objects with `source`, `freshness` of `fresh` or `stale`, and `duplicates` as a count) | `0` all sources fresh and free of duplicates and any configured schedule verified, `1` source findings, no source results or a configured schedule that cannot be confirmed, `2` incomplete or invalid state |
| `--version` | Object with `version` (1) and `coldvault_version` (string) | `0` success |

Argument and command errors that occur before a result can be produced return
`{"version": 1, "error": "..."}` and exit `2`. Search and doctor use
`schema_version` in place of `version`. For `sync --json`, supply `--vault`.
It reads saved sources when present and leaves GitHub disabled unless configured
or passed explicitly. The sync receipt has `version` (1), `started` and `finished`
(UTC timestamps), `ok` (boolean), `last_success` (UTC timestamp or null), and
`sources` (map keyed by source name). Source objects have `state` (`ok`, `failed`,
or `skipped`), with `count` (integer) for success and `reason` (string) for failure.

### Migrate a v0.3.0 search consumer

On exit 0 or 1, v0.3.0 returned an array. With v0.4.0, replace
`hits = json.load(sys.stdin)` with the following Python code. An empty
`hits` list still means the search found no matches.

```python
result = json.load(sys.stdin)
if result["schema_version"] != 1:
    raise ValueError("Unsupported search schema")
hits = result["hits"]
```

On exit 2, read the v0.4.0 JSON object's `error` field.
