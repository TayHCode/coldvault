#!/usr/bin/env python3
"""Build a small Obsidian vault from local Claude Code and Codex conversations, Markdown folders, Notion exports and the Notion API."""

import argparse
import contextlib
import csv
import glob
import hashlib
import io
import json
import os
import posixpath
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
import zipfile
import zoneinfo
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import Request, urlopen

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
    source, session_id = None, None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        payload = record.get("payload")
        kind = record.get("type")
        source = source or _SOURCES.get(kind)
        found = record.get("sessionId") or (payload.get("id") if kind == "session_meta" and isinstance(payload, dict) else None)
        if not session_id and isinstance(found, str) and found:
            session_id = found
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
    return project, day, "\n\n".join(lines), source, session_id or path.stem


class SourceError(Exception):
    pass


_GITHUB_API = "https://api.github.com/"


def _github_token():
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token and not re.fullmatch(r"[\x21-\x7e]+", token):
        raise SourceError("GITHUB_TOKEN contains spaces or control characters")
    return token


def _github_get(url, token, accept="application/vnd.github+json", etag=""):
    if not url.startswith(_GITHUB_API):
        raise SourceError("refusing a GitHub request outside api.github.com")
    request = Request(url, headers={"Accept": accept, "User-Agent": f"coldvault/{__version__}"})
    if etag:
        request.add_header("If-None-Match", etag)
    if token:
        request.add_unredirected_header("Authorization", f"Bearer {token}")
    with urlopen(request, timeout=4) as response:
        return response.read(), response.headers


def _github_error(error, token):
    return SourceError(redact(str(error).replace(token, "[REDACTED]") if token else str(error)))


def github_repos(username):
    if not username:
        return []
    token = _github_token()
    url = f"{_GITHUB_API}users/{username}/repos?per_page=100&sort=updated"
    repos = []
    try:
        while url:
            body, headers = _github_get(url, token)
            page = json.loads(body)
            if not isinstance(page, list):
                raise SourceError("GitHub returned an unexpected response")
            repos += [repo for repo in page if isinstance(repo, dict) and repo.get("name")]
            match = re.search(r'<(https://api\.github\.com/[^>]*)>;\s*rel="next"', headers.get("Link") or "")
            url = match and match.group(1)
        return repos
    except (OSError, URLError, ValueError) as error:
        raise _github_error(error, token) from None


_SOURCES = {"user": "claude-code", "assistant": "claude-code", "session_meta": "codex", "response_item": "codex", "event_msg": "codex"}
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


def _is_day(value):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _is_timestamp(value):
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def _frontmatter(fields):
    return "---\n" + "".join(f"{key}: {value}\n" for key, value in fields.items()) + "---\n\n"


def _has_frontmatter(text):
    return text.startswith("---\n") and "\n---\n" in text[3:]


def _field(text, name):
    if _has_frontmatter(text):
        for line in text[4:text.index("\n---\n", 3)].split("\n"):
            if line.startswith(f"{name}:"):
                return line.removeprefix(f"{name}:").strip().strip("'\"")
    return None


def _imported_at(path, now):
    value = _field(_read(path), "imported-at") if _is_managed_note(path) else None
    return value if _is_timestamp(value) else now


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
        if not _inside(base, resolved):
            raise ValueError("A note path escaped the chosen vault folder")
    if (target.exists() or target.is_symlink()) and not _is_managed_note(target):
        target = _copy_target(target)
    resolved = target.resolve()
    if not _inside(base, resolved):
        raise ValueError("A note path escaped the chosen vault folder")
    return target


def _inside(base, path):
    try:
        return os.path.commonpath((base, path)) == str(base)
    except ValueError:
        return False


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
    return _write_rendered(root, relative, _render(text))


def _render(text):
    body = f"{text.rstrip()}\n"
    if _has_frontmatter(body):
        unhashed = f"---\n{_NOTE_MARKER}\n{body[4:]}"
    else:
        unhashed = f"---\n{_NOTE_MARKER}\n---\n\n{body}"
    lines = unhashed.split("\n")
    lines.insert(2, _HASH_FIELD + _digest(unhashed))
    return "\n".join(lines)


def _unchanged(root, relative, rendered):
    existing = root / relative
    return not existing.is_symlink() and existing.is_file() and _inside(root.resolve(), existing.resolve()) and _current_text(existing) == rendered.replace("\n", os.linesep)


