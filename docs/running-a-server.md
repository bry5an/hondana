# Running Hondana as a server

`uv run cli.py serve` is all a laptop needs. For a library the whole household (and their
phones) can reach at any time, run it on an always-on machine as a service: with Docker
([docker.md](docker.md)), or from a checkout under systemd as below. How phones and other devices
find it (IP and QR code, `.local` names, your own DNS, a reverse proxy, Tailscale) is in
[networking.md](networking.md).

## As a systemd service (Linux)

`/etc/systemd/system/hondana.service`, with your user and paths:

```ini
[Unit]
Description=Hondana library
After=network.target

[Service]
User=<you>
WorkingDirectory=/home/<you>/hondana
ExecStart=/home/<you>/.local/bin/uv run cli.py serve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
cp config.example.toml config.toml         # then set the paths for this machine
sudo systemctl enable --now hondana
journalctl -u hondana -f                   # its log
```

Running as your own user (not root) keeps the repo and `data/` writable without sudo.

**Restart with `systemctl restart`, never by killing the process.** `ExecStart` runs `uv`, which
starts uvicorn as a child. Killing `uv` leaves uvicorn holding the port, so the restarted service
can't bind it and keeps failing while the old process goes on serving old code. If you must kill
something, kill whatever holds the port: `ss -ltnp 'sport = :8000'`.

## Behind a reverse proxy (optional)

To serve on port 80 under a friendly name, put nginx (or Caddy) in front, and set `host =
"127.0.0.1"` so only the proxy can reach the app. Two settings matter:

- **Upload size.** Add allows EPUBs up to 100 MB, but nginx's default body limit is 1 MB, which
  shows up as a 413 on Add. Set `client_max_body_size 110m;`. The Admin page's Dictionaries card
  uploads zips of tens of megabytes (Jitendex is ~40 MB), so allow at least that, or
  `client_max_body_size 0;` for that location, or copy the zips into `data/dicts/` by hand.
- **Big downloads.** Audiobooks and dictionaries are hundreds of megabytes, and the phone
  resumes them with `Range` requests. Don't set short timeouts or turn off range support.

