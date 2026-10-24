# Running Hondana in Docker

The image is `ghcr.io/bry5an/hondana`, built for `linux/amd64` and `linux/arm64` (a Raspberry Pi
4/5 or an ARM NAS works). Tags:

| Tag | What |
|---|---|
| `latest` | The newest release |
| `0.2`, `0.2.1` | A release line, or one exact release |
| `edge` | The tip of `main`: newest, least tested |

## Quick start

Copy [`docker/compose.yaml`](../docker/compose.yaml), point its volumes at your folders, and:

```bash
docker compose up -d
docker compose logs -f        # Ctrl-C to stop following
```

Open `http://<this machine>:8000`, then follow the **Getting started** card: run the pipeline on
**Admin**, then import your known words.

Or without compose:

```bash
docker run -d --name hondana -p 8000:8000 \
  -v ./config:/config -v ./data:/data \
  -v /path/to/EPUBs:/books -v /path/to/Audiobooks:/audiobooks \
  ghcr.io/bry5an/hondana:latest
```

## Folders

| In the container | What | |
|---|---|---|
| `/data` | Indices, covers, profiles, known words, saved settings | **Back this up.** The hand-made part can't be rebuilt ([configuration.md](configuration.md#files-under-data)) |
| `/books` | Your EPUBs | Books added on **Add Book** are written here, so it must be writable |
| `/audiobooks` | Your audiobooks | Read-only works, but then deleting an audiobook from the app fails |
| `/config` | An optional `config.toml` and `.env` | |

The image sets `HONDANA_DATA_DIR`, `HONDANA_EPUB_DIR` and `HONDANA_AUDIOBOOK_DIRS` to these
paths, so they show as set by the environment and can't be changed from the web app: change
which host folder you mount instead. To scan several audiobook folders, mount each one and set
`HONDANA_AUDIOBOOK_DIRS=/audiobooks:/audiobooks2`.

## Permissions

The container runs as user `1000:1000` by default. Run it as the user that owns your folders
instead, with `user:` in compose (`PUID`/`PGID`) or `docker run --user`:

```bash
id -u; id -g                       # your ids on the host
PUID=1001 PGID=100 docker compose up -d
```

If `/data` or `/books` aren't writable by that user, the server logs a permission error on the
first write. `sudo chown -R 1001:100 ./data` fixes an existing folder.

## Settings

Everything in [configuration.md](configuration.md) applies. In Docker the easy ways are:

- environment variables: `HONDANA_<KEY>`, e.g. `HONDANA_NATIVELY_DELAY: "1.0"`, or
  `GOOGLE_BOOKS_API_KEY`;
- a `config.toml` in the folder you mount as `/config`;
- the web app's settings, saved in `/data`.

`docker exec hondana hondana config` shows what is in effect and where each value came from.

## Your phone and the network

With the port mapping above (Docker's default bridge network), the container can't see your
network:

- **Bonjour doesn't reach your LAN**, so Hondana for iPhone won't find the server by itself.
- **The container doesn't know its address on your network**, so the QR code on **Profile** can
  only show the address you opened the page with.

Either:

- **Set `HONDANA_PUBLIC_URL`** to the address your phone should use (e.g.
  `http://192.168.1.20:8000`, or a name from [networking.md](networking.md)). The QR code uses
  it, and you type it once in the app.
- **Use host networking (Linux only)**: `network_mode: host` in compose, and drop `ports:`.
  Discovery and the QR code then work as on a normal install. Docker Desktop on macOS and
  Windows can't pass your LAN's multicast through, so there it has to be `HONDANA_PUBLIC_URL`.

For a friendly name, HTTPS or access away from home, see [networking.md](networking.md).

## Running pipeline commands

The web app's **Admin** page runs every stage. The command line works too:

```bash
docker exec hondana hondana all          # the full pipeline
docker exec hondana hondana --help
```

## Updating

```bash
docker compose pull && docker compose up -d
```

Your data in `/data` carries over. A new version that changes the vocabulary analysis
re-analyzes books on the next pipeline run.

## Building the image yourself

```bash
docker build -t hondana .
docker/smoke.sh hondana        # starts it on empty folders and analyzes two test books
```