def _write_rendered(root, relative, rendered):
    if _unchanged(root, relative, rendered):
        return relative
    target = note_target(root, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(target, rendered)
    return target.relative_to(root).as_posix()


def _current_text(path):
    try:
        with open(path, encoding="utf-8", newline="") as stream:
            return stream.read()
    except (OSError, UnicodeError):
        return None


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
    if sys.argv[1:2] == ["status"]:
        sys.exit(status_main(sys.argv[2:]))
    if sys.argv[1:2] == ["schedule"]:
        sys.exit(schedule_main(sys.argv[2:]))
    if sys.argv[1:2] == ["setup"]:
        setup_main(sys.argv[2:])
        return
    sync_main(sys.argv[2:] if sys.argv[1:2] == ["sync"] else sys.argv[1:])


_STANDARD_TRANSCRIPTS = ((".claude", "projects"), (".codex", "sessions"), (".codex", "archived_sessions"))


def _standard_transcript_folders():
    return [Path.home().joinpath(*parts) for parts in _STANDARD_TRANSCRIPTS]


def _config_path(root):
    return root / ".coldvault" / "config.json"


def _check_sources(parser, root, notes, notion_exports, transcripts, chat_exports=()):
    for folder in notes:
        path = Path(folder).expanduser().resolve()
        if not path.is_dir():
            parser.error(f"notes folder not found: {folder}")
        if path == root or root in path.parents:
            parser.error(f"choose a notes folder outside the vault: {folder}")
    for export in notion_exports:
        path = Path(export).expanduser().resolve()
        if path.is_dir():
            if path == root or root in path.parents:
                parser.error(f"choose a Notion export outside the vault: {export}")
        elif not zipfile.is_zipfile(path):
            parser.error(f"Notion export must be a .zip file or an unzipped folder: {export}")
    for export in chat_exports:
        if not zipfile.is_zipfile(Path(export).expanduser()):
            parser.error(f"ChatGPT and Claude.ai exports must be the downloaded .zip file: {export}")
    for folder in transcripts:
        if not Path(folder).expanduser().is_dir():
            parser.error(f"transcript folder not found: {folder}")


FOLDERS = {"projects": "Projects", "sessions": "Sessions", "imported": "Imported"}


def _folder_name(value):
    return (isinstance(value, str) and value == value.strip() and value not in ("", ".", "..")
            and not value.startswith(".") and not any(character in value for character in "/\\:\0[]|#^`"))


def _valid_folders(folders):
    if not isinstance(folders, dict) or not set(folders) <= set(FOLDERS) or not all(map(_folder_name, folders.values())):
        return False
    names = {**FOLDERS, **folders}.values()
    return len({name.casefold() for name in names}) == len(names)


def _load_config(parser, root):
    path = _config_path(root)
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink():
        parser.error(f"{path} is a symlink; move it aside, then run setup")
    try:
        config = json.loads(_read(path))
    except (OSError, UnicodeError, ValueError) as error:
        parser.error(f"cannot read {path}: {error}")
    lists = ("transcripts", "notes", "notion_exports")
    if (not isinstance(config, dict) or type(config.get("version")) is not int or config["version"] != 1
            or not isinstance(config.get("username"), str)
            or not all(isinstance(config.get(key), list) and all(isinstance(item, str) and item and "\0" not in item for item in config[key]) for key in lists)
            or ("schedule_hours" in config and (type(config["schedule_hours"]) is not int or config["schedule_hours"] < 1))
            or ("folders" in config and not _valid_folders(config["folders"]))):
        parser.error(f"{path} is not a Coldvault config; move it aside, then run setup")
    return config


def sync_main(argv):
    parser = argparse.ArgumentParser(description="Build an Obsidian vault from local Claude Code and Codex transcripts, Markdown folders, Notion exports and ChatGPT or Claude.ai exports. Run 'setup' once to save your choices.")
    parser.add_argument("--version", action="version", version=f"coldvault {__version__}")
    parser.add_argument("--username", help="GitHub username for a public repository lookup")
    parser.add_argument("--transcripts", help="Transcript folder (default: standard Claude and Codex folders)")
    parser.add_argument("--output", "--vault", dest="output", help="Vault folder (asked when omitted)")
    parser.add_argument("--notes", action="append", default=[], metavar="DIR", help="Markdown folder to import under Imported/ (repeatable)")
    parser.add_argument("--notion-export", action="append", default=[], metavar="ZIP", help="Notion 'Markdown & CSV' export (.zip or unzipped folder) to import under Imported/Notion/ (repeatable)")
    parser.add_argument("--chatgpt-export", action="append", default=[], metavar="ZIP", help="ChatGPT data export (.zip) to import as session notes (repeatable)")
    parser.add_argument("--claude-export", action="append", default=[], metavar="ZIP", help="Claude.ai data export (.zip) to import as session notes (repeatable)")
    args = parser.parse_args(argv)
    config = _load_config(parser, Path(args.output).expanduser().resolve()) if args.output else None
    notes = args.notes or (config["notes"] if config else [])
    notion_exports = args.notion_export or (config["notion_exports"] if config else [])
    imports = notes or notion_exports or args.chatgpt_export or args.claude_export

    def ask(prompt):
        try:
            return input(prompt).strip()
        except EOFError:
            parser.error("pass --username and --output when not interactive")

    if args.username is not None:
        username = args.username
    elif config:
        username = config["username"]
    else:
        username = "" if imports else ask("GitHub username (optional): ")
    output = args.output or ask("Folder for your Obsidian vault: ")
    if not output:
        parser.error("choose an output folder")
    root = Path(output).expanduser().resolve()
    started = _utc_now()
    if root.is_dir():
        def refuse(message):
            with contextlib.suppress(OSError):
                _record_run(root, started, {"sources": {"state": "failed", "reason": message}})
            argparse.ArgumentParser.error(parser, message)
        parser.error = refuse
    folders = {**FOLDERS, **(config.get("folders", {}) if config else {})}
    previous, pending = _previous_folders(root)
    if pending and pending != folders:
        parser.error(f"an earlier folder rename to {', '.join(pending.values())} has not finished; set those folders again and sync (move any of your files in the way first)")
    crossed = _crossed_folder(previous, folders)
    if crossed:
        parser.error(f"'{folders[crossed]}' is still another generated folder; rename it to a new name first, sync, then rename {crossed} to '{folders[crossed]}'")
    _check_sources(parser, root, notes, notion_exports, [args.transcripts] if args.transcripts else config["transcripts"] if config else [], args.chatgpt_export + args.claude_export)
    if args.transcripts:
        files = sorted(Path(args.transcripts).expanduser().rglob("*.jsonl"))
        if not files:
            parser.error(f"no transcripts (*.jsonl) found in {args.transcripts}")
    elif config:
        files = [p for folder in config["transcripts"] for p in Path(folder).expanduser().rglob("*.jsonl")]
    elif imports:
        files = []
    else:
        files = [p for folder in _standard_transcript_folders() if folder.is_dir() for p in folder.rglob("*.jsonl")]
        if not files and not username:
            parser.error("no transcripts found in ~/.claude/projects, ~/.codex/sessions or ~/.codex/archived_sessions; pass --transcripts DIR, --notes DIR, --notion-export ZIP, --chatgpt-export ZIP or --claude-export ZIP")
    try:
        sources = build_from_files(files, output, username, notes, notion_exports, folders, args.chatgpt_export, args.claude_export)
    except OSError as error:
        detail = f"{error.filename}: {error.strerror}" if error.filename else str(error)
        print(f"{parser.prog}: error: {detail}", file=sys.stderr)
        with contextlib.suppress(OSError):
            _record_run(root, started, {"vault": {"state": "failed", "reason": detail}})
        sys.exit(2)
    failed = [f"{name}: failed ({source['reason']})" for name, source in sources.items() if source["state"] == "failed"]
    for line in failed:
        print(line, file=sys.stderr)
    try:
        _record_run(root, started, sources)
    except OSError as error:
        print(f"{parser.prog}: error: cannot record this run: {error}", file=sys.stderr)
        sys.exit(2)
    if failed:
        sys.exit(3)


_RECEIPT = "last-run.json"
_HISTORY = "runs.jsonl"
_STATES = ("ok", "failed", "skipped")


def _record_run(root, started, sources):
    folder = _owned_folder(root, (".coldvault",))
    ok = all(source["state"] != "failed" for source in sources.values())
    finished = _utc_now()
    previous = _receipt(folder / _RECEIPT)
    last_success = finished if ok else previous["last_success"] if isinstance(previous, dict) else None
    receipt = {"version": 1, "started": started, "finished": finished, "ok": ok, "last_success": last_success, "sources": sources}
    line = json.dumps(receipt, ensure_ascii=False)
    try:
        with open(folder / _HISTORY, "a", encoding="utf-8", opener=lambda path, flags: os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), 0o600)) as stream:
            stream.write(line + "\n")
    except OSError as error:
        sources = {**sources, "history": {"state": "failed", "reason": f"cannot append to {_HISTORY}: {error.strerror or error}"}}
        receipt.update(ok=False, last_success=previous["last_success"] if isinstance(previous, dict) else None, sources=sources)
        _atomic_write(folder / _RECEIPT, json.dumps(receipt, ensure_ascii=False) + "\n")
        raise
    _atomic_write(folder / _RECEIPT, line + "\n")


def _when(value):
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _receipt(path):
    if not path.exists() and not path.is_symlink():
        return None
    try:
        if path.is_symlink():
            raise ValueError("it is a symlink")
        receipt = json.loads(_read(path))
        if not isinstance(receipt, dict) or receipt.get("version") != 1 or type(receipt.get("ok")) is not bool:
            raise ValueError("unexpected content")
        _when(receipt["started"]), _when(receipt["finished"])
        if receipt["last_success"] is not None:
            _when(receipt["last_success"])
        sources = receipt["sources"]
        if not isinstance(sources, dict) or not sources or not all(
                isinstance(name, str) and isinstance(source, dict) and source.get("state") in _STATES
                and (source["state"] != "failed" or isinstance(source.get("reason"), str))
                and (source["state"] != "ok" or (type(source.get("count")) is int and source["count"] >= 0))
                for name, source in sources.items()):
            raise ValueError("unexpected sources")
        if receipt["ok"] != all(source["state"] != "failed" for source in sources.values()):
            raise ValueError("ok does not match the sources")
        if receipt["ok"] and receipt["last_success"] != receipt["finished"]:
            raise ValueError("a successful run must be the last success")
        if receipt["last_success"] is not None and _when(receipt["last_success"]) > _when(receipt["finished"]):
            raise ValueError("last success is after the run")
        return receipt
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as error:
        return f"cannot read {path}: {error}"


