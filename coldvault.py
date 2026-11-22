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
import plistlib
import posixpath
import re
import shlex
import shutil
import socket
import stat
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
from xml.parsers.expat import ExpatError

try:
    import sqlite3
except ImportError:
    sqlite3 = None

__version__ = "0.4.0"


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


def _complete_jsonl(data):
    try:
        for line in data.decode("utf-8").splitlines():
            if line.strip():
                json.loads(line, parse_constant=_reject_constant)
    except ValueError:
        return False
    return True


def _reject_constant(name):
    raise ValueError(f"{name} is not JSON")


def transcript_info(path, data=None):
    lines, dates, project, named = [], [], (path.parent.name, path.parent.name), False
    source, session_id = None, None
    for line in (path.read_bytes() if data is None else data).decode("utf-8", errors="replace").splitlines():
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
    return project, day, "\n\n".join(lines), source, session_id


class SourceError(Exception):
    def __init__(self, reason, *, notes=()):
        super().__init__(reason)
        self.notes = notes


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


class GitHubListingError(SourceError):
    def __init__(self, error, token, repos):
        super().__init__(f"incomplete repository listing ({_github_error(error, token)})")
        self.repos = repos


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
    except (OSError, URLError, ValueError, SourceError) as error:
        raise GitHubListingError(error, token, repos) from None


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


_NOTE_SOURCE_PATH = "coldvault-source-path"
_NOTE_SOURCE_PATH_KEYS = (_NOTE_SOURCE_PATH, f'"{_NOTE_SOURCE_PATH}"', f"'{_NOTE_SOURCE_PATH}'")
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
    if not hasattr(os, "O_NOFOLLOW") or os.rename not in os.supports_dir_fd:
        target = note_target(root, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, rendered)
        return target.relative_to(root).as_posix()

    base = root.resolve()
    parent = (root / relative).parent
    resolved = parent.resolve()
    if not _inside(base, resolved):
        raise ValueError("A note path escaped the chosen vault folder")
    with _note_directory(base, resolved) as directory:
        target = note_target(root, relative)
        _check_note_directory(parent, resolved, directory)
        temporary = f".coldvault-{uuid.uuid4().hex}"
        handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(rendered)
            _check_note_directory(parent, resolved, directory)
            os.replace(temporary, target.name, src_dir_fd=directory, dst_dir_fd=directory)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=directory)
            raise
    return target.relative_to(root).as_posix()


@contextlib.contextmanager
def _note_directory(base, parent):
    flags = getattr(os, "O_SEARCH", getattr(os, "O_PATH", os.O_RDONLY)) | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        directory = os.open(base, flags)
    except FileNotFoundError:
        base.mkdir(parents=True, exist_ok=True)
        directory = os.open(base, flags)
    try:
        for part in parent.relative_to(base).parts:
            try:
                child = os.open(part, flags, dir_fd=directory)
            except FileNotFoundError:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, dir_fd=directory)
                child = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        yield directory
    finally:
        os.close(directory)


def _check_note_directory(parent, resolved, directory):
    if parent.resolve() != resolved or not os.path.samestat(
        resolved.lstat(), os.fstat(directory)
    ):
        raise OSError("A note's parent directory changed during the write")


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


def _sync_path(path, flags):
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def link(relative):
    return f"[[{relative.removesuffix('.md')}]]"


def build(transcript_dir, output, username=""):
    files = sorted(Path(transcript_dir).expanduser().rglob("*.jsonl"))
    build_from_files(files, output, username)


def _json_output(value):
    print(json.dumps(value))


def _json_requested(argv):
    options = argv[:argv.index("--")] if "--" in argv else argv
    return "--json" in options


class _CommandParser(argparse.ArgumentParser):
    def __init__(self, argv, *, version_key="version", **kwargs):
        super().__init__(**kwargs)
        self.version_key = version_key
        self.json_output = _json_requested(argv)
        self.add_argument("--json", action="store_true", help="Write machine-readable JSON to stdout")

    def error(self, message):
        if self.json_output:
            self.fail(message)
        super().error(message)

    def fail(self, message):
        if self.json_output:
            _json_output({self.version_key: 1, "error": redact(message)})
            self.exit(2)
        self.exit(2, f"{self.prog}: error: {message}\n")


def main():
    command = sys.argv[1:2]
    commands = {"search": search_main, "status": status_main, "schedule": schedule_main,
                "setup": setup_main, "sync": sync_main, "doctor": doctor_main}
    handler = commands.get(command[0], sync_main) if command else sync_main
    argv = sys.argv[2:] if command and command[0] in commands else sys.argv[1:]
    try:
        result = handler(argv)
    except (OSError, ValueError, OverflowError, RuntimeError) as error:
        version_key = "schema_version" if handler in (search_main, doctor_main) else "version"
        _CommandParser(argv, version_key=version_key).fail(str(error))
    if result or handler in (search_main, status_main, schedule_main, doctor_main):
        sys.exit(result)


def search_main(argv):
    parser = _CommandParser(argv, version_key="schema_version", prog=f"{Path(sys.argv[0]).name} search",
                            description="Search the notes in a vault.")
    parser.add_argument("term")
    parser.add_argument("--vault", required=True, help="Vault folder to search")
    args = parser.parse_args(argv)
    root = Path(args.vault).expanduser()
    if not root.is_dir():
        parser.error("vault folder not found")
    if not args.json:
        return 0 if search(root, args.term) else 1
    found = list(_search_hits(root, args.term))
    _json_output({"schema_version": 1, "hits": found})
    return 0 if found else 1


_STANDARD_TRANSCRIPTS = ((".claude", "projects"), (".codex", "sessions"), (".codex", "archived_sessions"))


def _standard_transcript_folders():
    return [Path.home().joinpath(*parts) for parts in _STANDARD_TRANSCRIPTS]


def _config_path(root):
    return root / ".coldvault" / "config.json"


def _check_sources(root, notes=(), notion_exports=(), transcripts=(), chat_exports=()):
    for folder in notes:
        path = Path(folder).expanduser().resolve()
        if not path.is_dir():
            raise SourceError(f"notes folder not found: {folder}")
        if path == root or root in path.parents:
            raise SourceError(f"choose a notes folder outside the vault: {folder}")
    for export in notion_exports:
        path = Path(export).expanduser().resolve()
        if path.is_dir():
            if path == root or root in path.parents:
                raise SourceError(f"choose a Notion export outside the vault: {export}")
        elif not zipfile.is_zipfile(path):
            raise SourceError(f"Notion export must be a .zip file or an unzipped folder: {export}")
    for export in chat_exports:
        if not zipfile.is_zipfile(Path(export).expanduser()):
            raise SourceError(f"ChatGPT and Claude.ai exports must be the downloaded .zip file: {export}")
    for folder in transcripts:
        if not Path(folder).expanduser().is_dir():
            raise SourceError(f"transcript folder not found: {folder}")


FOLDERS = {"projects": "Projects", "sessions": "Sessions", "imported": "Imported"}


def _folder_name(value):
    return (isinstance(value, str) and value == value.strip() and value not in ("", ".", "..")
            and not value.startswith(".") and not any(character in value for character in "/\\:\0[]|#^`"))


def _valid_folders(folders):
    if not isinstance(folders, dict) or not set(folders) <= set(FOLDERS) or not all(map(_folder_name, folders.values())):
        return False
    names = {**FOLDERS, **folders}.values()
    return len({name.casefold() for name in names}) == len(names)


def _valid_receipts(settings):
    return (isinstance(settings, dict) and set(settings) in ({"folder", "host"}, {"folder", "host", "archive_folder"})
            and all(isinstance(value, str) and value == value.strip() and value and "\0" not in value and "\n" not in value
                    for value in settings.values())
            and os.path.isabs(settings["folder"])
            and ("archive_folder" not in settings or os.path.isabs(settings["archive_folder"])))


def _load_config(parser, root, *, allow_invalid=False):
    path = _config_path(root)
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink():
        parser.error(f"{path} is a symlink; move it aside, then run setup")
    try:
        config = json.loads(_read(path))
    except ValueError as error:
        if allow_invalid:
            return None
        parser.error(f"cannot read {path}: {error}")
    except OSError as error:
        parser.error(f"cannot read {path}: {error}")
    lists = {"transcripts": None, "notes": None, "notion_exports": None, "chatgpt_exports": [], "claude_exports": []}
    if (not isinstance(config, dict) or type(config.get("version")) is not int or config["version"] != 1
            or not isinstance(config.get("username"), str)
            or not all(isinstance(paths := config.get(key, default), list)
                       and all(isinstance(item, str) and item and "\0" not in item for item in paths)
                       for key, default in lists.items())
            or ("schedule_hours" in config and (type(config["schedule_hours"]) is not int or config["schedule_hours"] < 1))
            or ("folders" in config and not _valid_folders(config["folders"]))
            or ("preservation_receipts" in config and not _valid_receipts(config["preservation_receipts"]))):
        if allow_invalid:
            return None
        parser.error(f"{path} is not a Coldvault config; move it aside, then run setup")
    return config


def sync_main(argv):
    parser = _CommandParser(argv, description="Build an Obsidian vault from local Claude Code and Codex transcripts, Markdown folders, Notion exports and ChatGPT or Claude.ai exports. Run 'setup' once to save your choices.")
    if parser.json_output:
        parser.add_argument("--version", action="store_true")
    else:
        parser.add_argument("--version", action="version", version=f"coldvault {__version__}")
    parser.add_argument("--username", help="GitHub username for a public repository lookup")
    parser.add_argument("--transcripts", help="Transcript folder (default: standard Claude and Codex folders)")
    parser.add_argument("--output", "--vault", dest="output", help="Vault folder (asked when omitted)")
    parser.add_argument("--notes", action="append", default=[], metavar="DIR", help="Markdown folder to import under Imported/ (repeatable)")
    parser.add_argument("--notion-export", action="append", default=[], metavar="ZIP", help="Notion 'Markdown & CSV' export (.zip or unzipped folder) to import under Imported/Notion/ (repeatable)")
    parser.add_argument("--chatgpt-export", action="append", default=[], metavar="ZIP", help="ChatGPT data export (.zip) to import as session notes (repeatable)")
    parser.add_argument("--claude-export", action="append", default=[], metavar="ZIP", help="Claude.ai data export (.zip) to import as session notes (repeatable)")
    args = parser.parse_args(argv)
    if args.json and args.version:
        _json_output({"version": 1, "coldvault_version": __version__})
        return 0
    if args.json and not args.output:
        parser.error("pass --vault when using --json")
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
        username = "" if imports or args.json else ask("GitHub username (optional): ")
    output = args.output or ask("Folder for your Obsidian vault: ")
    if not output:
        parser.error("choose an output folder")
    root = Path(output).expanduser().resolve()
    try:
        attempt = _start_attempt(root)
    except OSError as error:
        parser.error(f"cannot record the start of this run: {error}")
    with attempt:
        return _sync(parser, args, config, root, username, notes, notion_exports, attempt.fileno())


