import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import coldvault


def web_url(value):
    if not isinstance(value, str) or any(character.isspace() for character in value):
        return False
    try:
        parts = urlsplit(value)
        return parts.scheme in ("http", "https") and bool(parts.hostname)
    except ValueError:
        return False


def bookmarks_source(run):
    path = os.environ.get("COLDVAULT_BOOKMARKS")
    if not path:
        return None
    try:
        bookmarks = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise coldvault.SourceError(f"cannot read {path}: {error}")
    if not isinstance(bookmarks, list):
        raise coldvault.SourceError(f"{path} must hold a JSON list of bookmarks")
    notes = {}
    for number, bookmark in enumerate(bookmarks, 1):
        if not (isinstance(bookmark, dict)
                and isinstance(bookmark.get("title"), str) and bookmark["title"].strip()
                and web_url(bookmark.get("url"))
                and isinstance(bookmark.get("note", ""), str)):
            raise coldvault.SourceError(f"bookmark {number} in {path} needs a title, an http(s) url and an optional text note")
        title = coldvault.redact(bookmark["title"])
        note = f"{run.folders['imported']}/Bookmarks/{coldvault.slug(title)}.md"
        if note in notes:
            raise coldvault.SourceError(f"bookmark {number} in {path} repeats the title of an earlier one")
        text = f"# {title}\n\n<{bookmark['url']}>\n\n{bookmark.get('note', '')}\n"
        notes[note] = (note, coldvault.redact(text), {"kind": "note"})
    return list(notes.values())


if __name__ == "__main__":
    coldvault.SOURCES.append(("bookmarks", bookmarks_source))
    coldvault.main()