def _last_line(path):
    try:
        with open(path, "rb") as stream:
            stream.seek(max(0, stream.seek(0, os.SEEK_END) - 65536))
            return json.loads(stream.read().decode("utf-8").splitlines()[-1])
    except (OSError, UnicodeError, ValueError, IndexError):
        return None


def _age(since, now):
    hours = (now - since).total_seconds() / 3600
    return f"{hours:.1f}h ago"


def status_main(argv, now=None):
    parser = argparse.ArgumentParser(prog=f"{Path(sys.argv[0]).name} status", description="Show the last run of each source and whether the vault is up to date. Exits 0 only when the last run succeeded and is not stale.")
    parser.add_argument("--vault", required=True, help="Vault folder")
    args = parser.parse_args(argv)
    root = Path(args.vault).expanduser().resolve()
    config = _load_config(parser, root)
    hours = config.get("schedule_hours") if config else None
    receipt = _receipt(root / ".coldvault" / _RECEIPT)
    history = root / ".coldvault" / _HISTORY
    if history.is_symlink():
        receipt = f"cannot record runs: {history} is a symlink; move it aside"
    elif isinstance(receipt, dict) and receipt["ok"] and _last_line(history) != receipt:
        receipt = f"cannot read the last run: {_RECEIPT} does not match the last line of {history}"
    now = now or datetime.now(timezone.utc)
    schedule = f"schedule: installed, next run due {_next_run(now, hours, _cron_zone())} (every {hours}h)" if hours else "schedule: not installed"
    if receipt is None:
        print(f"no run recorded in {root}; run: coldvault sync --vault {root}")
        print(schedule)
        return 1
    if isinstance(receipt, str):
        print(receipt)
        return 1
    healthy = receipt["ok"]
    print(f"last run: {receipt['finished']} ({_age(_when(receipt['finished']), now)}), {'ok' if healthy else 'failed'}")
    for name, source in receipt["sources"].items():
        detail = source.get("reason") if source["state"] == "failed" else source.get("count")
        print(f"{name}: {source['state']}" + (f" ({detail})" if detail is not None else ""))
    success = receipt["last_success"] and _when(receipt["last_success"])
    print(f"last success: {receipt['last_success']} ({_age(success, now)})" if success else "last success: never")
    print(schedule)
    if hours and (not success or now - success > timedelta(hours=2 * hours)):
        print(f"stale: no successful run in the last {2 * hours}h")
        healthy = False
    return 0 if healthy else 1


_CRON_TAG = "# coldvault-schedule "
_SCHEDULE_HOURS = (1, 2, 3, 4, 6, 8, 12, 24)


def _schedule_id(root):
    return hashlib.sha1(str(root).encode()).hexdigest()[:12]


def _cron_zone():
    try:
        with open("/etc/localtime", "rb") as stream:
            return zoneinfo.ZoneInfo.from_file(stream)
    except (OSError, ValueError):
        return None


def _next_run(now, hours, zone=None):
    moment = now.astimezone(timezone.utc).replace(second=0, microsecond=0)
    for _ in range(26 * 60):
        moment += timedelta(minutes=1)
        local = moment.astimezone(zone)
        if local.minute == 0 and local.hour % hours == 0:
            return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    return "unknown"


def _run(parser, command, stdin="", missing_ok=None):
    try:
        result = subprocess.run(command, input=stdin.encode("utf-8", "surrogateescape"), capture_output=True)
    except OSError as error:
        parser.error(f"cannot run {command[0]}: {error}")
    stderr = result.stderr.decode("utf-8", "replace").strip()
    if result.returncode and not (missing_ok and missing_ok in stderr.lower()):
        parser.error(f"{' '.join(command)} failed: {stderr or result.returncode}")
    return result.stdout.decode("utf-8", "surrogateescape") if result.returncode == 0 else ""


def _edit_crontab(parser, root, line=None):
    tag = _CRON_TAG + _schedule_id(root)
    text = _run(parser, ["crontab", "-l"], missing_ok="no crontab")
    lines = text.split("\n")[:-1] if text.endswith("\n") else text.split("\n") if text else []
    kept = [entry for entry in lines if not entry.endswith(tag)]
    if line is None and len(kept) == len(lines):
        return 0
    if _run(parser, ["crontab", "-l"], missing_ok="no crontab") != text:
        parser.error("your crontab changed while Coldvault was editing it; run the command again")
    _run(parser, ["crontab", "-"], "".join(entry + "\n" for entry in kept + ([line] if line else [])))
    return len(lines) - len(kept)


def _install_cron(parser, root, command, hours):
    hour = "*" if hours == 1 else "0" if hours == 24 else f"*/{hours}"
    line = f"0 {hour} * * * {' '.join(map(shlex.quote, command)).replace('%', chr(92) + '%')} {_CRON_TAG}{_schedule_id(root)}"
    replaced = _edit_crontab(parser, root, line)
    print(f"{'Replaced' if replaced else 'Added'} crontab line: {line}")


def _remove_cron(parser, root):
    removed = _edit_crontab(parser, root)
    if removed:
        print(f"Removed {removed} crontab line(s)")
    return removed > 0


def schedule_main(argv):
    parser = argparse.ArgumentParser(prog=f"{Path(sys.argv[0]).name} schedule", description="Run 'coldvault sync' for a vault from your crontab on a schedule, or remove that schedule. Other crontab lines are left alone.")
    parser.add_argument("action", nargs="?", choices=("install", "remove"), default="install")
    parser.add_argument("--vault", required=True, help="Vault folder set up with 'coldvault setup'")
    parser.add_argument("--hours", type=int, default=1, choices=_SCHEDULE_HOURS, help="Hours between runs, on the hour (default 1)")
    args = parser.parse_args(argv)
    if sys.platform != "linux":
        parser.error("schedule supports Linux only; on a Mac use the LaunchAgent steps in docs/customize.md, elsewhere run 'coldvault sync' from your own scheduler")
    import fcntl
    lock = Path.home() / ".cache" / "coldvault-schedule.lock"
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock, "a")
    except OSError as error:
        parser.error(f"cannot lock {lock}: {error}")
    with handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        return _schedule_locked(parser, args)


def _schedule_locked(parser, args):
    root = Path(args.vault).expanduser().resolve()
    config = _load_config(parser, root)
    if config is None:
        parser.error(f"no saved config in {root}; run: coldvault setup")
    command = [sys.executable, str(Path(__file__).resolve()), "sync", "--vault", str(root)]
    if any("\n" in part or "\r" in part for part in command):
        parser.error("cannot schedule a path that contains a line break")
    saved = json.dumps(config, indent=2) + "\n"
    config.pop("schedule_hours", None)
    if args.action == "install":
        config["schedule_hours"] = args.hours
    try:
        _atomic_write(_config_path(root), json.dumps(config, indent=2) + "\n")
    except OSError as error:
        parser.error(f"cannot save the config in {root}: {error}")
    try:
        if args.action == "install":
            _install_cron(parser, root, command, args.hours)
        elif not _remove_cron(parser, root):
            print(f"No Coldvault schedule found for {root}")
    except BaseException as error:
        _atomic_write(_config_path(root), saved)
        if isinstance(error, OSError):
            parser.error(f"cannot change the schedule for {root}: {error}")
        raise
    return 0