Ready-made Caddy and nginx examples are in [networking.md](networking.md#4-a-reverse-proxy-a-name-without-the-port-optionally-https).

While it runs, the server also announces itself over Bonjour (`_hondana._tcp`), and the app lists it
in Settings › Server. The **Profile** page shows the address and a QR code as the fallback.
Behind nginx the app is bound to `127.0.0.1`, which can't be announced on its own: set the
proxy's port in `config.toml`:

```toml
[discovery]
advertise_port = 80
advertise_host = "books.local"     # only if the proxy serves just this Host name
```

## Updating

```bash
git pull --ff-only
uv sync                          # if pyproject.toml / uv.lock changed
sudo systemctl restart hondana   # if anything under epublib/ changed
```

Templates and files under `static/` are read from disk, so a change to them is live without a
restart. Browsers pick it up on the next page load, because every asset URL carries its file's
modification time. A pull that changes Python code *does* need the restart. Without it the new
pages call routes the old process doesn't have.

## Keeping the library fresh

Books added on Add (or from the phone) are processed right away. For files copied into
`epub_dir` or `audiobook_dirs` by hand, run **Admin › Run full pipeline**, or schedule it, e.g.
nightly:

```cron
30 3 * * *  cd /home/<you>/hondana && /home/<you>/.local/bin/uv run cli.py all >> data/logs/nightly.log 2>&1
```

Don't run it while someone is running a stage from Admin: the two would race on the same files.

## Backups

Most of `data/` is generated and comes back by re-running the pipeline. The hand-made part, listed
in [configuration.md](configuration.md#files-under-data), doesn't. There are two ways to keep it, and
they work together: a **zip** you make whenever you like (and restore anywhere), and a **git mirror**
that a timer keeps current. Neither includes your EPUB and audiobook folders: back those up yourself.

### A zip: `export`

```bash
uv run cli.py export                       # data/backups/hondana-<date>.zip, everything
uv run cli.py export --curated             # only your hand-made data: a few MB
uv run cli.py export --out ~/usb/hondana.zip
```

- A **full** export is the hand-made data plus the slow-to-rebuild caches (vocabulary, reader packs,
  covers, the audiobook id history, dictionaries), so a restore is ready to use at once. Never
  included: the admin password and its key, locks, logs, the search index and temp files.
- A **curated** export is exactly the files `push` mirrors. Run `uv run cli.py all` after importing it
  to rebuild the rest.
- The default place, `data/backups/`, is on the same disk as the data, so it doesn't survive that
  disk: copy the zip somewhere else, or write it there with `--out`.
- It prints what the zip holds (books, audiobook files, catalog rows, and per profile the known words,
  books read, positions, bookmarks): keep that line to compare with after a restore.
- It is safe to run while the server is up. Each file is consistent, but they're read one after another,
  so stop the server first for a copy that is consistent as a whole.
- `settings.json` is in it, because it holds your Settings-page choices, but a restore doesn't apply it
  unless you say so (below): it names folders on the machine it came from.

### Restoring and moving a library

```bash
uv run cli.py import ~/usb/hondana.zip            # a dry run: says what would happen, writes nothing
uv run cli.py import ~/usb/hondana.zip --apply
```

The source can be a zip from `export`, a **data folder** (an old `data/` you copied, or another
checkout's), a **checkout** that has a `data/` in it, or a **clone of your git mirror**
(`git clone <your backup repo> ~/hondana-backup`, then `import ~/hondana-backup --apply`; that holds
only hand-made data, so run `hondana all` afterwards).

- **Stop the server first.** An import refuses while a server (or the desktop app) is running on the
  folder, and a server refuses to start while an import is.
- A folder that already holds your own work (known words, read marks, overrides, hand-set covers…) is
  **refused**, so a mistake can't overwrite it. `--replace` replaces it: it first saves a full export
  of what is there to `data/backups/before-import-<time>.zip`, then clears everything except
  `backups/`, `logs/`, the admin password and settings. To undo, `import` that file with `--replace`.
- Profile folders the source's `profiles.json` doesn't list (leftovers of an old registry) are left
  out, by name.
- `settings.json` isn't imported unless you pass `--settings`; without it the dry run and the
  import print its values so you can set them by hand.
- When it is done, `--rebuild index` (the default) refreshes the indexes (`audio-scan`, which also
  upgrades an older audiobook index, then `build` and the known-words figures), `--rebuild all` runs
  every stage (slow, and it fetches from Natively and the like), and `--rebuild none` stops there.
  It ends by comparing every count with the source's and listing any difference.
- A zip is checked before anything is written: every path must stay inside the folder, links are
  refused, the zip may hold nothing its manifest doesn't list, and every file's size and sha256 must
  match. A damaged or tampered zip is refused whole.

**On a new machine:** install Hondana, put your EPUB and audiobook folders back (the same paths
under their folders keep your read marks and corrections attached: a book's id comes from its path
and size, and a rename is carried over by the next `scan`), set the folders in `config.toml` or on
the Settings page, then `import … --apply`, start the server, and compare the counts.

**Docker:** `docker exec hondana hondana export` writes into the data volume (`/data/backups/`). To
restore, stop the container first, then run the import in a one-off one on the same volumes:
`docker compose stop && docker compose run --rm hondana hondana import /data/backups/<file>.zip --apply`.

**The desktop app:** quit it, then run the same commands with the console build: `hondana-cli.exe export`
or `hondana-cli.exe import C:\path\to\hondana.zip --apply` on Windows (it sits next to `hondana.exe`),
and `/Applications/Hondana.app/Contents/MacOS/Hondana export` on macOS.

### A git mirror: `push`

`cli.py push` copies your hand-made data into a private git repo and commits, which suits a timer:

1. Create a **private** repo (it holds your reading history and word lists) and clone it on the
   server, e.g. `git clone git@github.com:<you>/hondana-backup.git ~/hondana-backup`. The server's
   git needs push access to it (an SSH deploy key with write access works).
2. In `config.toml`:

   ```toml
   [data_mirror]
   publish = true
   path = "/home/<you>/hondana-backup"
   ```

3. Run `uv run cli.py push` and check the commit landed: `git -C ~/hondana-backup log --oneline -1`.
4. Run it daily with a systemd timer. `/etc/systemd/system/hondana-backup.service`:

   ```ini
   [Unit]
   Description=Back up Hondana's hand-made data
   After=network-online.target
   Wants=network-online.target

   [Service]
   Type=oneshot
   User=<you>
   WorkingDirectory=/home/<you>/hondana
   ExecStart=/home/<you>/.local/bin/uv run cli.py push
   ```

   and `/etc/systemd/system/hondana-backup.timer`:

   ```ini
   [Unit]
   Description=Daily Hondana backup

   [Timer]
   OnCalendar=*-*-* 02:00:00
   Persistent=true

   [Install]
   WantedBy=timers.target
   ```

   then `sudo systemctl daemon-reload && sudo systemctl enable --now hondana-backup.timer`
   (`systemctl list-timers hondana-backup` shows the next run). A cron line running
   `uv run cli.py push` does the same job.

A backup you've never restored isn't a backup: clone the repo somewhere else now and then and
`diff` its files against `data/`, or [import it](#restoring-and-moving-a-library) into an empty folder
and check the counts it prints.

## Security

Out of the box Hondana has **no login**: anyone who can reach the port can read, download, edit
and delete. **Settings › Admin password** keeps changes to whoever knows it: Settings, Admin, and
adding, editing and deleting books then need it, while browsing, downloads, each reader's own
marks and the iPhone app stay open. A forgotten password is cleared on the server with
`uv run cli.py admin-password clear`. Scripts (such as `align-remote`) use a token from
**Settings › Script tokens**. Browsers can't make changes from another site's page either way.

Even with a password, keep it on your home network:

- Don't forward the port or expose it to the internet.
- For access away from home, use a VPN to your network (WireGuard, Tailscale, …). Hondana for
  iPhone allows plain `http://` only to local names and addresses (`name.local`, `192.168.…`), so
  a VPN host name such as a Tailscale `*.ts.net` name needs `https://`: `tailscale serve`
  provides it (see [networking.md](networking.md#5-away-from-home-tailscale)).
- If you ever need it on the internet, put an authenticating reverse proxy in front (Caddy with
  basic auth, for example): the admin password doesn't protect browsing and downloads.
- Behind a reverse proxy, forward the `Host` header (`proxy_set_header Host $host;` in nginx),
  or set `trusted_origins` in the config: changes from the browser are refused when the page's
  origin doesn't match the host the request arrived for.
