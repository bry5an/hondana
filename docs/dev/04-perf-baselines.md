# Performance baselines

Measured 2026-07-19 on a laptop (Apple Silicon): 73 analyzed books, 6,968 known words,
~330k word defs, ~352k sentences.

Recorded because the prose in `CLAUDE.md` describes several of these
as taking "a couple of seconds", which led to a design with a staging area, an
incremental-normalize path and a background worker — none of which were needed.
**Measure before designing around slowness.**

| Operation | Time |
|---|---|
| `json.load()` of `known_words.json` (311 KB) | <0.01 s |
| `import fugashi` + `Tagger()` (unidic-lite) | 0.02 s |
| `normalize()` — tokenize all 6,968 known words | 0.21 s |
| tokenize a single word | ~0 ms |
| load all 73 sentence indices (8.6 MB gzipped) | 0.32 s |
| `known.recompute()` — all 73 books | 0.99 s |
| `build.build()` | 0.02 s |
| `corpus.frequent_unknown()` — cold (parses every sentence index) | 0.50–0.85 s |
| `corpus.frequent_unknown()` — warm (cached ranked list) | ~1 ms |
| `corpus.frequent_unknown()` — after a mark (rebuild ranked list, tallies cached) | ~0.2 s |
| `POST /api/known-words/mark` end to end (HTTP, incl. recompute + build) | ~1.6 s |

## What follows from these

- A single-word write can take the **same full `save → recompute → build` path**
  as a bulk import. No staging, no incremental key updates, no background job.
  The UI batches clicks with a 700 ms debounce purely to avoid redundant round
  trips, not because a single write is expensive.
- `corpus.frequent_unknown()` caches in process memory at two levels. `_aggregate` holds the
  **reader-independent** word tallies (the ~0.6 s gunzip + parse sweep), keyed by the
  sentence files' name/mtime/size. `_ranked` holds the **fully folded + sorted review list**
  per profile, keyed by stats of `known_words.json`, `read_books.json`, the read books'
  wordlists, the library index and the language. So nothing is recomputed until a book is
  added/removed/re-analyzed, a word is marked/unmarked/imported, or a book is marked read;
  every write goes through `save_json` (os.replace), so there are no invalidation hooks.
  Show more / filter chips are a `bisect` + slice of the cached list (~1 ms). A mark costs
  one rebuild on the next request (the folded sort over every unknown type). A restart
  starts cold, so `server.py` pre-warms it in a background thread at startup. No cache
  file on disk.
- fugashi/wordfreq imports are lazy *on purpose* so `serve` startup stays light.
  Keep them inside functions. The dictionary load itself is only 0.02 s with
  unidic-lite, so the cost of the first call is negligible either way.

## Re-measuring

`recompute()` and `build()` write index files, so neutralize the writer before
timing them against real data:

```bash
./.venv/bin/python - <<'PY'
import time
from epublib import config, known, build, index_store
index_store.save_json = lambda *a, **k: None   # measurement only — no writes
known.save_json = lambda *a, **k: None
build.save_json = lambda *a, **k: None
cfg = config.load()
t0 = time.time(); stats = known.recompute(cfg); print(f"recompute: {time.time()-t0:.2f}s ({len(stats)} books)")
t0 = time.time(); build.build(cfg);             print(f"build:     {time.time()-t0:.2f}s")
PY
```

Note `config.load()`, not `config.load_config()` — the latter is the alias
`server.py` imports it under, and doesn't exist on the module.

For anything that *does* write, back up `data/users/<slug>/known_words.json` first; it's
hand-supplied, and the only copy of a list built up over time.