def _sync(parser, args, config, root, username, notes, notion_exports, attempt):
    started = _utc_now()

    def finish_attempt():
        os.ftruncate(attempt, 0)
        _attempt_path(root).unlink(missing_ok=True)

    def record(sources):
        receipt = _record_run(root, started, sources)
        finish_attempt()
        return receipt

    def refuse(message):
        with contextlib.suppress(OSError):
            if root.is_dir():
                record({"sources": {"state": "failed", "reason": message}})
            else:
                finish_attempt()
        _CommandParser.error(parser, message)

    parser.error = refuse
    folders = {**FOLDERS, **(config.get("folders", {}) if config else {})}
    previous, pending = _previous_folders(root)
    if pending and pending != folders:
        parser.error(f"an earlier folder rename to {', '.join(pending.values())} has not finished; set those folders again and sync (move any of your files in the way first)")
    crossed = _crossed_folder(previous, folders)
    if crossed:
        parser.error(f"'{folders[crossed]}' is still another generated folder; rename it to a new name first, sync, then rename {crossed} to '{folders[crossed]}'")
    try:
        _check_sources(root, args.notes, args.notion_export, [args.transcripts] if args.transcripts else [],
                       args.chatgpt_export + args.claude_export)
    except SourceError as error:
        parser.error(str(error))
    transcript_folders = ()
    if args.transcripts:
        files = sorted(Path(args.transcripts).expanduser().rglob("*.jsonl"))
        if not files:
            parser.error(f"no transcripts (*.jsonl) found in {args.transcripts}")
    elif config:
        files, transcript_folders = [], config["transcripts"]
    elif notes or notion_exports or args.chatgpt_export or args.claude_export:
        files = []
    else:
        files = [p for folder in _standard_transcript_folders() if folder.is_dir() for p in folder.rglob("*.jsonl")]
        if not files and not username:
            parser.error("no transcripts found in ~/.claude/projects, ~/.codex/sessions or ~/.codex/archived_sessions; pass --transcripts DIR, --notes DIR, --notion-export ZIP, --chatgpt-export ZIP or --claude-export ZIP")
    chatgpt_exports = args.chatgpt_export or (config or {}).get("chatgpt_exports", [])
    claude_exports = args.claude_export or (config or {}).get("claude_exports", [])
    try:
        sources = build_from_files(files, root, username, notes, notion_exports, folders,
                                   chatgpt_exports, claude_exports, transcript_folders=transcript_folders,
                                   receipts=(config or {}).get("preservation_receipts"),
                                   show_summary=not args.json)
    except (OSError, ValueError, RuntimeError) as error:
        detail = str(error)
        if isinstance(error, OSError) and error.filename:
            detail = f"{error.filename}: {error.strerror}"
        with contextlib.suppress(OSError):
            record({"vault": {"state": "failed", "reason": detail}})
        parser.fail(detail)
    failed = [f"{name}: failed ({source['reason']})" for name, source in sources.items() if source["state"] == "failed"]
    for line in failed:
        print(line, file=sys.stderr)
    try:
        receipt = record(sources)
    except OSError as error:
        parser.fail(f"cannot record this run: {error}")
    if args.json:
        _json_output(receipt)
    return 1 if failed else 0


_RECEIPT = "last-run.json"
_HISTORY = "runs.jsonl"
_HISTORY_BYTES = 1024 * 1024
_RECEIPT_BYTES = 65536
_STATES = ("ok", "failed", "skipped")


def _attempt_path(root):
    key = hashlib.sha256(os.fsencode(root)).hexdigest()
    return Path.home() / ".cache" / "coldvault" / "sync" / key


