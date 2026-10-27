# Configuration

Hondana runs with no configuration at all: everything has a default. To change something,
set it in one of these places. Each one overrides the ones above it:

1. **Built-in defaults.** Folders go under Hondana's home folder (below).
2. **`config.toml`**, a TOML file for the paths and settings you want to pin down. Start from
   the template: `cp config.example.toml config.toml`.
3. **Settings saved from the web app**, kept in `<data_dir>/settings.json`.
4. **Environment variables** named `HONDANA_<KEY>`, e.g. `HONDANA_EPUB_DIR` or
   `HONDANA_NATIVELY_DELAY` (a table key's dot becomes `_`). Handy for Docker and systemd.

`uv run cli.py config` prints every setting, its value and which of these it came from.

## The Settings page

**Settings** in the sidebar changes the folders, network, online lookups and thresholds without
touching a file. Each field says where its current value comes from. **Reset** drops what was
saved there, going back to the config file's value or the default. Saving a library folder starts
a scan. Host, port and discovery changes wait for a restart; under Docker, systemd or the desktop
app the page offers a **Restart now** button.

Some values can't be changed there, and the page shows them locked:
- **Set by an environment variable.** The environment wins; remove the variable first.
- **Config-file only.** The SubPlz command (the server runs it), the backup settings, and the
  data folder itself (the saved settings live in it).

The same page sets the optional **admin password**, which keeps Settings, Admin and changes to
the library to whoever knows it (see [running-a-server.md](running-a-server.md#security)).

## Which file is used

The first that applies wins:

1. `--config PATH` on the command line (`uv run cli.py --config PATH serve`), or the
   `HONDANA_CONFIG` (or older `EPUB_LIBRARY_CONFIG`) environment variable. A file named this way
   must exist.
2. In a git checkout: `config.<env>.toml`, where `<env>` is `EPUB_LIBRARY_ENV` (default `dev`), if
   it exists, then `config.toml`. Useful when one checkout serves two setups. The variable may be
   set in the checkout's `.env` (see below), so every command picks the same file.
3. `config.toml` in Hondana's home folder.
4. No file: defaults only.

Relative paths in a config file are relative to **the file's own folder**.

## The home folder

Defaults live under one folder:

| | Home folder |
|---|---|
| `HONDANA_HOME` is set | that folder |
| A config file was found | the file's folder (a checkout's `config.toml` → the checkout) |
| macOS | `~/Library/Application Support/Hondana` |
| Windows | `%LOCALAPPDATA%\Hondana` |
| Linux | `$XDG_DATA_HOME/hondana`, i.e. `~/.local/share/hondana` |

Under it: `data/` (the `data_dir`), `books/` (the `epub_dir`) and `audiobooks/` (the one
`audiobook_dirs` entry), plus an optional `config.toml` and `.env`. Hondana creates these three
folders when it uses them as defaults. It never creates a folder you configured yourself, so a
drive that isn't mounted shows up as missing rather than as an empty library.

## Keys

| Key | Default | What it is |
|---|---|---|
| `epub_dir` | `<home>/books` | The folder of `.epub` files, scanned recursively. Books added on **Add** are filed into its `Japanese/` or `English/` subfolder. |
| `data_dir` | `<home>/data` | Where every generated index, cover, per-profile file and saved setting is written. Back it up (see [running-a-server.md](running-a-server.md#backups)). Can't be changed from the web app. |
| `audiobook_dirs` | `["<home>/audiobooks"]` | Folders scanned for audiobooks: `.m4b` files, or a folder of `.mp3`s as one book. The first entry is the *canonical root* that `consolidate` moves files into. In an environment variable, separate folders with `:` (`;` on Windows) or give a JSON list. |
| `[inbox] dir` | unset (off) | A folder to drop new EPUBs and audiobooks into. They are filed once they've stopped changing for a minute; see [the inbox folder](audiobooks.md#the-inbox-folder). Can't be inside the EPUB or audiobook folders. Applies at once. |
| `audiobook_data` | `<data_dir>/audiobook_catalog_base.json` | Optional base layer of the audiobook catalog, keyed by Audible ASIN. Leave it absent to start empty (see [audiobooks.md](audiobooks.md)). |
| `audiobook_natively` | `<data_dir>/natively_levels.json` | Natively levels for the audiobook catalog, written by `enrich-audiobooks`. |
| `dicts_dir` | `<data_dir>/dicts` | Your Yomitan dictionary zips, served to Hondana for iPhone. |
| `host` | `0.0.0.0` | Bind address. `0.0.0.0` makes the server reachable from your phone; `127.0.0.1` only from this machine (use that behind a reverse proxy). |
| `port` | `8000` | Port. |
| `[discovery] enabled` | `true` | Announce the server over Bonjour (`_hondana._tcp`) so Hondana for iPhone lists it. Set `false` to stop. |
| `[discovery] advertise_port` | unset | The port to announce. Unset announces `port`, but nothing at all when `host` is `127.0.0.1`. Behind a reverse proxy set it to the proxy's port (e.g. `80`). |
| `[discovery] advertise_host` | unset | The host name your reverse proxy answers to (e.g. `books.local`), sent in the advert as TXT `host`. Needed when the proxy refuses any other `Host` header, which is what the advert's own name would be. |
| `[discovery] name` | the hostname | The name Hondana for iPhone lists ("Hondana" in a container, where the hostname is a random id). |
| `[discovery] advertise_addresses` | this machine's LAN addresses | IPv4 addresses to announce, when the automatic choice is wrong (Docker, several networks). In the environment: comma-separated. |
| `public_url` | unset | The address phones should use, shown as the QR code on **Profile**, e.g. `http://192.168.1.20:8000` for Docker's bridge network, or an `https://` Tailscale name. See [networking.md](networking.md). |
| `[audible] marketplace` | `jp` | The Audible store your audiobooks are bought from: `jp`, `us`, `uk`, `de`, `fr`, `ca`, `au`, `in`, `it` or `es`. It decides where book details are looked up by ASIN, where **Find on Audible** searches, and which site the Audible links open. Applies at once. |
| `[natively] delay` | `0.4` | Seconds between learnnatively.com requests. Please keep it polite. |
| `[natively] min_score` | `0.55` | Minimum title similarity (0–1) to accept a Natively match. |
| `[pairing] min_score` | `0.5` | Not used: pairing's thresholds are fixed in `epublib/pairing.py`. Kept so old configs still load. |
| `[xref] min_score` | `0.62` | Minimum title/series similarity to link an audiobook to an EPUB. |
| `google_books_api_key` | unset | Google Books API key for genres and descriptions by ISBN (also `GOOGLE_BOOKS_API_KEY`, see below). |
| `[alignment] transcribe_cmd` | `""` | The command that transcribes one audio file (SubPlz `gen`), for audiobook ↔ text sync. It takes `{audio}` and `{out}` only: Hondana matches what was heard to the book itself, so it is used in place of `subplz_cmd` when both are set. Same rules as `subplz_cmd`: config file or environment only, a string or a TOML array. See [audio-sync.md](audio-sync.md). |
| `[alignment] subplz_cmd` | `""` | The command that aligns one audio file with a book's text, for audiobook ↔ text sync. See [audio-sync.md](audio-sync.md). Empty on a server without a GPU. The server runs this command, so it can only be set in the config file or the environment, never from the web app. Written as a string it is split like a shell command line; written as a TOML array of arguments it is used as is, which is what a Windows path with backslashes needs. |
| `[data_mirror] publish` | `false` | Whether `cli.py push` commits your hand-made data to a private git repo. Turn it on only on the machine that holds the real library. |
| `trusted_origins` | `[]` | Extra browser origins (`https://books.example`) allowed to make changes, for a proxy that doesn't forward `Host`. Config file or environment only. |
| `[data_mirror] path` | unset | The local clone of that repo; required when `publish` is true. See [running-a-server.md](running-a-server.md#backups). |

A wrong path doesn't fail loudly: a missing `audiobook_data` or an `audiobook_dirs` entry that
isn't mounted just looks like an empty catalog. After changing paths, check that the Audiobooks
page (or `GET /api/audiobooks`) isn't empty.

## `.env` (secrets)

Hondana reads `KEY=VALUE` lines from a `.env` in the checkout and then in its home folder, if
there are any, without overriding variables already set (so the checkout's wins over the home
folder's, as its config file does). The checkout's is read first because it can name the config
file itself (`EPUB_LIBRARY_ENV`, `HONDANA_CONFIG`, `HONDANA_HOME`). Copy `.env.example`. The key
is optional:

| Variable | Used by |
|---|---|
| `GOOGLE_BOOKS_API_KEY` | The `epub-catalog` stage, which fills in a genre and description by ISBN for EPUBs that lack them. Without a key the stage does nothing (keyless access shares an exhausted quota). Get one free from a Google Cloud project with the Books API enabled. |

## Files under `data/`

Everything under `data/` belongs to this machine and is ignored by git. Most of it is generated
and can be rebuilt by running the pipeline again. These files are **hand-made** and can't be
rebuilt, so back them up:

- `profiles.json` and everything in `users/<profile>/`: each reader's known words, read books,
  learning words, lookups and reading positions.
- `*_overrides.json`: your corrections to titles, authors, series, genres, languages, covers,
  editions and audiobook links, plus the hand-set cover images in `covers/custom/`.
- `audiobook_hidden.json`: audiobooks you removed from the catalog.
- `audiobook_catalog_base.json` and `natively_levels.json`, if you have them. They can be only
  partly rebuilt.
- `alignments/*.srt` and `alignments/*.anchors.jsonl`: audiobook transcripts that took hours to make,
  and the sync fixes made on the phone.

`cli.py push` copies all of these to a private git repo, and `cli.py export --curated` writes the same
files to a zip (see [running-a-server.md](running-a-server.md#backups)).

Developers: `CLAUDE.md` has the full table of `data/` files and which code writes each.