def fts5_available():
    if sqlite3 is None:
        return False
    try:
        with contextlib.closing(sqlite3.connect(":memory:")) as database:
            database.execute("CREATE VIRTUAL TABLE probe USING fts5(body)")
        return True
    except sqlite3.Error:
        return False


def setup_main(argv):
    parser = argparse.ArgumentParser(prog=f"{Path(sys.argv[0]).name} setup", description="Check prerequisites, choose your sources and save them in <vault>/.coldvault/config.json, then build the vault.")
    parser.parse_args(argv)

    def ask(prompt):
        try:
            return input(prompt).strip()
        except EOFError:
            parser.error("setup ended before every question was answered")

    def ask_many(prompt):
        answers = []
        while answer := ask(prompt):
            answers.append(str(Path(answer).expanduser().resolve()))
        return answers

    print(f"Python {sys.version.split()[0]}: ok")
    print("SQLite FTS5: ok" if fts5_available() else "warning: SQLite FTS5 is not available; search will scan every note instead of using an index")
    found = []
    for folder in _standard_transcript_folders():
        if folder.is_dir():
            found.append(str(folder))
            print(f"{folder}: {sum(1 for _ in folder.rglob('*.jsonl'))} sessions")
        else:
            print(f"{folder}: not found")
    output = ask("Folder for your Obsidian vault (new or existing): ")
    if not output:
        parser.error("choose a vault folder")
    root = Path(output).expanduser().resolve()
    saved = _load_config(parser, root)
    transcripts = found if found and ask("Import the session folders found above? [Y/n]: ").lower() in ("", "y", "yes") else []
    transcripts += ask_many("Another transcript folder (Enter to finish): ")
    notes = ask_many("Markdown notes folder to import (Enter to finish): ")
    notion_exports = ask_many("Notion export .zip or folder to import (Enter to finish): ")
    username = ask("GitHub username (optional): ")
    _check_sources(parser, root, notes, notion_exports, transcripts)
    config = {"version": 1, "transcripts": transcripts, "notes": notes, "notion_exports": notion_exports, "username": username}
    for key in ("folders", "schedule_hours"):
        if saved and key in saved:
            config[key] = saved[key]
    try:
        folder = _owned_folder(root, (".coldvault",))
        _atomic_write(folder / "config.json", json.dumps(config, indent=2) + "\n")
    except OSError as error:
        parser.error(f"cannot save the config in {root}: {error}")
    print(f"Saved {_config_path(root)}. Later runs only need: coldvault sync --vault {root}")
    sync_main(["--vault", str(root)])


def markdown_files(source, vault, suffixes=(".md",)):
    for folder, dirs, names in os.walk(source):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and (Path(folder) / d).resolve() != vault)
        for name in sorted(names):
            path = Path(folder) / name
            if name.endswith(suffixes) and not name.startswith(".") and not path.is_symlink():
                yield path


def _import_folders(run):
    sources = list(dict.fromkeys(Path(source).expanduser().resolve() for source in run.notes))
    exports = list(dict.fromkeys(Path(export).expanduser().resolve() for export in run.notion_exports))
    bases = [source.name.lstrip(".") or "notes" for source in sources] + ["Notion"] * len(exports)
    folders = [
        f"{base} ({hashlib.sha1(str(source).encode()).hexdigest()[:8]})"
        if [b.casefold() for b in bases].count(base.casefold()) > 1 else base
        for source, base in zip(sources + exports, bases)
    ]
    return list(zip(sources, folders)), list(zip(exports, folders[len(sources):]))


def notes_source(run):
    if not run.notes:
        return None
    notes = []
    for source, folder in _import_folders(run)[0]:
        for path in markdown_files(source, run.root):
            text = redact(path.read_text(encoding="utf-8-sig", errors="replace"))
            notes.append((f"{run.folders['imported']}/{folder}/{path.relative_to(source).as_posix()}", text, {"kind": "note"}))
    return notes


def notion_source(run):
    if not run.notion_exports:
        return None
    return [(note, text, {"kind": "note", "source": "notion"})
            for export, folder in _import_folders(run)[1]
            for note, text in notion_notes(export, run.root, f"{run.folders['imported']}/{folder}")]


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


def _readme(title, body):
    return f"---\nrecord_type: index\ntitle: {title}\nauthorship: assistant\n---\n\n# {title}\n\n{body}\n"


def _home(folders):
    return _readme("Home", (
        "This is your vault map. Coldvault fills some folders on every run; the rest are yours.\n\n"
        "- [[START-HERE]]: how to use and search the vault.\n"
        "- [[Index]]: every project and imported note Coldvault wrote.\n"
        f"- `{folders['projects']}/`: one note per project, generated from your sessions and repositories.\n"
        f"- `{folders['sessions']}/`: one note per Claude Code or Codex conversation, generated.\n"
        f"- `{folders['imported']}/`: Markdown folders and Notion exports you imported, generated.\n"
        "- [[Daily/README|Daily]]: one note per day, listing that day's sessions and imported notes.\n"
        "- [[Writing/README|Writing]]: things you wrote yourself.\n"
        "- [[Context/README|Context]]: other people's articles, posts and transcripts.\n"
        "- [[Capabilities/README|Capabilities]]: tools and skills you use, and when.\n"
        "- [[Agent-Space/README|Agent-Space]]: notes your AI agents keep: lessons, decisions, proposals.\n"
        "- [[Templates/README|Templates]]: note templates with the frontmatter each note type uses.\n"
        "- `Attachments/`: images and files you paste into notes.\n\n"
        "Coldvault never changes notes in your own folders. Delete any folder you don't need."
    ))


VAULT_TEMPLATE = {
    "Home.md": _home(FOLDERS),
    "Daily/README.md": _readme("Daily", "One note per day, named `YYYY-MM-DD.md`. Coldvault writes the note for each day with sessions or imported notes and lists them; if you edit it, your version is kept and Coldvault writes its list to `YYYY-MM-DD (Coldvault).md`. Create today's note with the Daily notes command; it uses [[Templates/Daily]]."),
    "Writing/README.md": _readme("Writing", "Your own writing: drafts, articles, posts and letters. Set `authorship: me` so you can tell your words from imported ones. New notes can start from [[Templates/Note]]."),
    "Context/README.md": _readme("Context", "Reference material written by other people: articles, posts and video transcripts. Record where each came from in `source`. Start from [[Templates/Context]]."),
    "Capabilities/README.md": _readme("Capabilities", "One note per tool, skill or service you rely on: what it does, when to use it and how. Start from [[Templates/Capability]]."),
    "Agent-Space/README.md": _readme("Agent-Space", "A place for your AI agents to write. Keep a `Notes/<project>.md` per project for lessons and decisions, and put larger plans in `Proposals/`."),
    "Templates/README.md": _readme("Templates", "Templates for each note type. Obsidian's Templates command inserts them; `{{date}}` and `{{title}}` are filled in for you."),
    "Templates/Daily.md": '---\nrecord_type: daily\ndate: "{{date}}"\nauthorship: me\nprojects: []\n---\n\n# {{date}}\n\n## Notes\n\n## Done\n',
    "Templates/Note.md": '---\nrecord_type: note\ntitle: "{{title}}"\ndate: "{{date}}"\nauthorship: me\nprojects: []\ntags: []\n---\n\n# {{title}}\n',
    "Templates/Context.md": '---\nrecord_type: context\ntitle: "{{title}}"\nsource: \nauthor: \ndate: "{{date}}"\nauthorship: other\nprojects: []\n---\n\n# {{title}}\n\n## Summary\n\n## Notes\n',
    "Templates/Capability.md": '---\nrecord_type: capability\ntitle: "{{title}}"\nkind: tool\nstatus: active\n---\n\n# {{title}}\n\n## What it does\n\n## When to use it\n\n## How\n',
    ".obsidian/app.json": json.dumps({"attachmentFolderPath": "Attachments", "newLinkFormat": "shortest"}, indent=2) + "\n",
    ".obsidian/core-plugins.json": json.dumps({name: True for name in (
        "file-explorer", "global-search", "switcher", "graph", "backlink", "outgoing-link", "tag-pane",
        "properties", "page-preview", "daily-notes", "templates", "command-palette", "bookmarks", "outline",
        "file-recovery",
    )}, indent=2) + "\n",
    ".obsidian/daily-notes.json": json.dumps({"folder": "Daily", "format": "YYYY-MM-DD", "template": "Templates/Daily"}, indent=2) + "\n",
    ".obsidian/templates.json": json.dumps({"folder": "Templates"}, indent=2) + "\n",
}


