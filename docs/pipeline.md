# The pipeline

Hondana turns your folders of EPUBs and audiobooks into the library you browse through a
series of **stages**. Each reads the files earlier stages wrote, and each is incremental: running
it again only does new or changed work, so it is cheap to re-run.

You can run them from **Admin** in the browser (one button per stage, plus **Run full
pipeline**), or from the command line:

```bash
uv run cli.py all          # the whole chain
uv run cli.py scan         # or one stage
uv run cli.py --help       # every command
```

The stage list lives in one place, `epublib/pipeline.py` (`STAGES`). The CLI subcommands and
the Admin page are both generated from it.

## The full chain

`all` (Admin: **Run full pipeline**) runs these in order:

| Stage (CLI) | Admin label | What it does | Writes |
|---|---|---|---|
| `inbox` | File the inbox | Moves new EPUBs and audiobooks out of the [inbox folder](audiobooks.md#the-inbox-folder) into the library, then scans what it filed. Does nothing when no inbox is set. First, so the stages after it index what it filed. | `epub_dir`, `audiobook_dirs` |
| `scan` | Scan EPUBs | Reads each EPUB's metadata and cover, then rebuilds the index. | `epub_library.json`, `covers/` |
| `analyze` | Analyze vocab | Tokenizes each Japanese book (MeCab/UniDic) and counts its vocabulary. About 1–2 s a book. | `vocab_epub.json`, `wordlists/`, `sentences/` |
| `enrich` | Enrich (Natively) | Looks up each Japanese book's level and ratings on learnnatively.com. | `natively_epub.json` |
| `pair` | Pair EN/JP | Matches English and Japanese editions of the same book. | `pairings.json` |
| `audio-scan` | Scan audiobooks | Scans `audiobook_dirs` (reading tags, keeping ids across moves) and joins the files with the catalog. `--dry-run` prints the moves and id changes and writes nothing. | `audiobook_files.json`, `audiobook_tags.json`, `covers/audiobooks/`, `audiobook_library.json` |
| `catalog` | Fetch audiobook metadata | Looks up audiobooks whose `[ASIN]` the catalog doesn't know yet, from Audible's public catalog API. Makes no request when nothing is new. | `audiobook_catalog_extra.json` |
| `xref` | Cross-ref audiobooks | Links each EPUB to its audiobook. | `audiobook_links.json` |
| `build` | Build index | Joins everything into the index the pages read: cleaned titles, series and volume numbers, genres, descriptions, pairs, audiobooks, search. | `epub_library.json`, `search.sqlite3` |
| `epub-catalog` | Fetch EPUB metadata | Asks Google Books for a genre and description by ISBN, for EPUBs that still lack them. Needs `GOOGLE_BOOKS_API_KEY`. | `epub_catalog_extra.json` |

Per-reader numbers (Known %, strain, words to 98%) are not a stage. They are recomputed from
the stored vocabulary whenever a known-words list or a read book changes, in about a second.

## You rarely need to run it by hand

- **Adding a book on Add** (or from Hondana for iPhone) runs that book's own chain on the server:
  analyze → enrich → pair → link audiobook → build → Google Books lookup. The page shows the
  result as soon as the analysis lands.
- **Admin › Scan EPUBs** also queues that chain for every Japanese or English book that hasn't
  been analyzed, so books you copied into the folder by hand get their numbers too.
- With an inbox folder set, the server files what lands there on its own (every 30 seconds, once it
  has stopped changing). `inbox` is the same thing for the command line.
- Re-run **Run full pipeline** after adding audiobooks, or to retry lookups.

## Stages outside the chain

| Stage (CLI) | What it does |
|---|---|
| `enrich-audiobooks` | Natively levels for the whole audiobook catalog (about an hour the first time). New entries, and misses older than 14 days. See [audiobooks.md](audiobooks.md#natively-levels). |
| `enrich-unmatched` | Retries every Natively miss now, however recent. |
| `annotate` | Pre-builds the reader pack for every Japanese book. Optional: a pack is otherwise built the first time Hondana for iPhone downloads the book. |

`--refresh` redoes everything instead of only new work. It applies to `analyze`, `enrich`
(and `all`), `enrich-audiobooks` and `annotate`.

## Maintenance commands

| Command | What it does |
|---|---|
| `seed-known [--top-n N]` | Starts the active profile's known-words list from the N most common words (Japanese or English). It refuses to overwrite a list you imported. The **Known words** page offers the same thing. |
| `recover-moves` | Re-attaches read marks and corrections to books whose files moved between folders. A book's id comes from its path and size, so a move looks like a delete plus an add. `scan` already does this for moves it can match. |
| `gc [--apply]` | Lists data still kept for books no longer in the library; `--apply` removes it. Don't run it while a stage is running. |
| `consolidate`, `drm` | Audiobook file tidying. See [audiobooks.md](audiobooks.md). |
| `push` | Backs up your hand-made data. See [running-a-server.md](running-a-server.md#backups). |
| `align-remote` | Audiobook ↔ text sync from another machine. See [audio-sync.md](audio-sync.md). |

## How the data flows

```
EPUB files (epub_dir)                 Audiobook files (audiobook_dirs)
   │  scan     → epub_library.json       │  audio-scan → audiobook_files.json
   │  analyze  → vocab_epub.json,        │               audiobook_library.json
   │             wordlists/, sentences/  │  catalog    → audiobook_catalog_extra.json
   │  enrich   → natively_epub.json      │     (joined with the catalog and
   │  pair     → pairings.json           │      natively_levels.json)
   │  xref     → audiobook_links.json ◀──┘
   │  build    → epub_library.json  (denormalized: what the pages and the API serve)
   ▼
FastAPI server  →  web pages, downloads, and the Reader API for Hondana for iPhone
                   (+ per-profile known words → Known %, strain, Read Next)
```

## How EN/JP pairing works

An English book and its Japanese original share an **author**, written in a different script
(住野よる ↔ Yoru Sumino). `pair` romanizes Japanese authors offline with
[pykakasi](https://pypi.org/project/pykakasi/) and matches name words in any order, so clean
cases pair automatically. It only auto-links **confident, unambiguous** matches. Pen names whose
reading romanizes differently (雨穴 reads "Uketsu" but romanizes as "ame ana"), and authors with
several books in each language, are left for you.

ISBNs and ASINs don't help: the two editions are different products, so their identifiers never
match.

You decide the rest **on the book page**: an unpaired book with a likely match asks "Is this the
English edition?" (Yes / Not this one / No English edition), and its Edit dialog can link any
book by hand. **Editions** (`/pair`) is the overview: the pairs, the books waiting for a
decision, and the pairings you blocked.

Decisions are saved in `data/pairing_overrides.json`. It is keyed by **stable identifiers**
(`asin:…`, `isbn:…` or `path:…`), so your decisions survive rescans and renames:

```json
{
  "force": [ { "en": "isbn:9781529029598", "jp": "asin:B01BLAY1G6" } ],
  "block": [ { "en": "path:English/Wrong.epub", "jp": "path:Japanese/別.epub" } ],
  "none":  [ "asin:B0XXXXXXXX" ]
}
```

`force` links a pair whatever the score, `block` prevents one, and `none` marks a book that has
no other edition.

## How search works

The search box on Library and Audiobooks uses a local SQLite FTS5 index (`data/search.sqlite3`),
rebuilt by `build`. It uses the **trigram** tokenizer, which indexes overlapping 3-character runs
rather than words, so Japanese works without a dictionary. Every title, author and series is
also indexed by its **readings**: hiragana, and romaji with and without spaces. So `murakami`,
`かぜがつよく` and `カゼガツヨク` all find the Japanese book.

Queries shorter than 3 characters can't use trigrams. They fall back to a plain substring scan,
so the box never comes up empty for a short query.