def _start_attempt(root):
    import fcntl

    path = _attempt_path(root)
    _owned_folder(path.parents[2], ("coldvault", "sync"))
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK
    while True:
        if path.is_symlink():
            raise OSError(f"{path} is a symlink")
        attempt = os.fdopen(os.open(path, flags, 0o600), "r+b")
        ready = False
        try:
            descriptor = attempt.fileno()
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                current = path.lstat()
            except FileNotFoundError:
                continue
            info = os.fstat(descriptor)
            if not os.path.samestat(info, current):
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise OSError(f"{path} is not a plain file")
            os.ftruncate(descriptor, 1)
            os.fsync(descriptor)
            _sync_path(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            ready = True
            return attempt
        finally:
            if not ready:
                attempt.close()


def _attempt_problem(root):
    path = _attempt_path(root)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        return f"cannot read the sync attempt: {error}"
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        return f"cannot read the sync attempt: {path} is not a plain file"
    if info.st_size:
        return "sync attempt did not finish; run coldvault sync again if it is no longer running"


def _record_run(root, started, sources):
    folder = _owned_folder(root, (".coldvault",))
    sources = {name: {**source, "reason": redact(source["reason"])[:1024]} if source["state"] == "failed" else source
               for name, source in sources.items()}
    ok = all(source["state"] != "failed" for source in sources.values())
    finished = _utc_now()
    previous = _receipt(folder / _RECEIPT)
    last_success = finished if ok else previous["last_success"] if isinstance(previous, dict) else None
    receipt = {"version": 1, "started": started, "finished": finished, "ok": ok, "last_success": last_success, "sources": sources}
    line = json.dumps(receipt) + "\n"
    try:
        _write_history(folder / _HISTORY, line)
    except OSError as error:
        sources = {**sources, "history": {"state": "failed", "reason": f"cannot append to {_HISTORY}: {error.strerror or error}"}}
        receipt.update(ok=False, last_success=previous["last_success"] if isinstance(previous, dict) else None, sources=sources)
        _write_receipt(folder / _RECEIPT, json.dumps(receipt, ensure_ascii=False) + "\n")
        raise
    _write_receipt(folder / _RECEIPT, line)
    return receipt


def _write_history(path, line):
    budget = _HISTORY_BYTES - len(line)
    with open(path, "a+b", opener=lambda name, flags: os.open(name, flags | getattr(os, "O_NOFOLLOW", 0), 0o600)) as stream:
        size = stream.seek(0, os.SEEK_END)
        offset = max(0, size - budget)
        stream.seek(max(0, offset - 1))
        tail = stream.read(budget + bool(offset))
    if offset:
        tail = tail.partition(b"\n")[2]
    if tail and not tail.endswith(b"\n"):
        tail = tail[:tail.rfind(b"\n") + 1]
    _write_receipt(path, tail.decode("utf-8", errors="ignore") + line)


def _write_receipt(path, line):
    _atomic_write(path, line)
    _sync_path(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    _sync_path(path.parent, os.O_RDONLY | os.O_DIRECTORY)


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
            stream.seek(max(0, stream.seek(0, os.SEEK_END) - _RECEIPT_BYTES))
            return json.loads(stream.read().removesuffix(b"\n").rsplit(b"\n", 1)[-1].decode("utf-8"))
    except (OSError, UnicodeError, ValueError, IndexError):
        return None


def _age(since, now):
    hours = (now - since).total_seconds() / 3600
    return f"{hours:.1f}h ago"


def status_main(argv, now=None):
    parser = _CommandParser(argv, prog=f"{Path(sys.argv[0]).name} status", description="Show the last run of each source and whether the vault is up to date. Exits 0 only when the last run succeeded and is not stale.")
    parser.add_argument("--vault", required=True, help="Vault folder")
    args = parser.parse_args(argv)
    root = Path(args.vault).expanduser().resolve()
    config = _load_config(parser, root)
    hours = config.get("schedule_hours") if config else None
    receipt = _receipt(root / ".coldvault" / _RECEIPT)
    history = root / ".coldvault" / _HISTORY
    attempt_problem = _attempt_problem(root)
    messages = [attempt_problem] if attempt_problem else []
    if history.is_symlink():
        receipt = f"cannot record runs: {history} is a symlink; move it aside"
    elif isinstance(receipt, dict) and receipt["ok"] and _last_line(history) != receipt:
        messages.append(f"cannot read the last run: {_RECEIPT} does not match the last line of {history}")
    now = now or datetime.now(timezone.utc)
    _, schedule = _schedule_status(root, hours, now)
    if receipt is None:
        messages.append(f"no run recorded in {root}; run: coldvault sync --vault {root}")
        messages.append(schedule)
        code = 1
    elif isinstance(receipt, str):
        messages.append(receipt)
        code = 2
    else:
        healthy = receipt["ok"] and not messages
        label = "last completed run" if attempt_problem else "last run"
        if not messages or attempt_problem:
            messages.append(f"{label}: {receipt['finished']} ({_age(_when(receipt['finished']), now)}), {'ok' if receipt['ok'] else 'failed'}")
            for name, source in receipt["sources"].items():
                detail = source.get("reason") if source["state"] == "failed" else source.get("count")
                messages.append(f"{name}: {source['state']}" + (f" ({detail})" if detail is not None else ""))
            success = receipt["last_success"] and _when(receipt["last_success"])
            messages.append(f"last success: {receipt['last_success']} ({_age(success, now)})" if success else "last success: never")
            messages.append(schedule)
            if hours and (not success or now - success > timedelta(hours=2 * hours)):
                messages.append(f"stale: no successful run in the last {2 * hours}h")
                healthy = False
        code = 0 if healthy else 1
    if args.json:
        _json_output({"version": 1, "ok": code == 0, "receipt": receipt if isinstance(receipt, dict) else None,
                      "schedule": schedule.removeprefix("schedule: "), "messages": messages})
    else:
        print("\n".join(messages))
    return code


def _source_duplicates(root):
    seen = set()
    duplicates = Counter()
    for _, text in _notes(root, strict=True):
        if not _has_frontmatter(text):
            continue
        for source, keys in (("notes", _NOTE_SOURCE_PATH_KEYS), ("notion", ("notion-id",)), ("github", ("repo",))):
            identity = next((value for key in keys if (value := _field(text, key))), None)
            if identity is not None:
                break
        else:
            fields = _frontmatter_values(text)
            session = _session_identity(fields)
            if not session or not all(session):
                continue
            source = "chat-exports" if session[0] in _CHAT_EXPORTS else "transcripts"
            identity = (*session, fields.get("session-id-fallback"), fields.get("paragraphs-before", "0"))
        key = (source, identity)
        if source in ("transcripts", "chat-exports", "github"):
            _, _, body = text[4:].partition("\n---\n")
            key += (_digest(body),)
        if key in seen:
            duplicates[source] += 1
        seen.add(key)
    return duplicates


def doctor_main(argv):
    parser = _CommandParser(argv, version_key="schema_version", prog=f"{Path(sys.argv[0]).name} doctor",
                            description="Report source freshness, duplicate imported notes and schedule health.")
    parser.add_argument("--vault", required=True, help="Vault folder")
    args = parser.parse_args(argv)
    root = Path(args.vault).expanduser().resolve()
    if not root.is_dir():
        parser.error("vault folder not found")
    if (root / ".coldvault").is_symlink():
        parser.fail(".coldvault is a symlink")
    config = _load_config(parser, root) or {}
    receipt_path = root / ".coldvault" / _RECEIPT
    if receipt_path.exists() and not receipt_path.is_file():
        parser.fail(f"{_RECEIPT} is not a plain file")
    receipt = _receipt(receipt_path)
    if isinstance(receipt, str):
        parser.fail(receipt)
    problem = _attempt_problem(root)
    if problem:
        parser.fail(problem)
    history = root / ".coldvault" / _HISTORY
    if receipt and (history.is_symlink() or not history.is_file() or _last_line(history) != receipt):
        parser.fail("the last run does not match the run history")
    duplicates = _source_duplicates(root)
    sources = receipt["sources"] if receipt else {}
    now = datetime.now(timezone.utc)
    finished = _when(receipt["finished"]) if receipt else None
    stale_after = timedelta(hours=2 * config.get("schedule_hours", 24))
    current = finished is not None and timedelta(0) <= now - finished <= stale_after
    rows = []
    for name, _ in SOURCES:
        state = sources.get(name, {}).get("state", "skipped")
        if state == "skipped" and not duplicates[name]:
            continue
        rows.append({"source": name, "freshness": "fresh" if current and state == "ok" else "stale",
                     "duplicates": duplicates[name]})
    hours = config.get("schedule_hours")
    schedule = _schedule_status(root, hours, now)[0] if hours else "not_configured"
    if args.json:
        _json_output({"schema_version": 1, "sources": rows, "schedule": {"expected_hours": hours, "state": schedule}})
    else:
        for row in rows:
            print(f"{row['source']}: {row['freshness']}, {row['duplicates']} duplicate(s)")
        print(f"schedule: {schedule}" + (f" (expected every {hours}h)" if hours else ""))
    healthy = rows and all(row["freshness"] == "fresh" and not row["duplicates"] for row in rows)
    return 0 if healthy and (not hours or schedule == "installed") else 1


_CRON_TAG = "# coldvault-schedule "
_SCHEDULE_HOURS = (1, 2, 3, 4, 6, 8, 12, 24)


def _schedule_id(root):
    return hashlib.sha1(str(root).encode(), usedforsecurity=False).hexdigest()[:12]


def _cron_zone():
    try:
        with open("/etc/localtime", "rb") as stream:
            return zoneinfo.ZoneInfo.from_file(stream)
    except (OSError, ValueError):
        return None


def _next_run(now, hours, zone):
    if zone is None:
        return "unknown"
    moment = now.astimezone(timezone.utc).replace(second=0, microsecond=0)
    offset_before = (moment - timedelta(hours=3)).astimezone(zone).utcoffset()
    for _ in range(26 * 60):
        moment += timedelta(minutes=1)
        local = moment.astimezone(zone)
        if hours == 24 and local.utcoffset() != offset_before:
            return "unknown"
        if local.minute == 0 and local.hour % hours == 0:
            return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    return "unknown"


def _cron_hour(hours):
    return "*" if hours == 1 else "0" if hours == 24 else f"*/{hours}"


def _sync_command(root):
    return [sys.executable, str(Path(__file__).resolve()), "sync", "--vault", str(root)]


def _targets_vault(arguments, root):
    if (not isinstance(arguments, list) or len(arguments) != 5
            or not all(isinstance(arg, str) and "\0" not in arg for arg in arguments)
            or arguments[2:4] != ["sync", "--vault"]):
        return False
    python, script, _, _, vault = arguments
    if not all(os.path.isabs(path) for path in (python, script, vault)):
        return False
    if python != sys.executable and not re.fullmatch(r"(?:python|pypy)(?:[0-9]+(?:\.[0-9]+)*t?)?", Path(python).name):
        return False
    if Path(script).name not in {Path(__file__).name, "coldvault.py", "vault_kit.py"}:
        return False
    try:
        return Path(vault).resolve() == root
    except (OSError, RuntimeError):
        return False


def _cron_command(root):
    return " ".join(map(shlex.quote, _sync_command(root))).replace("%", "\\%")


def _installed_schedule(hours, expected_hours, now, timezone_override=False):
    state = "installed" if expected_hours in (None, hours) else "unverified"
    if timezone_override:
        return state, "schedule: installed, next run due unknown (CRON_TZ is set above this entry)"
    return state, f"schedule: installed, next run due {_next_run(now, hours, _cron_zone())} (every {hours}h)"


def _schedule_status(root, expected_hours, now):
    if sys.platform == "darwin":
        return _launch_status(root, expected_hours, now)
    expected = f" (expected every {expected_hours}h)" if expected_hours else ""
    if sys.platform != "linux":
        return "unknown", f"schedule: installation unverified{expected}"
    try:
        text = _run(["crontab", "-l"], missing_ok="no crontab")
    except OSError:
        return "unknown", f"schedule: unknown{expected}"
    tag = _CRON_TAG + _schedule_id(root)
    entries = []
    has_timezone_override = False
    for entry in text.split("\n"):
        entry = entry.strip()
        if re.match(r"""(['"]?)CRON_TZ\1[ \t]*=""", entry):
            has_timezone_override = True
        if not entry.endswith(tag):
            continue
        fields = entry.removesuffix(tag).rstrip().split(maxsplit=5)
        if len(fields) >= 2 and fields[0].startswith("@") and not fields[1].startswith("#"):
            entries.append((fields, has_timezone_override))
        elif len(fields) == 6 and fields[0][0] in "0123456789*" and not fields[-1].startswith("#"):
            entries.append((fields, has_timezone_override))
    if not entries:
        return "missing", f"schedule: installation unverified{expected}" if expected_hours else "schedule: not installed"
    if len(entries) == 1:
        fields, has_timezone_override = entries[0]
        if fields[-1] != _cron_command(root):
            return "unverified", "schedule: installed, next run due unknown (crontab command differs from this Coldvault command)"
        for hours in _SCHEDULE_HOURS:
            if fields[:5] == ["0", _cron_hour(hours), "*", "*", "*"]:
                return _installed_schedule(hours, expected_hours, now, has_timezone_override)
    return "unverified", "schedule: installed, next run due unknown"


def _run(command, stdin="", missing_ok=None):
    try:
        result = subprocess.run(command, input=stdin.encode("utf-8", "surrogateescape"), capture_output=True)
    except OSError as error:
        raise OSError(f"cannot run {command[0]}: {error}") from error
    stderr = result.stderr.decode("utf-8", "replace").strip()
    if result.returncode and not (missing_ok and missing_ok in stderr.lower()):
        raise OSError(f"{' '.join(command)} failed: {stderr or result.returncode}")
    return result.stdout.decode("utf-8", "surrogateescape") if result.returncode == 0 else ""


def _edit_crontab(parser, root, line=None):
    tag = _CRON_TAG + _schedule_id(root)
    text = _run(["crontab", "-l"], missing_ok="no crontab")
    lines = text.split("\n")
    candidates = []
    owned = []
    for index, entry in enumerate(lines):
        entry = entry.strip()
        tagged = entry.endswith(tag)
        fields = entry.split(maxsplit=1 if entry.startswith("@") else 5)
        invocation = fields[-1] if len(fields) in (2, 6) and entry[0] in "@*0123456789" else ""
        try:
            words = shlex.split(invocation.replace("\\%", "%"))
        except ValueError:
            words = []
        arguments = words[:5]
        targets_vault = _targets_vault(arguments, root)
        if tagged or targets_vault:
            candidates.append(index)
        if tagged and targets_vault and invocation == shlex.join(arguments).replace("%", "\\%") + " " + tag:
            owned.append(index)
    if candidates != owned or len(candidates) > 1:
        locations = ", ".join(f"line {index + 1}" for index in candidates)
        parser.error(f"ambiguous Coldvault schedule for {root}; crontab candidates: {locations}")
    if candidates:
        index, = candidates
        if line is None:
            del lines[index]
            if index == len(lines) and lines:
                lines.append("")
        else:
            lines[index] = line
        updated = "\n".join(lines)
    elif line is not None:
        separator = "\n" if text and not text.endswith("\n") else ""
        updated = text + separator + line + "\n"
    else:
        return 0
    if _run(["crontab", "-l"], missing_ok="no crontab") != text:
        parser.error("your crontab changed while Coldvault was editing it; run the command again")
    _run(["crontab", "-"], updated)
    return len(owned)


def _install_cron(parser, root, command, hours, *, quiet=False):
    hour = _cron_hour(hours)
    line = f"0 {hour} * * * {command} {_CRON_TAG}{_schedule_id(root)}"
    replaced = _edit_crontab(parser, root, line)
    if not quiet:
        print(f"{'Replaced' if replaced else 'Added'} crontab line: {line}")


def _remove_cron(parser, root, *, quiet=False):
    removed = _edit_crontab(parser, root)
    if removed and not quiet:
        print(f"Removed {removed} crontab line(s)")
    return removed > 0


def _launch_agent(root):
    label = "com.coldvault.sync." + _schedule_id(root)
    domain = f"gui/{os.getuid()}"
    path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    return label, domain, f"{domain}/{label}", path


def _launch_intervals(hours):
    if hours == 1:
        return [{"Minute": 0}]
    return [{"Hour": hour, "Minute": 0} for hour in range(0, 24, hours)]


def _launch_job(service):
    return _run(["/bin/launchctl", "print", service], missing_ok="could not find service")


def _launch_arguments(job):
    arguments = re.search(r"^\targuments = \{\n(.*?)^\t\}$", job, re.S | re.M)
    program = re.search(r"(?m)^\tprogram = (.+)$", job)
    if arguments and program:
        lines = arguments[1].splitlines()
        if lines and lines[0] == "\t\t" + program[1] and all(line.startswith("\t\t") for line in lines):
            return [line[2:] for line in lines]
    return []


def _launch_status(root, expected_hours, now):
    expected = f" (expected every {expected_hours}h)" if expected_hours else ""
    _, _, service, _ = _launch_agent(root)
    try:
        job = _launch_job(service)
    except OSError:
        return "unknown", f"schedule: unknown{expected}"
    if not job:
        return "missing", f"schedule: installation unverified{expected}" if expected else "schedule: not installed"
    if _launch_arguments(job) == _sync_command(root):
        descriptors = re.findall(
            r"\t{3}stream = com\.apple\.launchd\.calendarinterval\n[^{}]*"
            r"\t{3}descriptor = \{\n([^{}]*)\t{3}\}", job,
        )
        loaded = sorted(sorted(line.strip() for line in part.splitlines()) for part in descriptors)
        for hours in _SCHEDULE_HOURS:
            intervals = sorted(sorted(f'"{key}" => {value}' for key, value in interval.items())
                               for interval in _launch_intervals(hours))
            if loaded == intervals:
                return _installed_schedule(hours, expected_hours, now)
    return "unverified", "schedule: installed, next run due unknown"


def _edit_launch_agent(root, hours=None):
    label, domain, service, path = _launch_agent(root)
    loaded = _launch_job(service)
    if loaded:
        origin = re.search(r"(?m)^\tpath = (.+)$", loaded)
        if not origin or Path(origin[1]).resolve() != path.resolve():
            raise OSError(f"launchd job {service} was loaded from another path; candidates: {origin[1] if origin else service}, {path}")
    if path.is_symlink():
        raise OSError(f"cannot change symlink {path}")
    try:
        previous = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        previous = None
    except UnicodeError as error:
        raise OSError(f"cannot read {path}: {error}") from error
    if hours is None:
        if loaded and not _targets_vault(_launch_arguments(loaded), root):
            raise OSError(f"ambiguous LaunchAgent arguments; candidates: {service}, {path}")
        if previous is not None:
            try:
                job = plistlib.loads(previous.encode("utf-8"))
            except (plistlib.InvalidFileException, ValueError, ExpatError) as error:
                raise OSError(f"cannot verify LaunchAgent candidate {path}: {error}") from error
            arguments = job.get("ProgramArguments") if isinstance(job, dict) else None
            if (not _targets_vault(arguments, root) or job.get("Label") != label
                    or job.get("Program", arguments[0]) != arguments[0]):
                raise OSError(f"ambiguous LaunchAgent ownership; candidate: {path}")
    else:
        if loaded and previous is None:
            raise OSError("the loaded LaunchAgent has no plist to restore on failure. Run 'schedule remove' first")
        job = {"Label": label, "ProgramArguments": _sync_command(root), "RunAtLoad": True,
               "StartCalendarInterval": _launch_intervals(hours)}
        try:
            plist = plistlib.dumps(job).decode("utf-8")
        except ValueError as error:
            raise OSError(f"cannot write a LaunchAgent for {root}: {error}") from error
        path.parent.mkdir(parents=True, exist_ok=True)
    if loaded:
        _run(["/bin/launchctl", "bootout", service])
    try:
        if hours is None:
            path.unlink(missing_ok=True)
        else:
            _atomic_write(path, plist)
            _run(["/bin/launchctl", "bootstrap", domain, str(path)])
    except (OSError, KeyboardInterrupt) as error:
        try:
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_write(path, previous)
                if loaded:
                    _run(["/bin/launchctl", "bootstrap", domain, str(path)])
        except OSError as rollback_error:
            raise OSError(f"{error}. Restoring the LaunchAgent also failed: {rollback_error}") from error
        raise
    return bool(loaded) or previous is not None


def schedule_main(argv):
    parser = _CommandParser(argv, prog=f"{Path(sys.argv[0]).name} schedule", description="Install or remove a schedule for one vault using cron on Linux or launchd on macOS.")
    parser.add_argument("action", nargs="?", choices=("install", "remove"), default="install")
    parser.add_argument("--vault", required=True, help="Vault folder set up with 'coldvault setup'")
    parser.add_argument("--hours", type=int, default=1, choices=_SCHEDULE_HOURS, help="Hours between runs, on the hour (default 1)")
    args = parser.parse_args(argv)
    if sys.platform not in ("linux", "darwin"):
        parser.error("schedule supports Linux and macOS; run 'coldvault sync' from your own scheduler on other systems")
    import fcntl
    lock = Path.home() / ".cache" / "coldvault-schedule.lock"
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock, "a")
    except OSError as error:
        parser.error(f"cannot lock {lock}: {error}")
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
            return _schedule_locked(parser, args)
        except OSError as error:
            parser.error(str(error))


def _schedule_locked(parser, args):
    root = Path(args.vault).expanduser().resolve()
    if (root / ".coldvault").is_symlink():
        parser.error(".coldvault is a symlink")
    install = args.action == "install"
    config = _load_config(parser, root, allow_invalid=not install)
    if any("\n" in arg or "\r" in arg for arg in _sync_command(root)):
        parser.error("cannot schedule a path that contains a line break")
    if install:
        if config is None:
            parser.error(f"no saved config in {root}; run: coldvault setup")
        saved = json.dumps(config, indent=2) + "\n"
        config["schedule_hours"] = args.hours
        _atomic_write(_config_path(root), json.dumps(config, indent=2) + "\n")
    try:
        if sys.platform == "darwin":
            changed = _edit_launch_agent(root, args.hours if install else None)
            if not args.json:
                if install:
                    print(f"{'Replaced' if changed else 'Installed'} LaunchAgent for {root}")
                else:
                    print(f"Removed LaunchAgent for {root}" if changed else f"No Coldvault schedule found for {root}")
        elif install:
            _install_cron(parser, root, _cron_command(root), args.hours, quiet=args.json)
        elif not _remove_cron(parser, root, quiet=args.json) and not args.json:
            print(f"No Coldvault schedule found for {root}")
    except BaseException:
        if install:
            _atomic_write(_config_path(root), saved)
        raise
    if not install and config and "schedule_hours" in config:
        del config["schedule_hours"]
        _atomic_write(_config_path(root), json.dumps(config, indent=2) + "\n")
    if args.json:
        _json_output({"version": 1, "action": args.action, "vault": str(root),
                      "backend": "launchd" if sys.platform == "darwin" else "cron",
                      "hours": args.hours if args.action == "install" else None})
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
    parser = _CommandParser(argv, prog=f"{Path(sys.argv[0]).name} setup", description="Check prerequisites, choose your sources and save them in <vault>/.coldvault/config.json, then build the vault.")
    args = parser.parse_args(argv)
    output_stream = sys.stderr if args.json else sys.stdout

    def ask(prompt):
        try:
            if args.json:
                print(prompt, end="", file=output_stream, flush=True)
                return input().strip()
            return input(prompt).strip()
        except EOFError:
            parser.error("setup ended before every question was answered")

    def ask_many(prompt):
        answers = []
        while answer := ask(prompt):
            answers.append(str(Path(answer).expanduser().resolve()))
        return answers

    print(f"Python {sys.version.split()[0]}: ok", file=output_stream)
    print("SQLite FTS5: ok" if fts5_available() else "warning: SQLite FTS5 is not available; search will scan every note instead of using an index", file=output_stream)
    found = []
    for folder in _standard_transcript_folders():
        if folder.is_dir():
            found.append(str(folder))
            print(f"{folder}: {sum(1 for _ in folder.rglob('*.jsonl'))} sessions", file=output_stream)
        else:
            print(f"{folder}: not found", file=output_stream)
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
    chatgpt_exports = ask_many("ChatGPT export .zip to import (Enter to finish): ")
    claude_exports = ask_many("Claude.ai export .zip to import (Enter to finish): ")
    try:
        _check_sources(root, notes, notion_exports, transcripts, chatgpt_exports + claude_exports)
    except SourceError as error:
        parser.error(str(error))
    config = {"version": 1, "transcripts": transcripts, "notes": notes, "notion_exports": notion_exports,
              "chatgpt_exports": chatgpt_exports, "claude_exports": claude_exports, "username": username}
    for key in ("folders", "schedule_hours", "preservation_receipts"):
        if saved and key in saved:
            config[key] = saved[key]
    try:
        folder = _owned_folder(root, (".coldvault",))
        _atomic_write(folder / "config.json", json.dumps(config, indent=2) + "\n")
    except OSError as error:
        parser.error(f"cannot save the config in {root}: {error}")
    print(f"Saved {_config_path(root)}. Later runs only need: coldvault sync --vault {root}", file=output_stream)
    return sync_main(["--vault", str(root), *(["--json"] if args.json else [])])


def markdown_files(source, vault, suffixes=(".md",)):
    def onerror(error):
        raise error

    for folder, dirs, names in os.walk(source, onerror=onerror):
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
        f"{base} ({hashlib.sha1(str(source).encode(), usedforsecurity=False).hexdigest()[:8]})"
        if [b.casefold() for b in bases].count(base.casefold()) > 1 else base
        for source, base in zip(sources + exports, bases)
    ]
    return list(zip(sources, folders)), list(zip(exports, folders[len(sources):]))


