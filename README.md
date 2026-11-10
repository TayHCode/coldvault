# Coldvault

Build a private Obsidian vault from your Claude Code and Codex conversations and public GitHub repositories. Python 3.9 or newer is all you need; there are no packages to install, and git is optional.

## Quickstart

Download the script into a `coldvault` folder in your home folder:

    mkdir -p ~/coldvault && cd ~/coldvault
    curl -O https://raw.githubusercontent.com/TayHCode/coldvault/main/vault_kit.py

Or, if you have git, clone the repository instead. The clone also holds the examples used below:

    git clone https://github.com/TayHCode/coldvault.git ~/coldvault
    cd ~/coldvault

Then run this one command. It asks for your GitHub username (optional) and where to create the vault. You can instead pass `--transcripts FOLDER` to read a folder of JSONL transcripts.

    python3 vault_kit.py

By default it looks in `~/.claude/projects` and `~/.codex/sessions`. Public repositories are fetched by username without login, every page of them; lookup failure or no network simply skips them. GitHub allows 60 unauthenticated requests an hour, and each page holds 100 repositories. To raise that limit, set `GITHUB_TOKEN` to any GitHub token; it is sent only to `api.github.com` and is never printed or written to the vault.

For an offline example from the repository root:

    python3 vault_kit.py --username '' --transcripts examples/transcripts --output ./QuickstartVault

To skip every prompt, pass `--username NAME` (use `''` to skip GitHub) and `--output FOLDER`; add `--transcripts FOLDER` to read a folder other than the standard ones. Without a terminal (for example from cron), a missing answer, a missing or empty transcript folder, or an output folder it cannot write stops with a one-line error and exit status 2.

To import any folder of Markdown notes (a git repository, another Obsidian vault, a Logseq graph, or a Notion or Google Docs Markdown export), pass `--notes FOLDER`:

    python3 vault_kit.py --notes examples/notes --output ./NotesVault

Each `.md` file is copied, with its subfolders, under `Imported/<folder name>/` and linked from the index. Hidden files and folders (such as `.git`, `.obsidian` and `.trash`) and symlinks are skipped, and other files such as images are not copied. With `--notes` and no `--transcripts`, only notes are imported and GitHub is skipped unless you pass `--username`. Repeat `--notes` to import several folders; folders with the same name get a short suffix derived from their path. The index lists the notes from the folders passed on that run, so pass every folder each time. For a Logseq graph, pass its `pages` and `journals` folders so its `logseq/bak` backups are not imported.

For Notion, export a page or workspace with **Export → Markdown & CSV** and pass the downloaded zip (or the folder it unzips to) with `--notion-export`:

    python3 vault_kit.py --notion-export ~/Downloads/Export.zip --output ./NotionVault

Pages land under `Imported/Notion/` without Notion's 32-character ID in their file and folder names, and links between pages become `[[wikilinks]]` (a link's `#section` is kept; links inside code are left as written). Each database becomes a note holding its rows as a Markdown table, so row values are searchable. Notion's multi-part download, a zip holding only `…-Part-N.zip` files, is read too. Images and other attachments, zip files included, are not copied; their links become `name (not imported)`. Hidden and `__MACOSX` entries are skipped. Notes that would share a name keep the first 8 characters of their ID (or of a hash of their path when they have no ID). Each note's frontmatter records `source: notion` and, when its filename has one, its `notion-id`.

