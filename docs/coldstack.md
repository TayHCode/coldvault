# Importing ColdStack output (optional)

Coldvault doesn't need any other ColdStack tool, and it has no code that knows about them. If you use Coldfix, Coldpass, Coldverify or Coldcontribute, keep the Markdown they produce in ordinary folders and import those folders with `--notes FOLDER`, the same way you import any other notes.

Only `.md` files are imported. Save JSON or plain-text output as a `.md` file first.

## 1. Keep one folder per tool

Use a different folder name for each tool, because each folder lands under `Imported/<folder name>/`:

    mkdir -p ~/coldstack-notes/coldfix-lessons ~/coldstack-notes/coldverify-reports ~/coldstack-notes/coldpass-reports ~/coldstack-notes/coldcontribute-history

- **Coldfix lessons:** export each lesson as Markdown into the folder:

      coldfix export --lesson LESSON_ID --output ~/coldstack-notes/coldfix-lessons/LESSON_ID.md

- **Coldverify reports:** a strict run that gets past its preflight checks writes `.cold/verify-receipt.md` in the repository it checked, replacing the previous one. Other runs leave the old receipt in place, so check it is from the run you want, then copy it under a dated name:

      cp .cold/verify-receipt.md ~/coldstack-notes/coldverify-reports/"$(date +%Y-%m-%d-%H%M)-REPO.md"

- **Coldpass reports:** copy each report you want to keep, as a `.md` file, into `~/coldstack-notes/coldpass-reports/`.
- **Coldcontribute history:** save the report it prints as a dated note:

      ./coldcontribute report > ~/coldstack-notes/coldcontribute-history/"$(date +%Y-%m-%d)".md

## 2. Import the folders

Pass each folder with `--notes`, next to your usual options:

    python3 coldvault.py --output ~/ColdvaultVault \
      --notes ~/coldstack-notes/coldfix-lessons \
      --notes ~/coldstack-notes/coldverify-reports \
      --notes ~/coldstack-notes/coldpass-reports \
      --notes ~/coldstack-notes/coldcontribute-history

The notes appear under `Imported/coldfix-lessons/`, `Imported/coldverify-reports/` and so on (`Imported` is the default name; see [customize.md](customize.md#folders) to rename it), linked from the index and searchable with `search`. Each one is listed in the `Daily/` note for its `imported-at` date: the day it was first imported, or the date in the note's own `imported-at` field if it has one. A note whose `imported-at` field isn't a valid date is left out of `Daily/` until you fix or remove the field.

The index lists only the folders passed on that run, so pass all of them every time. To save them once instead, run `setup` and enter the folders when it asks for Markdown folders. `sync --vault ~/ColdvaultVault` then imports them on every run, including scheduled ones.

To stop importing a tool's output, leave its folder out of the command, or, if you saved it with `setup`, run `setup` again without it. Notes already imported stay in the vault until you delete them. There is nothing else to turn off.
