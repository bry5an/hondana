# Audiobooks

Hondana lists every audiobook in its **catalog** and marks the ones whose files it found on disk
as local. A local audiobook can be downloaded from the web app, streamed or downloaded by Hondana
for iPhone, linked to its EPUB, and synced with the text ([audio-sync.md](audio-sync.md)).

## Getting audiobooks in

1. List the folders that hold them in `audiobook_dirs` ([configuration.md](configuration.md)).
   Hondana understands two shapes:
   - one `.m4b` file per book;
   - a folder of `.mp3` files (and no `.m4b`) as one book, played in file-name order.
2. Put the Audible **ASIN** in the file or folder name in square brackets, e.g.
   `[08] リアデイルの大地にて8 [B0CCN9M612].m4b`. That is how a file is matched to its catalog
   entry.
3. Run **Run full pipeline** on Admin (or `uv run cli.py all`).

`catalog` looks up every ASIN the catalog doesn't know in Audible Japan's public catalog API
(no login) and stores the row in `data/audiobook_catalog_extra.json`. The row carries the title,
author, narrator, series and volume, runtime, cover, description and a genre. An ASIN Audible has
no book for is asked about again after 14 days.

iOS can't play some Audible downloads as they are. See [Files iOS won't play](#files-ios-wont-play).

## The catalog

The catalog is two layers, read through `epublib/audiobook_catalog.py`:

- `data/audiobook_catalog_base.json`: an **optional** base, keyed by ASIN, for a catalog you
  curate by hand: for example every audiobook your Audible account owns, downloaded or not, with
  your own genres, series and volume numbers. A fresh install doesn't have one and doesn't need
  one. Scripts never write it.
- `data/audiobook_catalog_extra.json`: rows pulled by `catalog`, laid over the base field by
  field. Generated; re-running `catalog` rebuilds it.

Neither file is in git. Both are per machine, under `data/`.

A row looks like this (base-file shape; pulled rows have the same fields where Audible has them):

```json
"B0CCN9M612": {
  "title": "リアデイルの大地にて8",
  "author": "Ceez",
  "series": "リアデイルの大地にて",
  "order": "08",
  "new_path": "[Ceez] リアデイルの大地にて/[08] リアデイルの大地にて8 [B0CCN9M612].m4b",
  "cover": "https://m.media-amazon.com/images/I/51qtJc9TyzL._SL500_.jpg",
  "release_date": "2019-01-30",
  "genre": "ライトノベル",
  "runtime_length_min": "395",
  "description_full": "…",
  "narrators": "小田切 優衣"
}
```

| Field | Notes |
|---|---|
| `series` | Always set; a standalone book repeats its title. |
| `order` | Zero-padded volume number (`"01"`, `"08"`). |
| `new_path` | Where `consolidate` puts the file, relative to the canonical root. |
| `genre` | One of the catalog's genre words (ライトノベル, ミステリー・サスペンス, SF・ファンタジー, ホラー, 児童書, 時代小説, 文芸作品, …). EPUBs linked to the audiobook inherit it. |

Older base rows may be keyed `kikubon N`, `audiobook.jp N` or `dwango N` instead of an ASIN.

## Linking audiobooks to EPUBs

`xref` links each EPUB to its audiobook by title, series, volume number and author. A volume
number must agree, so volume 3 of the book never links to volume 4 of the audiobook. When it gets
one wrong, fix it on the book page: **Edit › Audiobook** picks the right catalog entry, or says
there is none. Your choice is kept in `data/audiobook_link_overrides.json` and applied after the
automatic links.

## Removing audiobooks from the list

A scrape of everything an account ever owned includes books you'll never open. There are two
separate actions, both in Edit mode on an audiobook series page:

- **Remove from library…** hides the row. No file is touched, and it can be undone. Hidden rows
  are kept in `data/audiobook_hidden.json`.
- **Delete file…** deletes the local file to reclaim disk space. This can't be undone. The row
  stays (as not local) unless you also hide it.

To prune in bulk, use **Admin › Prune audiobooks**: search, tick, Remove. A second list there
restores what you removed.

## Natively levels

[learnnatively.com](https://learnnatively.com) is a community site that grades Japanese books by
difficulty (roughly 1–60). Hondana shows the level as a sticker on covers and lets you sort by it.
It never decides Read Next: your own strain does.

- **EPUBs:** `enrich` (part of the full chain) looks up each Japanese book.
- **Audiobooks:** `enrich-audiobooks` looks up the catalog into `data/natively_levels.json`. It
  isn't part of the full chain, because the first run over a big catalog takes about an hour.

```bash
uv run cli.py enrich-audiobooks            # new entries, plus misses older than 14 days
uv run cli.py enrich-audiobooks --refresh  # re-fetch everything
```

Rules for both:

- A match is final.
- A miss is retried after 14 days.
- An entry with `match_type: "manual_url"` (set by hand) is never touched, not even by
  `--refresh`.
- Progress is saved every 50 lookups, so an interrupted run only needs re-running.
- Requests are spaced by `[natively] delay` (0.4 s). Please keep it that way: it's a small site.

**How a lookup works** (`epublib/natively.py`):
1. It searches Natively's autocomplete API
   (`https://learnnatively.com/api/autocomplete-api/?q=…&language=jpn`) with a cleaned title.
   Manga and comics are excluded.
2. It falls back to a series-level match if no volume matches.
3. It reads the ratings from the book page's embedded Next.js JSON, with `RATINGS_RE`.

**Ratings stopped appearing?** `RATINGS_RE` in `natively.py` is the first suspect: it matches
doubly-escaped JSON, and a site change breaks it. To check, compare it against a live page:

```bash
curl -s "https://learnnatively.com/book/SOMEID/" | grep -o 'reviewData[^}]*}' | head -1
```

**A book you know is on Natively isn't found?** Try the autocomplete API yourself and compare
its titles with the catalog's:

```bash
curl "https://learnnatively.com/api/autocomplete-api/?q=ハリーポッター&language=jpn"
```

Fields in `natively_levels.json` (and `natively_epub.json`):

| Field | Notes |
|---|---|
| `natively_level` | int or null. null = no match. |
| `natively_url` | The book page, or the series page for a series-level match. |
| `matched_title`, `confidence` | What it matched, and the title similarity (0–1). |
| `match_type` | `"item"` (this volume), `"series"` (the series only, a rougher level), `"manual_url"`, or null. |
| `overall_rating`, `language_learning_rating` (+ `_count`) | Community ratings; null when nobody rated it. |
| `fetched_at`, `query` | When and with what it was looked up. Entries from the old standalone scraper don't have these. |

## Consolidating folders

If your audiobooks are spread over several folders or drives, `consolidate` gathers them into
the **canonical root**, the first entry of `audiobook_dirs`, under
`[Author] Series/[NN] Title [ASIN].m4b`. It runs in two phases.

**1. Plan (always; touches nothing).** Each `.m4b` and each single-book `.mp3` folder is matched
to a catalog row, trying the most reliable method first:

1. the ASIN (or `[kikubon N]` key) in the file name;
2. an exact normalized title, only when exactly one catalog row has it;
3. a fuzzy title (≥ 0.90 and clearly ahead of the runner-up), reported separately; `--no-fuzzy`
   turns this off.

Every file is given one action, tallied in the report and written to
`data/consolidation_plan.json`:

| Action | Meaning | Moved by `--execute`? |
|---|---|---|
| `noop` | already where it belongs | — |
| `move-isbn` / `move-title` / `move-fuzzy` | matched; moves to its `new_path` | yes |
| `move-keep-layout` / `move-unsorted` | unmatched; keeps its `[Author]…` layout, else goes to `_unsorted/` | yes |
| `move-stray` | a `.pdf` or `.zip` next to its `.m4b` | yes |
| `duplicate` | the same size is already at the destination | no; the source stays where it is |
| `conflict` | something of a different size is at the destination | no; resolve it by hand |
| `ambiguous` | the title matches several catalog rows | no; reported only |
| junk (`._*`, `.DS_Store`) | counted | deleted only with `--clean-junk` |

**2. Execute (`uv run cli.py consolidate --execute`).** Performs only the `move-*` actions.
- A move within one volume is a rename.
- A move across volumes copies to a `.part` file, checks the size (add `--verify-hash` for
  sha256), replaces atomically, and deletes the source last. An interrupted run never loses data,
  and re-running converges to all `noop`.
- Before starting it checks free space (plus a 5 GB margin).
- Afterwards it re-runs `audio-scan`.

Duplicates are never copied: delete those sources yourself once you've checked the consolidated
copies.

## Files iOS won't play

Hondana serves audiobook files as they are; it doesn't convert or decrypt them. Bring DRM-free
`.m4b` or `.mp3` files (tools such as Libation or OpenAudible export your own purchases that way).
If iOS refuses an `.m4b` whose container is laid out oddly, a lossless remux usually fixes it:
`ffmpeg -i in.m4b -c copy -movflags +faststart out.m4b`.
