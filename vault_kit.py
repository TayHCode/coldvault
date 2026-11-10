#!/usr/bin/env python3
"""Build a small Obsidian vault from local Claude Code and Codex conversations, Markdown folders, Notion exports and the Notion API."""

import argparse
import contextlib
import csv
import hashlib
import html
import io
import json
import os
import posixpath
import re
import shutil
import socket
import stat
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

try:
    import sqlite3
except ImportError:
    sqlite3 = None

__version__ = "0.1.0"


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
    r"(?<![A-Za-z0-9])(?:secret|ntn)_[A-Za-z0-9]{32,}",
)]
SECRET_AFTER_PREFIX = [re.compile(pattern) for pattern in (
    r"(?i)(\bbearer[ \t]+)[A-Za-z0-9._~+/-]{16,}=*",
    r"(?i)(\b[a-z][a-z0-9+.-]*://[^\s:/?#@]*:)[^\s/?#]+(?=@)",
)]
SECRET_ASSIGNMENT = re.compile(
    r"""(?i)(?<![a-z0-9])((?:password|passwd|secret|token|api[_-]?key|access[_-]?key|client[_-]?secret)[a-z0-9_]*["']?[ \t]*[=:][ \t]*)(?:(["'`])((?:\\[\s\S]|(?!\2)[\s\S])*)\2|([^\s"'`]+))"""
)
REDACTED = "[REDACTED]"


def _redact_assignment(match):
    prefix, quote, quoted, bare = match.groups()
    if quote:
        return f"{prefix}{quote}{REDACTED}{quote}" if quoted else match.group(0)
    if re.fullmatch(r"none|null|true|false|\$\{\w+\}|<[\w-]+>|(?:os\.environ|process\.env)\b.*", bare, re.I):
        return match.group(0)
    return prefix + REDACTED