def _saved_import_notes(run, imports):
    index = note_target(run.root, "Index.md")
    if not _is_managed_note(index):
        return []
    previous, _ = _previous_folders(run.root)
    prefixes = {run.root / folders["imported"] / name
                for folders in (previous, run.folders) for _, name in imports}
    section = _read(index).partition("\n## Imported notes\n\n")[2]
    notes = []
    for target in re.findall(r"^- \[\[([^\n]+)\]\]$", section, re.MULTILINE):
        path = run.root / f"{target}.md"
        if prefixes.intersection(path.parents) and path.is_file() and path.resolve() == path:
            notes.append((path.relative_to(run.root).as_posix(), "", {"kind": "note", "kept": True}))
    return notes


def notes_source(run):
    if not run.notes:
        return None
    imports, _ = _import_folders(run)
    notes = []
    try:
        _check_sources(run.root, notes=run.notes)
        for source, folder in imports:
            for path in markdown_files(source, run.root):
                text = redact(path.read_text(encoding="utf-8-sig", errors="replace"))
                relative = f"{folder}/{path.relative_to(source).as_posix()}"
                if not _has_frontmatter(text):
                    text = _frontmatter({_NOTE_SOURCE_PATH: json.dumps(redact(relative))}) + text
                elif all(_field(text, key) is None for key in _NOTE_SOURCE_PATH_KEYS):
                    text = f"---\n{_NOTE_SOURCE_PATH}: {json.dumps(redact(relative))}\n{text[4:]}"
                notes.append((f"{run.folders['imported']}/{relative}", text, {"kind": "note"}))
    except (SourceError, OSError) as error:
        raise SourceError(str(error), notes=_saved_import_notes(run, imports)) from None
    return notes