def _apply_template(root, folders=FOLDERS):
    created = {root}
    for folder in sorted({(root / relative).parent for relative in VAULT_TEMPLATE} | {root / "Attachments"}):
        if folder not in created:
            with contextlib.suppress(FileExistsError):
                folder.mkdir()
                created.add(folder)
    for relative, text in VAULT_TEMPLATE.items():
        target = root / relative
        if target.parent in created:
            with contextlib.suppress(FileExistsError), open(target, "x", encoding="utf-8", newline="\n") as stream:
                stream.write(_home(folders) if relative == "Home.md" else text)


def _unbuilt(root):
    if not root.exists():
        return True
    state = root / ".coldvault"
    return (root.is_dir() and all(path == state for path in root.iterdir())
            and (not state.exists() or (state.is_dir() and not state.is_symlink() and all(path.name in ("config.json", ".gitignore") for path in state.iterdir()))))


def transcript_source(run):
    sessions = {}
    try:
        cache_path = _owned_folder(run.root, (".coldvault",)) / "transcripts.json"
    except OSError:
        cache_path = None
    cached, parsed = _transcript_cache(cache_path) if cache_path else {}, {}
    for path in sorted(run.files):
        status = path.stat()
        stamp = f"{status.st_size} {status.st_mtime_ns} {status.st_ctime_ns} {status.st_ino}"
        source = str(path.resolve())
        entry = cached.get(source)
        info = entry[1] if entry and entry[0] == stamp else transcript_info(path)
        parsed[source] = [stamp, info]
        if not info:
            continue
        (project, name), day, text, source, session_id = info
        path_key = _short_hash(str(path.resolve()))
        key = _short_hash(f"{project}\n{path.stem}")
        earlier = sessions.get(key)
        if earlier and not (earlier[1].startswith(text) or text.startswith(earlier[1])):
            key = path_key
            earlier = sessions.get(key)
        legacy = [f"{run.folders['sessions']}/{day}-{slug(name)}-{path_key}.md", *(earlier[2]["legacy"] if earlier else ())]
        if earlier and len(earlier[1]) >= len(text):
            earlier[2]["legacy"] = legacy
        else:
            sessions[key] = (f"{run.folders['sessions']}/{day}-{slug(name)}-{key}.md", text, {
                "kind": "session", "project": (project, name), "date": day,
                "source": source, "session-id": session_id, "legacy": legacy})
    if cache_path and parsed != cached:
        with contextlib.suppress(OSError):
            _atomic_write(cache_path, json.dumps({"tag": _CACHE_TAG, "files": parsed}, ensure_ascii=False))
    return list(sessions.values()) if run.files else None


def github_source(run):
    if not run.username:
        return None
    token = _github_token()
    repos = github_repos(run.username)
    try:
        cache_path = _owned_folder(run.root, (".coldvault",)) / "github.json"
    except OSError:
        cache_path = None
    cached = _github_cache(cache_path) if cache_path else {}
    kept, limited, notes = {}, False, []
    for repo in repos:
        full_name, pushed = repo.get("full_name"), repo.get("pushed_at")
        metadata = {"kind": "repo", "project": repo["name"], "repo": json.dumps(full_name if isinstance(full_name, str) else str(repo["name"]))}
        if _is_timestamp(pushed):
            metadata["pushed-at"] = pushed
        text = f"GitHub: [{repo['html_url']}]({repo['html_url']})\n\n{repo.get('description') or ''}\n\n"
        if isinstance(full_name, str) and _REPO_NAME.fullmatch(full_name):
            entry = cached.get(full_name)
            if not limited:
                entry = dict(entry or {"pushed-at": "", "docs": "", "issues": "", "etag": ""})
                try:
                    _github_repo_content(full_name, repo, token, entry)
                except (OSError, URLError, ValueError) as error:
                    limited = isinstance(error, HTTPError) and (error.code == 429 or (error.headers or {}).get("X-RateLimit-Remaining") == "0")
                    if not limited:
                        print(f"GitHub: kept the saved README and issues of {full_name} ({_github_error(error, token)})", file=sys.stderr)
            if entry:
                kept[full_name] = entry
                text += "".join(f"{section}\n\n" for section in (entry["docs"], entry["issues"]) if section)
        notes.append((f"{run.folders['projects']}/{repo['name']}.md", text, metadata))
    if limited:
        print("GitHub rate limit reached: some READMEs and issue lists wait for the next run (GITHUB_TOKEN raises the limit)", file=sys.stderr)
    if cache_path and kept != cached:
        with contextlib.suppress(OSError):
            _atomic_write(cache_path, json.dumps({"tag": _CACHE_TAG, "repos": kept}, ensure_ascii=False))
    return notes


_REPO_NAME = re.compile(r"(?!\.\.?/)[A-Za-z0-9_.-]+/(?!\.\.?$)[A-Za-z0-9_.-]+")


def _github_cache(path):
    try:
        saved = json.loads(_read(path)) if path.is_file() and not path.is_symlink() else {}
    except (OSError, UnicodeError, ValueError):
        return {}
    repos = saved.get("repos") if isinstance(saved, dict) and saved.get("tag") == _CACHE_TAG else None
    if not isinstance(repos, dict):
        return {}
    return {name: entry for name, entry in repos.items() if isinstance(entry, dict)
            and set(entry) == {"pushed-at", "docs", "issues", "etag"} and all(isinstance(value, str) for value in entry.values())}


def _github_repo_content(full_name, repo, token, entry):
    api = f"{_GITHUB_API}repos/{full_name}"
    pushed = repo["pushed_at"] if _is_timestamp(repo.get("pushed_at")) else ""
    if not pushed or entry["pushed-at"] != pushed:
        sections = []
        try:
            _github_docs(api, token, sections)
        except Exception:
            entry["docs"] = entry["docs"] or "\n\n".join(sections)
            raise
        entry.update({"pushed-at": pushed, "docs": "\n\n".join(sections)})
    if repo.get("open_issues_count") == 0:
        entry.update(issues=_NO_ISSUES, etag="")
        return
    try:
        body, headers = _github_get(f"{api}/issues?state=open&per_page=100", token, etag=entry["etag"])
    except HTTPError as error:
        if error.code == 304:
            return
        if error.code not in (404, 410):
            raise
        entry.update(issues="", etag="")
        return
    items = json.loads(body)
    lines = [f"- {'PR' if 'pull_request' in item else 'Issue'} #{item['number']}: [{_link_text(item['title'])}]({item['html_url']})"
             for item in items if isinstance(item, dict) and type(item.get("number")) is int and isinstance(item.get("title"), str)
             and isinstance(item.get("html_url"), str) and item["html_url"].startswith("https://github.com/")] if isinstance(items, list) else []
    entry.update(issues="## Open issues and pull requests\n\n" + "\n".join(lines) if lines else _NO_ISSUES, etag=headers.get("ETag") or "")


