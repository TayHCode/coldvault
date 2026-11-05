#!/usr/bin/env python3
"""Build a small Obsidian vault from local Claude Code and Codex conversations."""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen


def slug(value):
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "project"


SECRET_PATTERNS = [re.compile(pattern) for pattern in (
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)",
    r"(?<![A-Za-z0-9])sk-(?:ant-|proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}",
    r"(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})",
    r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])",
    r"(?<![A-Za-z0-9])xox[abprs]-[A-Za-z0-9-]{10,}",
    r"(?<![A-Za-z0-9])AIza[A-Za-z0-9_-]{35}",
    r"(?<![A-Za-z0-9])[sr]k_(?:live|test)_[A-Za-z0-9]{16,}",
    r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
)]
SECRET_ASSIGNMENT = re.compile(
    r"""(?i)\b((?:password|passwd|secret|token|api[_-]?key|access[_-]?key|client[_-]?secret)[a-z0-9_]*\s*["']?\s*[=:]\s*["']?)([^\s"'`,;]{8,})"""
)
REDACTED = "[REDACTED]"


def _redact_assignment(match):
    value = match.group(2)
    if re.search(r"[A-Za-z]", value) and re.search(r"[0-9]", value) and not re.match(r"[$<{%]|os\.|process\.env", value):
        return match.group(1) + REDACTED
    return match.group(0)


def redact(text):
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return SECRET_ASSIGNMENT.sub(_redact_assignment, text)


def conversation(record):
    """Return text from a known user/assistant message, excluding tool outputs."""
    kind = record.get("type")
    message = record.get("message")
    if kind == "response_item":
        message = record.get("payload", {})
        if message.get("type") != "message":
            return None
    elif kind == "event_msg":
        event = record.get("payload", {})
        if event.get("type") not in {"user_message", "agent_message"}:
            return None
        return event.get("message") if isinstance(event.get("message"), str) else None
    elif kind not in {"user", "assistant"}:
        return None
    if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block["text"] for block in content
            if isinstance(block, dict)
            and block.get("type") in {"text", "input_text", "output_text"}
            and isinstance(block.get("text"), str)
        )
    return None


def transcript_info(path):
    lines, dates, project, named = [], [], path.parent.name, False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        payload = record.get("payload")
        cwd = record.get("cwd") or (payload.get("cwd", "") if isinstance(payload, dict) else "")
        if cwd and not named:
            project, named = Path(cwd).name or project, True
        stamp = record.get("timestamp")
        if stamp:
            dates.append(str(stamp)[:10])
        text = conversation(record)
        if text and text.strip():
            lines.append(redact(text.strip()))
    if not lines:
        return None
    day = next((d for d in dates if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d)), "undated")
    return project, day, "\n\n".join(lines)


def github_repos(username):
    if not username:
        return []
    url = f"https://api.github.com/users/{username}/repos?per_page=100&sort=updated"
    request = Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "vault-kit"})
    try:
        with urlopen(request, timeout=4) as response:
            repos = json.load(response)
        if not isinstance(repos, list):
            return []
        return [repo for repo in repos if isinstance(repo, dict) and repo.get("name")]
    except (OSError, URLError, ValueError) as error:
        print(f"GitHub lookup skipped: {error}", file=sys.stderr)
        return []


_NOTE_MARKER = "vault-kit-managed: true"
_HASH_FIELD = "vault-kit-hash: "


def _digest(body):
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _is_managed_note(path):
    """A note is ours to refresh only while its body is exactly what vault-kit wrote."""
    if path.is_symlink() or not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    header, separator, body = text.partition("\n---\n\n")
    lines = header.splitlines()
    if _NOTE_MARKER not in lines[:3]:
        return False
    recorded = next((line[len(_HASH_FIELD):] for line in lines if line.startswith(_HASH_FIELD)), None)
    return recorded is None or (bool(separator) and recorded == _digest(body))


def note_target(root, relative):
    base = root.resolve()
    target = root / relative
    if not target.is_symlink():
        resolved = target.resolve()
        if resolved != base and base not in resolved.parents:
            raise ValueError("A note path escaped the chosen vault folder")
    if target.exists() or target.is_symlink():
        if not _is_managed_note(target):
            original = target
            number = 1
            while (target.exists() or target.is_symlink()) and not _is_managed_note(target):
                suffix = " (Vault Kit)" if number == 1 else f" (Vault Kit {number})"
                target = original.with_name(f"{original.stem}{suffix}{original.suffix}")
                number += 1
    resolved = target.resolve()
    if resolved != base and base not in resolved.parents:
        raise ValueError("A note path escaped the chosen vault folder")
    return target