def notion_source(run):
    if not run.notion_exports:
        return None
    _, imports = _import_folders(run)
    try:
        _check_sources(run.root, notion_exports=run.notion_exports)
        return [(note, text, {"kind": "note", "source": "notion"})
                for export, folder in imports
                for note, text in notion_notes(export, run.root, f"{run.folders['imported']}/{folder}")]
    except (SourceError, OSError, zipfile.BadZipFile) as error:
        raise SourceError(str(error), notes=_saved_import_notes(run, imports)) from None


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
            base = entry[0] + " " + (entry[1] or hashlib.sha1(name.encode(), usedforsecurity=False).hexdigest())[:8]
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
        f"- [[Index#Projects|Projects]]: project notes in `{folders['projects']}/` collect sessions and repository context.\n"
        f"- [[START-HERE|AI conversations]]: browse transcripts in `{folders['sessions']}/` through their project notes.\n"
        f"- [[Index#Projects|GitHub repositories]]: find repository details in the corresponding notes in `{folders['projects']}/`.\n"
        "- [[Writing/README|Notes and writing]]: keep your notes, research, and drafts in Writing.\n"
        "- [[Decisions/README|Decisions]]: record what you decided and what you learned.\n"
        "- [[Context/README|Reference]]: keep articles, posts, and other material from outside the vault.\n"
        "- [[Archive/README|Archive]]: keep older material you want to refer to later.\n"
        f"- `{folders['imported']}/`: Markdown folders and Notion exports you imported, generated.\n"
        "- [[Daily/README|Daily]]: one note per day, listing that day's sessions and imported notes.\n"
        "- [[Templates/README|Templates]]: note templates with the frontmatter each note type uses.\n"
        "- `Attachments/`: images and files you paste into notes.\n\n"
        "Coldvault never changes notes in your own folders. Delete any folder you don't need."
    ))


VAULT_TEMPLATE = {
    "Home.md": _home(FOLDERS),
    "Daily/README.md": _readme("Daily", "One note per day, named `YYYY-MM-DD.md`. Coldvault writes the note for each day with sessions or imported notes and lists them; if you edit it, your version is kept and Coldvault writes its list to `YYYY-MM-DD (Coldvault).md`. Create today's note with the Daily notes command; it uses [[Templates/Daily]]."),
    "Writing/README.md": _readme("Writing", "Keep your own notes, research, drafts, articles, posts, and letters here. Set `authorship: me` so you can tell your words from imported ones. New notes can start from [[Templates/Note]]."),
    "Context/README.md": _readme("Context", "Reference material written by other people: articles, posts and video transcripts. Record where each came from in `source`. Start from [[Templates/Context]]."),
    "Decisions/README.md": _readme("Decisions", "Record decisions and lessons learned here. Write down what you decided, why, and which project or conversation informed it."),
    "Archive/README.md": _readme("Archive", "Move older notes and reference material here when you want to keep them for later."),
    "Templates/README.md": _readme("Templates", "Templates for each note type. Obsidian's Templates command inserts them; `{{date}}` and `{{title}}` are filled in for you."),
    "Templates/Daily.md": '---\nrecord_type: daily\ndate: "{{date}}"\nauthorship: me\nprojects: []\n---\n\n# {{date}}\n\n## Notes\n\n## Done\n',
    "Templates/Note.md": '---\nrecord_type: note\ntitle: "{{title}}"\ndate: "{{date}}"\nauthorship: me\nprojects: []\ntags: []\n---\n\n# {{title}}\n',
    "Templates/Context.md": '---\nrecord_type: context\ntitle: "{{title}}"\nsource: \nauthor: \ndate: "{{date}}"\nauthorship: other\nprojects: []\n---\n\n# {{title}}\n\n## Summary\n\n## Notes\n',
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
    _check_sources(run.root, transcripts=run.transcript_folders)
    files = run.files
    if run.transcript_folders:
        files = [p for folder in run.transcript_folders for p in Path(folder).expanduser().rglob("*.jsonl")]
    sessions = {}
    try:
        cache_path = _owned_folder(run.root, (".coldvault",)) / "transcripts.json"
    except OSError:
        cache_path = None
    cached, parsed = _transcript_cache(cache_path) if cache_path else {}, {}
    for path in sorted(files):
        status = path.stat()
        stamp = f"{status.st_size} {status.st_mtime_ns} {status.st_ctime_ns} {status.st_ino}"
        location = str(path.resolve())
        entry = cached.get(location)
        if entry and entry[0] == stamp:
            info, read_digest = entry[1], entry[2]
        else:
            data = path.read_bytes()
            info = transcript_info(path, data)
            read_digest = hashlib.sha256(data).hexdigest() if _complete_jsonl(data) else None
        parsed[location] = [stamp, info, read_digest]
        if not info:
            continue
        (project, name), day, text, source, session_id = info
        path_key = _short_hash(str(path.resolve()))
        project_key = _short_hash(f"{project}\n{path.stem}")
        key = (source, session_id) if session_id else project_key
        earlier = sessions.get(key)
        if earlier and (earlier[2]["source"] != source or not _same_transcript(earlier[1], text)):
            key = (source, session_id, path_key) if session_id else path_key
            earlier = sessions.get(key)
        prefix = f"{run.folders['sessions']}/{day}-{slug(name)}"
        legacy = [f"{prefix}-{path_key}.md", *(earlier[2]["legacy"] if earlier else ())]
        previous_paths = [f"{prefix}-{project_key}.md", *(earlier[2]["previous-paths"] if earlier else ())]
        if earlier and len(earlier[1]) >= len(text):
            earlier[2]["legacy"] = legacy
            earlier[2]["previous-paths"] = previous_paths
        else:
            digest = hashlib.sha256(json.dumps(key).encode()).hexdigest()[:16] if session_id else key
            sessions[key] = (f"{prefix}-{digest}.md", text, {
                "kind": "session", "project": (project, name), "date": day,
                "source": source, "session-id": session_id or path.stem, "legacy": legacy,
                "session-id-fallback": "false" if session_id else "true", "previous-paths": previous_paths,
                **({"read": (location, read_digest)} if read_digest else {})})
    if cache_path and parsed != cached:
        with contextlib.suppress(OSError):
            cached_text = json.dumps({"tag": _CACHE_TAG, "files": parsed})
            if len(cached_text) <= _TRANSCRIPT_CACHE_BYTES:
                _atomic_write(cache_path, cached_text)
            else:
                cache_path.unlink(missing_ok=True)
    return list(sessions.values()) if files else None


def github_source(run):
    if not run.username:
        return None
    token = _github_token()
    try:
        cache_path = _owned_folder(run.root, (".coldvault",)) / "github.json"
    except OSError:
        cache_path = None
    saved = _github_cache(cache_path) if cache_path else {}
    username = run.username.casefold()
    matching_user = saved.get("username") == username
    cached = saved.get("repos", {}) if matching_user else {}
    inventory = _github_inventory(saved.get("inventory"), username) if matching_user else {}
    had_complete = matching_user and saved.get("complete") is True
    listing_failure = ""
    try:
        observed = _github_inventory(github_repos(run.username), username)
    except GitHubListingError as error:
        observed = _github_inventory(error.repos, username)
        listing_failure = str(error)
        inventory.update(observed)
    else:
        inventory = observed
    kept, limited, notes = {}, False, {}
    refreshed = set()
    failure = ""
    for identity, repo in inventory.items():
        full_name, pushed = repo.get("full_name"), repo.get("pushed_at")
        metadata = {"kind": "repo", "project": repo["name"], "repo": json.dumps(full_name if isinstance(full_name, str) else str(repo["name"]))}
        if _is_timestamp(pushed):
            metadata["pushed-at"] = pushed
        text = f"GitHub: [{repo['html_url']}]({repo['html_url']})\n\n{repo.get('description') or ''}\n\n"
        if isinstance(full_name, str) and _REPO_NAME.fullmatch(full_name):
            entry = cached.get(full_name)
            if identity in observed and not limited:
                entry = dict(entry or {"pushed-at": "", "docs": "", "issues": "", "etag": ""})
                try:
                    _github_repo_content(full_name, repo, token, entry)
                except (OSError, URLError, ValueError) as error:
                    limited = isinstance(error, HTTPError) and (error.code == 429 or (error.headers or {}).get("X-RateLimit-Remaining") == "0")
                    if not limited:
                        detail = _github_error(error, token)
                        failure = failure or f"incomplete repository content ({detail})"
                        print(f"GitHub: kept the saved README and issues of {full_name} ({detail})", file=sys.stderr)
                else:
                    refreshed.add(identity)
            if entry:
                kept[full_name] = entry
                docs = entry["docs"]
                if not entry["pushed-at"] and not docs and "pending-docs" in entry:
                    docs = _github_doc_text(entry["pending-docs"]["sections"])
                text += "".join(f"{section}\n\n" for section in (docs, entry["issues"]) if section)
        notes[identity] = (f"{run.folders['projects']}/{repo['name']}.md", text, metadata)
    if limited:
        failure = "GitHub rate limit reached: some READMEs and issue lists wait for the next run (GITHUB_TOKEN raises the limit)"
    if (listing_failure or failure) and (matching_user or "username" not in saved):
        for name, note in _saved_github_notes(run):
            if name not in refreshed and (name in notes or (listing_failure and not had_complete)):
                notes[name] = note
    updated = {"version": 1, "username": username, "complete": had_complete or not listing_failure,
               "inventory": list(inventory.values()), "repos": kept}
    if cache_path and updated != saved:
        with contextlib.suppress(OSError):
            _atomic_write(cache_path, json.dumps(updated, ensure_ascii=False))
    failure = "; ".join(filter(None, (listing_failure, failure)))
    if failure:
        raise SourceError(failure, notes=list(notes.values()))
    return list(notes.values())


_REPO_NAME = re.compile(r"(?!\.\.?/)[A-Za-z0-9_.-]+/(?!\.\.?$)[A-Za-z0-9_.-]+")
_GITHUB_DOC_NAME = re.compile(r"[^/\\]+\.md", re.IGNORECASE)


def _github_inventory(rows, username):
    inventory = {}
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, dict):
            continue
        name, url = row.get("name"), row.get("html_url")
        if not isinstance(name, str) or not _REPO_NAME.fullmatch(f"{username}/{name}"):
            continue
        full_name = row.get("full_name")
        if full_name is not None and (not isinstance(full_name, str) or full_name.casefold() != f"{username}/{name.casefold()}"):
            continue
        if not isinstance(url, str) or url.casefold() != f"https://github.com/{username}/{name.casefold()}":
            continue
        repo = {"name": name, "full_name": full_name, "html_url": url}
        if isinstance(row.get("description"), str):
            repo["description"] = redact(row["description"])
        if _is_timestamp(row.get("pushed_at")):
            repo["pushed_at"] = row["pushed_at"]
        if type(row.get("open_issues_count")) is int:
            repo["open_issues_count"] = row["open_issues_count"]
        inventory[name.casefold()] = repo
    return inventory