def _link_text(title):
    return re.sub(r"([\[\]\\])", r"\\\1", " ".join(redact(title).split()))


_NO_ISSUES = "## Open issues and pull requests\n\nNone open."


def _github_docs(api, token, sections):
    readme = _github_text(f"{api}/readme", token)
    if readme:
        sections.append(f"## README\n\n{readme}")
    try:
        listing = json.loads(_github_get(f"{api}/contents/docs", token)[0])
    except HTTPError as error:
        if error.code != 404:
            raise
        listing = []
    names = sorted(entry["name"] for entry in listing if isinstance(entry, dict) and entry.get("type") == "file"
                   and isinstance(entry.get("name"), str) and re.fullmatch(r"[^/\\]+\.md", entry["name"], re.IGNORECASE)) if isinstance(listing, list) else []
    for name in names:
        text = _github_text(f"{api}/contents/docs/{quote(name)}", token)
        if text:
            sections.append(f"## docs/{name}\n\n{text}")


def _github_text(url, token):
    try:
        body = _github_get(url, token, accept="application/vnd.github.raw")[0]
    except HTTPError as error:
        if error.code != 404:
            raise
        return ""
    return redact(body.decode("utf-8", errors="replace")).strip()


_CHAT_EXPORTS = {"chatgpt": "ChatGPT", "claude-ai": "Claude.ai"}
_CONVERSATION_FILE = re.compile(r"conversations(?:-\d+)?\.json")


def _export_conversations(export):
    try:
        with zipfile.ZipFile(export) as archive:
            names = sorted(name for name in archive.namelist() if _CONVERSATION_FILE.fullmatch(name.rsplit("/", 1)[-1]))
            if not names:
                raise SourceError(f"{export.name}: no conversations.json in the zip")
            parts = [json.loads(archive.read(name)) for name in names]
    except (OSError, zipfile.BadZipFile, UnicodeError, ValueError) as error:
        raise SourceError(f"{export.name}: {error}") from None
    if not all(isinstance(part, list) and all(isinstance(record, dict) for record in part) for part in parts):
        raise SourceError(f"{export.name}: conversations.json is not a list of conversations")
    return [record for part in parts for record in part]


def _chatgpt_branch(mapping, node_id):
    chain = []
    while node_id is not None:
        if len(chain) > len(mapping):
            raise ValueError("cycle in mapping")
        chain.append(mapping[node_id])
        node_id = mapping[node_id].get("parent")
    chain.reverse()
    return chain


def _chatgpt_conversation(record):
    found, mapping = [], record["mapping"]
    branch = record.get("current_node") is not None
    nodes = _chatgpt_branch(mapping, record["current_node"]) if branch else mapping.values()
    for index, node in enumerate(nodes):
        message = node.get("message")
        if not message or message["author"]["role"] not in ("user", "assistant"):
            continue
        content, flags = message["content"], message.get("metadata") or {}
        if (content["content_type"] not in ("text", "multimodal_text") or message.get("recipient", "all") != "all"
                or message.get("channel") not in (None, "final")
                or flags.get("is_visually_hidden_from_conversation") or flags.get("is_user_system_message")):
            continue
        if not isinstance(content["parts"], list):
            raise TypeError("parts is not a list")
        stamp = message.get("create_time")
        found.append((0 if branch or type(stamp) not in (int, float) else stamp, index, "\n".join(part for part in content["parts"] if isinstance(part, str))))
    stamp = record.get("create_time")
    day = datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%d") if type(stamp) in (int, float) else "undated"
    return record.get("conversation_id") or record.get("id"), record.get("title"), day, [text for *_, text in sorted(found)]


def _claude_conversation(record):
    texts = []
    messages = record["chat_messages"]
    if not isinstance(messages, list):
        raise TypeError("chat_messages is not a list")
    for message in messages:
        if message["sender"] not in ("human", "assistant"):
            continue
        blocks = message.get("content")
        if blocks:
            texts.append("\n".join(block["text"] for block in blocks if block["type"] == "text"))
        elif isinstance(message.get("text"), str):
            texts.append(message["text"])
    created = record.get("created_at")
    day = created[:10] if isinstance(created, str) and _is_day(created[:10]) else "undated"
    return record.get("uuid"), record.get("name"), day, texts


def chat_export_source(run):
    exports = [("chatgpt", export) for export in run.chatgpt_exports] + [("claude-ai", export) for export in run.claude_exports]
    if not exports:
        return None
    sessions = {}
    for source, export in exports:
        export = Path(export).expanduser().resolve()
        for record in _export_conversations(export):
            try:
                conversation_id, title, day, texts = (_chatgpt_conversation if source == "chatgpt" else _claude_conversation)(record)
            except (AttributeError, KeyError, TypeError, ValueError, OverflowError, OSError):
                raise SourceError(f"{export.name}: a conversation is not in the expected format") from None
            if not isinstance(conversation_id, str) or not conversation_id:
                raise SourceError(f"{export.name}: a conversation has no id")
            lines = [redact(text.strip()) for text in texts if text.strip()]
            if not lines:
                continue
            if isinstance(title, str) and title.strip():
                lines.insert(0, "## " + redact(" ".join(title.split())))
            text, name, digest = "\n\n".join(lines), _CHAT_EXPORTS[source], hashlib.sha256(f"{source}\n{conversation_id}".encode()).hexdigest()[:16]
            earlier = sessions.get((source, conversation_id))
            if earlier and len(earlier[1]) >= len(text):
                continue
            sessions[source, conversation_id] = (f"{run.folders['sessions']}/{day}-{slug(name)}-{digest}.md", text, {
                "kind": "session", "project": (source, name), "date": day,
                "source": source, "session-id": conversation_id, "legacy": []})
    return list(sessions.values())


SOURCES = [
    ("transcripts", transcript_source),
    ("notes", notes_source),
    ("notion", notion_source),
    ("chat-exports", chat_export_source),
    ("github", github_source),
]


def build_from_files(files, output, username, notes=(), notion_exports=(), folders=(), chatgpt_exports=(), claude_exports=()):
    root = Path(output).expanduser().resolve()
    folders = {**FOLDERS, **dict(folders)}
    fresh = _unbuilt(root)
    root.mkdir(parents=True, exist_ok=True)
    if fresh:
        _apply_template(root, folders)
    run = SimpleNamespace(root=root, files=files, username=username, notes=notes, notion_exports=notion_exports, folders=folders,
                          chatgpt_exports=chatgpt_exports, claude_exports=claude_exports)
    items, sources = [], {}
    for name, source in SOURCES:
        try:
            found = source(run)
            found = None if found is None else list(found)
        except SourceError as error:
            sources[name] = {"state": "failed", "reason": str(error)}
            continue
        if found is None:
            sources[name] = {"state": "skipped"}
            continue
        sources[name] = {"state": "ok", "count": len(found)}
        items += found
    _rename_folders(root, folders, all(source["state"] != "failed" for source in sources.values()))
    projects, sessions = _write_vault(root, items, folders)
    imported = sum(1 for _, _, metadata in items if metadata.get("kind", "note") == "note")
    print(f"Built vault at {root} ({projects} projects, {sessions} sessions, {imported} imported notes)")
    return sources