def write_note(root, relative, text):
    target = note_target(root, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    body = f"{text.rstrip()}\n"
    handle, temporary = tempfile.mkstemp(prefix=".vault-kit-", dir=target.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(f"---\n{_NOTE_MARKER}\n{_HASH_FIELD}{_digest(body)}\n---\n\n{body}")
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return target.relative_to(root).as_posix()


def link(relative):
    return f"[[{relative.removesuffix('.md')}]]"


def build(transcript_dir, output, username=""):
    files = sorted(Path(transcript_dir).expanduser().rglob("*.jsonl"))
    build_from_files(files, output, username)


def main():
    if sys.argv[1:2] == ["search"]:
        finder = argparse.ArgumentParser(prog="vault_kit.py search", description="Search the notes in a vault.")
        finder.add_argument("term")
        finder.add_argument("--vault", required=True, help="Vault folder to search")
        args = finder.parse_args(sys.argv[2:])
        if not Path(args.vault).expanduser().is_dir():
            finder.error("vault folder not found")
        sys.exit(0 if search(args.vault, args.term) else 1)
    parser = argparse.ArgumentParser(description="Build an Obsidian vault from local Claude Code and Codex transcripts.")
    parser.add_argument("--username", help="GitHub username for a public repository lookup")
    parser.add_argument("--transcripts", help="Transcript folder (default: standard Claude and Codex folders)")
    parser.add_argument("--output", help="Vault folder (asked when omitted)")
    args = parser.parse_args()
    username = args.username if args.username is not None else input("GitHub username (optional): ").strip()
    output = args.output or input("Folder for your Obsidian vault: ").strip()
    if not output:
        parser.error("choose an output folder")
    if args.transcripts:
        source = Path(args.transcripts).expanduser()
    else:
        # Look in each tool's standard folder without copying or modifying it.
        roots = [Path.home() / ".claude" / "projects", Path.home() / ".codex" / "sessions"]
        files = [p for folder in roots if folder.is_dir() for p in folder.rglob("*.jsonl")]
        build_from_files(files, output, username)
        return
    build(source, output, username)


def build_from_files(files, output, username):
    root = Path(output).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    projects, sessions = {}, []
    for path in sorted(files):
        info = transcript_info(path)
        if info:
            project, day, text = info
            projects.setdefault(project, [])
            key = hashlib.sha1(str(path.resolve()).encode()).hexdigest()[:8]
            sessions.append((project, day, f"Sessions/{day}-{slug(project)}-{key}.md", text))
    _write_vault(root, projects, sessions, github_repos(username))
    print(f"Built vault at {root} ({len(projects)} projects, {len(sessions)} sessions)")


def _planned(root, relative):
    return note_target(root, relative).relative_to(root).as_posix()


def _write_vault(root, projects, sessions, repos):
    for repo in repos:
        projects.setdefault(repo["name"], [])
    names = sorted(projects, key=str.casefold)
    session_paths = {note: _planned(root, note) for _, _, note, _ in sessions}
    project_paths = {name: _planned(root, f"Projects/{slug(name)}.md") for name in names}
    index_path = _planned(root, "Index.md")
    start_path = _planned(root, "START-HERE.md")
    for project, day, note, text in sessions:
        write_note(root, session_paths[note], f"# {project} — {day}\n\nProject: {link(project_paths[project])}\n\n{text}")
    for name in names:
        repo = next((r for r in repos if r["name"] == name), None)
        body = f"# {name}\n\n"
        if repo:
            body += f"GitHub: [{repo['html_url']}]({repo['html_url']})\n\n{repo.get('description') or ''}\n\n"
        links = [f"- {link(session_paths[note])} ({day})" for project, day, note, _ in sessions if project == name]
        body += "## Sessions\n\n" + ("\n".join(links) if links else "No local sessions found.")
        write_note(root, project_paths[name], body)
    write_note(root, index_path, "# Vault index\n\n## Projects\n\n" + "\n".join(f"- {link(project_paths[name])}" for name in names))
    write_note(root, start_path, (
        f"# Start here\n\nOpen {link(index_path)} to browse projects and sessions.\n\n"
        "Each project note lists its conversation sessions; each session note holds the conversation text "
        "(tool outputs are not imported).\n\n"
        "Search the vault from a terminal:\n\n"
        "    python3 vault_kit.py search TERM --vault DIR\n"
    ))


def _notes(root):
    for path in sorted(root.rglob("*.md")):
        if path.is_symlink():
            continue
        try:
            yield path.relative_to(root).as_posix(), path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue


def _ranked(notes, words):
    """Order notes containing every word by SQLite FTS5 relevance; None when FTS5 is unavailable."""
    try:
        database = sqlite3.connect(":memory:")
        database.execute("CREATE VIRTUAL TABLE notes USING fts5(path UNINDEXED, body)")
    except sqlite3.Error:
        return None
    with database:
        database.executemany("INSERT INTO notes VALUES (?, ?)", notes.items())
        query = " ".join('"' + word.replace('"', '""') + '"*' for word in words)
        return [path for (path,) in database.execute("SELECT path FROM notes WHERE notes MATCH ? ORDER BY bm25(notes), path", (query,))]


def search(vault, term):
    """Print matching lines, most relevant notes first; return how many lines matched."""
    notes = dict(_notes(Path(vault).expanduser()))
    words = [word.casefold() for word in term.split()]
    if not words:
        return 0
    ranked = _ranked(notes, words)
    if ranked is None:
        ranked = [path for path, text in notes.items() if all(word in text.casefold() for word in words)]
    found = 0
    for path in ranked:
        for number, line in enumerate(notes[path].splitlines(), 1):
            if any(word in line.casefold() for word in words):
                print(f"{path}:{number}: {line.strip()}")
                found += 1
    return found


if __name__ == "__main__":
    main()
