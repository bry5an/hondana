# Gotchas

Things that silently produce wrong-but-plausible output.

## One book's word table holds several entries per orthBase

The `words` array in `sentences/{id}.json.gz` is keyed by
`(orthBase, lemma, POS, reading, difficulty)` — not by orthBase. So 「ただ」 can
appear as three separate defs in a single book.

Any aggregate that counts *defs* to answer "how many books use this word" will
overcount. A prototype of the library-wide frequency list reported words
appearing in **136 books** out of 73 before this was noticed — a number wrong
enough to spot, but the ordering it produced looked entirely reasonable.

Dedupe per book with a `set()` of orthBase before incrementing any per-book
counter (see `corpus.frequent_unknown()`). The assertion to keep in a test:
`max(books) <= books_scanned`.

## Character names get split into common nouns and aren't flagged free

`is_free` catches 固有名詞, so the tokenizer's *own* proper nouns never count as
lookups. But it can't catch what it didn't recognize as a name:

| Fragment | From | Appears as |
|---|---|---|
| 鞠 | 小鞠 | 225× "unknown word" |
| 焼塩 | (surname) | 205× |
| 奈 | 八奈見 | 11× |
| 温水, 甘夏, 檸檬, 古都, 杏 | character names | top of the study list |

So the head of any single book's "most common words you don't know yet" is
often name debris rather than vocabulary. This is a real limitation, not a bug
to fix casually — improving it means a name-detection heuristic over the whole
book, and the current click-to-mark-known flow handles it well enough (a reader
marks the name known once and it's gone).

Ranking library-wide by **breadth first** (how many books use a word) rather than
raw frequency makes these sink on their own, since a name appears in one book.

## UniDic lemma artifacts make known words look unknown

`感ずる` sits in the top 10 unknown words library-wide. Nobody's deck contains
`感ずる` — it's UniDic's canonical lemma for 感じる. Same family: `おく` (於く) for
the ておく auxiliary, `よる` (因る), `たまる` (堪る).

These are genuine matching failures, not noise. Marking them known via the ✓
button fixes them for good, which is currently the intended remedy.

## Shared-library changes need `recompute_all()`; per-user writes need `recompute()`

`known.recompute(cfg)` refreshes one profile (`cfg.active_user`). Anything that
changes the *shared* library — `analyze()` (so `cli.py all` and every `/admin`
stage run), deleting a book — calls `known.recompute_all(cfg, languages=None)`
instead, which loops the registered profiles (optionally only those learning a
given language). Request-scoped writes (mark/import words, mark read) only affect
the requester and stay on `recompute(req_cfg)`. Don't hand-roll a loop over
`profiles.list_profiles()` (an early `analyze_book` did, and had to be replaced).

## Known-words writes need `recompute()`, not `build()`

This used to be the reverse: `known.recompute()` wrote `known_stats.json`, but
the API served `epub_library.json`'s baked-in `known` field, produced by
`build.build()` — skip the build and the numbers on screen wouldn't move.

That stopped being true with per-device profiles. `known` is now
resolved per-request and overlaid onto a fresh copy of each book at request
time (see CLAUDE.md's "Reader profiles" section) rather than
baked into the shared `epub_library.json` — a stale baked `known` field would
be wrong for every profile but the one that last ran `build()`. So a
known-words write path only needs `known.recompute(req_cfg)`; calling
`build.build(cfg)` afterward does nothing for the known numbers and just adds
needless rebuild cost.

## `config.load()`, not `config.load_config()`

`server.py` does `from .config import load as load_config`. The module itself
only exports `load`. Scripts that copy the server's spelling fail with
`AttributeError: module 'epublib.config' has no attribute 'load_config'`.

## PGroonga on Supabase Free leaks disk with no reclaim path (2026-07-21)

An earlier version briefly mirrored the local indices into Supabase Postgres with PGroonga
full-text indexes backing `/api/search` (removed the same day it was added, in
favour of the local SQLite FTS5 index in `epublib/search_index.py`). The database blew from ~20 MB of real
rows past the 0.5 GB Free-tier cap within a day.

`pg_database_size()` walks the data directory rather than summing `pg_class`,
so PGroonga's own files (`base/<dboid>/pgrn.*`, entirely outside normal
relation tracking) count toward the quota while being invisible to
`pg_relation_size`/`pg_total_relation_size` and to Supabase's own Table Editor
size column. Worse: **every `REINDEX`, every `DROP INDEX`/`CREATE INDEX`, and
even `DROP EXTENSION pgroonga CASCADE` created a brand-new generation of
PGroonga's internal tables (`Lexicon<id>_*`, `Sources<id>`) without freeing
the previous one** — confirmed directly via
`pgroonga_command('table_list')`/`object_inspect`, which kept showing old
generations with real `disk_usage` long after Postgres had zero `pg_class`
reference to them. Three separate remediation attempts (targeted rebuild,
full extension drop+recreate, project restart) each made the total *larger*,
never smaller — 940 MB → 1.29 GB → 1.68 GB. Nothing reachable through SQL
(no superuser, no `pg_ls_dir`/`pg_stat_file` on managed Postgres) or the
dashboard (project restart included) ever reclaimed the leaked space.

If full-text search over this data is ever worth revisiting, don't repeat
this path unmodified: budget for PGroonga's ~8 MB fixed floor per indexed
column regardless of content size, and assume any index rebuild is
one-way — plan to burn the whole project and start a fresh one rather than
trying to reindex in place.

## A `book_id` from another machine isn't yours

`book_id` is `sha1(rel_path + filesize)`, so two machines with different folders give the same
book different ids. A `/book/<id>` URL from someone else's server almost certainly doesn't exist
in your index.

## The known-words list is irreplaceable

`data/users/<slug>/known_words.json` is hand-supplied: years of imports plus anything marked by
hand. It is not in git. Back it up before exercising any write path:

```bash
cp data/users/<slug>/known_words.json /tmp/known_words.backup.json
```

`DELETE /api/known-words` clears the whole thing, `marked` set included.

## Tests: `tests/`, run with `uv run pytest`

`uv run pytest -q` runs small, fixture-based
regression tests over the tuned numeric behaviour in `analyze.py`, `known.py`,
`corpus.py`, `build.py`, and `study.py` — `SCHEMA_VERSION`, `Z_EASY`/`Z_HARD`,
`STRAIN_THRESHOLD`, `ACQUIRE_AFTER`, `EXPOSURE_MIN`, the two-space matching,
`_title_series()`'s volume-marker regexes — plus a smoke test running
`scan → analyze → pair` over `tests/fixtures/epubs/` (4 files, including a
deliberately malformed `broken_book.epub` and an EN/JP Harry Potter pair).
`tests/conftest.py`'s `make_cfg()` builds a `Config` directly against a
pytest `tmp_path`, no `config.toml` needed. CI runs them on every pull request.

The invariant behind the 136-out-of-73 bug above is now an actual assertion
in `tests/test_corpus.py` (`max(books) <= books_scanned`), not just a note.