def _previous_folders(root):
    state = root / ".coldvault" / "folders.json"
    try:
        saved = json.loads(_read(state)) if not state.is_symlink() else None
    except (OSError, UnicodeError, ValueError):
        saved = None
    if not isinstance(saved, dict):
        return FOLDERS, None
    pending = saved.pop("pending", None)
    complete = _valid_folders(pending) and set(pending) == set(FOLDERS)
    return (saved if _valid_folders(saved) and set(saved) == set(FOLDERS) else FOLDERS), (pending if complete else None)


def _save_folders(root, folders, pending=None):
    state = {**folders, "pending": pending} if pending else folders
    _atomic_write(_owned_folder(root, (".coldvault",)) / "folders.json", json.dumps(state, indent=2) + "\n")


def _crossed_folder(previous, folders):
    return next((key for key, name in folders.items() for other, old in previous.items()
                 if other != key and name.casefold() == old.casefold()), None)


def _relink(path, renames):
    if not _is_managed_note(path):
        return
    lines = _read(path).split("\n")
    if lines[1] != _NOTE_MARKER or not lines[2].startswith(_HASH_FIELD):
        return
    del lines[2]
    before = "\n".join(lines)

    def target(match):
        head, slash, rest = match.group(0).partition("/")
        return match.group(0) if match.group(1) or match.group(2) or not slash or head not in renames else f"{renames[head]}/{rest}"

    after = _CODE_OR_WIKILINK.sub(target, before)
    if after != before:
        lines = after.split("\n")
        lines.insert(2, _HASH_FIELD + _digest(after))
        _atomic_write(path, "\n".join(lines))


def _rename_folders(root, folders, ready=True):
    saved, pending = _previous_folders(root)
    if saved == folders or _crossed_folder(saved, folders) or (pending and pending != folders):
        return
    _save_folders(root, saved, folders)
    if not ready:
        return
    base = root.resolve()
    moves = []
    for key, old in saved.items():
        source = root / old
        if old == folders[key] or source.is_symlink() or not source.is_dir():
            continue
        moves += [(source, path, root / folders[key] / path.relative_to(source)) for path in sorted(source.rglob("*.md"))]
    emptied, stranded = set(), False
    for source, path, target in moves:
        if not _is_managed_note(path) or not _inside(base, path.resolve()):
            continue
        if not _inside(base, target.parent.resolve()):
            stranded = True
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.samefile(path):
            continue
        if not (target.exists() or target.is_symlink()):
            os.replace(path, target)
        elif _is_managed_note(target):
            path.unlink()
        else:
            stranded = True
            continue
        emptied.update(folder for folder in path.parents if folder == source or source in folder.parents)
    for folder in sorted(emptied, key=lambda folder: len(folder.parts), reverse=True):
        with contextlib.suppress(OSError):
            folder.rmdir()
    renames = {saved[key]: folders[key] for key in folders if saved[key] != folders[key]}
    for name in folders.values():
        folder = root / name
        if not folder.is_symlink() and folder.is_dir():
            for path in sorted(folder.rglob("*.md")):
                if _inside(base, path.resolve()):
                    _relink(path, renames)
    home = root / "Home.md"
    if not home.is_symlink() and _current_text(home) == _home(saved):
        _atomic_write(home, _home(folders))
    _save_folders(root, saved if stranded else folders, folders if stranded else None)


_CACHE_TAG = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _transcript_cache(path):
    if path.is_symlink() or not path.is_file():
        return {}
    try:
        saved = json.loads(_read(path))
        files = saved["files"]
        if saved["tag"] != _CACHE_TAG or not isinstance(files, dict):
            return {}
        for stamp, info in files.values():
            if not isinstance(stamp, str):
                return {}
            if info is not None:
                if not isinstance(info, list):
                    return {}
                (key, name), day, text, source, session = info
                if not all(isinstance(value, str) for value in (key, name, day, text, source, session)):
                    return {}
        return files
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        return {}


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


_KEPT_SESSION = re.compile(r"# [^\n]*\n\nProject: [^\n]*\n\n(.*)", re.DOTALL)


def _frontmatter_values(text):
    values = {}
    for line in text[4:text.index("\n---\n", 3)].split("\n"):
        name, _, value = line.partition(": ")
        try:
            values[name] = json.loads(value) if value.startswith('"') else value
        except ValueError:
            return {}
    return values


def _kept_sessions(root, folder, sessions):
    base = root / folder
    if base.is_symlink() or not base.is_dir():
        return []
    taken = {path for *_, meta, note, _ in sessions for path in (note, *meta["legacy"])}
    keys = {name: key for key, name, *_ in sessions}
    kept = []
    for path in sorted(base.glob("*.md")):
        note = path.relative_to(root).as_posix()
        if note in taken or path.name.endswith(" (continued).md") or _OLD_COPY.search(path.name) or path.is_symlink() or not path.is_file():
            continue
        try:
            text = _read(path)
        except (OSError, UnicodeError):
            continue
        if not _has_frontmatter(text):
            continue
        values = _frontmatter_values(text)
        body = _KEPT_SESSION.fullmatch(text[text.index("\n---\n", 3) + 6:].lstrip("\n"))
        fields = [values.get(name) for name in ("project", "date", "source", "session-id")]
        if not body or not all(isinstance(value, str) for value in fields):
            continue
        name, date, source, session_id = fields
        recorded = values.get("project-key")
        key = recorded if isinstance(recorded, str) else keys.get(name, name)
        kept.append((key, name, {"kind": "session", "project": (key, name), "date": date, "source": source,
                                 "session-id": session_id, "legacy": [], "kept": True,
                                 "keyed": isinstance(recorded, str) or name in keys}, note, body.group(1).rstrip()))
    return kept


