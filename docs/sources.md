# Import other sources

`setup` asks for transcript folders, Markdown folders, Notion exports and a GitHub username, and saves them for every later `sync`. This page covers each source, and the ones you pass on the command line only.

## Running without setup

You can also run Coldvault without `setup`:

    python3 coldvault.py

It asks for your GitHub username (optional) and where to create the vault. To skip every prompt, pass `--username NAME` (use `''` to skip GitHub) and `--output FOLDER`; add `--transcripts FOLDER` to read a folder other than the standard ones. Without a terminal (for example from cron), a missing answer, a missing or empty transcript folder, or an output folder it cannot write stops with a one-line error and exit status 2.

## Claude Code and Codex transcripts

By default Coldvault looks in `~/.claude/projects`, `~/.codex/sessions` and `~/.codex/archived_sessions` (where Codex moves sessions you archive). A vault set up with `setup` reads the folders saved then; run `setup` again to add one that appeared later.

## GitHub

Public repositories are fetched by username without login, every page of them; lookup failure or no network simply skips them. GitHub allows 60 unauthenticated requests an hour, and each page holds 100 repositories. To raise that limit, set `GITHUB_TOKEN` to any GitHub token; it is sent only to `api.github.com` and is never printed or written to the vault. Each repository's project note also gets its README, the Markdown files at the top of its `docs/` folder and the titles and links of its first 100 open issues and pull requests, with the same credential redaction as transcripts. They are cached in `.coldvault/github.json`: a rerun fetches the README and docs again only when the repository's `pushed_at` changed, and asks for issues with the saved ETag. If the rate limit runs out, the run says so, keeps the repository list, and the next run fetches what is still missing.

## Markdown folders

To import any folder of Markdown notes (a git repository, another Obsidian vault, a Logseq graph, or a Notion or Google Docs Markdown export), pass `--notes FOLDER`:

    python3 coldvault.py --notes examples/notes --output ./NotesVault

Each `.md` file is copied, with its subfolders, under `Imported/<folder name>/` and linked from the index. Hidden files and folders (such as `.git`, `.obsidian` and `.trash`) and symlinks are skipped, and other files such as images are not copied. With `--notes` and no `--transcripts`, only notes are imported and GitHub is skipped unless you pass `--username`. Repeat `--notes` to import several folders; folders with the same name get a short suffix derived from their path. The index lists the notes from the folders passed on that run, so pass every folder each time, or save them once with `setup`. For a Logseq graph, pass its `pages` and `journals` folders so its `logseq/bak` backups are not imported.

## Notion export

For Notion, export a page or workspace with **Export → Markdown & CSV** and pass the downloaded zip (or the folder it unzips to) with `--notion-export`:

    python3 coldvault.py --notion-export ~/Downloads/Export.zip --output ./NotionVault

Pages land under `Imported/Notion/` without Notion's 32-character ID in their file and folder names, and links between pages become `[[wikilinks]]` (a link's `#section` is kept; links inside code are left as written). Each database becomes a note holding its rows as a Markdown table, so row values are searchable. Notion's multi-part download, a zip holding only `…-Part-N.zip` files, is read too. Images and other attachments, zip files included, are not copied; their links become `name (not imported)`. Hidden and `__MACOSX` entries are skipped. Notes that would share a name keep the first 8 characters of their ID (or of a hash of their path when they have no ID). Each note's frontmatter records `source: notion` and, when its filename has one, its `notion-id`.

## ChatGPT and Claude.ai exports

For ChatGPT (Settings → Data controls → **Export data**) or Claude.ai (Settings → Privacy → **Export data**), pass the downloaded zip with `--chatgpt-export` or `--claude-export` (repeatable):

    python3 coldvault.py --chatgpt-export ~/Downloads/chatgpt-export.zip --claude-export ~/Downloads/claude-export.zip --output ./ChatVault

Each conversation becomes one session note under `Sessions/`, linked from a `ChatGPT` or `Claude.ai` project note and named by its conversation ID, so rerunning with the same zip adds nothing and a newer export adds only the conversations that are new (a conversation that grew is updated in place). The note holds the title and the user and assistant messages; tool output, system prompts, hidden messages and thinking are skipped, and the text is redacted like transcripts. Numbered `conversations-NNN.json` files are read too, and only the branch ChatGPT shows (its `current_node`) is imported when a reply was edited or regenerated. A zip without conversation files, or one in an unexpected format, fails the `chat-exports` source (exit status 3). Pass the latest export each time, since the index lists only the conversations from the exports passed on that run.
