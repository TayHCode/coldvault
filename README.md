# Coldvault

Build a private Obsidian vault from your Claude Code and Codex conversations and public GitHub repositories. Python 3.9 or newer is all you need; there are no packages to install, and git is optional. Coldvault is tested on Linux; macOS is not fully tested yet, and Windows is not supported for now.

## Install

Download the script into a `coldvault` folder in your home folder:

    mkdir -p ~/coldvault && cd ~/coldvault
    curl -O https://raw.githubusercontent.com/TayHCode/coldvault/main/coldvault.py

Or, if you have git, clone the repository instead. The clone also holds the examples used below:

    git clone https://github.com/TayHCode/coldvault.git ~/coldvault
    cd ~/coldvault

To get a `coldvault` command instead of `python3 coldvault.py`, run `pipx install .` from the clone ([pipx](https://pipx.pypa.io)). It takes the same options.

## Set up

Run the guided setup:

    python3 coldvault.py setup

It checks your Python version and whether SQLite full-text search is available, and shows how many sessions it found in `~/.claude/projects`, `~/.codex/sessions` and `~/.codex/archived_sessions`. Then it asks for the vault folder (new or existing; your files are kept), which transcript and Markdown folders to import, and an optional GitHub username for your public repositories. It saves the answers, and no secrets, in `<vault>/.coldvault/config.json` and builds the vault. Run `setup` again to change them.

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

    python3 ~/coldvault/coldvault.py schedule --vault ~/ColdvaultVault

This adds one line to your crontab on Linux and leaves your other lines alone. `schedule remove --vault ~/ColdvaultVault` deletes it. On a Mac, use the steps in [Customize your vault](docs/customize.md#schedule), with `sync --vault` and your vault folder in the scheduled line so it reuses your saved setup.

Claude Code deletes transcripts after `cleanupPeriodDays` days (30 by default). Notes already in the vault stay, but a deleted transcript can't be imported again. Raise the limit in `~/.claude/settings.json`, for example `"cleanupPeriodDays": 365`.

## Check that it is working

    python3 ~/coldvault/coldvault.py status --vault ~/ColdvaultVault

It prints each source as `ok`, `failed (reason)` or `skipped`, the time of the last fully successful run, and when the next scheduled run is due. It exits 0 when the last run succeeded, and 1 when it failed or no run has succeeded for twice the schedule interval.

To search the vault from a terminal, run `python3 coldvault.py search TERM --vault ~/ColdvaultVault` (add `--json` for JSON).

## Sync it to other devices

To reach your vault on other devices, see [Sync it to other devices](docs/sync.md): a private git repository, Obsidian Sync, iCloud or S3. Read its privacy warning first.

## Uninstall

Uninstalling removes Coldvault, not your vault. The notes are yours and stay: they are plain Markdown files that Obsidian opens without Coldvault. Delete the vault folder yourself only if you no longer want them.

First stop the schedule, while the vault still has its saved setup. If you set it up with `schedule`, remove it:

    python3 ~/coldvault/coldvault.py schedule remove --vault ~/ColdvaultVault

If you added the line by hand, run `crontab -e` and delete the line that runs `coldvault.py` (or the older `vault_kit.py`). On a Mac, unload the launchd agent and delete it:

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
    mkdir -p ~/Documents
    mv ~/ColdvaultVault ~/Documents/ColdvaultVault
    python3 "$HOME/coldvault/coldvault.py" sync --vault "$HOME/Documents/ColdvaultVault"
    python3 ~/coldvault/coldvault.py schedule --vault ~/Documents/ColdvaultVault

`.coldvault/` moves with the vault, so the answers saved by `setup` still apply, and the search index rebuilds itself on the next search. If you never ran `setup`, run your usual command with the new `--output` instead and change `--output` in your cron line. On a Mac, change it in the plist and reload it as described in [Customize your vault](docs/customize.md#schedule). Open the new folder in Obsidian with **Open folder as vault**.

**Restoring from the sync target.** See [Restore from the sync target](docs/sync.md#restore-from-the-sync-target).

## Privacy

The vault contains your own conversations. Keep it private like the transcripts it comes from. The script writes only inside the output folder you choose, plus the search index, which holds a copy of your notes' text.

## Limitations

Transcript formats can change, and public GitHub lookup requires an internet connection. Only public repositories are found; private repositories are not accessed.

## More guides

- [Import other sources](docs/sources.md): Markdown folders, Notion, ChatGPT and Claude.ai exports, and GitHub.
- [Sync it to other devices](docs/sync.md): private git, Obsidian Sync, iCloud and S3, and restoring from them.
- [What Coldvault writes](docs/vault.md): the notes, their frontmatter, edits, reruns and the search index.
- [Customize your vault](docs/customize.md): which sources to import, folder names and the schedule.
- [Add a source](docs/add-a-source.md): a worked example that imports a bookmarks file, with its fixture and test.
