# Vocab pipeline: where the word data actually lives

There is **no database anywhere in this project**. Every index is a flat JSON
file under `data/`, written atomically by
`index_store.save_json()` (temp file + `os.replace`, so a crash mid-write can't
corrupt an index).

## The four per-book artifacts

The analyze stage writes four things per book. Knowing which one to read is most
of the work in any vocab-related change.

| Artifact | Shape | Has | Use it for |
|---|---|---|---|
| `vocab_epub.json` | one dict per `book_id` | aggregate stats only | "Vocabulary & difficulty" card |
| `wordlists/{id}.json` | `orthBase → count` | counts | coverage %, exposure |
| `sentences/{id}.json.gz` | word defs + per-sentence id lists | **everything** | reading load, study path, anything needing lemma/reading/difficulty |
| `known_stats.json` | one dict per `book_id` | derived, reader-relative | Known %, flow/i+1/hard |

### The choice that matters

`wordlists/` looks like the obvious frequency source — it's a plain
`orthBase → count` map, small and uncompressed. **It is usually the wrong one.**
It carries no lemma, no reading, no difficulty, and no free-word flag, so
anything built on it:

- over-reports unknown words (misses the lemma matching space — see
  [03-known-words.md](03-known-words.md))
- can't filter out proper nouns
- can't show a reading or difficulty in the UI

`sentences/{id}.json.gz` has all of it. Each entry in its `words` table is:

```
[orthBase, lemma, is_free, kanaBase_reading, difficulty]
```

and `sents` is a list of word-id lists, one per sentence, in reading order.
`study.py` deliberately recounts occurrences from `sents` rather than reading the
wordlist, so its counts stay consistent with the reading-load walk.

Loading all 73 sentence indices takes **0.32 s**, so "it's gzipped, it must be
expensive" is not a reason to reach for `wordlists/` instead.

## User-independent vs reader-relative

A hard split, and worth preserving:

- `vocab_epub.json` — **user-independent.** Length, unique words, difficulty
  distribution. Does not change when the known list changes.
- `known_stats.json` — **reader-relative.** Known %, flow/i+1/hard, words to 98%.
  Recomputed from scratch whenever the known list or the read-books list changes.

This is why `known.recompute()` never touches an EPUB or the tokenizer: it reads
only the stored indices, so it can refresh the whole library in ~1 s.

## Data flow

```
EPUB file
  └─ analyze.analyze()
       ├─ vocab_epub.json        aggregate stats
       ├─ wordlists/{id}.json    orthBase → count
       ├─ sentences/{id}.json.gz word defs + per-sentence ids
       └─ known.recompute_all()  every profile, not just the active one

known_words.json + read_books.json   (the reader)
  └─ known.recompute()               reads wordlists/ + sentences/
       └─ known_stats.json

build.build()
  └─ epub_library.json          book.vocab + book.known attached

GET /api/books/{id}         → Reading load, Vocabulary & difficulty
GET /api/books/{id}/study   → study.study_path(), reads sentences/ live
GET /api/known-words/frequent → corpus.frequent_unknown(), sweeps all of sentences/
```

**`recompute()` must be followed by `build.build(cfg)`** or the served index keeps
the old numbers — `epub_library.json` is a denormalized join, and the API reads
it directly.

## Serving

`_load_library()` re-reads `epub_library.json` from disk on **every request** —
no in-memory cache. Pipeline output is therefore visible without a restart, and
a test can check its effect by curling the API immediately after a write.

`_index_lock` (a `threading.Lock` in `server.py`) serializes every pipeline stage
run (the admin worker holds it for the whole stage) and every index write. Any new
endpoint that writes an index must take it, and must mutate a `load_json` copy, not
the `_load_library()` cached dict.

## Adding a new derived index

Follow the `known_stats.json` pattern: derive it in a function that reads only
stored indices, write it with `save_json`, add a path property on the `Config`
dataclass, and gitignore it if it's cheap to rebuild. Don't add a cache file
before measuring — see [04-perf-baselines.md](04-perf-baselines.md).
