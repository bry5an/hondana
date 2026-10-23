# Running Hondana as a server

`uv run cli.py serve` is all a laptop needs. For a library the whole household (and their
phones) can reach at any time, run it on an always-on machine as a service.

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

Phones find a Mac or Linux box by its mDNS name (`<hostname>.local`) on most home networks.
Hondana for iPhone accepts `name.local:8000`, an IP address, or an `https://` address.

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
in [configuration.md](configuration.md#files-under-data), doesn't. `cli.py push` copies it into a
private git repo and commits:

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
`diff` its files against `data/`. Also back up your EPUB and audiobook folders themselves.

## Security

Hondana has **no login**. Anyone who can reach the port can read, download, edit and delete. Keep
it on your home network:

- Don't forward the port or expose it to the internet.
- For access away from home, use a VPN to your network (WireGuard, Tailscale, …). Hondana for
  iPhone allows plain `http://` only to local names and addresses (`name.local`, `192.168.…`), so
  a VPN host name such as a Tailscale `*.ts.net` name needs `https://` (a reverse proxy with a
  certificate).
- If you ever need it on the internet, put an authenticating reverse proxy in front (Caddy with
  basic auth, for example). The app itself needs no change.
