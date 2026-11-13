# Sync it to other devices

**Privacy warning:** each recipe below copies your conversations off this machine. Redaction replaces common credential formats, not every private thing you typed. Send the vault only to places that only you can read: a private repository, your own Obsidian Sync or iCloud account, or a private bucket. Use one of these per vault, not several at once.

`.coldvault/` holds only local caches and run records, so you can leave it out of what you sync.

**A private git repository.** Create an empty private repository on your git host, then push the vault to it. The search index in `.coldvault/` has its own `.gitignore`, so it is never committed.

    cd ~/ColdvaultVault
    git init -b main
    git add -A
    git commit -m "Vault update"
    git remote add origin https://github.com/YOUR-NAME/YOUR-VAULT.git
    git push -u origin main

After each rebuild, run `git add -A && git commit -m "Vault update" && git push` from the vault folder.

**Obsidian Sync.** This needs an Obsidian Sync subscription, a remote vault created in Obsidian, and Node.js for [Obsidian Headless](https://obsidian.md/help/sync/headless) (open beta). Don't also sync this folder with the Obsidian app on the same machine.

    npm install -g obsidian-headless
    ob login
    cd ~/ColdvaultVault
    ob sync-setup --vault "YOUR REMOTE VAULT"
    ob sync

**iCloud on a Mac.** Build the vault inside Obsidian's iCloud folder, which appears once Obsidian is installed with iCloud Drive turned on. The vault then opens in Obsidian on your iPhone and iPad. Use the same `--output` in your schedule.

    python3 coldvault.py --username '' --output "$HOME/Library/Mobile Documents/iCloud~md~obsidian/Documents/Coldvault"

**S3 with rclone.** Set up a remote for your bucket with `rclone config` (named `s3` here) and turn on bucket versioning, so an unwanted change can be undone. `rclone sync` makes the bucket folder match the vault, deleting files there that the vault no longer has.

    rclone sync ~/ColdvaultVault s3:YOUR-BUCKET/coldvault --exclude '.coldvault/**'

## Restore from the sync target

Copy the vault back from where you synced it into a new folder, then run Coldvault into it as usual. Coldvault recognizes the notes it wrote, so the run updates them in place instead of making `(Coldvault)` copies. The git and S3 recipes leave `.coldvault/` out, so pass your usual options or run `setup` again. From a private git repository:

    git clone https://github.com/YOUR-NAME/YOUR-VAULT.git ~/ColdvaultVault

From S3 with rclone (with bucket versioning on, you can also restore an older version of a single note there):

    rclone copy s3:YOUR-BUCKET/coldvault ~/ColdvaultVault

Then rebuild into it:

    python3 "$HOME/coldvault/coldvault.py" --username '' --output "$HOME/ColdvaultVault"

With Obsidian Sync, run the Obsidian Sync commands above in a new empty folder. With iCloud, the vault is already on every device signed in to your iCloud account. Both also sync `.coldvault/`, so build the vault on one machine only and just open it in Obsidian on the others. Run `setup` on a new machine only when it replaces the old one.