def _write_vault(root, items, folders):
    sessions = [(*meta["project"], meta, note, text) for note, text, meta in items if meta.get("kind") == "session"]
    sessions += _kept_sessions(root, folders["sessions"], sessions)
    repos = {}
    for _, text, meta in items:
        if meta.get("kind") == "repo":
            repos.setdefault(meta["project"], (text, meta))
    imported = [(note, text) for note, text, meta in items if meta.get("kind", "note") == "note"]
    notion = {note for note, _, meta in items if meta.get("kind", "note") == "note" and meta.get("source") == "notion"}
    projects = {project: name for project, name, *_ in sessions}
    for name in repos:
        if name not in projects.values():
            projects[name] = name
    keys = sorted(projects, key=lambda key: (projects[key].casefold(), projects[key], key))
    project_paths = {key: _planned(root, f"{folders['projects']}/{unique}.md") for key, unique in project_slugs((key, projects[key]) for key in keys).items()}
    now = _utc_now()
    session_notes = {}
    for project, _, meta, note, text in sessions:
        target, title, header = note, f"{projects[project]} — {meta['date']}", f"Project: {link(project_paths[project])}"
        if meta.get("kept") and _frozen(root, note):
            continue
        if _frozen(root, note):
            text = _new_messages(root, note, text)
            if not text:
                continue
            target, title, header = f"{note.removesuffix('.md')} (continued).md", f"{title} (continued)", f"{header}\n\nContinues {link(note)}"
        session_notes[target] = (note, _render(_frontmatter({
            "source": meta["source"],
            "date": meta["date"] if _is_day(meta["date"]) else json.dumps(meta["date"]),
            "project": json.dumps(projects[project]),
            **({"project-key": json.dumps(project)} if meta.get("keyed", True) else {}),
            "session-id": json.dumps(meta["session-id"]),
            "imported-at": _imported_at(root / _planned(root, target), now),
            "paragraphs": len(text.split("\n\n")),
        }) + f"# {title}\n\n{header}\n\n{text}"))
    current = {target for target, (_, rendered) in session_notes.items() if _unchanged(root, target, rendered)}
    session_files = {target: target if target in current else _planned(root, target) for target in session_notes}
    session_paths = {note: note for *_, note, _ in sessions}
    continuations = {note: f"{note.removesuffix('.md')} (continued).md" for *_, meta, note, _ in sessions
                     if meta.get("kept") and _is_managed_note(root / f"{note.removesuffix('.md')} (continued).md")}
    for target, (note, _) in session_notes.items():
        if target == note:
            session_paths[note] = session_files[target]
        else:
            continuations[note] = session_files[target]
    imported_paths, reserved = {}, set()
    for note, _ in imported:
        planned, number = _planned(root, note), 2
        while planned.casefold() in reserved:
            planned, number = _planned(root, f"{note.removesuffix('.md')} ({number}).md"), number + 1
        reserved.add(planned.casefold())
        imported_paths[note] = planned
    index_path = _planned(root, "Index.md")
    start_path = _planned(root, "START-HERE.md")
    for target, (_, rendered) in session_notes.items():
        if target not in current:
            _write_rendered(root, session_files[target], rendered)
    for key in keys:
        name = projects[key]
        body = f"# {name}\n\n"
        if name in repos:
            text, meta = repos[name]
            body = _frontmatter({field: meta[field] for field in ("repo", "pushed-at") if field in meta}) + body + text
        links = [f"- {link(session_paths[note])} ({meta['date']})" + (f" · {link(continuations[note])}" if note in continuations else "")
                 for project, _, meta, note, _ in sessions if project == key]
        body += "## Sessions\n\n" + ("\n".join(links) if links else "No local sessions found.")
        write_note(root, project_paths[key], body)
    moved = {note.removesuffix(".md"): path.removesuffix(".md") for note, path in imported_paths.items() if note != path and note in notion}
    days, written = {}, set()
    for project, _, meta, note, _ in sessions:
        if _is_day(meta["date"]) and not meta.get("kept"):
            days.setdefault(meta["date"], ([], []))[0].append(f"- {link(session_paths[note])} · {link(project_paths[project])}")
    for note, text in imported:
        if moved and note in notion:
            text = _CODE_OR_WIKILINK.sub(lambda match: match.group(0) if match.group(1) or match.group(2) else moved.get(match.group(0), match.group(0)), text)
        stamp = _field(text, "imported-at")
        if stamp is None:
            original = root / note
            stamp = _imported_at(root / imported_paths[note], None)
            edited = "" if original.is_symlink() or not original.is_file() else _current_text(original) or ""
            if stamp is None and set(edited.split("\n")[:3]) & set(_MARKERS):
                stamp = _field(edited, "imported-at")
            stamp = stamp if _is_timestamp(stamp) else now
            text = f"---\nimported-at: {stamp}\n{text[4:]}" if _has_frontmatter(text) else _frontmatter({"imported-at": stamp}) + text
        if _is_timestamp(stamp):
            days.setdefault(stamp[:10], ([], []))[1].append(f"- {link(imported_paths[note])}")
        write_note(root, imported_paths[note], text)
    for day, (day_sessions, day_imported) in sorted(days.items()):
        body = _frontmatter({"record_type": "daily_index", "date": day}) + f"# {day}\n"
        if day_sessions:
            body += "\n## Sessions\n\n" + "\n".join(day_sessions) + "\n"
        if day_imported:
            body += "\n## Imported notes\n\n" + "\n".join(day_imported) + "\n"
        written.add(write_note(root, _planned(root, f"Daily/{day}.md"), body))
    daily = root / "Daily"
    for path in [] if daily.is_symlink() or not daily.is_dir() else sorted(daily.glob("*.md")):
        if path.relative_to(root).as_posix() not in written and _DAILY_NOTE.fullmatch(path.name) and _is_managed_note(path) and _field(_read(path), "record_type") == "daily_index":
            path.unlink()
    index = "# Vault index\n\n## Projects\n\n" + "\n".join(f"- {link(project_paths[key])}" for key in keys)
    if imported:
        index += "\n\n## Imported notes\n\n" + "\n".join(f"- {link(imported_paths[note])}" for note, _ in imported)
    write_note(root, index_path, index)
    write_note(root, start_path, (
        f"# Start here\n\nOpen {link(index_path)} to browse projects and sessions.\n\n"
        "Each project note lists its conversation sessions; each session note holds the conversation text "
        "(tool outputs are not imported).\n\n"
        "Search the vault from a terminal:\n\n"
        "    python3 coldvault.py search TERM --vault DIR\n"
    ))
    _retire_path_keyed_sessions(root, [session for session in sessions if not session[2].get("kept")], set(session_files.values()))
    return len(keys), len(sessions)


_OLD_COPY = re.compile(r" \((?:Coldvault|Vault Kit)(?: \d+)?\)\.md")
_DAILY_NOTE = re.compile(r"\d{4}-\d{2}-\d{2}(?: \((?:Coldvault|Vault Kit)(?: \d+)?\))?\.md")


def _frozen(root, note):
    path = root / note
    return (path.exists() or path.is_symlink()) and not _is_managed_note(path)


def _new_messages(root, note, text):
    path = root / note
    readable = not path.is_symlink() and path.is_file() and _inside(root.resolve(), path.resolve())
    kept = ((_current_text(path) or "") if readable else "").replace("\r\n", "\n")
    paragraphs, covered = text.split("\n\n"), _field(kept, "paragraphs") or ""
    if re.fullmatch(r"[1-9][0-9]{0,8}", covered) and int(covered) <= len(paragraphs):
        return "\n\n".join(paragraphs[int(covered):])
    kept, position = [part.strip() for part in kept.split("\n\n")], 0
    for start, paragraph in enumerate(paragraphs):
        try:
            position = kept.index(paragraph.strip(), position) + 1
        except ValueError:
            return "\n\n".join(paragraphs[start:])
    return ""


def _retire_path_keyed_sessions(root, sessions, written):
    for *_, meta, note, _ in sessions:
        stem = Path(note).stem
        copies = [path.relative_to(root).as_posix() for path in (root / note).parent.glob(f"{glob.escape(stem)} (*).md")
                  if _OLD_COPY.fullmatch(path.name[len(stem):])]
        continued = [] if _frozen(root, note) else [f"{note.removesuffix('.md')} (continued).md"]
        for old in [*meta["legacy"], *continued, *copies]:
            if old not in written and _is_managed_note(root / old):
                (root / old).unlink()


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
    for name in (".gitignore", "transcripts.json", "github.json", _RECEIPT, _HISTORY, "search.sqlite", "search.sqlite-journal", "search.sqlite-wal", "search.sqlite-shm"):
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


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


if __name__ == "__main__":
    main()
