# Customize your vault

Everything here lives in one file, `<vault>/.coldvault/config.json`. `python3 coldvault.py setup` writes it, and `python3 coldvault.py sync --vault FOLDER` reads it on every run. An option you pass on a run (such as `--notes` or `--username`) replaces the saved value for that run only. If the file isn't valid, sync stops and asks you to move it aside and run `setup` again.

A complete example:

```json
{
  "version": 1,
  "transcripts": ["~/.claude/projects", "~/.codex/sessions"],
  "notes": ["~/notes"],
  "notion_exports": ["~/Downloads/Export.zip"],
  "username": "your-github-name",
  "folders": {"projects": "Work", "sessions": "Conversations", "imported": "Library"},
  "schedule_hours": 1
}
```

## Which sources

| Key | What it imports | To turn it off |
|---|---|---|
| `transcripts` | Claude Code and Codex sessions (`*.jsonl`) from each folder | `[]` |
| `notes` | Every `.md` file in each folder, under `Imported/<folder name>/` | `[]` |
| `notion_exports` | Notion **Export → Markdown & CSV** zips or unzipped folders, under `Imported/Notion/` | `[]` |
| `username` | Your public GitHub repositories, one project note each | `""` |

Each list holds folder or file paths (`setup` saves them as absolute paths; `~` also works). `setup` asks for every one of them, so the easiest way to change sources is to run `setup` again; it keeps your `folders`. Notes and Notion folders must be outside the vault. To add a kind of source Coldvault doesn't know, see [add-a-source.md](add-a-source.md).

A source you remove stops updating: its notes are no longer listed in the index, but notes already written stay in the vault until you delete them.

## Folders

Generated notes go in `Projects/`, `Sessions/` and `Imported/`. To use other names, set `folders` with any of the three keys `projects`, `sessions` and `imported`. Each name is a single folder: it can't start with `.` or contain `/ \ : [ ] | # ^` or a backtick. The three names must differ, ignoring case, including the defaults you keep.

The next `sync` moves the notes Coldvault wrote, and you haven't edited, into the new folders and updates the links. Edited notes and your own files stay where they are. If a moved note's name is already taken by one of your files, or a source fails, the move finishes on a later sync. Until it does, sync refuses a different name. You can't swap two names in one step (for example, `projects` to `Sessions`): rename to a new name first, sync, then rename again.

The starter folders created in a new vault (`Writing/`, `Context/`, `Capabilities/`, `Agent-Space/`, `Templates/`, `Attachments/`) are yours. Rename, move or delete them in Obsidian as you like; Coldvault never recreates them. `Daily/` is different: Coldvault writes a `Daily/YYYY-MM-DD.md` note for each day with sessions or imports, so it comes back after you delete it.

## Schedule

On Linux, `python3 ~/coldvault/coldvault.py schedule --vault ~/ColdvaultVault` adds one crontab line that rebuilds the vault every hour (or every `--hours N`, where N is 2, 3, 4, 6, 8, 12 or 24; runs start on the hour) and saves the interval as `schedule_hours`. Running it again replaces that line, and `schedule remove --vault ~/ColdvaultVault` deletes it. Windows is not supported for now.

To schedule it by hand instead, the lines below rebuild `~/ColdvaultVault` every hour from the standard Claude and Codex folders, using the `~/coldvault/coldvault.py` from the README. Replace `''` with your GitHub username to include your public repositories.

On Linux, add this line with `crontab -e`:

    0 * * * * python3 "$HOME/coldvault/coldvault.py" --username '' --output "$HOME/ColdvaultVault"

Older versions named the script `vault_kit.py`. A schedule that still runs `vault_kit.py` keeps working, because that file now just runs the `coldvault.py` beside it. If you downloaded only `vault_kit.py`, download `coldvault.py` into the same folder too.

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
        <string>python3 "$HOME/coldvault/coldvault.py" --username '' --output "$HOME/ColdvaultVault"</string>
      </array>
      <key>StartInterval</key>
      <integer>3600</integer>
    </dict>
    </plist>
    EOF
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/coldvault.rebuild.plist

The first run is an hour later. To run it now, use `launchctl kickstart gui/$(id -u)/coldvault.rebuild`; to stop it, use `launchctl bootout gui/$(id -u)/coldvault.rebuild`. After you change the plist, run that `launchctl bootout` and then the `launchctl bootstrap` line again so launchd reads the change.

To reuse the sources saved by `setup`, use `sync --vault FOLDER` in the line instead:

    0 */6 * * * python3 "$HOME/coldvault/coldvault.py" sync --vault "$HOME/ColdvaultVault"

That runs every 6 hours. On a Mac, set `StartInterval` in the launchd file to the interval in seconds (`21600` for 6 hours).

When you schedule by hand, add `"schedule_hours"` to `config.json`: the interval in whole hours (`6` here). `python3 coldvault.py status --vault FOLDER` then reports `stale` and exits 1 when no run has succeeded for twice that interval, so a broken schedule shows up. `setup` doesn't keep `schedule_hours`; add it back after running `setup` again.
