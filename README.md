# vault-kit

Build a private Obsidian vault from your Claude Code and Codex conversations and public GitHub repositories. Python 3.9 or newer is all you need; there are no packages to install.

## Quickstart

Clone this repository, then run this one command. It asks for your GitHub username (optional) and where to create the vault. You can instead pass `--transcripts FOLDER` to read a folder of JSONL transcripts.

    python3 vault_kit.py

By default it looks in `~/.claude/projects` and `~/.codex/sessions`. Public repositories are fetched by username without login; lookup failure or no network simply skips them.

For an offline example from the repository root:

    python3 vault_kit.py --username '' --transcripts examples/transcripts --output ./QuickstartVault

To skip every prompt, pass all three options: `--username NAME` (use `''` to skip GitHub), `--transcripts FOLDER` and `--output FOLDER`.

It creates `START-HERE.md`, an index, a project note for each repository or detected project, and linked notes for each transcript session. Transcript imports keep conversation text, skip tool outputs, and replace common credential formats (provider API keys, GitHub and Slack tokens, cloud access keys, private keys, JWTs and literal `password = ...` style assignments) with `[REDACTED]`.

Search the vault from a terminal:

    python3 vault_kit.py search TERM --vault ./QuickstartVault

Every word must appear somewhere in a note (punctuation and accents are ignored), and each word also matches longer words that start with it. Notes are ranked by relevance with SQLite full-text search when your Python includes it; otherwise they are listed in path order. Each matching line is printed as `path:line: text`.

Existing notes are preserved; when a generated note's filename is already in use, vault-kit writes its copy with `(Vault Kit)` in the name and refreshes only notes marked as vault-kit managed that you have not edited. If you edit a generated note (or it comes from an older vault-kit version and cannot be verified), it is kept and vault-kit writes a fresh copy beside it. Notes are written atomically, so an interrupted run never leaves a half-written note. Links always point at the note vault-kit wrote, and running it again updates sessions whose transcripts have grown.

## Privacy

The vault contains your own conversations. Keep it private like the transcripts it comes from. The script writes only inside the output folder you choose.

## Limitations

Transcript formats can change, and public GitHub lookup requires an internet connection. Only public repositories are found; private repositories are not accessed.