def redact(text):
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    for pattern in SECRET_AFTER_PREFIX:
        text = pattern.sub(lambda match: match.group(1) + REDACTED, text)
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
    lines, dates, project, named = [], [], (path.parent.name, path.parent.name), False
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
            named = True
            if Path(cwd).name:
                project = (str(Path(cwd)), Path(cwd).name)
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
    headers = {"Accept": "application/vnd.github+json", "User-Agent": f"coldvault/{__version__}"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        if not re.fullmatch(r"[\x21-\x7e]+", token):
            print("GitHub lookup skipped: GITHUB_TOKEN contains spaces or control characters", file=sys.stderr)
            return []
        headers["Authorization"] = f"Bearer {token}"
    url = f"https://api.github.com/users/{username}/repos?per_page=100&sort=updated"
    repos = []
    try:
        while url:
            with urlopen(Request(url, headers=headers), timeout=4) as response:
                page = json.load(response)
                link = response.headers.get("Link") or ""
            if not isinstance(page, list):
                return []
            repos += [repo for repo in page if isinstance(repo, dict) and repo.get("name")]
            match = re.search(r'<(https://api\.github\.com/[^>]*)>;\s*rel="next"', link)
            url = match and match.group(1)
        return repos
    except (OSError, URLError, ValueError) as error:
        print(f"GitHub lookup skipped: {error}", file=sys.stderr)
        return []


_NOTE_MARKER = "coldvault-managed: true"
_HASH_FIELD = "coldvault-hash: "
_MARKERS = {_NOTE_MARKER: _HASH_FIELD, "vault-kit-managed: true": "vault-kit-hash: "}


def _digest(body):
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _is_managed_note(path):
    if path.is_symlink() or not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    lines = text.split("\n")
    fields = [field for marker, field in _MARKERS.items() if marker in lines[:3]]
    if len(fields) != 1:
        return False
    hashed = [index for index, line in enumerate(lines[:3]) if line.startswith(fields[0])]
    if len(hashed) != 1:
        return False
    recorded = lines.pop(hashed[0])[len(fields[0]):]
    return recorded == _digest("\n".join(lines))


def note_target(root, relative):
    base = root.resolve()
    target = root / relative
    if not target.is_symlink():
        resolved = target.resolve()
        if resolved != base and base not in resolved.parents:
            raise ValueError("A note path escaped the chosen vault folder")
    if (target.exists() or target.is_symlink()) and not _is_managed_note(target):
        target = _copy_target(target)
    resolved = target.resolve()
    if resolved != base and base not in resolved.parents:
        raise ValueError("A note path escaped the chosen vault folder")
    return target


def _copy_target(original):
    for label in ("Vault Kit", "Coldvault"):
        number = 1
        while True:
            copy = original.with_name(f"{original.stem} ({label}{'' if number == 1 else f' {number}'}){original.suffix}")
            if not (copy.exists() or copy.is_symlink()):
                break
            if _is_managed_note(copy):
                return copy
            number += 1
    return copy


def write_note(root, relative, text):
    target = note_target(root, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    body = f"{text.rstrip()}\n"
    if body.startswith("---\n") and "\n---\n" in body[3:]:
        unhashed = f"---\n{_NOTE_MARKER}\n{body[4:]}"
    else:
        unhashed = f"---\n{_NOTE_MARKER}\n---\n\n{body}"
    lines = unhashed.split("\n")
    lines.insert(2, _HASH_FIELD + _digest(unhashed))
    _atomic_write(target, "\n".join(lines))
    return target.relative_to(root).as_posix()


def _atomic_write(target, text):
    handle, temporary = tempfile.mkstemp(prefix=".coldvault-", dir=target.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def link(relative):
    return f"[[{relative.removesuffix('.md')}]]"


def build(transcript_dir, output, username=""):
    files = sorted(Path(transcript_dir).expanduser().rglob("*.jsonl"))
    build_from_files(files, output, username)


def main():
    if sys.argv[1:2] == ["search"]:
        finder = argparse.ArgumentParser(prog=f"{Path(sys.argv[0]).name} search", description="Search the notes in a vault.")
        finder.add_argument("term")
        finder.add_argument("--vault", required=True, help="Vault folder to search")
        finder.add_argument("--json", action="store_true", help="Print a JSON list of {path, line, text} objects")
        args = finder.parse_args(sys.argv[2:])
        if not Path(args.vault).expanduser().is_dir():
            finder.error("vault folder not found")
        if args.json:
            found = [{"path": path, "line": number, "text": text} for path, number, text in hits(args.vault, args.term)]
            print(json.dumps(found, ensure_ascii=False))
            sys.exit(0 if found else 1)
        sys.exit(0 if search(args.vault, args.term) else 1)
    if sys.argv[1:2] == ["notion-sync"]:
        sys.exit(notion_main(sys.argv[2:]))
    parser = argparse.ArgumentParser(description="Build an Obsidian vault from local Claude Code and Codex transcripts, Markdown folders and Notion exports.")
    parser.add_argument("--version", action="version", version=f"coldvault {__version__}")
    parser.add_argument("--username", help="GitHub username for a public repository lookup")
    parser.add_argument("--transcripts", help="Transcript folder (default: standard Claude and Codex folders)")
    parser.add_argument("--output", help="Vault folder (asked when omitted)")
    parser.add_argument("--notes", action="append", default=[], metavar="DIR", help="Markdown folder to import under Imported/ (repeatable)")
    parser.add_argument("--notion-export", action="append", default=[], metavar="ZIP", help="Notion 'Markdown & CSV' export (.zip or unzipped folder) to import under Imported/Notion/ (repeatable)")
    args = parser.parse_args()
    imports = args.notes or args.notion_export

    def ask(prompt):
        try:
            return input(prompt).strip()
        except EOFError:
            parser.error("pass --username and --output when not interactive")

    if args.username is not None:
        username = args.username
    else:
        username = "" if imports else ask("GitHub username (optional): ")
    output = args.output or ask("Folder for your Obsidian vault: ")
    if not output:
        parser.error("choose an output folder")
    root = Path(output).expanduser().resolve()
    for folder in args.notes:
        notes = Path(folder).expanduser().resolve()
        if not notes.is_dir():
            parser.error(f"notes folder not found: {folder}")
        if notes == root or root in notes.parents:
            parser.error(f"choose a notes folder outside the vault: {folder}")
    for export in args.notion_export:
        path = Path(export).expanduser().resolve()
        if path.is_dir():
            if path == root or root in path.parents:
                parser.error(f"choose a Notion export outside the vault: {export}")
        elif not zipfile.is_zipfile(path):
            parser.error(f"Notion export must be a .zip file or an unzipped folder: {export}")
    if args.transcripts:
        transcripts = Path(args.transcripts).expanduser()
        if not transcripts.is_dir():
            parser.error(f"transcript folder not found: {args.transcripts}")
        files = sorted(transcripts.rglob("*.jsonl"))
        if not files:
            parser.error(f"no transcripts (*.jsonl) found in {args.transcripts}")
    elif imports:
        files = []
    else:
        roots = [Path.home() / ".claude" / "projects", Path.home() / ".codex" / "sessions"]
        files = [p for folder in roots if folder.is_dir() for p in folder.rglob("*.jsonl")]
        if not files and not username:
            parser.error("no transcripts found in ~/.claude/projects or ~/.codex/sessions; pass --transcripts DIR, --notes DIR or --notion-export ZIP")
    try:
        build_from_files(files, output, username, args.notes, args.notion_export)
    except OSError as error:
        detail = f"{error.filename}: {error.strerror}" if error.filename else str(error)
        print(f"{parser.prog}: error: {detail}", file=sys.stderr)
        sys.exit(2)


def markdown_files(source, vault, suffixes=(".md",)):
    for folder, dirs, names in os.walk(source):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and (Path(folder) / d).resolve() != vault)
        for name in sorted(names):
            path = Path(folder) / name
            if name.endswith(suffixes) and not name.startswith(".") and not path.is_symlink():
                yield path


def imported_notes(sources, vault, notion_exports=()):
    sources = list(dict.fromkeys(Path(source).expanduser().resolve() for source in sources))
    exports = list(dict.fromkeys(Path(export).expanduser().resolve() for export in notion_exports))
    bases = [source.name.lstrip(".") or "notes" for source in sources] + ["Notion"] * len(exports)
    folders = [
        f"{base} ({hashlib.sha1(str(source).encode()).hexdigest()[:8]})"
        if [b.casefold() for b in bases].count(base.casefold()) > 1 else base
        for source, base in zip(sources + exports, bases)
    ]
    notes, notion = [], []
    for source, folder in zip(sources, folders):
        for path in markdown_files(source, vault):
            text = redact(path.read_text(encoding="utf-8-sig", errors="replace"))
            notes.append((f"Imported/{folder}/{path.relative_to(source).as_posix()}", text))
    for export, folder in zip(exports, folders[len(sources):]):
        notion.extend(notion_notes(export, vault, f"Imported/{folder}"))
    return notes + notion, {note for note, _ in notion}


_NOTION_ID = re.compile(r"(?:^|\s)([0-9a-f]{32})$")
_NOTION_LINK_ID = re.compile(r"([0-9a-f]{32})(?:_all)?(?:\.(?:md|csv))?$")
_CODE = (
    r"(?ms:^[ \t]*(`{3,}|~{3,}).*?(?:^[ \t]*\1(?:(?<=`)`*|(?<=~)~*)[ \t]*$|\Z))"
    r"|(?<!`)(`+)(?!`)(?s:(?:(?!\n[ \t]*\n).)+?)(?<!`)\2(?!`)"
)
_LINK_TARGET = r"[^()\s]*"
for _ in range(31):
    _LINK_TARGET = rf"(?:[^()\s]|\({_LINK_TARGET}\))*"
_CODE_OR_LINK = re.compile(_CODE + rf"|!?\[([^\]\n]*)\]\(((?:[^()\s]|\({_LINK_TARGET}\))+)\)")
_CODE_OR_WIKILINK = re.compile(_CODE + r"|(?<=\[\[)[^\]|\\#\n]+")
_TABLE_DELIMITER = re.compile(r"\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*")


def _notion_part(name):
    match = _NOTION_ID.search(name)
    title = name[:match.start()] if match else name
    title = re.sub(r"\s+", " ", re.sub(r"[#^\[\]|]", " ", title)).strip().lstrip(". ") or "Untitled"
    return title, match.group(1) if match else None


def _skipped(name):
    parts = name.replace("\\", "/").split("/")
    return not parts[0] or any(part.startswith((".", "__MACOSX")) for part in parts)


def _zip_entries(archive, nested=True):
    infos = [info for info in archive.infolist() if not info.is_dir() and not _skipped(info.filename)]
    if nested and infos and all(re.fullmatch(r"[^/\\]*-Part-\d+\.zip", info.filename, re.I) for info in infos):
        for info in infos:
            with tempfile.TemporaryFile() as spool:
                with archive.open(info) as stream:
                    shutil.copyfileobj(stream, spool)
                spool.seek(0)
                with zipfile.ZipFile(spool) as inner:
                    yield from _zip_entries(inner, nested=False)
        return
    for info in infos:
        if info.filename.endswith((".md", ".csv")):
            yield info.filename, archive.read(info)


def _notion_files(export, vault):
    if export.is_dir():
        entries = [(p.relative_to(export).as_posix(), p.read_bytes()) for p in markdown_files(export, vault, (".md", ".csv"))]
    else:
        with zipfile.ZipFile(export) as archive:
            entries = list(_zip_entries(archive))
    files = {}
    for name, data in entries:
        if not _skipped(name):
            files["/".join(part for part in name.replace("\\", "/").split("/") if part)] = data
    return files


def _table_cell(value):
    return re.sub(r"\r?\n", "<br>", redact(value).strip()).replace("|", "\\|")


def _csv_note(title, text):
    csv.field_size_limit(2**31 - 1)
    rows = list(csv.reader(io.StringIO(text)))
    lines = [f"# {title}", ""]
    if rows:
        width = max(len(row) for row in rows)
        rows = [[_table_cell(cell) for cell in row] + [""] * (width - len(row)) for row in rows]
        lines.append("| " + " | ".join(rows[0]) + " |")
        lines.append("|" + " --- |" * width)
        lines.extend("| " + " | ".join(row) + " |" for row in rows[1:])
    return "\n".join(lines)


def _notion_links(text, source, targets):
    def replace(match):
        label, target = match.group(3), match.group(4)
        if target is None:
            return match.group(0)
        try:
            parts = urlsplit(target)
        except ValueError:
            return match.group(0)
        host = parts.hostname or ""
        internal = host == "notion.so" or host.endswith((".notion.so", ".notion.site"))
        if (parts.scheme or parts.netloc) and not internal or not parts.path:
            return match.group(0)
        path = unquote(parts.path)
        filename = path.rstrip("/").rsplit("/", 1)[-1]
        found = _NOTION_LINK_ID.search(filename)
        dest = targets.get(found.group(1)) if found else None
        if dest is None and not internal:
            dest = targets.get(posixpath.normpath(posixpath.join(posixpath.dirname(source), path)))
        if dest:
            alias = ("\\|" if text.count("\n", 0, match.start()) in rows else "|") + label if label else ""
            section = re.sub(r"[\[\]|#^]", " ", unquote(parts.fragment)).strip()
            return f"[[{dest}{'#' + section if section else ''}{alias}]]"
        if internal:
            return match.group(0)
        return f"{label or filename} (not imported)"
    rows = _table_rows(text)
    return _CODE_OR_LINK.sub(replace, text)


def _table_rows(text):
    lines, rows, in_table = text.split("\n"), set(), False
    for index, line in enumerate(lines):
        if "|" not in line:
            in_table = False
        elif index and "|" in line and "|" in lines[index - 1] and _TABLE_DELIMITER.fullmatch(line):
            in_table = True
            rows.add(index - 1)
        if in_table:
            rows.add(index)
    return rows


def notion_notes(export, vault, folder):
    """Turn a Notion 'Markdown & CSV' export into notes without ID suffixes, linked with [[wikilinks]]."""
    files = _notion_files(export, vault)
    views = {}
    for name in sorted((name for name in files if name.endswith(".csv")), key=len, reverse=True):
        full = name[:-4] + "_all.csv"
        if full in files and full not in views:
            views[name] = full
    sources = [name for name in sorted(files) if name not in views]
    planned = {}
    for name in sources:
        *dirs, filename = name.split("/")
        stem = posixpath.splitext(filename)[0]
        if name in views.values():
            stem = stem.removesuffix("_all")
        title, page_id = _notion_part(stem)
        dirs = [re.sub(r"(?i)(?:\.md)+$", "", _notion_part(d)[0]).strip() or "Untitled" for d in dirs]
        planned[name] = [posixpath.join(folder, *dirs, title), page_id, title]
    taken = Counter(dest.casefold() for dest, _, _ in planned.values())
    used = {dest.casefold() for dest, _, _ in planned.values() if taken[dest.casefold()] == 1}
    for name, entry in planned.items():
        if taken[entry[0].casefold()] > 1:
            base = entry[0] + " " + (entry[1] or hashlib.sha1(name.encode()).hexdigest())[:8]
            entry[0], number = base, 1
            while entry[0].casefold() in used:
                number += 1
                entry[0] = f"{base} {number}"
            used.add(entry[0].casefold())
    targets = {page_id: dest for dest, page_id, _ in planned.values() if page_id}
    targets.update((name, dest) for name, (dest, _, _) in planned.items())
    targets.update((view, planned[full][0]) for view, full in views.items())
    notes = []
    for name in sources:
        dest, page_id, title = planned[name]
        text = files[name].decode("utf-8-sig", errors="replace")
        body = _notion_links(_csv_note(title, text) if name.endswith(".csv") else redact(text), name, targets)
        front = "---\nsource: notion\n" + (f"notion-id: {page_id}\n" if page_id else "") + "---\n\n"
        notes.append((f"{dest}.md", front + body))
    return notes


def build_from_files(files, output, username, notes=(), notion_exports=()):
    root = Path(output).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    imported, notion = imported_notes(notes, root, notion_exports)
    projects, sessions = {}, {}
    for path in sorted(files):
        info = transcript_info(path)
        if not info:
            continue
        (project, name), day, text = info
        projects[project] = name
        path_key = _short_hash(str(path.resolve()))
        key = _short_hash(f"{project}\n{path.stem}")
        earlier = sessions.get(key)
        if earlier and not (earlier[3].startswith(text) or text.startswith(earlier[3])):
            key = path_key
            earlier = sessions.get(key)
        legacy = [f"Sessions/{day}-{slug(name)}-{path_key}.md", *(earlier[4] if earlier else ())]
        if earlier and len(earlier[3]) >= len(text):
            sessions[key] = (*earlier[:4], legacy)
        else:
            sessions[key] = (project, day, f"Sessions/{day}-{slug(name)}-{key}.md", text, legacy)
    _write_vault(root, projects, list(sessions.values()), github_repos(username), imported, notion)
    print(f"Built vault at {root} ({len(projects)} projects, {len(sessions)} sessions, {len(imported)} imported notes)")


def _short_hash(value):
    return hashlib.sha1(value.encode()).hexdigest()[:8]


def project_slugs(projects):
    slugs, taken, seen = {}, set(), set()
    for key, name in projects:
        unique = base = slug(name)
        digest = hashlib.sha1((key if name in seen else name).encode()).hexdigest()
        seen.add(name)
        length = 8
        while unique in taken:
            unique = f"{base}-{digest[:length]}" if length <= len(digest) else f"{base}-{digest}-{length}"
            length += 1
        slugs[key] = unique
        taken.add(unique)
    return slugs


def _planned(root, relative):
    return note_target(root, relative).relative_to(root).as_posix()


def _write_vault(root, projects, sessions, repos, imported=(), notion=frozenset()):
    for repo in repos:
        if repo["name"] not in projects.values():
            projects[repo["name"]] = repo["name"]
    keys = sorted(projects, key=lambda key: (projects[key].casefold(), projects[key], key))
    session_paths = {note: _planned(root, note) for _, _, note, _, _ in sessions}
    project_paths = {key: _planned(root, f"Projects/{unique}.md") for key, unique in project_slugs((key, projects[key]) for key in keys).items()}
    imported_paths, reserved = {}, set()
    for note, _ in imported:
        planned, number = _planned(root, note), 2
        while planned.casefold() in reserved:
            planned, number = _planned(root, f"{note.removesuffix('.md')} ({number}).md"), number + 1
        reserved.add(planned.casefold())
        imported_paths[note] = planned
    index_path = _planned(root, "Index.md")
    start_path = _planned(root, "START-HERE.md")
    for project, day, note, text, _ in sessions:
        write_note(root, session_paths[note], f"# {projects[project]} — {day}\n\nProject: {link(project_paths[project])}\n\n{text}")
    for key in keys:
        name = projects[key]
        repo = next((r for r in repos if r["name"] == name), None)
        body = f"# {name}\n\n"
        if repo:
            body += f"GitHub: [{repo['html_url']}]({repo['html_url']})\n\n{repo.get('description') or ''}\n\n"
        links = [f"- {link(session_paths[note])} ({day})" for project, day, note, _, _ in sessions if project == key]
        body += "## Sessions\n\n" + ("\n".join(links) if links else "No local sessions found.")
        write_note(root, project_paths[key], body)
    moved = {note.removesuffix(".md"): path.removesuffix(".md") for note, path in imported_paths.items() if note != path and note in notion}
    for note, text in imported:
        if moved and note in notion:
            text = _CODE_OR_WIKILINK.sub(lambda match: match.group(0) if match.group(1) or match.group(2) else moved.get(match.group(0), match.group(0)), text)
        write_note(root, imported_paths[note], text)
    index = "# Vault index\n\n## Projects\n\n" + "\n".join(f"- {link(project_paths[key])}" for key in keys)
    if imported:
        index += "\n\n## Imported notes\n\n" + "\n".join(f"- {link(imported_paths[note])}" for note, _ in imported)
    write_note(root, index_path, index)
    write_note(root, start_path, (
        f"# Start here\n\nOpen {link(index_path)} to browse projects and sessions.\n\n"
        "Each project note lists its conversation sessions; each session note holds the conversation text "
        "(tool outputs are not imported).\n\n"
        "Search the vault from a terminal:\n\n"
        "    python3 vault_kit.py search TERM --vault DIR\n"
    ))
    _retire_path_keyed_sessions(root, sessions, set(session_paths.values()))


def _retire_path_keyed_sessions(root, sessions, written):
    for _, _, _, _, legacy in sessions:
        for note in legacy:
            if note not in written and _is_managed_note(root / note):
                (root / note).unlink()


def _read(path):
    return path.read_text(encoding="utf-8")


def _notes(root):
    for path in sorted(root.rglob("*.md")):
        if path.is_symlink():
            continue
        try:
            yield path.relative_to(root).as_posix(), _read(path)
        except (OSError, UnicodeError):
            continue


def _index_folders(root):
    yield root, (".coldvault",)
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    yield cache, ("coldvault", hashlib.sha1(str(root.resolve()).encode()).hexdigest()[:16])
    yield None


def _owned_folder(base, parts):
    base.mkdir(parents=True, exist_ok=True)
    folder = base
    for part in parts:
        folder = folder / part
        with contextlib.suppress(FileExistsError):
            folder.mkdir(mode=0o700)
        if folder.is_symlink() or not folder.is_dir():
            raise OSError(f"{folder} is not a plain folder")
    for name in (".gitignore", "search.sqlite", "search.sqlite-journal", "search.sqlite-wal", "search.sqlite-shm"):
        if (folder / name).is_symlink():
            raise OSError(f"{folder / name} is a symlink")
    with contextlib.suppress(FileExistsError), open(folder / ".gitignore", "x", encoding="utf-8") as stream:
        stream.write("*\n")
    return folder


def _open_index(location, tag):
    if location is None:
        database = sqlite3.connect(":memory:", isolation_level=None)
    else:
        database = sqlite3.connect(str(_owned_folder(*location) / "search.sqlite"), timeout=60, isolation_level=None)
    try:
        database.execute("BEGIN IMMEDIATE")
        tables = {name for (name,) in database.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if tables and "meta" not in tables:
            raise sqlite3.DatabaseError("not a Coldvault search index")
        database.execute("CREATE TABLE IF NOT EXISTS meta(tag TEXT NOT NULL)")
        if database.execute("SELECT tag FROM meta").fetchall() != [(tag,)]:
            database.execute("DROP TABLE IF EXISTS files")
            database.execute("DROP TABLE IF EXISTS notes")
            database.execute("DELETE FROM meta")
            database.execute("INSERT INTO meta VALUES (?)", (tag,))
        database.execute("CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, stamp TEXT NOT NULL)")
        database.execute("CREATE VIRTUAL TABLE IF NOT EXISTS notes USING fts5(path UNINDEXED, body)")
    except BaseException:
        database.close()
        raise
    return database


def _forget(database, relative):
    database.execute("DELETE FROM notes WHERE rowid = (SELECT id FROM files WHERE path = ?)", (relative,))
    database.execute("DELETE FROM files WHERE path = ?", (relative,))


def _refresh(database, root):
    known = dict(database.execute("SELECT path, stamp FROM files"))
    seen = set()
    for path in sorted(root.rglob("*.md")):
        if path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        try:
            status = path.stat()
        except OSError:
            continue
        seen.add(relative)
        stamp = f"{status.st_size} {status.st_mtime_ns} {status.st_ctime_ns} {status.st_ino}"
        if known.get(relative) == stamp:
            continue
        _forget(database, relative)
        try:
            text = _read(path)
        except (OSError, UnicodeError):
            continue
        row = database.execute("INSERT INTO notes(path, body) VALUES (?, ?)", (relative, _fold(text))).lastrowid
        database.execute("INSERT INTO files VALUES (?, ?, ?)", (row, relative, stamp))
    for relative in known.keys() - seen:
        _forget(database, relative)
    database.execute("COMMIT")


def _ranked(root, words):
    """Order notes containing every word by SQLite FTS5 relevance from the vault's index; None when FTS5 is unavailable."""
    if sqlite3 is None:
        return None
    query = " ".join('"' + _fold(word).replace('"', '""') + '"*' for word in words)
    tag = f"1 {unicodedata.unidata_version} {root.resolve()}"
    for location in _index_folders(root):
        try:
            database = _open_index(location, tag)
        except (OSError, sqlite3.Error):
            continue
        try:
            _refresh(database, root)
            return [path for (path,) in database.execute("SELECT path FROM notes WHERE notes MATCH ? ORDER BY bm25(notes), path", (query,))]
        except sqlite3.Error:
            continue
        finally:
            database.close()
    return None


def _fold(text):
    if text.isascii():
        return text.lower()
    return "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))


def _has(word, text):
    return re.search(r"(?<![^\W_])" + re.escape(_fold(word)), _fold(text)) is not None


def search(vault, term):
    """Print matching lines, most relevant notes first; return how many lines matched."""
    found = 0
    for path, number, text in hits(vault, term):
        print(f"{path}:{number}: {text}")
        found += 1
    return found


def hits(vault, term):
    """Yield (path, line number, stripped line) for each matching line, most relevant notes first."""
    root = Path(vault).expanduser()
    words = re.findall(r"[^\W_]+", term)
    if not words:
        return
    ranked = _ranked(root, words)
    notes = None
    if ranked is None:
        notes = dict(_notes(root))
        ranked = [path for path, text in notes.items() if all(_has(word, text) for word in words)]
    for path in ranked:
        if notes is not None:
            text = notes[path]
        else:
            try:
                text = _read(root / path)
            except (OSError, UnicodeError):
                continue
        for number, line in enumerate(text.splitlines(), 1):
            if any(_has(word, line) for word in words):
                yield path, number, line.strip()


NOTION_API = "https://api.notion.com"
NOTION_VERSION = "2026-03-11"
NOTION_REQUESTS_PER_SECOND = 3
_NOTION_PAGE_ID = re.compile(r"[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}")
_NOTION_UNKNOWN = re.compile(r'<unknown url="[^"#]*#([0-9a-fA-F-]{32,36})"[^>]*/>')
_NOTION_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z")
_SIGNED_QUERY = re.compile(r"(?i)(?:^|&)(?:x-amz-[a-z-]+|signature|expirationtimestamp|expires)=")
_NOTION_MEDIA = re.compile(r'<(file|image|video|audio|pdf)\b[^>]*?\bsource="([^"]*)"[^>]*>(.*?)</\1>', re.S)
_LINK_START = re.compile(r"!?\[([^\]\n]*)\]\((?=https?://)|https?://")
_BARE_URL = re.compile(r"[^\s<>\"']*")
_MARKDOWN_ESCAPE = re.compile(r"\\([!-/:-@\[-`{-~])")


class NotionBudgetSpent(Exception):
    pass


class _RefuseRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NOTION_OPENER = build_opener(_RefuseRedirect)


def _signed(url):
    try:
        return bool(_SIGNED_QUERY.search(unquote(urlsplit(html.unescape(_MARKDOWN_ESCAPE.sub(r"\1", url))).query)))
    except ValueError:
        return False


def strip_signed_urls(markdown):
    """Replace expiring pre-signed file links with their caption; attachments are not downloaded."""
    def media(match):
        if not _signed(match.group(2)):
            return match.group(0)
        return f"{re.sub(r'<[^>]*>', '', match.group(3)).strip() or 'file'} (file not imported)"

    text, kept, at = _NOTION_MEDIA.sub(media, markdown), [], 0
    while (match := _LINK_START.search(text, at)):
        kept.append(text[at:match.start()])
        if match.group(0).startswith("http"):
            url = text[match.start():_BARE_URL.match(text, match.end()).end()]
            url = url[:len(url) - min(len(url) - len(url.rstrip(")")), max(0, url.count(")") - url.count("(")))]
            end = match.start() + len(url)
            kept.append("(file link not imported)" if _signed(url) else url)
        else:
            depth, end = 1, match.end()
            while end < len(text) and depth and text[end] not in " \t\n<>\"'":
                depth += {"(": 1, ")": -1}.get(text[end], 0)
                end += 2 if text[end] == "\\" else 1
            url, caption = text[match.end():end - (depth == 0)], strip_signed_urls(match.group(1))
            kept.append(f"{caption.strip() or 'file'} (file not imported)" if _signed(url) else text[match.start():match.start(1)] + caption + text[match.end(1):end])
        at = end
    return "".join(kept) + text[at:]


def read_notion_token(path):
    path = Path(path).expanduser()
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("the token file is not a regular file")
    if info.st_mode & 0o077:
        raise ValueError("the token file must be readable only by you (chmod 600)")
    token = path.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[\x21-\x7e]+", token):
        raise ValueError("the token file must hold one token on one line")
    return token


def _retry_after(value):
    try:
        return max(float(value), 0.0)
    except (TypeError, ValueError):
        return 1.0


class NotionClient:
    """Notion API calls at most NOTION_REQUESTS_PER_SECOND, honouring 429 Retry-After, within a deadline."""

    def __init__(self, token, api, deadline, clock, sleep):
        self.token, self.api, self.deadline, self.clock, self.sleep = token, api.rstrip("/"), deadline, clock, sleep
        self.next_start = clock()
        self.requests = 0

    def _pause(self, seconds):
        if self.clock() + max(seconds, 0) >= self.deadline:
            raise NotionBudgetSpent()
        if seconds > 0:
            self.sleep(seconds)

    @staticmethod
    def _fetch(request, timeout):
        answer = {}

        def fetch():
            try:
                with _NOTION_OPENER.open(request, timeout=timeout) as response:
                    answer["result"] = json.load(response)
            except Exception as error:
                answer["error"] = error

        worker = threading.Thread(target=fetch, daemon=True)
        worker.start()
        worker.join(timeout)
        if worker.is_alive():
            raise TimeoutError("the Notion API did not answer in time")
        if "error" in answer:
            raise answer["error"]
        return answer["result"]

    def call(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
            "User-Agent": f"coldvault/{__version__}",
        }
        while True:
            self._pause(self.next_start - self.clock())
            self.next_start = self.clock() + 1 / NOTION_REQUESTS_PER_SECOND
            self.requests += 1
            timeout = min(30.0, self.deadline - self.clock())
            try:
                result = self._fetch(Request(self.api + path, data=data, method=method, headers=headers), timeout)
            except HTTPError as error:
                error.close()
                if error.code != 429:
                    raise
                self._pause(_retry_after(error.headers.get("Retry-After")))
                continue
            except OSError as error:
                timed_out = (socket.timeout, TimeoutError)
                if timeout < 30 and (isinstance(error, timed_out) or isinstance(getattr(error, "reason", None), timed_out)):
                    raise NotionBudgetSpent() from None
                raise
            if not isinstance(result, dict):
                raise ValueError("unexpected Notion API response")
            return result


def _notion_title(page):
    for prop in (page.get("properties") or {}).values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            text = "".join(part.get("plain_text") or "" for part in prop.get("title") or [] if isinstance(part, dict))
            return re.sub(r"\s+", " ", text).strip() or "Untitled"
    return "Untitled"


def _notion_scrub(text, token):
    return redact(strip_signed_urls(text.replace(token, "[REDACTED]")))


def _notion_inventory(client, progress):
    saved = json.loads(progress.read_text(encoding="utf-8")) if progress.is_file() else {}
    pages, cursor = saved.get("pages", {}), saved.get("cursor")
    if saved.get("complete"):
        return pages, bool(saved.get("resumed"))
    while True:
        body = {"filter": {"property": "object", "value": "page"}, "page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        try:
            result = client.call("POST", "/v1/search", body)
        except NotionBudgetSpent:
            _atomic_write(progress, json.dumps({"cursor": cursor, "pages": pages}, sort_keys=True, ensure_ascii=False) + "\n")
            raise
        except HTTPError as error:
            if not (cursor and saved and error.code == 400):
                raise
            pages, cursor, saved = {}, None, {}
            continue
        results = result.get("results")
        if not isinstance(results, list):
            raise ValueError("unexpected Notion search response")
        for page in results:
            if not isinstance(page, dict) or page.get("object") != "page" or page.get("in_trash") or page.get("archived"):
                continue
            page_id, edited = str(page.get("id", "")).lower(), page.get("last_edited_time")
            if not _NOTION_PAGE_ID.fullmatch(page_id) or not isinstance(edited, str) or not _NOTION_TIME.fullmatch(edited):
                raise ValueError("unexpected Notion page id or last_edited_time")
            page_id = page_id.replace("-", "")
            pages[page_id] = {"id": page_id, "last_edited_time": edited, "title": _notion_scrub(_notion_title(page), client.token) or "Untitled"}
        if not result.get("has_more"):
            _atomic_write(progress, json.dumps({"complete": True, "pages": pages, "resumed": bool(saved.get("cursor"))}, sort_keys=True, ensure_ascii=False) + "\n")
            return pages, bool(saved.get("cursor"))
        cursor = result.get("next_cursor")
        if not isinstance(cursor, str) or not cursor:
            raise ValueError("unexpected Notion search cursor")


def _notion_read(client, block_id):
    result = client.call("GET", f"/v1/pages/{block_id}/markdown?markdown_version=v2")
    markdown = result.get("markdown")
    if not isinstance(markdown, str):
        raise ValueError("unexpected Notion markdown response")
    unknown = (str(item).lower().replace("-", "") for item in result.get("unknown_block_ids") or []) if result.get("truncated") else ()
    return _notion_scrub(markdown, client.token), [item for item in unknown if _NOTION_PAGE_ID.fullmatch(item)]


def _notion_fill(client, work):
    while work["pending"]:
        block = work["pending"][0]
        part, more = _notion_read(client, block)
        filled = _NOTION_UNKNOWN.sub(lambda tag: part if tag.group(1).lower().replace("-", "") == block else tag.group(0), work["markdown"])
        work["markdown"] = filled if filled != work["markdown"] else f"{work['markdown']}\n\n{part}"
        work["pending"] = work["pending"][1:] + [item for item in more if item not in work["pending"]]


def _notion_note(page, markdown):
    body = markdown.strip()
    return (
        f"---\nsource: notion\nnotion-id: {page['id']}\nlast-edited: {page['last_edited_time']}\nauthorship: other\n---\n\n"
        f"# {page['title']}\n\n{body}\n"
    )


def _notion_name(title):
    return re.sub(r"[^a-z0-9]+", "-", _fold(title))[:80].strip("-") or "untitled"


def _jsonl(path):
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def notion_sync(token, out, state, folder="Imported/Notion", budget_seconds=300, api=NOTION_API, clock=time.monotonic, sleep=time.sleep):
    """Copy every page shared with the integration into OUT; return 0 (pages changed since the last run that returned 0), 10 (no page changed), 20 (budget spent) or 1."""
    out, state = Path(out), Path(state)
    pages_dir, unreported, unreported_retired = state / "pages", state / "unreported", state / "retired.jsonl"
    pages_dir.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    (out / ".notion-sync").touch()
    started = _utc_now()
    client = NotionClient(token, api, clock() + budget_seconds, clock, sleep)
    cached = {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in sorted(pages_dir.glob("*.json"))}
    counts = {"seen": 0, "written": 0, "updated": 0, "unchanged": 0, "duplicate": 0, "failed": 0, "deferred": 0, "retired": 0}
    notes = ["Signed file URLs are replaced by their captions; attachments are not downloaded."]
    try:
        inventory, resumed = _notion_inventory(client, state / "inventory.json")
        if resumed:
            notes.append("The page list was gathered over more than one run; a page unshared meanwhile is retired by the next complete listing.")
    except NotionBudgetSpent:
        inventory = None
        notes.append("The budget ran out while listing pages, so nothing was fetched or retired; the next run continues the listing.")
    changed = []
    if inventory is not None:
        counts["seen"] = len(inventory)
        for page_id in sorted(set(cached) - set(inventory)):
            unreported.touch()
            (pages_dir / f"{page_id}.json").unlink()
            del cached[page_id]
        changed = sorted(
            (page for page in inventory.values()
             if cached.get(page["id"], {}).get("last_edited_time") != page["last_edited_time"] or cached.get(page["id"], {}).get("pending")),
            key=lambda page: (page["last_edited_time"], page["id"]),
        )
    gone, unfinished = 0, 0
    for done, page in enumerate(changed):
        earlier, work = cached.get(page["id"], {}), None
        try:
            if earlier.get("pending") and earlier.get("last_edited_time") == page["last_edited_time"]:
                work = {"markdown": earlier["markdown"], "pending": list(earlier["pending"])}
            else:
                markdown, pending = _notion_read(client, page["id"])
                work = {"markdown": markdown, "pending": pending}
            _notion_fill(client, work)
        except HTTPError as error:
            if error.code != 404 or work is not None:
                raise
            gone += 1
            if cached.pop(page["id"], None) is not None:
                unreported.touch()
                (pages_dir / f"{page['id']}.json").unlink()
            continue
        except NotionBudgetSpent:
            if work is not None:
                unfinished = 1
                cached[page["id"]] = dict(page, text=_notion_note(page, work["markdown"]), **work)
                unreported.touch()
                _atomic_write(pages_dir / f"{page['id']}.json", json.dumps(cached[page["id"]], sort_keys=True, ensure_ascii=False) + "\n")
            counts["deferred"] = len(changed) - done
            notes.append(f"The budget ran out; {counts['deferred']} changed pages wait for the next run.")
            break
        counts["updated" if page["id"] in cached else "written"] += 1
        cached[page["id"]] = dict(page, text=_notion_note(page, work["markdown"]))
        unreported.touch()
        _atomic_write(pages_dir / f"{page['id']}.json", json.dumps(cached[page["id"]], sort_keys=True, ensure_ascii=False) + "\n")
    if inventory is not None and not counts["deferred"]:
        (state / "inventory.json").unlink()
    incomplete = sum("<unknown " in page["text"] for page in cached.values())
    if incomplete:
        notes.append(f"{incomplete} pages hold blocks Notion did not return; they appear as <unknown> tags.")
    if gone:
        notes.append(f"{gone} listed pages could not be read (no longer shared) and were left out.")
    counts["unchanged"] = len(cached) - counts["written"] - counts["updated"] - unfinished

    earlier_rows = {row["relative_path"]: row for row in _jsonl(unreported_retired) + _jsonl(state / "manifest.jsonl")}
    rows, taken = [], set()
    for page_id in sorted(cached):
        name = _notion_name(cached[page_id]["title"])
        relative = f"{folder}/{name}-{page_id[:8]}.md"
        if relative.casefold() in taken:
            relative = f"{folder}/{name}-{page_id}.md"
        taken.add(relative.casefold())
        target = out / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, cached[page_id]["text"])
        rows.append({"id": f"notion:{page_id}", "relative_path": relative, "file_hash": "sha256:" + _sha256(cached[page_id]["text"].encode("utf-8"))})
    rows.sort(key=lambda row: row["relative_path"])
    retired = sorted(set(earlier_rows) - {row["relative_path"] for row in rows})
    if retired:
        unreported.touch()
    _atomic_write(unreported_retired, "".join(json.dumps(earlier_rows[relative], sort_keys=True) + "\n" for relative in retired))
    counts["retired"] = len(retired)
    manifest = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
    _atomic_write(out / "manifest.jsonl", manifest)
    _atomic_write(state / "manifest.jsonl", manifest)

    secret = token.encode("utf-8")
    verified = all(
        (out / row["relative_path"]).is_file()
        and (out / row["relative_path"]).read_bytes() == cached[row["id"][7:]]["text"].encode("utf-8")
        and "sha256:" + _sha256((out / row["relative_path"]).read_bytes()) == row["file_hash"]
        and secret not in (out / row["relative_path"]).read_bytes()
        for row in rows
    )
    partial = inventory is None or counts["deferred"] > 0
    receipt = {
        "source": "notion",
        "run_id": uuid.uuid4().hex,
        "started_at": started,
        "finished_at": _utc_now(),
        "tool_versions": {"vault_kit": "notion-sync-1", "notion_version": NOTION_VERSION, "markdown_version": "v2"},
        "counts": counts,
        "reconciled": inventory is not None,
        "partial": partial,
        "retired": retired,
        "upstream": {
            "newest_source_event_at": max((page["last_edited_time"] for page in cached.values()), default=None),
            "collector_completed_at": None,
            "frontier": {"pages": len(cached), "requests": client.requests, "inventory_complete": inventory is not None},
            "coverage_notes": notes,
        },
        "verification": {"command": "vault_kit.py notion-sync: manifest hashes match the written notes", "ok": verified},
    }
    _atomic_write(out / "receipt.json", json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(json.dumps(receipt, sort_keys=True), flush=True)
    print(f"Notion sync: {len(cached)} pages, {counts['written']} new, {counts['updated']} updated, {counts['retired']} retired, {counts['deferred']} deferred", file=sys.stderr)
    if not verified:
        return 1
    if partial:
        return 20
    if not unreported.exists():
        unreported_retired.unlink()
        return 10
    unreported.unlink()
    unreported_retired.unlink()
    return 0


def notion_main(argv, api=NOTION_API, clock=time.monotonic, sleep=time.sleep):
    parser = argparse.ArgumentParser(prog="vault_kit.py notion-sync", description="Copy the Notion pages shared with an integration into a folder.")
    parser.add_argument("--token-file", required=True, help="File holding the integration token, readable only by you (chmod 600)")
    parser.add_argument("--out", required=True, help="New or empty folder (or one notion-sync started) for the notes, manifest.jsonl and receipt.json")
    parser.add_argument("--state", required=True, help="Folder for the sync state, outside --out")
    parser.add_argument("--folder", default="Imported/Notion", help="Folder for the notes inside --out (default: Imported/Notion)")
    parser.add_argument("--budget-seconds", type=int, default=300, help="Stop fetching after this many seconds (default: 300)")
    args = parser.parse_args(argv)
    if args.budget_seconds < 1:
        parser.error("--budget-seconds must be at least 1")
    parts = args.folder.split("/")
    if not all(re.fullmatch(r"[^\\:]+", part) and not part.startswith(".") for part in parts):
        parser.error("--folder must be a relative path such as Imported/Notion")
    out, state = Path(args.out).expanduser().resolve(), Path(args.state).expanduser().resolve()
    if out == state or out in state.parents or state in out.parents:
        parser.error("keep --state and --out apart")
    marker = out / ".notion-sync"
    if out.exists() and (not out.is_dir() or any(out.iterdir())) and not (marker.is_file() and not marker.is_symlink()):
        parser.error("--out must be a new or empty folder, or one notion-sync started")
    if any((out / "/".join(parts[:n])).is_symlink() for n in range(1, len(parts) + 1)):
        parser.error("--folder must not pass through a symlink inside --out")
    try:
        token = read_notion_token(args.token_file)
        return notion_sync(token, out, state, args.folder, args.budget_seconds, api, clock, sleep)
    except HTTPError as error:
        print(f"{parser.prog}: error: the Notion API returned HTTP {error.code}", file=sys.stderr)
    except (OSError, URLError, ValueError) as error:
        print(f"{parser.prog}: error: {error}", file=sys.stderr)
    if (state / "inventory.json").is_file():
        (state / "inventory.json").unlink()
    return 1


if __name__ == "__main__":
    main()