def _saved_github_notes(run):
    index = note_target(run.root, "Index.md")
    folder = run.root / _previous_folders(run.root)[0]["projects"]
    if folder.is_symlink() or not _is_managed_note(index):
        return
    links = _read(index).partition("\n## Projects\n\n")[2].split("\n\n", 1)[0]
    for relative in re.findall(r"^- \[\[([^\n]+)\]\]$", links, re.MULTILINE):
        path = run.root / f"{relative}.md"
        if path.parent != folder or not _is_managed_note(path):
            continue
        text = _read(path)
        full_name = _field(text, "repo")
        if not full_name or not _REPO_NAME.fullmatch(full_name):
            continue
        owner, name = full_name.split("/")
        if owner.casefold() != run.username.casefold():
            continue
        body = text.split("\n---\n", 1)[1].lstrip("\n").partition("\n\n")[2].rpartition("\n\n## Sessions\n\n")[0]
        if body:
            metadata = {"kind": "repo", "project": name, "repo": json.dumps(full_name)}
            pushed = _field(text, "pushed-at")
            if _is_timestamp(pushed):
                metadata["pushed-at"] = pushed
            yield name.casefold(), (relative + ".md", body + "\n\n", metadata)


def _github_cache(path):
    try:
        saved = json.loads(_read(path)) if path.is_file() and not path.is_symlink() else {}
    except (OSError, UnicodeError, ValueError):
        return {}
    if not isinstance(saved, dict) or saved.get("version") != 1:
        return {}
    repos, valid = saved.get("repos"), {}
    for name, entry in (repos.items() if isinstance(repos, dict) else ()):
        if not isinstance(entry, dict) or not all(isinstance(entry.get(key), str) for key in ("pushed-at", "docs", "issues", "etag")):
            continue
        if not _valid_pending_docs(entry.get("pending-docs")):
            entry.pop("pending-docs", None)
        valid[name] = entry
    saved["repos"] = valid
    return saved


def _valid_pending_docs(pending):
    if not isinstance(pending, dict) or not _is_timestamp(pending.get("revision")):
        return False
    names, sections = pending.get("names", []), pending.get("sections")
    return (isinstance(names, list) and all(isinstance(name, str) and _GITHUB_DOC_NAME.fullmatch(name) for name in names)
            and isinstance(sections, list) and len(sections) <= 1 + len(names)
            and all(isinstance(section, str) for section in sections))


def _github_repo_content(full_name, repo, token, entry):
    api = f"{_GITHUB_API}repos/{full_name}"
    pushed = repo["pushed_at"] if _is_timestamp(repo.get("pushed_at")) else ""
    if not pushed or entry["pushed-at"] != pushed:
        pending = entry.get("pending-docs", {})
        if pushed and pending.get("revision") == pushed:
            pending = {**pending, "sections": pending["sections"].copy()}
        else:
            pending = {"revision": pushed, "sections": []}
        entry["pending-docs"] = pending
        _github_docs(api, token, pending)
        entry.update({"pushed-at": pushed, "docs": _github_doc_text(pending["sections"])})
        del entry["pending-docs"]
    else:
        entry.pop("pending-docs", None)
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


def _github_doc_text(sections):
    return "\n\n".join(filter(None, sections))


def _github_docs(api, token, pending):
    sections = pending["sections"]
    if not sections:
        readme = _github_text(f"{api}/readme", token)
        sections.append(f"## README\n\n{readme}" if readme else "")
    if "names" not in pending:
        try:
            listing = json.loads(_github_get(f"{api}/contents/docs", token)[0])
        except HTTPError as error:
            if error.code != 404:
                raise
            listing = []
        pending["names"] = sorted(entry["name"] for entry in listing if isinstance(entry, dict) and entry.get("type") == "file"
                                  and isinstance(entry.get("name"), str) and _GITHUB_DOC_NAME.fullmatch(entry["name"])) if isinstance(listing, list) else []
    for name in pending["names"][len(sections) - 1:]:
        text = _github_text(f"{api}/contents/docs/{quote(name)}", token)
        sections.append(f"## docs/{name}\n\n{text}" if text else "")


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
    try:
        return _chat_export_notes(run, exports)
    except SourceError as error:
        saved, _, history = _folder_state(run.root)
        sessions, _ = _resolve_sessions(run.root, run.folders["sessions"], [*history, saved["sessions"]], [])
        notes = [(note, text, meta) for _, _, meta, note, text in sessions if meta["source"] in _CHAT_EXPORTS]
        raise SourceError(str(error), notes=notes) from None


def _chat_export_notes(run, exports):
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


def build_from_files(files, output, username, notes=(), notion_exports=(), folders=(), chatgpt_exports=(), claude_exports=(),
                     *, transcript_folders=(), receipts=None, show_summary=True):
    root = Path(output).expanduser().resolve()
    folders = {**FOLDERS, **dict(folders)}
    fresh = _unbuilt(root)
    root.mkdir(parents=True, exist_ok=True)
    if fresh:
        _apply_template(root, folders)
    run = SimpleNamespace(root=root, files=files, username=username, notes=notes, notion_exports=notion_exports, folders=folders,
                          chatgpt_exports=chatgpt_exports, claude_exports=claude_exports, transcript_folders=transcript_folders)
    items, sources = [], {}
    for name, source in SOURCES:
        try:
            found = source(run)
            found = None if found is None else list(found)
        except SourceError as error:
            sources[name] = {"state": "failed", "reason": str(error)}
            items += error.notes
            continue
        if found is None:
            sources[name] = {"state": "skipped"}
            continue
        sources[name] = {"state": "ok", "count": len(found)}
        items += found
    failed = {name for name, result in sources.items() if result["state"] == "failed"}
    history = _rename_folders(root, folders, not failed)
    update_daily = failed.isdisjoint(("transcripts", "notes", "notion", "chat-exports"))
    projects, sessions, preserved = _write_vault(root, items, folders, history, update_daily)
    if receipts and "archive_folder" in receipts and sources["transcripts"]["state"] == "ok":
        _write_preservation_receipts(root, receipts, preserved)
    imported = sum(1 for _, _, metadata in items if metadata.get("kind", "note") == "note")
    if show_summary:
        print(f"Built vault at {root} ({projects} projects, {sessions} sessions, {imported} imported notes)")
    return sources


def _write_preservation_receipts(root, settings, preserved):
    folder, host = Path(settings["folder"]), settings["host"]
    if folder.is_symlink():
        raise OSError(f"{folder} is a symlink")
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    _check_private_folder(folder)
    archive = Path(settings["archive_folder"])
    if archive.is_symlink() or _inside(root, archive.resolve()):
        raise OSError("choose an archive folder outside the vault that is not a symlink")
    archive = archive.resolve()
    archive.mkdir(mode=0o700, exist_ok=True)
    _check_private_folder(archive)
    _sync_path(archive.parent, os.O_RDONLY | os.O_DIRECTORY)
    for relative, rendered, (path, read_digest) in preserved:
        note = root / relative
        if not _unchanged(root, relative, rendered) or not _sync_note_folders(root, note):
            continue
        verified = _verified_bytes(note, rendered.replace("\n", os.linesep).encode("utf-8"))
        if not verified:
            continue
        try:
            source = Path(path)
            before = source.lstat()
            data = source.read_bytes()
            current = hashlib.sha256(data).hexdigest()
        except OSError:
            continue
        if current != read_digest or not _same_note(note, verified):
            continue
        destination = archive / current
        if source == destination or not stat.S_ISREG(before.st_mode):
            continue
        archived = _archive_original(destination, data)
        if not _same_note(source, before) or not _same_note(note, verified) or not _same_note(destination, archived):
            continue
        line = json.dumps({"schema_version": 2, "host": host, "path": path, "sha256": current,
                           "destination": str(destination), "verified_at": _utc_now()}) + "\n"
        target = folder / (hashlib.sha256(f"{host}:{path}".encode()).hexdigest() + ".json")
        if not _private_file(target) or _current_text(target) != line:
            _publish_preservation_receipt(target, line)


def _check_private_folder(folder):
    status = folder.lstat()
    if not stat.S_ISDIR(status.st_mode):
        raise OSError(f"{folder} is not a folder")
    if os.name == "posix" and (status.st_uid != os.getuid() or status.st_mode & 0o022):
        raise OSError(f"{folder} must be owned by you and not writable by others")


