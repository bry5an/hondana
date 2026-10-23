# Configuration

Hondana reads one TOML file for its paths and settings, plus an optional `.env` for secrets.
Start from the template:

```bash
cp config.example.toml config.toml
```

All paths may be absolute or relative to the repo root.

## Which file is used

The first that applies wins:

1. `--config PATH` on the command line (`uv run cli.py --config PATH serve`), or the
   `EPUB_LIBRARY_CONFIG` environment variable.
2. `config.<env>.toml`, where `<env>` is `EPUB_LIBRARY_ENV` (default `dev`), if it exists. Useful
   when one checkout serves two setups, e.g. `config.dev.toml` on a laptop.
3. `config.toml`.

If none exists, every command stops and says which file to create.

## Keys

| Key | Default | What it is |
|---|---|---|
| `epub_dir` | `./books` | The folder of `.epub` files, scanned recursively. Books added on **Add** are filed into its `Japanese/` or `English/` subfolder. |
| `data_dir` | `./data` | Where every generated index, cover and per-profile file is written. Back it up (see [running-a-server.md](running-a-server.md#backups)). |
| `audiobook_dirs` | `[]` | Folders scanned for audiobooks: `.m4b` files, or a folder of `.mp3`s as one book. The first entry is the *canonical root* that `consolidate` moves files into. |
| `audiobook_data` | `./data/audiobook_catalog_base.json` | Optional base layer of the audiobook catalog, keyed by Audible ASIN. Leave it absent to start empty (see [audiobooks.md](audiobooks.md)). |
| `audiobook_natively` | `./data/natively_levels.json` | Natively levels for the audiobook catalog, written by `enrich-audiobooks`. |
| `dicts_dir` | `<data_dir>/dicts` | Your Yomitan dictionary zips, served to Hondana for iPhone. |
| `host` | `0.0.0.0` | Bind address. `0.0.0.0` makes the server reachable from your phone; `127.0.0.1` only from this machine (use that behind a reverse proxy). |
| `port` | `8000` | Port. |
| `[discovery] enabled` | `true` | Announce the server over Bonjour (`_hondana._tcp`) so Hondana for iPhone lists it. Set `false` to stop. |
| `[discovery] advertise_port` | unset | The port to announce. Unset announces `port`, but nothing at all when `host` is `127.0.0.1`. Behind a reverse proxy set it to the proxy's port (e.g. `80`). |
| `[discovery] advertise_host` | unset | The host name your reverse proxy answers to (e.g. `books.local`), sent in the advert as TXT `host`. Needed when the proxy refuses any other `Host` header, which is what the advert's own name would be. |
| `[natively] delay` | `0.4` | Seconds between learnnatively.com requests. Please keep it polite. |
| `[natively] min_score` | `0.55` | Minimum title similarity (0–1) to accept a Natively match. |
| `[pairing] min_score` | `0.5` | Not used: pairing's thresholds are fixed in `epublib/pairing.py`. Kept so old configs still load. |
| `[xref] min_score` | `0.62` | Minimum title/series similarity to link an audiobook to an EPUB. |
| `[alignment] subplz_cmd` | `""` | The command that aligns one audio file with a book's text, for audiobook ↔ text sync. See [audio-sync.md](audio-sync.md). Empty on a server without a GPU. |
| `[data_mirror] publish` | `false` | Whether `cli.py push` commits your hand-made data to a private git repo. Turn it on only on the machine that holds the real library. |
| `[data_mirror] path` | unset | The local clone of that repo; required when `publish` is true. See [running-a-server.md](running-a-server.md#backups). |

A wrong path doesn't fail loudly: a missing `audiobook_data` or an `audiobook_dirs` entry that
isn't mounted just looks like an empty catalog. After changing paths, check that the Audiobooks
page (or `GET /api/audiobooks`) isn't empty.

## `.env` (secrets)

`config.load()` reads `KEY=VALUE` lines from a `.env` at the repo root, if there is one,
without overriding variables already set. Copy `.env.example`. The key is optional:

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
- `alignments/*.srt`: audiobook transcripts that took hours to make.

`cli.py push` copies all of these to a private git repo (see
[running-a-server.md](running-a-server.md#backups)).

Developers: `CLAUDE.md` has the full table of `data/` files and which code writes each.
