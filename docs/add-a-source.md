# Add a source

A source is one function that returns the notes it wants in the vault. Coldvault ships five (transcripts, notes, Notion, ChatGPT and Claude.ai exports, and GitHub) in the `SOURCES` list in `coldvault.py`. This guide adds another, a bookmarks file, without editing `coldvault.py`. The example, its fixture and its test live in this repository:

- `examples/sources/bookmarks.py`: the source and a script that runs Coldvault with it
- `examples/sources/bookmarks.json`: a synthetic bookmarks file
- `tests/test_add_a_source.py`: the test

## The contract

A source is `fn(run)` and returns one of:

- `None` when it has nothing to do this run (for example, it isn't configured). The run records the source as `skipped`.
- A list of `(path, text, metadata)` tuples, one per note. The run records `ok` with the count.

Or it raises `coldvault.SourceError("reason")` when it can't do its job. The run records `failed (reason)`, exits 3, and still writes the notes from the other sources.

- `path` is the note's path inside the vault, using `/`. Put imported notes under `run.folders['imported']`, so a renamed folder (see [customize.md](customize.md)) applies to them. Build file names with `coldvault.slug(...)`, so they can't escape their folder.
- `text` is the Markdown body. Coldvault adds the frontmatter. Pass anything a person typed through `coldvault.redact(...)`, the same redaction the other sources use.
- `metadata` is `{"kind": "note"}` for an imported note. It's counted, listed in the index and in `Daily/`, and kept or refreshed like any other generated note. The other kinds (`session`, `repo`) need fields only the built-in sources fill in, so use `note`.

`run` carries `run.root` (the vault folder), `run.folders` (the folder names) and the options of the run. Read your own input from somewhere you choose. The example uses an environment variable.

## The example

```python
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
```

It reads `COLDVAULT_BOOKMARKS`. With the variable unset it returns `None`. It checks every bookmark before writing any note: each needs a non-empty `title`, an `http(s)` `url` with a host name and no spaces, and an optional text `note`. The title is redacted before it becomes the file name, so a pasted token can't end up in a path or link. Two titles that would give the same file name are refused rather than silently merged. Any problem raises `SourceError` naming the file and the bookmark number.

The fixture it is tested with:

```json
[
  {"title": "Raised bed soil mix", "url": "https://example.com/garden/soil-mix", "note": "One third compost, one third peat-free mix, one third grit."},
  {"title": "Tomato spacing", "url": "https://example.org/tomatoes", "note": "45 cm between plants."}
]
```

## Run it

Copy `bookmarks.py` into the folder that holds `coldvault.py`, so it can import it, then run it instead of `coldvault.py`. It takes the same options, and the saved `sync --vault` setup works too:

    cp examples/sources/bookmarks.py .
    COLDVAULT_BOOKMARKS=examples/sources/bookmarks.json python3 bookmarks.py --username '' --transcripts examples/transcripts --output ./BookmarksVault

Each bookmark becomes `Imported/Bookmarks/<title>.md` and is listed in `Index.md`. `python3 coldvault.py status --vault ./BookmarksVault` shows `bookmarks: ok (2)`. To run it on a schedule, use the cron or launchd lines in [customize.md](customize.md#schedule) with `bookmarks.py` in place of `coldvault.py`, and set `COLDVAULT_BOOKMARKS` in the same line.

## Test it

The test runs the script end to end against the fixture, then checks the skipped, failed and redacted cases. From the repository root:

    python3 -m unittest tests.test_add_a_source

Copy it as the starting point for your own source's test. Keep fixtures synthetic: never commit real bookmarks, notes or tokens.