def _sync_note_folders(root, note):
    folders = note.parents
    try:
        for folder in folders[:folders.index(root) + 1]:
            _sync_path(folder, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return False
    for folder in root.parents:
        try:
            _sync_path(folder, os.O_RDONLY | os.O_DIRECTORY)
        except OSError:
            break
    return True


def _archive_original(destination, data):
    if not os.path.lexists(destination):
        descriptor, temporary = tempfile.mkstemp(prefix=".coldvault-", dir=destination.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            Path(temporary).unlink(missing_ok=True)
    if not _private_file(destination):
        raise OSError("archive destination must be a private regular file")
    verified = _verified_bytes(destination, data)
    if not verified or verified.st_nlink != 1:
        raise OSError("could not verify independent original bytes at the archive destination")
    _sync_path(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
    return verified


def _verified_bytes(note, expected):
    try:
        descriptor = os.open(note, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        before = os.fstat(descriptor)
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            if stream.read() != expected:
                return None
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        return after if _same_file(before, after) and _same_note(note, after) else None
    except OSError:
        return None
    finally:
        os.close(descriptor)


def _same_file(first, second):
    return os.path.samestat(first, second) and all(
        getattr(first, field) == getattr(second, field) for field in ("st_size", "st_mtime_ns", "st_ctime_ns"))


def _same_note(note, verified):
    try:
        return _same_file(verified, note.lstat())
    except OSError:
        return False


def _publish_preservation_receipt(target, line):
    handle, temporary = tempfile.mkstemp(prefix=".coldvault-", dir=target.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    try:
        _sync_path(target.parent, os.O_RDONLY | os.O_DIRECTORY)
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def _private_file(path):
    try:
        status = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(status.st_mode) and stat.S_IMODE(status.st_mode) == 0o600


def _previous_folders(root):
    folders, pending, _ = _folder_state(root)
    return folders, pending


def _folder_state(root):
    state = root / ".coldvault" / "folders.json"
    try:
        saved = json.loads(_read(state)) if not state.is_symlink() else None
    except (OSError, UnicodeError, ValueError):
        saved = None
    if not isinstance(saved, dict):
        return FOLDERS, None, []
    pending = saved.pop("pending", None)
    history = saved.pop("session-folders", [])
    if not isinstance(history, list) or not all(map(_folder_name, history)):
        history = []
    complete = _valid_folders(pending) and set(pending) == set(FOLDERS)
    return (saved if _valid_folders(saved) and set(saved) == set(FOLDERS) else FOLDERS), (pending if complete else None), history


def _save_folders(root, folders, history, pending=None):
    state = dict(folders)
    if pending:
        state["pending"] = pending
    if history:
        state["session-folders"] = history
    _atomic_write(_owned_folder(root, (".coldvault",)) / "folders.json", json.dumps(state, indent=2) + "\n")


def _crossed_folder(previous, folders):
    return next((key for key, name in folders.items() for other, old in previous.items()
                 if other != key and name.casefold() == old.casefold()), None)


def _relink(path, renames, retained):
    if not _is_managed_note(path):
        return
    lines = _read(path).split("\n")
    if lines[1] != _NOTE_MARKER or not lines[2].startswith(_HASH_FIELD):
        return
    del lines[2]
    before = "\n".join(lines)

    def target(match):
        original = match.group(0)
        head, slash, rest = original.partition("/")
        if match.group(1) or match.group(2) or not slash or head not in renames or original in retained:
            return original
        return f"{renames[head]}/{rest}"

    after = _CODE_OR_WIKILINK.sub(target, before)
    if after != before:
        lines = after.split("\n")
        lines.insert(2, _HASH_FIELD + _digest(after))
        _atomic_write(path, "\n".join(lines))


def _rename_folders(root, folders, ready=True):
    saved, pending, history = _folder_state(root)
    if saved == folders or _crossed_folder(saved, folders) or (pending and pending != folders):
        return history
    if saved["sessions"] != folders["sessions"] and saved["sessions"] not in history:
        history.append(saved["sessions"])
    _save_folders(root, saved, history, folders)
    if not ready:
        return history
    base = root.resolve()
    moves = []
    retained = set()
    for name in history:
        folder = root / name
        if folder.is_symlink() or not folder.is_dir():
            continue
        paths = sorted(folder.glob("*.md"))
        frozen = set()
        for path in paths:
            text, fields = _session_text(path)
            if _session_identity(fields) and set(text.split("\n", 3)[:3]) & _MARKERS.keys() and not _is_managed_note(path):
                frozen.add(_session_family(path.name))
        retained.update(f"{name}/{path.stem}" for path in paths if _session_family(path.name) in frozen)
    for key, old in saved.items():
        source = root / old
        if old == folders[key] or source.is_symlink() or not source.is_dir():
            continue
        moves += [(source, path, root / folders[key] / path.relative_to(source)) for path in sorted(source.rglob("*.md"))]
    emptied, stranded = set(), False
    for source, path, target in moves:
        if path.relative_to(root).as_posix().removesuffix(".md") in retained or not _is_managed_note(path) or not _inside(base, path.resolve()):
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
    for name in dict.fromkeys([*folders.values(), *history]):
        folder = root / name
        if not folder.is_symlink() and folder.is_dir():
            for path in sorted(folder.rglob("*.md")):
                if _inside(base, path.resolve()):
                    _relink(path, renames, retained)
    home = root / "Home.md"
    if not home.is_symlink() and _current_text(home) == _home(saved):
        _atomic_write(home, _home(folders))
    _save_folders(root, saved if stranded else folders, history, folders if stranded else None)
    return history


_CACHE_TAG = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
_TRANSCRIPT_CACHE_BYTES = 64 * 1024 * 1024


def _transcript_cache(path):
    if path.is_symlink() or not path.is_file():
        return {}
    try:
        if path.stat().st_size > _TRANSCRIPT_CACHE_BYTES:
            path.unlink()
            return {}
        saved = json.loads(_read(path))
        files = saved["files"]
        if saved["tag"] != _CACHE_TAG or not isinstance(files, dict):
            return {}
        for stamp, info, read_digest in files.values():
            if not isinstance(stamp, str) or not (read_digest is None or isinstance(read_digest, str) and re.fullmatch("[0-9a-f]{64}", read_digest)):
                return {}
            if info is not None:
                if not isinstance(info, list):
                    return {}
                (key, name), day, text, source, session = info
                if not all(isinstance(value, str) for value in (key, name, day, text, source)):
                    return {}
                if session is not None and not isinstance(session, str):
                    return {}
        return files
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        return {}


def _short_hash(value):
    return hashlib.sha1(value.encode(), usedforsecurity=False).hexdigest()[:8]


def project_slugs(projects):
    slugs, taken, seen = {}, set(), set()
    for key, name in projects:
        unique = base = slug(name)
        digest = hashlib.sha1((key if name in seen else name).encode(), usedforsecurity=False).hexdigest()
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
_CONTINUATION = re.compile(r" \(continued(?: (\d+))?\)\.md$")


def _session_family(filename):
    return _CONTINUATION.sub(".md", filename)


def _frontmatter_values(text):
    values = {}
    for line in text[4:text.index("\n---\n", 3)].split("\n"):
        name, _, value = line.partition(": ")
        try:
            values[name] = json.loads(value) if value.startswith('"') else value
        except ValueError:
            return {}
    return values


def _session_identity(fields):
    source, session_id = fields.get("source"), fields.get("session-id")
    if isinstance(source, str) and isinstance(session_id, str):
        return source, session_id
    return None


def _same_transcript(first, second):
    return bool(first and second and (first.startswith(second) or second.startswith(first)))


def _different_session(fields, meta):
    identity = _session_identity(fields)
    return identity is not None and (
        identity != _session_identity(meta)
        or ("session-id-fallback" in fields and fields["session-id-fallback"] != meta.get("session-id-fallback"))
    )


def _session_text(path):
    if not path.is_symlink() and path.is_file():
        with contextlib.suppress(OSError, UnicodeError):
            text = _read(path)
            if _has_frontmatter(text):
                return text, _frontmatter_values(text)
    return "", {}


def _resolve_sessions(root, folder, history, sessions):
    continuation_candidates = {}
    stored, by_identity = {}, {}
    history = [name for name in dict.fromkeys(history) if name != folder]
    for name in [folder, *history]:
        base = root / name
        if base.is_symlink() or not base.is_dir():
            continue
        for path in sorted(base.glob("*.md")):
            part = _CONTINUATION.search(path.name)
            if part:
                note = f"{name}/{_session_family(path.name)}"
                continuation_candidates.setdefault(note, []).append((int(part[1] or 1), path))
                continue
            if _OLD_COPY.search(path.name):
                continue
            text, fields = _session_text(path)
            identity = _session_identity(fields)
            if not identity or (name != folder and not set(text.split("\n", 3)[:3]) & _MARKERS.keys()):
                continue
            note = path.relative_to(root).as_posix()
            match = _KEPT_SESSION.fullmatch(text[text.index("\n---\n", 3) + 6:].lstrip("\n"))
            stored[note] = (fields, match.group(1).rstrip() if match else "")
            if fields.get("session-id-fallback") != "true":
                by_identity.setdefault(identity, []).append(note)

    live_counts = Counter(_session_identity(meta) for *_, meta, _, _ in sessions)
    resolved, taken = [], set()
    for key, name, meta, note, text in sessions:
        identity = _session_identity(meta)
        explicit_id = meta.get("session-id-fallback") == "false"
        prior = [f"{name}/{Path(note).name}" for name in history]
        matches = by_identity.get(identity, ()) if explicit_id else [candidate for candidate in prior if candidate in stored]
        ambiguous = live_counts[identity] > 1 or len(matches) > 1
        preferred = note
        previous_paths = set(meta.get("previous-paths", ())) | set(meta["legacy"])

        def matches_note(candidate):
            fields, body = stored[candidate]
            if _different_session(fields, meta):
                return False
            if _same_transcript(body, text):
                return True
            if ambiguous:
                return False
            if not explicit_id and ("session-id-fallback" in fields or "session-id-fallback" not in meta):
                return True
            associated = candidate == preferred or candidate in prior or (explicit_id and candidate in previous_paths)
            return associated and _frozen(root, candidate)

        if note not in stored or not matches_note(note):
            note = next((candidate for candidate in matches if candidate not in taken and matches_note(candidate)), note)
        stem, number = note.removesuffix(".md"), 2
        while note in taken or (note in stored and not matches_note(note)):
            note = f"{stem} ({number}).md"
            number += 1
        legacy = [path for path in meta["legacy"]
                  if path not in stored or (not _different_session(stored[path][0], meta) and _same_transcript(stored[path][1], text))]
        resolved.append((key, name, {**meta, "legacy": legacy}, note, text))
        taken.add(note)
    taken.update(path for *_, meta, _, _ in resolved for path in meta["legacy"] if _is_managed_note(root / path))

    keys = {name: key for key, name, *_ in sessions}
    for note, (values, body) in stored.items():
        if note in taken:
            continue
        fields = [values.get(name) for name in ("project", "date", "source", "session-id")]
        if not body or not all(isinstance(value, str) for value in fields):
            continue
        name, date, source, session_id = fields
        recorded = values.get("project-key")
        key = recorded if isinstance(recorded, str) else keys.get(name, name)
        resolved.append((key, name, {"kind": "session", "project": (key, name), "date": date, "source": source,
                                     "session-id": session_id, "legacy": [], "kept": True,
                                     "keyed": isinstance(recorded, str) or name in keys,
                                     **({"session-id-fallback": values["session-id-fallback"]} if "session-id-fallback" in values else {})},
                         note, body))
    return resolved, continuation_candidates


def _session_continuations(root, note, meta, candidates):
    parts = {}
    next_number = 1
    for number, path in sorted(candidates):
        next_number = number + 1
        _, fields = _session_text(path)
        if _session_identity(fields) is not None and not _different_session(fields, meta):
            parts[path.relative_to(root).as_posix()] = fields.get("paragraphs-before", "")
    suffix = "" if next_number == 1 else f" {next_number}"
    return parts, f"{note.removesuffix('.md')} (continued{suffix}).md"


def _write_vault(root, items, folders, history, update_daily):
    sessions = [(*meta["project"], meta, note, text) for note, text, meta in items if meta.get("kind") == "session"]
    sessions, continuation_candidates = _resolve_sessions(root, folders["sessions"], history, sessions)
    repos = {}
    for _, text, meta in items:
        if meta.get("kind") == "repo":
            repos.setdefault(meta["project"], (text, meta))
    imported = [(note, text, meta) for note, text, meta in items if meta.get("kind", "note") == "note"]
    notion = {note for note, _, meta in imported if meta.get("source") == "notion"}
    projects = {project: name for project, name, *_ in sessions}
    for name in repos:
        if name not in projects.values():
            projects[name] = name
    keys = sorted(projects, key=lambda key: (projects[key].casefold(), projects[key], key))
    project_paths = {key: _planned(root, f"{folders['projects']}/{unique}.md") for key, unique in project_slugs((key, projects[key]) for key in keys).items()}
    now = _utc_now()
    session_notes = {}
    continuations = {}
    readable = {}
    for project, _, meta, note, text in sessions:
        target, title, header = note, f"{projects[project]} — {meta['date']}", f"Project: {link(project_paths[project])}"
        parts, next_part = _session_continuations(root, note, meta, continuation_candidates.get(note, ()))
        continuations[note] = list(parts)
        frozen = _frozen(root, note)
        if meta.get("kept") and (frozen or parts):
            continue
        if frozen or parts:
            paragraphs = text.split("\n\n")
            prefix = [note, *parts]
            target = next_part
            if parts and _is_managed_note(root / prefix[-1]):
                target = prefix.pop()
            for part in [*prefix, target]:
                before = parts.get(part, "")
                if re.fullmatch(r"[0-9]{1,9}", before):
                    text = "\n\n".join(paragraphs[int(before):])
                if part != target:
                    text = _new_messages(root, part, text)
            if not text:
                continue
            paragraphs_before = len(paragraphs) - len(text.split("\n\n"))
            title = f"{title} (continued)"
            header = f"{header}\n\nContinues {link(prefix[-1])}"
        elif not meta.get("kept") and "read" in meta:
            readable[target] = meta["read"]
        session_notes[target] = (note, _render(_frontmatter({
            "source": meta["source"],
            "date": meta["date"] if _is_day(meta["date"]) else json.dumps(meta["date"]),
            "project": json.dumps(projects[project]),
            **({"project-key": json.dumps(project)} if meta.get("keyed", True) else {}),
            "session-id": json.dumps(meta["session-id"]),
            **({"session-id-fallback": meta["session-id-fallback"]} if "session-id-fallback" in meta else {}),
            "imported-at": _imported_at(root / _planned(root, target), now),
            **({"paragraphs-before": paragraphs_before} if target != note else {}),
            "paragraphs": len(text.split("\n\n")),
        }) + f"# {title}\n\n{header}\n\n{text}"))
    current = {target for target, (_, rendered) in session_notes.items() if _unchanged(root, target, rendered)}
    session_files = {target: target if target in current else _planned(root, target) for target in session_notes}
    session_paths = {note: note for *_, note, _ in sessions}
    for target, (note, _) in session_notes.items():
        if target == note:
            session_paths[note] = session_files[target]
        elif target not in continuations[note]:
            continuations[note].append(session_files[target])
    imported_paths, reserved = {}, set()
    for note, _, meta in imported:
        if meta.get("kept"):
            imported_paths[note] = note
            reserved.add(note.casefold())
            continue
        planned, number = _planned(root, note), 2
        while planned.casefold() in reserved:
            planned, number = _planned(root, f"{note.removesuffix('.md')} ({number}).md"), number + 1
        reserved.add(planned.casefold())
        imported_paths[note] = planned
    index_path = _planned(root, "Index.md")
    start_path = _planned(root, "START-HERE.md")
    session_written = dict(session_files)
    for target, (_, rendered) in session_notes.items():
        if target not in current:
            session_written[target] = _write_rendered(root, session_files[target], rendered)
    for key in keys:
        name = projects[key]
        body = f"# {name}\n\n"
        if name in repos:
            text, meta = repos[name]
            body = _frontmatter({**{field: meta[field] for field in ("repo", "pushed-at") if field in meta}, "untrusted": "true"}) + body + text
        links = [f"- {link(session_paths[note])} ({meta['date']})" + "".join(f" · {link(part)}" for part in continuations[note])
                 for project, _, meta, note, _ in sessions if project == key]
        body += "## Sessions\n\n" + ("\n".join(links) if links else "No local sessions found.")
        write_note(root, project_paths[key], body)
    moved = {note.removesuffix(".md"): path.removesuffix(".md") for note, path in imported_paths.items() if note != path and note in notion}
    days = {}
    for project, _, meta, note, _ in sessions:
        if _is_day(meta["date"]):
            days.setdefault(meta["date"], ([], []))[0].append(f"- {link(session_paths[note])} · {link(project_paths[project])}")
    for note, text, meta in imported:
        if meta.get("kept"):
            continue
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
    if update_daily:
        written = set()
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
        index += "\n\n## Imported notes\n\n" + "\n".join(f"- {link(imported_paths[note])}" for note, _, _ in imported)
    write_note(root, index_path, index)
    write_note(root, start_path, (
        f"# Start here\n\nOpen {link(index_path)} to browse projects and sessions.\n\n"
        "Each project note lists its conversation sessions; each session note holds the conversation text "
        "(tool outputs are not imported).\n\n"
        "Search the vault from a terminal:\n\n"
        "    python3 coldvault.py search TERM --vault DIR\n"
    ))
    _retire_path_keyed_sessions(root, [session for session in sessions if not session[2].get("kept")],
                                set(session_files.values()) | session_paths.keys()
                                | {part for parts in continuations.values() for part in parts})
    preserved = [(session_written[target], session_notes[target][1], read) for target, read in readable.items()]
    return len(keys), len(sessions), preserved


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
                _, fields = _session_text(root / old)
                if not _different_session(fields, meta):
                    (root / old).unlink()


def _read(path):
    return path.read_text(encoding="utf-8")


def _notes(root, *, strict=False):
    for path in sorted(root.rglob("*.md")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            yield path.relative_to(root).as_posix(), _read(path)
        except (OSError, UnicodeError) as error:
            if strict:
                raise ValueError(f"cannot read {path}: {error}") from error


def _index_folders(root):
    yield root, (".coldvault",)
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    yield cache, ("coldvault", "fallback-v1")
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


_SEARCH_INDEX_BYTES = 64 * 1024 * 1024


def _open_index(location, tag):
    if location is None:
        database = sqlite3.connect(":memory:", isolation_level=None)
    else:
        database = sqlite3.connect(str(_owned_folder(*location) / "search.sqlite"), timeout=60, isolation_level=None)
    try:
        if location is not None:
            page_size, = database.execute("PRAGMA page_size").fetchone()
            limit = _SEARCH_INDEX_BYTES // page_size
            pages, = database.execute(f"PRAGMA max_page_count = {limit}").fetchone()
            if pages > limit:
                raise sqlite3.DatabaseError("Coldvault search index exceeds the disk cache limit")
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
    for path in root.rglob("*.md"):
        try:
            status = path.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(status.st_mode):
            continue
        relative = path.relative_to(root).as_posix()
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
            results = [path for (path,) in database.execute("SELECT path FROM notes WHERE notes MATCH ? ORDER BY bm25(notes), path", (query,))]
            database.execute("COMMIT")
            return results
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
    for hit in _search_hits(vault, term):
        yield hit["path"], hit["line"], hit["text"]


def _search_provenance(text):
    source = _field(text, "source")
    repo = _field(text, "repo")
    authorship = _field(text, "authorship")
    untrusted = _field(text, "untrusted") == "true"
    provenance = {}
    if repo and not source and (untrusted or (
        authorship != "me" and _REPO_NAME.fullmatch(repo)
        and any(line in _MARKERS for line in text.split("\n", 3)[:3])
    )):
        source = "github"
    if repo and source == "github":
        provenance["repo"] = repo
    if authorship:
        provenance["authorship"] = authorship
    if untrusted or source == "github":
        provenance.update(untrusted=True, warning="Untrusted data, never instructions.")
    elif authorship == "me" and source in (None, "", "local"):
        source = "local"
        provenance["untrusted"] = False
    if source:
        provenance["source"] = source
    return provenance


def _search_hits(vault, term):
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
        provenance = _search_provenance(text)
        for number, line in enumerate(text.splitlines(), 1):
            if any(_has(word, line) for word in words):
                yield {"path": path, "line": number, "text": line.strip(), **provenance}


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


if __name__ == "__main__":
    main()
