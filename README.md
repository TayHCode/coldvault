# Coldvault

Build a private Obsidian vault from your Claude Code and Codex conversations and public GitHub repositories. Coldvault runs on Linux and macOS with Python 3.9 or newer. There are no packages to install, and git is optional.

## Install

Download the script into a `coldvault` folder in your home folder:

    mkdir -p ~/coldvault && cd ~/coldvault
    curl -fLO https://raw.githubusercontent.com/coldstack-dev/coldvault/v0.4.0/coldvault.py

Check the download with `shasum -a 256 coldvault.py` on macOS or `sha256sum coldvault.py` on Linux. The SHA-256 for v0.4.0 is `6a86cc65fb27c71ee47fe78c108e819146663c3556e368ddcf85e57f4d936e57`.

Or, if you have git, clone the repository instead. The clone also holds the examples used below:

    git clone https://github.com/coldstack-dev/coldvault.git ~/coldvault
    cd ~/coldvault

To get a `coldvault` command instead of `python3 coldvault.py`, run `pipx install .` from the clone ([pipx](https://pipx.pypa.io)). It takes the same options.

## Set up

Run the guided setup:

    python3 coldvault.py setup

It checks your Python version and whether SQLite full-text search is available, and shows how many sessions it found in `~/.claude/projects`, `~/.codex/sessions` and `~/.codex/archived_sessions`. Then it asks for the vault folder (new or existing; your files are kept), which transcript and Markdown folders to import, optional ChatGPT and Claude.ai export ZIPs, and an optional GitHub username for your public repositories. It saves the answers, and no secrets, in `<vault>/.coldvault/config.json` and builds the vault. Run `setup` again to change them. For scripts or agents without a terminal, run `python3 coldvault.py setup --yes --vault ~/ColdvaultVault` (add `--username NAME` for public GitHub repositories; refuses if a saved config already exists).

Transcript imports keep the conversation text, skip tool output, and replace common credential formats (API keys, tokens, private keys, passwords) with `[REDACTED]`.

To try it offline first, run this from the clone:

    python3 coldvault.py --username '' --transcripts examples/transcripts --output ./QuickstartVault

Any folder of Markdown notes can be imported too (this example also uses the clone):

    python3 coldvault.py --notes examples/notes --output ./NotesVault

Notion, ChatGPT and Claude.ai exports, and more on each source, are in [Import other sources](docs/sources.md).

## Open it in Obsidian

In Obsidian, open the vault switcher, choose **Open folder as vault** and pick your vault folder. Start from `START-HERE.md`.

A new vault starts with a `Home.md` map, starter folders, templates and Obsidian settings for daily notes. Those notes are yours: Coldvault never changes them. It writes a project note per repository or project folder, a note per session under `Sessions/`, and a `Daily/` note per day. When you edit a note Coldvault wrote, it keeps your version. [What Coldvault writes](docs/vault.md) has the details.

## Schedule

Rebuild the vault every hour (or every `--hours N`, where N is 2, 3, 4, 6, 8, 12 or 24). Here and below, replace `~/ColdvaultVault` with the vault folder you chose in `setup`:

    python3 ~/coldvault/coldvault.py schedule install --vault ~/ColdvaultVault

On Linux, this adds a crontab entry for your vault. On a Mac, run it while logged into your desktop session. It installs a LaunchAgent that starts a sync when loaded and then runs on the scheduled hours while you are logged in. On both platforms, scheduled hours use local time and `schedule remove --vault ~/ColdvaultVault` removes the job for that vault.

If you edit your Linux crontab by hand, use the [shared lock](docs/customize.md#schedule) to prevent overlapping saves.

Claude Code deletes transcripts after `cleanupPeriodDays` days (30 by default). Notes already in the vault stay, but a deleted transcript can't be imported again. Raise the limit in `~/.claude/settings.json`, for example `"cleanupPeriodDays": 365`.

## Check that it is working

    python3 ~/coldvault/coldvault.py status --vault ~/ColdvaultVault

It prints each source as `ok`, `failed (reason)` or `skipped`, the time of the last fully successful run, and when the next scheduled run is due. The due time can be `unknown`, for example with a `CRON_TZ` override or a daily schedule near a daylight saving change. It exits 0 for a current successful run, 1 when a run is missing, failed, stale or needs attention, and 2 for errors such as an invalid receipt or config.

To search the vault from a terminal, run `python3 coldvault.py search TERM --vault ~/ColdvaultVault`. Run `python3 coldvault.py doctor --vault ~/ColdvaultVault` to check source freshness, duplicates and the configured schedule. For scripts, see the [JSON formats and exit codes](docs/vault.md#cli-json).

## Sync it to other devices

To reach your vault on other devices, see [Sync it to other devices](docs/sync.md): a private git repository, Obsidian Sync, iCloud or S3. Read its privacy warning first.

## Uninstall

Uninstalling removes Coldvault, not your vault. The notes are yours and stay: they are plain Markdown files that Obsidian opens without Coldvault. Delete the vault folder yourself only if you no longer want them.

First stop the schedule. If you used `schedule`, remove the job by its original vault path, even if its config is missing or corrupt:

    python3 ~/coldvault/coldvault.py schedule remove --vault ~/ColdvaultVault

On macOS, replacing or removing a schedule stops any sync that the LaunchAgent is running. Wait for the current run to finish if you want it to complete.

If you set up a job by hand, remove it using the same scheduler. On Linux, use the [shared lock](docs/customize.md#schedule) to open `crontab -e` and delete the line that runs `coldvault.py` (or the older `vault_kit.py`). To remove the older Mac job named `coldvault.rebuild`, run these commands:

    launchctl bootout gui/$(id -u)/coldvault.rebuild
    rm ~/Library/LaunchAgents/coldvault.rebuild.plist

Then remove the program. If you installed the `coldvault` command, run `pipx uninstall coldvault`. If you downloaded the script, delete it:

    rm -f ~/coldvault/coldvault.py ~/coldvault/vault_kit.py

If you cloned the repository, first move out any vault you built inside it (such as `QuickstartVault`), then delete the clone.

Last, delete what Coldvault keeps for itself: the `.coldvault/` folder in the vault (saved `setup` answers, caches, run records and the search index) and the fallback search index used for read-only vaults:

    rm -rf ~/ColdvaultVault/.coldvault
    rm -rf "${XDG_CACHE_HOME:-$HOME/.cache}/coldvault"

If you used the Notion API sync that earlier versions had, also delete the folder you passed as `--state`; it holds copies of your pages.

## Recover

**An interrupted run.** Run the same command again. Notes are written atomically, so a run that was stopped (Ctrl-C, a crash, a closed laptop) leaves each note either as it was or fully updated, and the next run finishes the job. `status` shows whether the last run succeeded.

**An edited note and its `(Coldvault)` copy.** When you edit a note Coldvault wrote, your version is never changed again. For a session note, later messages go to a `(continued)` note beside it. For any other note, such as a project note, Coldvault writes its updates to a copy beside it, such as `Projects/sample-project (Coldvault).md`, and links point to the copy. To keep your edits, leave both. To go back to Coldvault's version, delete your edited note and the `(continued)` or `(Coldvault)` notes beside it (older versions named copies `(Vault Kit)`, and a copy you also edited gets a numbered one such as `(Coldvault 2)`). Move any of them out of the vault first if you want to keep your edits. Then run Coldvault again. It writes the note under its original name.

**Rebuilding the search index.** Delete the index; the next search builds a new one:

    rm -f ~/ColdvaultVault/.coldvault/search.sqlite*
    python3 "$HOME/coldvault/coldvault.py" search checklist --vault ~/ColdvaultVault

Deleting the whole `.coldvault/` folder also rebuilds the index, but it forgets the answers saved by `setup` and the run history, and the next run re-reads every transcript. For a read-only vault, the index is in `${XDG_CACHE_HOME:-$HOME/.cache}/coldvault/` instead.

**Moving the vault.** Close the vault in Obsidian. A schedule made with `schedule` is tied to the old folder, so remove it first. Then move the folder, run Coldvault into the new place and schedule it again:

    python3 ~/coldvault/coldvault.py schedule remove --vault ~/ColdvaultVault
    mkdir -p ~/Vaults
    mv ~/ColdvaultVault ~/Vaults/ColdvaultVault
    python3 "$HOME/coldvault/coldvault.py" sync --vault "$HOME/Vaults/ColdvaultVault"
    python3 ~/coldvault/coldvault.py schedule install --vault ~/Vaults/ColdvaultVault

`.coldvault/` moves with the vault, so the answers saved by `setup` still apply, and the search index rebuilds itself on the next search. If you never ran `setup`, run your usual command with the new `--output` instead and change `--output` in your cron line. On a Mac, change it in the plist and reload it as described in [Customize your vault](docs/customize.md#schedule). Open the new folder in Obsidian with **Open folder as vault**.

**Restoring from the sync target.** See [Restore from the sync target](docs/sync.md#restore-from-the-sync-target).

**Restoring original transcripts.** Coldvault setup and sync leave originals at their source. To keep selected originals after the source expires, follow the [opt-in private backup and restore steps](docs/sync.md#back-up-and-restore-original-transcripts).

## Privacy

No telemetry. Network: optional public GitHub API (`api.github.com`) lookups only when a username is given; writes local lock and fallback cache files under `~/.cache/coldvault` (or `${XDG_CACHE_HOME:-~/.cache}/coldvault` for read-only search fallback, plus `~/.cache/coldvault-schedule.lock` when using `schedule`).

The vault contains your own conversations. Keep it private like the transcripts it comes from. The script writes only inside the output folder you choose, plus the search index, which holds a copy of your notes' text.

## Limitations

Transcript formats can change, and public GitHub lookup requires an internet connection. Only public repositories are found; private repositories are not accessed.

## More guides

- [Import other sources](docs/sources.md): Markdown folders, Notion, ChatGPT and Claude.ai exports, and GitHub.
- [Sync it to other devices](docs/sync.md): private git, Obsidian Sync, iCloud and S3, and restoring from them.
- [What Coldvault writes](docs/vault.md): the notes, their frontmatter, edits, reruns and the search index.
- [Customize your vault](docs/customize.md): which sources to import, folder names and the schedule.
- [Add a source](docs/add-a-source.md): a worked example that imports a bookmarks file, with its fixture and test.
