# The known-words list

Everything reader-relative in the app is measured against one file per profile:
`data/users/<slug>/known_words.json`. It is hand-supplied (like
`pairing_overrides.json`) and backed up by `cli.py push`, and it is the single most
consequential input in the pipeline — a gap in it makes every book look harder than it is.

## Shape (schema 3)

```json
{
  "schema": 3,
  "updated_at": "2026-07-19T00:48:56+00:00",
  "source": "_Vocabulary I'll never forget_ deck export ….csv",
  "count": 6971,
  "words":  ["…"],          // the whole list: imported ∪ marked
  "marked": ["先輩", "弁当"], // added by clicking; the only removable subset
  "orth":   ["…"],          // normalized orthBase keys, derived
  "lemma":  ["…"]           // canonical lemma keys, derived
}
```

`words` is the union, so `load_sets()` and `recompute()` never need to know
where a word came from. `marked` exists only so a click is reversible and a
re-import can't silently discard hand-marked words.

Schema-2 files (no `marked`) load unchanged.

## Two matching spaces — the largest correction in the pipeline

A deck spells a word how its author typed it (かも知れない); a book spells it how
*its* author did (かもしれない). Comparing orthBase alone silently misses a large
share of what the reader knows.

So `normalize()` tokenizes every entry once at import and stores **both** an
orthBase key and a canonical lemma key. A book word counts as known if either
matches (`known_mask()`). On the staged decks this recovers **~40% more word
types** than orthBase alone.

Consequence: after any change to how normalization works, the list must be
re-imported — the stored `orth`/`lemma` keys are derived at write time, not
computed on read. `_write()` in `known.py` is the single writer for exactly this
reason: it re-normalizes on every path, so the keys can't drift out of sync with
`words`.

## Two ways in

**Bulk import** — `POST /api/known-words`, multipart file (≤5 MB) or pasted text,
`format=auto|jpdb|anki|text`, `mode=replace|merge`. `parse_words()` takes the
first comma/tab/semicolon field per line, strips HTML and `漢字[かんじ]` furigana,
NFKC-normalizes, and drops pure-ASCII lines (CSV headers and latin noise).

**Marking one word at a time** — `POST /api/known-words/mark` and `/unmark`,
`{"words": [...]}`. Wired to the ✓ button on the book page's study list and the
frequency list on `/known` (`static/mark.js`).

The second path exists because of a structural gap in the first: a deck records
what the reader chose to *drill*, not what they can *read*. 先輩, 弁当, 持つ and
すぐ are missing from every export for the same reason nobody makes a flashcard
for them — and they were therefore counted as unknown in every book. The top of
any study list is mostly these.

Two invariants worth not breaking:

- `unmark` only removes words present in `marked`. Imported words come back as
  `skipped` and are left alone — **the endpoint cannot eat the deck.**
- `save(mode="replace")` unions `marked` back in, so re-exporting a deck doesn't
  discard a set built up one click at a time.

`mark`/`unmark` skip the recompute entirely when nothing actually changed
(`books_updated: 0`), so a no-op click costs nothing.

## The concepts layered on top

| Concept | Constant | What it means |
|---|---|---|
| **Strain** | `STRAIN_THRESHOLD = 2` | A sentence with ≥2 unknown content words. One unknown is inferable from context; two forces you to stop and reassemble. `hard_pct` is the headline metric. |
| **In-book acquisition** | `ACQUIRE_AFTER = 5` | A word stops counting as a lookup after 5 encounters *in the same book*. Without it, all 113 mentions of a surname count as 113 lookups. Applies to sentence-level metrics only, not to `known_pct`/`words_to_98`. |
| **Free words** | — | Proper nouns, numerals, and bound affixes (接尾辞/接頭辞 — たち, くん, 大-) are never a lookup regardless of the list. Flagged in the sentence index. |
| **Exposed vocabulary** | `EXPOSURE_MIN = 5` | Words used ≥5× in a book marked read count as known everywhere else. Weaker evidence than a deck entry, so tallied separately in `exposed_unique`. |
| **Fluent coverage** | `FLUENT_COVERAGE = 0.98` | The target `words_to_98` costs out. |

`exposure_for()` always scores a book *excluding its own* contribution —
otherwise a finished book would trivially report near-zero strain against its
own vocabulary. The library-wide list in `corpus.py` has no book to exclude, so
it uses the exposure set flat; that asymmetry is intentional.

## Known bias worth remembering

**Longer books score easier.** At equal coverage the longest third of the library
shows ~26% strain vs ~34% for the shortest, because acquisition has more chances
to fire (correlation with log length ≈ −0.50). Compare same-length books, or read
`hard_pct` alongside the word count.

Also: `difficulty_avg` spans only 17–29 across the library and correlates with
actual strain at r≈0.24 — it ranks books close to randomly for "what can I read
now". That's why the reading-load metrics exist.

## Kana/kanji variants and loanwords (decided 2026-10-01)

**Scoring already merges あたる / 当たる** — `known_mask` matches on orthBase *or* UniDic
lemma, and both share lemma 当たる. Only the `/known` review list showed them twice;
`corpus.frequent_unknown` now folds a pure-kana spelling into the kanji spelling with the
same lemma **and** reading (one row, `variants` lists both, ✓ marks both). It deliberately
does *not* group on lemma alone: 撮る/獲る/採る all have lemma 取る but are different words.

**Loanwords are not discounted in ranking.** Defs carry UniDic `goshu` as a 6th element
(`外` = loanword; `SCHEMA_VERSION` 5), and `known_stats.json` has diagnostic
`hard_pct_ex_loan` / `loan_lookup_pct`. Measured on 73 books: loanwords are a median 11.6%
of lookups, worth ~3 points of `hard_pct`, and only 4 books move >10 rank places if they
are treated as free (biggest: self-help books, not translated novels). `goshu` can't
separate guessable English loanwords from game/medical jargon, so a blanket discount
would hide real strain. The `/known` review list has a "Loanwords" filter for bulk-marking
the ones you do know. **Anything unpacking a word def positionally must slice `[:5]`.**
