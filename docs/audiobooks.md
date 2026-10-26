# Audiobooks

Hondana lists every audiobook in its **catalog** and marks the ones whose files it found on disk
as local. A local audiobook can be downloaded from the web app, streamed or downloaded by Hondana
for iPhone, linked to its EPUB, and synced with the text ([audio-sync.md](audio-sync.md)).

## Getting audiobooks in

1. List the folders that hold them in `audiobook_dirs` ([configuration.md](configuration.md)).
   Hondana understands these shapes:
   - one `.m4b` file per book;
   - a folder of `.mp3` files (and no `.m4b`) as one book, played in file-name order;
   - `.m4a`, `.flac`, `.opus` or `.ogg` files (in a folder with no `.m4b`), grouped by their
     album tag: one file is a book, several files with the same album are one book of tracks.

   Folders whose name starts with a dot, NAS housekeeping folders (`@eaDir`, `#recycle`) and
   unfinished downloads (`.part`, `.crdownload`, Safari's `.download`) are skipped.
2. Tell Hondana which book it is. Any one of these works, and the first one found wins:
   1. the Audible **ASIN** in the file or folder name in square brackets, e.g.
      `[08] リアデイルの大地にて8 [B0CCN9M612].m4b`;
   2. a match you made by hand with **Find on Audible** ([below](#matching-a-file-to-an-audible-book));
   3. an ASIN in the file's tags (Libation and Audible's own files carry one).

   A file with none of them still shows up, with the title, author, narrator, series, runtime and
   cover from its tags, or else a guess from its file and folder names.
3. Run **Run full pipeline** on Admin (or `uv run cli.py all`).

`catalog` looks up every ASIN the catalog doesn't know in Audible's public catalog API (no login;
the store is `[audible] marketplace`, Japan by default) and stores the row in `data/audiobook_catalog_extra.json`. The row carries the title,
author, narrator, series and volume, runtime, cover, description and a genre. An ASIN Audible has
no book for is asked about again after 14 days.

iOS can't play some Audible downloads as they are. See [Files iOS won't play](#files-ios-wont-play).

### Adding audiobooks from the browser

You don't have to copy files into the folder yourself. **Add Book → Audiobook** takes `.m4b`, `.m4a`,
`.mp3`, `.flac`, `.opus` or `.ogg` files, or a `.zip` holding the tracks of one book, and files them
for you:

- **It survives a bad connection.** A file goes up in 16 MB pieces. If the connection drops, or you
  close the page, nothing is lost: choose the same file again and it carries on where it stopped. A
  connection that goes silent is given up on after a minute and retried.
- **It is filed like the rest of the library**, in the first folder of `audiobook_dirs`, as
  `[Author] Series/[NN] Title [ASIN].m4b`. The name comes from the Audible catalog if the ASIN is
  known, else from the file's tags. Anything that isn't an `.m4b` gets a folder of its own, so two
  books of one series can never be mistaken for one.
- **A file with no ASIN and no title tag** waits in `_unsorted/` under its own name and is marked
  **Needs details** in Audiobooks, where you can fill them in or match it to Audible.
- **The same book twice** is noticed: the same ASIN with the same size is "Already in your library" and
  nothing is stored; the same ASIN with a different size is refused (delete the old file first), because
  only one file per book can be listed.
- **A zip is one book.** Its tracks are put in play order, and a cover image named `cover`, `folder` or
  `front` comes along. A zip with paths that climb out of its folder, links, a password, or a size
  out of all proportion to its contents is refused whole.
- Once the file is in, Hondana scans, looks up a new ASIN on Audible and links the book to its EPUB, the
  same steps as **Run full pipeline** for audiobooks.

If you set an admin password, adding audiobooks needs it. An upload you abandon is cleared after a week.
Behind a reverse proxy, see [the notes on upload limits](networking.md#4-a-reverse-proxy-a-name-without-the-port-optionally-https).

### The inbox folder

If you'd rather drop files into a folder than open the web app (a Libation export, a download
folder, a share your other computer can write to), set an **Inbox folder** in Settings
(`[inbox] dir` in the config file; unset means off). Hondana checks it every 30 seconds and files
what it finds the way **Add Book** would, for audiobooks and EPUBs alike:

| Drop this | It is |
|---|---|
| a `.epub` | filed into the `Japanese/` or `English/` folder of `epub_dir`, analyzed and linked |
| an `.m4b`, `.m4a`, `.mp3`, `.flac`, `.opus` or `.ogg` file | one audiobook, filed under `audiobook_dirs[0]` as above |
| a `.zip` of tracks | one audiobook, unpacked and filed as above |
| a folder of tracks (and a `cover.jpg`) | one audiobook |
| a folder tree such as Libation writes (`Author/Series/Title [ASIN]/Title [ASIN].m4b`) | every folder that holds audio is a book; several `.m4b` files in one folder are separate books |

Things to know:

- **Files are moved, not copied.** What is filed is gone from the inbox. The folder it came in is
  tidied away too, but only when all that's left is cover images and notes (`.jpg`, `.json`,
  `.cue`, `.txt`…); anything else, such as a PDF, keeps the folder where it is.
- **Nothing is touched while it's arriving.** A file is filed once its size and modification time have
  stayed the same for two checks in a row and it is at least a minute old, so a slow copy over the
  network is left alone. A folder with a download or copy in progress (`.part`, `.crdownload`,
  rsync's `.name.XXXXXX`) isn't filed until that's gone. Hidden files, symlinks and other kinds of
  file are ignored.
- **What can't be filed isn't lost.** A broken file, a zip that is refused, a book the library
  already has, an EPUB over 100 MB: each goes to `_failed/` inside the inbox with a
  `name.error.txt` saying why, and isn't tried again. Settings shows how many are waiting there. If
  the library's drive is full or not mounted the item stays in the inbox and is tried again.
- **A multi-disc book needs one folder or one zip.** Each folder that holds audio is a book, so
  `CD1/` and `CD2/` side by side would be two.
- **The inbox can't be inside a library folder**, or hold one. Settings refuses it.
- **Only point it at a folder meant for this.** Every EPUB, zip and audio file in it is taken.

Without the server running, `uv run cli.py inbox` files what is ready (and `cli.py all` does it
first), then scans. Run it from a timer if you want that instead of a watching server; don't run
it while a server has the same inbox. Docker: see [docker.md](docker.md#folders).

### Matching a file to an Audible book

A file with no ASIN in its name or tags is a guess from its tags or file name. To say which Audible
book it is, open its series page and choose **Find on Audible** (beside "Guessed from the file
name"), or turn on **Edit** and use the row's ⋯ menu.

1. The search is filled in from the file's title and author. Edit it and press **Search**, or paste
   an ASIN or an Audible link (`…/pd/Title/B0CCN9M612`).
2. Pick the book. Books another file here already is, are greyed out.
3. Optionally tick **Also add [ASIN] to the file name**. The file (or the book's folder) is renamed
   to `name [ASIN]`, so the match doesn't depend on Hondana's data folder and `consolidate` and other
   tools read it too. It is left alone if the new name is taken, or when a folder holds two albums.
4. **Match.**

The file's id becomes the ASIN, and everything kept under the old id goes with it: your listening
position, the text sync, hand-typed details and the cover. The Audible book's details are stored in
`data/audiobook_catalog_extra.json` unless the catalog already has them, and the match is kept in
`data/audiobook_assignments.json`. An EPUB that matches the book now links to it, and it can be
synced with the text.

- **Change it** by matching again. Only what belongs to the *file* moves to the new book (your
  position and the text sync); details, Natively level and links belong to the book they were made for.
- **Remove it** from the same dialog ("or remove the match"): the file goes back to what its tags and
  name say.
- A file whose name already has `[ASIN]` can't be matched, because the name always wins. Rename the file.
- The search goes to the store set in **Settings → Metadata → Audible store**. An ASIN belongs to
  one store, so pick the one your audiobooks come from. **Find on Audible** needs internet access and
  says so when Audible doesn't answer.
- A search that finds nothing is repeated without the author, then as plain keywords, which
  forgives a trailing "(series 1)" that Audible's title search does not.
- Not on Audible at all? **Enter the details by hand** opens the details form instead.

### What a file's tags give

Tags are read with [mutagen](https://mutagen.readthedocs.io) and cached in
`data/audiobook_tags.json`, so a rescan only reads files that changed.

| Field | Where it's read from |
|---|---|
| title | the title tag (a book of tracks: the album) |
| author | album artist, else artist |
| narrator | a narrator tag, else composer |
| series, volume | `SERIES` / `SERIES-PART` (MP4 freeform, ID3 `TXXX`, Vorbis), else the movement name and number |
| ASIN | `ASIN` (MP4 freeform, ID3 `TXXX`, Vorbis), or Audible's `CDEK` atom |
| description, year, genre | the usual tags; a genre of just "Audiobook" is ignored |
| cover | the embedded picture, else `cover.jpg`, `folder.jpg` or `front.jpg` beside a book that has its folder to itself |

### Moving and renaming files

Each audiobook keeps its id when you move, rename or re-tag the file, so its listening position,
links and hand edits stay with it. A scan recognises a file it has seen before by the same place;
else the same name and size; else the same tag title and length; else the same size and contents.
It only does so when exactly one file fits, so two copies of the same file are never guessed
between.

When a file without an ASIN later gets one (you add `[ASIN]` to its name, tag it, or match it),
its id becomes the ASIN and everything kept under the old id moves with it: listening positions,
the hidden list, EPUB links, audio sync and hand edits. Hondana for iPhone's position updates still
reach the book if they use the old id.

To see what a scan would change without changing anything:

```bash
uv run cli.py audio-scan --dry-run
```

## The catalog

The catalog is four layers, read through `epublib/audiobook_catalog.py`:

- `data/audiobook_catalog_base.json`: an **optional** base, keyed by ASIN, for a catalog you
  curate by hand: for example every audiobook your Audible account owns, downloaded or not, with
  your own genres, series and volume numbers. A fresh install doesn't have one and doesn't need
  one. Scripts never write it.
- `data/audiobook_catalog_extra.json`: rows pulled by `catalog`, laid over the base field by
  field. Generated; re-running `catalog` rebuilds it.
- A row for every local file neither of them knows, made from its tags or its file name. These
  rows are keyed by the audiobook's id and carry `source: "tags"` or `"filename"`. A row guessed
  from a file name is never linked to an EPUB automatically (you can still link it by hand).
- `data/audiobook_metadata_overrides.json`: details typed by hand, laid over any row.

None of the files is in git. All are per machine, under `data/`.

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

## Correcting an audiobook's details

Any audiobook's details can be fixed by hand, whether they came from the catalog, the file's tags
or its file name. On an audiobook series page, turn on **Edit** and press the pencil on a volume.
A volume whose details were only guessed from its file name says so in its row and has an **Add
details** link, and the Audiobooks page's **Needs details** filter lists them all.

The form takes the title, author, narrator, series and volume, genre, release date and
description, and a cover image. Your values are kept in `data/audiobook_metadata_overrides.json`
(a cover in `data/covers/audiobooks/custom/`) and win over the catalog and the tags on every
rescan. **Reset** on a field goes back to the original value. Once a guessed book has a real title
and author, it is linked to its EPUB like any other.

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
iOS plays `.m4a` and `.flac` too, but not Ogg (`.opus`, `.ogg`): those play in the web app only.
If iOS refuses an `.m4b` whose container is laid out oddly, a lossless remux usually fixes it:
`ffmpeg -i in.m4b -c copy -movflags +faststart out.m4b`.
