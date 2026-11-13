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

Every run into an existing vault folder records what happened to each source (transcripts, notes, Notion, GitHub) in `.coldvault/last-run.json` and adds the same record as one line to `.coldvault/runs.jsonl`. A run exits 0 when every source worked, 3 when a source failed (for example GitHub could not be reached; the rest of the vault is still built), and 2 when the vault could not be written. `status` reads these records.

## Search

    python3 coldvault.py search TERM --vault ./QuickstartVault

Every word must appear somewhere in a note (punctuation and accents are ignored), and each word also matches longer words that start with it. Notes are ranked by relevance with SQLite full-text search when your Python includes it; otherwise they are listed in path order. Each matching line is printed as `path:line: text`. Add `--json` to get a JSON list of `{"path", "line", "text"}` objects instead, or `[]` when nothing matches. Either way the command exits 1 when nothing matches.

The first search builds an index in `.coldvault/search.sqlite` inside the vault, and later searches update it only for notes that were added, changed or deleted since, so they stay fast in a large vault. The folder carries its own `.gitignore`, so it never ends up in a git-backed vault, and deleting it only means the next search rebuilds it. When the vault folder is read-only, the index goes to `~/.cache/coldvault/` instead (or `$XDG_CACHE_HOME/coldvault/`). Search never follows a symlink to place the index, and never writes into a database it didn't create. When either folder is unusable, it falls back to the next one, and finally to an in-memory index.