To keep Notion pages in sync through the Notion API instead, create an internal integration at [notion.so/profile/integrations](https://www.notion.so/profile/integrations) with the "Read content" capability, share the pages you want with it (page menu → **Connections**), and save its token in a file only you can read:

    python3 vault_kit.py notion-sync --token-file ~/.config/notion-token --out "./NotionSync-$(date +%Y%m%d-%H%M%S)" --state ./notion-state

Run `chmod 600` on the token file first; the command refuses a token file other users can read. The token is sent only to `api.notion.com` and never printed or written anywhere. Each page shared with the integration becomes `Imported/Notion/<title>-<first 8 characters of its ID>.md` (pass `--folder` to choose another folder inside `--out`), read as Notion-flavored Markdown; parts of a very large page that Notion leaves out of the first reply are fetched separately. Its frontmatter records `source: notion`, `notion-id`, `last-edited` and `authorship: other`. Expiring file and image links are replaced by their captions with `(file not imported)`; attachments are not downloaded, and database properties other than the title are not copied. Requests stay at or below 3 per second and wait as long as a rate-limit reply asks. Every run lists all shared pages, then fetches only pages whose `last_edited_time` changed or that are new. Each run writes every shared page into `--out`, which must be a new or empty folder or one notion-sync started (it leaves a `.notion-sync` file there); the command refuses any other folder and never deletes a file. A rerun into a folder it started rewrites the notes there, so copy notes out before editing them. A renamed, deleted, trashed or unshared page is left out of the manifest and its old path is listed under `retired` in the receipt. `--out` also gets `manifest.jsonl` (every note with its SHA-256) and `receipt.json` (counts, page IDs and paths only), and the receipt is also printed on standard output as one JSON line. Keep `--state` outside `--out` and reuse it between runs. A run stops fetching after `--budget-seconds` (default 300). The exit status is 0 when Notion pages changed since the last run that exited 0, 10 when none did (the notes are still written to `--out`), 20 when the budget ran out first (run again to continue where it stopped) and 1 on error, including a part of a page that Notion refuses to return.

It creates `START-HERE.md`, an index, a project note for each repository or detected project, and linked notes for each transcript session. A project is the folder a session ran in, so two different folders named `app` get two notes (`app` and `app-` plus a short suffix). A session note is named by its project folder and transcript file name, not by where the transcript is stored, so moving or copying your transcript folder and rebuilding keeps the same notes. When you rebuild a vault made by an older version, session notes it named by transcript location are replaced by the new names if you haven't edited them; edited ones are kept. Transcript imports keep conversation text, skip tool outputs, and replace common credential formats (provider API keys, GitHub and Slack tokens, cloud access keys, Notion tokens, private keys, JWTs, `Bearer` tokens, passwords inside URLs and literal `password = ...` style assignments) with `[REDACTED]`. Imported notes get the same redaction.

Search the vault from a terminal:

    python3 vault_kit.py search TERM --vault ./QuickstartVault

Every word must appear somewhere in a note (punctuation and accents are ignored), and each word also matches longer words that start with it. Notes are ranked by relevance with SQLite full-text search when your Python includes it; otherwise they are listed in path order. Each matching line is printed as `path:line: text`. Add `--json` to get a JSON list of `{"path", "line", "text"}` objects instead, or `[]` when nothing matches. Either way the command exits 1 when nothing matches.

The first search builds an index in `.coldvault/search.sqlite` inside the vault, and later searches update it only for notes that were added, changed or deleted since, so they stay fast in a large vault. The folder carries its own `.gitignore`, so it never ends up in a git-backed vault, and deleting it only means the next search rebuilds it. When the vault folder is read-only, the index goes to `~/.cache/coldvault/` instead (or `$XDG_CACHE_HOME/coldvault/`). Search never follows a symlink to place the index, and never writes into a database it didn't create. When either folder is unusable, it falls back to the next one, and finally to an in-memory index.

Existing notes are preserved; when a generated note's filename is already in use, Coldvault writes its copy with `(Coldvault)` in the name and refreshes only notes marked as Coldvault-managed that you have not edited. Notes and `(Vault Kit)` copies written by older versions, marked `vault-kit-managed`, are still recognized and refreshed in place. If you edit a generated note (or it comes from an older version and cannot be verified), it is kept and Coldvault writes a fresh copy beside it. Notes are written atomically, so an interrupted run never leaves a half-written note. Links always point at the note Coldvault wrote, and running it again updates sessions whose transcripts have grown and imported notes that changed.

## Install as a command

To get a `coldvault` command instead of `python3 vault_kit.py`, install it with [pipx](https://pipx.pypa.io) from the repository root:

    pipx install .
    coldvault --version

`coldvault` takes the same options as `python3 vault_kit.py`, for example `coldvault search TERM --vault ./QuickstartVault`.

## Open it in Obsidian

In Obsidian, open the vault switcher, choose **Open folder as vault** and pick the folder you passed as `--output`. Start from `START-HERE.md`.

## Keep it up to date

Running Coldvault again into the same folder adds new sessions and updates changed ones, so it is safe to run on a schedule. The lines below rebuild `~/ColdvaultVault` every hour from the standard Claude and Codex folders, using the `~/coldvault/vault_kit.py` from the quickstart. Replace `''` with your GitHub username to include your public repositories.

On Linux, add this line with `crontab -e`:

    0 * * * * python3 "$HOME/coldvault/vault_kit.py" --username '' --output "$HOME/ColdvaultVault"

On a Mac, create a launchd agent and load it:

    mkdir -p ~/Library/LaunchAgents
    cat > ~/Library/LaunchAgents/coldvault.rebuild.plist <<'EOF'
    <?xml version="1.0" encoding="UTF-8"?>
    <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
    <plist version="1.0">
    <dict>
      <key>Label</key>
      <string>coldvault.rebuild</string>
      <key>ProgramArguments</key>
      <array>
        <string>/bin/sh</string>
        <string>-c</string>
        <string>python3 "$HOME/coldvault/vault_kit.py" --username '' --output "$HOME/ColdvaultVault"</string>
      </array>
      <key>StartInterval</key>
      <integer>3600</integer>
    </dict>
    </plist>
    EOF
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/coldvault.rebuild.plist

The first run is an hour later. To run it now, use `launchctl kickstart gui/$(id -u)/coldvault.rebuild`; to stop it, use `launchctl bootout gui/$(id -u)/coldvault.rebuild`.

## Sync it to other devices

**Privacy warning:** each recipe below copies your conversations off this machine. Redaction replaces common credential formats, not every private thing you typed. Send the vault only to places that only you can read: a private repository, your own Obsidian Sync or iCloud account, or a private bucket. Use one of these per vault, not several at once.

**A private git repository.** Create an empty private repository on your git host, then push the vault to it. The search index in `.coldvault/` has its own `.gitignore`, so it is never committed.

    cd ~/ColdvaultVault
    git init -b main
    git add -A
    git commit -m "Vault update"
    git remote add origin https://github.com/YOUR-NAME/YOUR-VAULT.git
    git push -u origin main

After each rebuild, run `git add -A && git commit -m "Vault update" && git push` from the vault folder.

**Obsidian Sync.** This needs an Obsidian Sync subscription, a remote vault created in Obsidian, and Node.js for [Obsidian Headless](https://obsidian.md/help/sync/headless) (open beta). Don't also sync this folder with the Obsidian app on the same machine.

    npm install -g obsidian-headless
    ob login
    cd ~/ColdvaultVault
    ob sync-setup --vault "YOUR REMOTE VAULT"
    ob sync

**iCloud on a Mac.** Build the vault inside Obsidian's iCloud folder, which appears once Obsidian is installed with iCloud Drive turned on. The vault then opens in Obsidian on your iPhone and iPad. Use the same `--output` in your schedule.

    python3 vault_kit.py --username '' --output "$HOME/Library/Mobile Documents/iCloud~md~obsidian/Documents/Coldvault"

**S3 with rclone.** Set up a remote for your bucket with `rclone config` (named `s3` here) and turn on bucket versioning, so an unwanted change can be undone. `rclone sync` makes the bucket folder match the vault, deleting files there that the vault no longer has.

    rclone sync ~/ColdvaultVault s3:YOUR-BUCKET/coldvault --exclude '.coldvault/**'

## Privacy

The vault contains your own conversations. Keep it private like the transcripts it comes from. The script writes only inside the output folder you choose, plus the search index described above, which holds a copy of your notes' text.

## Limitations

Transcript formats can change, and public GitHub lookup requires an internet connection. Only public repositories are found; private repositories are not accessed.
