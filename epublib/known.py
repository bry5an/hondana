"""The user's known-words list and the per-book reading load derived from it.

parse_words accepts the common export shapes line by line: jpdb CSV, Anki
TSV/CSV (HTML stripped), or a plain word-per-line list.

Matching happens in two spaces. Exports spell words however the deck author
typed them (かも知れない), while a book spells them however the author typed
them (かもしれない) — so an orthBase-only comparison silently misses a large
share of what the reader actually knows. Each entry is therefore tokenized
once at import time and recorded under both its orthBase and its canonical
lemma; a book word counts as known if either key matches. On the staged Anki
decks this recovers ~40% more word types than orthBase alone.

A deck records what the reader chose to *drill*, not what they can *read*:
先輩, 弁当 and 持つ are missing from every export for the same reason nobody
makes a flashcard for them. `mark` is the second way in — one word at a time,
clicked off the study list on a book page or the frequency list on /known.
Those words are kept in their own `marked` set as well as in `words`, so an
accidental click is reversible and a later re-import can't silently discard
them.

recompute derives two layers from those sets:
- coverage (known %), from the wordlists written by the analyze stage
- reading load (flow/i+1/hard sentences), from the per-sentence index

Both are pure recomputes over stored data — no EPUB access, no tokenizer — so
re-importing a list refreshes the whole library in about a second (measured:
0.2 s to re-normalize 7k words, 1.0 s to recompute 73 books). Marking a word
is cheap enough to take the same honest full path rather than an incremental
one; see docs/dev/04-perf-baselines.md.

known_words.json is committed (hand-supplied, like pairing_overrides.json);
known_stats.json is derived and gitignored.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, time, timezone
from pathlib import Path

from .config import Config
from .index_store import load_json, save_json

# 3 added the `marked` set. Schema-2 files load unchanged — they simply have
# no marked words yet.
SCHEMA_VERSION = 3

# Unknown content words in one sentence, above which the reader has to hold
# several gaps at once and reassemble the sentence rather than read it. The
# user-reported strain threshold, and the definition of `hard_pct`.
STRAIN_THRESHOLD = 2
# Encounters after which a word stops being a lookup *within the same book*.
# A novel's recurring vocabulary — a surname, 漁業 in a fishing story — is
# looked up early and then read normally; counting all 113 mentions of 正木 as
# 113 lookups overstates strain badly. This does not claim the word has been
# acquired for good, only for the rest of this book, so it applies to the
# sentence-level load metrics and not to the static coverage figures
# (known_pct, words_to_98).
ACQUIRE_AFTER = 5
# Encounters within a book the reader has finished, above which a word counts
# as *exposed*: met often enough to be recognised, though never formally
# studied. A study deck records what the reader chose to drill, not what they
# can read — 持つ and ところ are missing from it for the same reason nobody
# makes flashcards for them. Exposure is weaker evidence than a deck entry
# (it says nothing about retention), so it is tallied separately.
EXPOSURE_MIN = ACQUIRE_AFTER
# Token coverage at which a text reads without constant interruption — the
# standard extensive-reading figure, and the target `words_to_98` costs out.
FLUENT_COVERAGE = 0.98

_TAG_RE = re.compile(r"<[^>]+>")
# 漢字[かんじ] bracket furigana used in jpdb/Anki exports
_FURIGANA_RE = re.compile(r"[\[［][^\]］]*[\]］]")
# Matches seed_common's source string: "seed:top-10k-en" or "seed:top-10500-en".
_SEED_SOURCE_RE = re.compile(r"^seed:top-(\d+)(k)?-")


def _seeded_top_n(data: dict) -> int:
    """The seed ladder's current level. Prefers the `seeded_top_n` field;
    falls back to parsing it out of `source` for known_words.json files
    written before that field existed (older seeded profiles)
    so the ladder picks up where an old-format seed left off instead of
    reverting to the first-time "Seed common words" button.
    """
    top_n = data.get("seeded_top_n")
    if top_n:
        return int(top_n)
    m = _SEED_SOURCE_RE.match(data.get("source") or "")
    if not m:
        return 0
    n = int(m.group(1))
    return n * 1000 if m.group(2) else n


_PUNCT_POS = {"補助記号", "記号", "空白"}


def parse_words(text: str, fmt: str = "auto", lang: str = "ja") -> list[str]:
    """Extract a deduplicated word list from an uploaded export.

    fmt: "auto"/"jpdb"/"anki" take the first comma/tab/semicolon field per
    line; "text" treats each whole line as one word.
    lang: "ja" strips jpdb/Anki bracket furigana and drops pure-ASCII lines
    (CSV headers / latin noise, never Japanese vocabulary); "en" keeps both —
    ASCII *is* the vocabulary, and a bracketed gloss is meaningful text, not
    furigana.
    """
    words: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        line = line.lstrip("﻿").strip()
        if not line or line.startswith("#"):
            continue
        field = line if fmt == "text" else re.split(r"[,\t;]", line)[0]
        field = _TAG_RE.sub("", field)
        if lang != "en":
            field = _FURIGANA_RE.sub("", field)
        field = unicodedata.normalize("NFKC", field.strip().strip('"').strip())
        if not field:
            continue
        # Pure-ASCII lines are CSV headers ("word,reading,...") or latin
        # noise — never Japanese vocabulary.
        if lang != "en" and all(ord(c) < 128 for c in field):
            continue
        if field not in seen:
            seen.add(field)
            words.append(field)
    return words


def normalize(words: list[str], lang: str = "ja") -> tuple[list[str], list[str]]:
    """Map raw entries into the orthBase and lemma spaces used by the
    wordlists. Multi-token entries (ことができる, にとって) have no single
    dictionary form, so they are kept verbatim in the orthBase space only.
    """
    if lang == "en":
        return _normalize_en(words)
    return _normalize_ja(words)


def _normalize_ja(words: list[str]) -> tuple[list[str], list[str]]:
    """Imports fugashi lazily — `serve` must not pay for the dictionary."""
    import fugashi

    tagger = fugashi.Tagger()
    orth: set[str] = set(words)
    lemma: set[str] = set()
    for w in words:
        toks = [t for t in tagger(w) if t.feature.pos1 not in _PUNCT_POS]
        if len(toks) != 1:
            continue
        tok = toks[0]
        if tok.feature.orthBase:
            orth.add(unicodedata.normalize("NFKC", tok.feature.orthBase))
        if tok.feature.lemma:
            lemma.add(tok.feature.lemma)
    return sorted(orth), sorted(lemma)


def _normalize_en(words: list[str]) -> tuple[list[str], list[str]]:
    """English counterpart of _normalize_ja: orth is the lowercased surface —
    the same word-identity key `analyze._analyze_text_en` writes into the
    wordlists — and lemma is spaCy's token.lemma_, so an import matches a book
    on either space exactly as the JP orthBase/lemma pair does. Reuses
    analyze's lazily-loaded pipeline rather than a second copy of it.
    """
    from .analyze import _get_en_nlp

    nlp = _get_en_nlp()
    orth: set[str] = {unicodedata.normalize("NFKC", w.lower()) for w in words}
    lemma: set[str] = set()
    for doc in nlp.pipe(words):
        toks = [t for t in doc if not t.is_space and not t.is_punct]
        if len(toks) != 1:
            continue
        lemma_ = toks[0].lemma_
        if lemma_:
            lemma.add(unicodedata.normalize("NFKC", lemma_.lower()))
    return sorted(orth), sorted(lemma)


def _clean(words: list[str]) -> list[str]:
    """Deduplicated NFKC forms, empties dropped. Entries arriving from a JSON
    request body have not been through parse_words."""
    out: list[str] = []
    seen: set[str] = set()
    for w in words:
        w = unicodedata.normalize("NFKC", str(w).strip())
        if w and w not in seen:
            seen.add(w)
            out.append(w)
    return out


def _write(
    cfg: Config,
    words: list[str],
    marked: list[str],
    source: str,
    seeded_top_n: int | None = None,
    not_words: list[str] | None = None,
) -> dict:
    """The single writer for known_words.json.

    `not_words` None carries the stored set forward, so callers that only touch
    the word list (save, mark, unmark, seed) can't drop it.

    Every caller normalizes through here, so the orth/lemma keys can never
    drift out of sync with the word list that produced them. Resolves the
    active profile's language itself (like recompute()) so save/mark/unmark
    don't each need to thread it through.
    """
    from . import profiles

    orth, lemma = normalize(words, lang=profiles.active_language(cfg))
    if not_words is None:
        not_words = list(load_json(cfg.known_words, {}).get("not_words") or [])
    out = {
        "schema": SCHEMA_VERSION,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source,
        "count": len(words),
        "words": words,
        "marked": marked,
        "not_words": not_words,
        "orth": orth,
        "lemma": lemma,
        # How far up wordfreq's frequency ranking seed_common has reached, so
        # /known's "add the next 1,000" flow knows where to resume. None for
        # anything that isn't a seed (a real import replaces the baseline
        # this tracks, so it's deliberately not carried across those).
        "seeded_top_n": seeded_top_n,
    }
    save_json(cfg.known_words, out)
    return out


# ── snapshots ──────────────────────────────────────────────────────────
# The list is years of hand-built data, and two actions replace it wholesale
# (import with mode "replace", Clear). Each takes a copy first, under
# users/<slug>/history/, so a wrong file or an accidental Clear is one restore
# away. Only the words, marked set and provenance are kept — orth/lemma are
# derived and rebuilt on restore.

SNAPSHOT_KEEP = 10
_SNAPSHOT_ID_RE = re.compile(r"^\d{8}T\d{12}Z-[a-z]+$")
_SNAPSHOT_KEYS = (
    "schema", "updated_at", "source", "count", "words", "marked", "not_words", "seeded_top_n",
)


def _history_dir(cfg: Config) -> Path:
    return cfg.known_words.parent / "history"


def snapshot(cfg: Config, reason: str) -> str | None:
    """Copy the current list into history/ before `reason` (replace, clear,
    restore) overwrites it. Returns the snapshot id, or None for an empty list
    (nothing to lose). Keeps the newest SNAPSHOT_KEEP."""
    data: dict = load_json(cfg.known_words, {})
    if not data.get("words"):
        return None
    now = datetime.now(timezone.utc)
    sid = f"{now:%Y%m%dT%H%M%S%f}Z-{reason}"
    out = {k: data.get(k) for k in _SNAPSHOT_KEYS}
    out["snapshot_at"] = now.isoformat(timespec="seconds")
    out["reason"] = reason
    hist = _history_dir(cfg)
    save_json(hist / f"{sid}.json", out)
    for old in sorted(hist.glob("*.json"), reverse=True)[SNAPSHOT_KEEP:]:
        old.unlink(missing_ok=True)
    return sid


def list_snapshots(cfg: Config) -> list[dict]:
    """Saved lists, newest first: id, when, why, source, word and marked counts."""
    hist = _history_dir(cfg)
    if not hist.is_dir():
        return []
    out = []
    for path in sorted(hist.glob("*.json"), reverse=True):
        data = load_json(path, {})
        if not data.get("words"):
            continue
        out.append({
            "id": path.stem,
            "created_at": data.get("snapshot_at"),
            "reason": data.get("reason"),
            "source": data.get("source"),
            "count": data.get("count") or len(data["words"]),
            "marked": len(data.get("marked") or []),
        })
    return out


def restore(cfg: Config, snapshot_id: str) -> dict:
    """Put a snapshot back as the current list. The list it replaces is
    snapshotted first, so a restore is itself undoable. KeyError if the id is
    unknown (or not a snapshot id at all — it becomes a file name)."""
    path = _history_dir(cfg) / f"{snapshot_id}.json"
    if not _SNAPSHOT_ID_RE.fullmatch(snapshot_id) or not path.is_file():
        raise KeyError(snapshot_id)
    data: dict = load_json(path, {})
    snapshot(cfg, "restore")
    return _write(
        cfg,
        list(data.get("words") or []),
        list(data.get("marked") or []),
        data.get("source") or "restore",
        data.get("seeded_top_n") or None,
        list(data.get("not_words") or []),
    )


def save(
    cfg: Config,
    words: list[str],
    source: str,
    mode: str = "replace",
    seeded_top_n: int | None = None,
) -> dict:
    """Write an imported list. The result is what was written, plus `snapshot`: the
    id of the copy taken before a replace (None for a merge, a seed step, or an
    empty list), so /known can offer Undo straight from the import's result."""
    data: dict = load_json(cfg.known_words, {})
    marked = list(data.get("marked") or [])
    sid = None
    if mode == "merge" and data.get("words"):
        merged = list(dict.fromkeys(list(data["words"]) + words))
    else:
        # "Replace" swaps out the *imported* list. Hand-marked words were
        # never part of it, so they survive — losing a set built up one click
        # at a time to a routine deck re-export would be a nasty surprise.
        # A seed step is the exception to snapshotting: it only ever replaces
        # an earlier seed (seed_common refuses otherwise), and a snapshot per
        # rung would push real imports out of the history.
        if seeded_top_n is None:
            sid = snapshot(cfg, "replace")
        merged = list(dict.fromkeys(words + marked))
    return {**_write(cfg, merged, marked, source, seeded_top_n), "snapshot": sid}


# Languages with a wordfreq frequency list. JP readers typically already build
# their known-words list from jpdb/Anki decks, so seed_common refuses to run
# against a profile that already carries a non-seed import — see the guard
# below — rather than silently replacing years of curated data with a generic
# frequency list.
SEEDABLE_LANGUAGES = ("en", "ja")
SEED_TOP_N = 10000
# /known's "add the next N words" ladder, one rung at a time above the initial
# SEED_TOP_N baseline — lets an advanced reader (already fluent, just without
# a curated list) skim and accept batches past the generic starting point
# instead of the frequent-words review staying cluttered with words they've
# known for years. 20k is wordfreq's practical ceiling for a dictionary-word
# list before the tail turns to noise (contractions, proper-noun-adjacent
# entries, etc.) — shared across languages since both lists degrade at a
# similar point. JA steps in smaller 100-word rungs than EN's 1,000: kanji
# vocabulary density means each rung costs more study effort per word.
SEED_STEP = {"en": 1000, "ja": 100}
SEED_MAX = 20000


def seed_common(cfg: Config, lang: str, top_n: int = SEED_TOP_N) -> dict:
    """Replace the known-words list with the top-N most frequent words for
    `lang` (wordfreq's frequency-ranked corpus) — the fast-start baseline for
    a language with no jpdb/Anki-style import to lean on, and also the
    mechanism behind each "add the next N" step (same operation, a higher
    top_n). `save`'s mode="replace" keeps hand-marked words, same as any
    other re-import. Never shrinks an existing seed — `top_n` is clamped up
    to whatever was seeded before, so a stray low value can't silently drop
    already-seeded words.

    Refuses to run against a profile whose list already has words from a real
    import (a JP reader's jpdb/Anki deck, most likely) rather than from a
    previous seed step: `mode="replace"` would silently discard it. Continuing
    an existing seed ladder is always allowed — there is nothing non-seed to
    lose.
    """
    if lang not in SEEDABLE_LANGUAGES:
        raise ValueError(f"Seeding is not supported for language: {lang!r}")

    data: dict = load_json(cfg.known_words, {})
    floor = _seeded_top_n(data)
    if floor == 0 and data.get("count"):
        raise ValueError(
            "This list already has words from an import, not a seed — clear "
            "it first if you want to replace it with the common-words seed."
        )
    top_n = min(max(top_n, floor), SEED_MAX)

    import wordfreq

    words = wordfreq.top_n_list(lang, top_n)
    n = f"{top_n // 1000}k" if top_n >= 1000 and top_n % 1000 == 0 else str(top_n)
    return save(cfg, words, source=f"seed:top-{n}-{lang}", mode="replace", seeded_top_n=top_n)


def seed_preview(cfg: Config, lang: str, top_n: int) -> dict:
    """The words the next seed step up to `top_n` would add — read-only, for
    /known's skim-before-apply flow. Excludes anything already known (seeded,
    imported, or hand-marked) so the preview only ever shows genuinely new
    words, and excludes anything past SEED_MAX.
    """
    if lang not in SEEDABLE_LANGUAGES:
        raise ValueError(f"Seeding is not supported for language: {lang!r}")

    data: dict = load_json(cfg.known_words, {})
    at = _seeded_top_n(data)
    top_n = min(top_n, SEED_MAX)

    import wordfreq

    known = set(data.get("words") or []) | set(data.get("marked") or [])
    words = [w for w in wordfreq.top_n_list(lang, max(top_n, at))[at:] if w not in known]
    return {"from": at, "to": top_n, "words": words}


def apply(
    cfg: Config,
    mark: list[str] = (),
    unmark: list[str] = (),
    skip: list[str] = (),
    unskip: list[str] = (),
) -> dict:
    """Change the hand-marked words and the set-aside words in one read and one
    `_write`.

    `_write` re-normalizes the whole list (about 0.2 s), so a reading session's
    dozens of status changes must not each pay for it: the reader's outbox lands
    here as a single call, and the caller runs one recompute afterwards. mark,
    unmark, skip and unskip are this function with one argument, so the two
    paths cannot drift apart. Removals are applied before additions, so a word
    named on both sides ends up added.

    The invariants are the single-purpose functions' own:
    - `unmark` removes only hand-marked words; anything that came from an import
      is reported back in `skipped` and left in place. This must not be able to
      eat the deck.
    - already-known words still join `marked`, so the click is reversible.
    - `source` is left alone: it records where the imported list came from.

    Returns `added` / `removed` (known list), `skipped` (unmark refusals),
    `skip_added` / `unskip_removed` (set-aside words), the totals `count`,
    `marked` and `not_words`, and `written`: whether the file was rewritten.
    """
    data: dict = load_json(cfg.known_words, {})
    words = list(data.get("words") or [])
    marked = list(data.get("marked") or [])
    not_words = list(data.get("not_words") or [])
    marked_set = set(marked)

    unmark_in = _clean(unmark)
    removed = [w for w in unmark_in if w in marked_set]
    skipped = [w for w in unmark_in if w not in marked_set]
    drop = set(removed)
    new_words = [w for w in words if w not in drop]
    new_marked = [w for w in marked if w not in drop]

    mark_in = _clean(mark)
    have = set(new_words)
    added = [w for w in mark_in if w not in have]
    new_words += added
    new_marked = list(dict.fromkeys(new_marked + mark_in))

    drop_aside = set(_clean(unskip))
    unskip_removed = [w for w in not_words if w in drop_aside]
    new_not = [w for w in not_words if w not in drop_aside]
    merged = list(dict.fromkeys(new_not + _clean(skip)))
    kept = set(new_not)
    skip_added = [w for w in merged if w not in kept]

    written = (new_words, new_marked, merged) != (words, marked, not_words)
    if written:
        out = _write(
            cfg, new_words, new_marked, data.get("source") or "marked",
            _seeded_top_n(data) or None, merged,
        )
        count = out["count"]
    else:
        count = len(words)
    return {
        "added": added, "removed": removed, "skipped": skipped,
        "skip_added": skip_added, "unskip_removed": unskip_removed,
        "count": count, "marked": len(new_marked), "not_words": len(merged),
        "written": written,
    }


def mark(cfg: Config, words: list[str]) -> dict:
    """Add hand-marked words to the list.

    `source` is left alone: it records where the imported list came from, and
    clicking a word off a study list does not change that answer.
    """
    res = apply(cfg, mark=words)
    return {"added": res["added"], "count": res["count"], "marked": res["marked"]}


def unmark(cfg: Config, words: list[str]) -> dict:
    """Remove hand-marked words.

    Only words in the `marked` set can be removed; anything that came from an
    import is reported back as skipped and left in place. This endpoint must
    not be able to eat the deck.
    """
    res = apply(cfg, unmark=words)
    return {"removed": res["removed"], "skipped": res["skipped"], "count": res["count"]}


def skip(cfg: Config, words: list[str]) -> dict:
    """Set words aside as "not a word": names and tokenizer fragments (ばあ from
    おばあちゃん) that the study list opens on and that no one can learn.

    Stored apart from the known list, so they never inflate it, and counted as
    free by `load_sets` — never a lookup, never unknown — the way UniDic proper
    nouns are. Like `marked`, the set survives a re-import.
    """
    res = apply(cfg, skip=words)
    return {"added": res["skip_added"], "count": res["not_words"]}


def unskip(cfg: Config, words: list[str]) -> dict:
    """Take words back out of the set-aside list."""
    res = apply(cfg, unskip=words)
    return {"removed": res["unskip_removed"], "count": res["not_words"]}


def clear(cfg: Config) -> None:
    snapshot(cfg, "clear")
    save_json(cfg.known_words, {})


def load_sets(cfg: Config) -> tuple[set[str], set[str]]:
    """(orthBase keys, lemma keys). A list stored before schema 2 has no
    normalized keys, so its raw words serve as the orthBase set — matching
    the old behaviour until it is re-imported."""
    data: dict = load_json(cfg.known_words, {})
    words = data.get("words") or []
    # Set-aside words are free orthBase keys only — no lemma, so skipping a
    # fragment can't free the real word it was cut from.
    aside = set(data.get("not_words") or [])
    if not words:
        return aside, set()
    return set(data.get("orth") or words) | aside, set(data.get("lemma") or [])


def status(cfg: Config, library: dict | None = None) -> dict:
    data: dict = load_json(cfg.known_words, {})
    return {
        "count": data.get("count", 0),
        "updated_at": data.get("updated_at"),
        "source": data.get("source"),
        "schema": data.get("schema"),
        # Split out so /known can show what was imported vs clicked by hand.
        "marked": len(data.get("marked") or []),
        # Names and fragments set aside as "not a word"; /known lists them for undo.
        "not_words": list(data.get("not_words") or []),
        # Surfaced so /known can prompt for a re-import after a schema bump.
        "normalized": bool(data.get("orth")),
        # Drives /known's "add the next 1,000 words" ladder; 0 if never seeded.
        "seeded_top_n": _seeded_top_n(data),
        **summary(cfg, library),
    }


def read_ids(cfg: Config) -> list[str]:
    """Book ids the reader has marked as read."""
    data: dict = load_json(cfg.read_books, {})
    return [bid for bid, e in (data.get("books") or {}).items() if e.get("read")]


def read_entries(cfg: Config) -> list[dict]:
    """Book ids the reader has marked read, with read_at, most-recent first.

    Unlike read_ids(), keeps read_at so /profile can render the list back to
    the reader; does no library join, so a book_id no longer in
    epub_library.json still comes back here — the caller decides whether to
    skip it.
    """
    data: dict = load_json(cfg.read_books, {})
    entries = [
        {"book_id": bid, "read_at": e.get("read_at")}
        for bid, e in (data.get("books") or {}).items() if e.get("read")
    ]
    entries.sort(key=lambda e: e["read_at"] or "", reverse=True)
    return entries


def read_split(cfg: Config, library: dict | None = None) -> tuple[list[dict], list[dict]]:
    """read_entries() split into (books still in the library, books that are not).

    The one definition of "a book you've read": /profile's count, its list and
    its "no longer in your library" line all come from here, so they cannot
    disagree. A missing/empty library index means "can't tell", not "all gone".
    """
    entries = read_entries(cfg)
    if library is None:
        library = load_json(cfg.library_index, {})
    if not library:
        return entries, []
    return (
        [e for e in entries if e["book_id"] in library],
        [e for e in entries if e["book_id"] not in library],
    )


def clear_orphan_reads(cfg: Config, library: dict | None = None) -> list[str]:
    """Drop this profile's read marks for books no longer in the library.

    Removes only the marks (reversible by marking again, if the book comes
    back); no file is touched. Returns the ids dropped. The caller recomputes
    and holds the index lock.
    """
    _, gone = read_split(cfg, library)
    ids = [e["book_id"] for e in gone]
    if ids:
        data: dict = load_json(cfg.read_books, {})
        books = data.get("books") or {}
        for bid in ids:
            books.pop(bid, None)
        data["schema"] = SCHEMA_VERSION
        save_json(cfg.read_books, data)
    return ids


def mark_read(cfg: Config, book_id: str, read: bool = True) -> dict:
    data: dict = load_json(cfg.read_books, {})
    books = data.setdefault("books", {})
    if read:
        books[book_id] = {
            "read": True,
            "read_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    else:
        books.pop(book_id, None)
    data["schema"] = SCHEMA_VERSION
    save_json(cfg.read_books, data)
    return data


def set_read_date(cfg: Config, book_id: str, day: date) -> str:
    """Set the day this profile finished `book_id`; returns the stored `read_at`.

    Stored as that day at noon UTC so the page's month label (the viewer's local
    time) cannot slip into the neighbouring month in any timezone. Only a book
    already marked read can be dated (`KeyError` otherwise): this never creates a
    mark, and it leaves exposure alone, which depends on *which* books are read.
    The caller holds the index lock.
    """
    data: dict = load_json(cfg.read_books, {})
    entry = (data.get("books") or {}).get(book_id)
    if not entry or not entry.get("read"):
        raise KeyError(book_id)
    entry["read_at"] = datetime.combine(day, time(12), tzinfo=timezone.utc).isoformat(timespec="seconds")
    data["schema"] = SCHEMA_VERSION
    save_json(cfg.read_books, data)
    return entry["read_at"]


def exposure_counts(cfg: Config) -> dict[str, int]:
    """orthBase form → how many books marked read used it at least
    EXPOSURE_MIN times.

    Derived from the stored wordlists, so finishing a book widens the known
    vocabulary for every other book without any re-scanning. The per-book
    tally (rather than a flat set) lets a book be scored against exposure
    from every read book *except itself* — otherwise a book the reader has
    finished would trivially report near-zero strain against its own
    vocabulary.
    """
    counts: dict[str, int] = {}
    for bid in read_ids(cfg):
        wl = load_json(cfg.wordlists_dir / f"{bid}.json", {})
        for w, c in (wl.get("words") or {}).items():
            if c >= EXPOSURE_MIN:
                counts[w] = counts.get(w, 0) + 1
    return counts


def exposure_for(counts: dict[str, int], own: dict | None) -> set[str]:
    """The exposed vocabulary available when scoring one book, excluding that
    book's own contribution."""
    if not own:
        return set(counts)
    return {
        w for w, n in counts.items()
        if n > (1 if (own.get(w) or 0) >= EXPOSURE_MIN else 0)
    }


def known_mask(defs: list, orth: set[str], lemma: set[str]) -> list[bool]:
    """Per word-type known flags for a sentence index's `words` table.

    Word defs are [orthBase, lemma, is_free, reading, difficulty, goshu];
    goshu is absent from indices older than schema 5. "Free"
    words are never a lookup regardless of the known list: proper nouns and
    numerals (a character or place name is learned once on the page that
    introduces it, and counting every mention would make any novel look
    impassable) and bound affixes like たち/くん, which attach to a stem
    rather than carrying separate meaning.
    """
    return [
        bool(d[2]) or d[0] in orth or d[1] in lemma for d in defs
    ]


def _percentile(sorted_vals: list[int], q: float) -> float:
    """Nearest-rank percentile of an already-sorted list."""
    if not sorted_vals:
        return 0.0
    i = min(len(sorted_vals) - 1, max(0, round(q * len(sorted_vals)) - 1))
    return float(sorted_vals[i])


def walk(sents: list, known_flags: list[bool]) -> tuple[list[int], list[int], list[int]]:
    """Read the book in order, applying the in-book acquisition rule.

    Returns (unknowns per sentence, content tokens between lookups,
    occurrence count per word type).
    """
    counts = [0] * len(known_flags)
    seen = [0] * len(known_flags)
    per_sentence: list[int] = []
    gaps: list[int] = []
    run = 0
    for ids in sents:
        unknown = 0
        for wid in ids:
            counts[wid] += 1
            if known_flags[wid]:
                run += 1
                continue
            seen[wid] += 1
            if seen[wid] > ACQUIRE_AFTER:
                run += 1
                continue
            unknown += 1
            gaps.append(run)
            run = 0
        per_sentence.append(unknown)
    return per_sentence, gaps, counts


def _profile_from(per_sentence: list[int]) -> dict:
    """flow / i+1 / hard sentence percentages from an already-walked book."""
    n = len(per_sentence) or 1
    return {
        "flow_pct": round(100 * sum(1 for u in per_sentence if u == 0) / n, 1),
        "i1_pct": round(100 * sum(1 for u in per_sentence if u == 1) / n, 1),
        "hard_pct": round(
            100 * sum(1 for u in per_sentence if u >= STRAIN_THRESHOLD) / n, 1
        ),
    }


def profile(sents: list, known_flags: list[bool]) -> dict:
    """flow / i+1 / hard sentence percentages for one book.

    `study._curve` needs this: the acquisition rule is order-dependent, so
    each curve point (a different set of known_flags) requires its own walk.
    """
    per_sentence, _, _ = walk(sents, known_flags)
    return _profile_from(per_sentence)


def _reading_load(
    sent_index: dict, orth: set[str], lemma: set[str]
) -> tuple[dict, list[int], list[bool]] | None:
    """Sentence-level metrics for one book, plus the per-word-id occurrence
    counts and known flags backing them — `recompute()` reuses those to
    derive orthBase-space coverage without a second walk of `sents`."""
    defs: list = sent_index.get("words") or []
    sents: list = sent_index.get("sents") or []
    if not defs or not sents:
        return None

    known_flags = known_mask(defs, orth, lemma)
    per_sentence, gaps, counts = walk(sents, known_flags)

    n_sents = len(sents)
    total = sum(counts)
    if not n_sents or not total:
        return None
    known_tokens = sum(c for c, k in zip(counts, known_flags) if k)

    # Cheapest path to fluent coverage: learn this book's most frequent
    # unknown words until 98% of its tokens are covered.
    need = FLUENT_COVERAGE * total - known_tokens
    words_to_98 = 0
    if need > 0:
        unknown_counts = sorted(
            (c for c, k in zip(counts, known_flags) if not k and c), reverse=True
        )
        for c in unknown_counts:
            if need <= 0:
                break
            need -= c
            words_to_98 += 1

    # Diagnostic: how much of the strain is loanwords (goshu 外)? Re-walk
    # with them treated as known; the drop in lookups/hard sentences is
    # their contribution. Not used for ranking.
    loan = [len(d) > 5 and d[5] == "外" for d in defs]
    loan_metrics: dict = {}
    if any(loan):
        flags2 = [k or ln for k, ln in zip(known_flags, loan)]
        per2, gaps2, _ = walk(sents, flags2)
        loan_metrics = {
            "hard_pct_ex_loan": _profile_from(per2)["hard_pct"],
            "loan_lookup_pct": (
                round(100 * (1 - len(gaps2) / len(gaps)), 1) if gaps else 0.0
            ),
        }

    metrics = {
        **_profile_from(per_sentence),
        **loan_metrics,
        "unknown_p90": _percentile(sorted(per_sentence), 0.9),
        "tokens_per_lookup": round(sum(gaps) / len(gaps), 1) if gaps else 0.0,
        "words_to_98": words_to_98,
        # Lets the dialogue-heavy-books bias be checked: many very short
        # sentences inflate flow_pct without the book being easier.
        "avg_sentence_content": round(total / n_sents, 1),
    }
    return metrics, counts, known_flags


def recompute(cfg: Config) -> dict:
    """Derive per-book coverage and reading load from the analyze stage's
    stored wordlists and sentence indices."""
    from .analyze import load_sentences
    from . import profiles

    studied, lemma = load_sets(cfg)
    exposure = exposure_counts(cfg)
    read = set(read_ids(cfg))
    vocab: dict = load_json(cfg.vocab_index, {})

    # Score only books in the language the active profile is learning. The
    # wordlists/sentences dirs are global (both languages), and a JP reader's
    # words match ~nothing in an English book (and vice versa) — so an
    # unfiltered sweep would fill this reader's stats (and their Read Next) with
    # every book of the *other* language at ~0% known. None = no library index
    # yet, so don't filter.
    lang = profiles.active_language(cfg)
    library: dict = load_json(cfg.library_index, {})
    lang_ids: set[str] | None = (
        {bid for bid, b in library.items() if b.get("language") == lang}
        if library else None
    )

    stats: dict = {}
    loaded = 0
    if (studied or exposure) and cfg.wordlists_dir.exists():
        for wl_path in sorted(cfg.wordlists_dir.glob("*.json")):
            bid = wl_path.stem
            if lang_ids is not None and bid not in lang_ids:
                continue
            wl = load_json(wl_path, {})
            words: dict = wl.get("words") or {}
            if not words:
                continue
            bid = wl.get("book_id") or bid
            total = wl.get("total") or sum(words.values())

            # Vocabulary available to this book: what the reader studied, plus
            # what they met in other books they finished.
            exposed = exposure_for(exposure, words)
            orth = studied | exposed

            # Coverage from the wordlist (orthBase-keyed, no lemma column) —
            # the lemma space still helps via words whose two spellings both
            # appear in the export.
            known_unique = 0
            known_tokens = 0
            exposed_unique = 0
            for w, c in words.items():
                if w in orth:
                    known_unique += 1
                    known_tokens += c
                    if w not in studied:
                        exposed_unique += 1
            entry = {
                "known_pct": round(100 * known_tokens / total, 1) if total else 0.0,
                "known_unique": known_unique,
                "known_unique_pct": round(100 * known_unique / len(words), 1),
                # Share of the above that rests on exposure rather than study,
                # so a high known_pct built entirely on skimmed books is visible.
                "exposed_unique": exposed_unique,
                "read": bid in read,
            }

            sent_index = load_sentences(cfg.sentences_dir / f"{bid}.json.gz")
            load = _reading_load(sent_index, orth, lemma) if sent_index else None
            if load:
                metrics, word_counts, flags = load
                loaded += 1
                entry.update(metrics)
                # Recount coverage in the sentence index's space, where the
                # lemma fallback and the proper-noun rule both apply. This is
                # the figure the reading-load numbers are consistent with.
                # Aggregated from the walk() counts _reading_load() already
                # computed, keyed by orthBase, rather than re-walking sents.
                defs = sent_index["words"]
                counts: dict[str, list[int]] = {}
                for wid, c in enumerate(word_counts):
                    if not c:
                        continue
                    slot = counts.setdefault(defs[wid][0], [0, 0])
                    slot[0] += c
                    slot[1] |= flags[wid]
                tok_total = sum(v[0] for v in counts.values())
                tok_known = sum(v[0] for v in counts.values() if v[1])
                if tok_total:
                    entry["known_pct"] = round(100 * tok_known / tok_total, 1)
                    entry["known_unique"] = sum(1 for v in counts.values() if v[1])
                    entry["known_unique_pct"] = round(
                        100 * entry["known_unique"] / len(counts), 1
                    )
                # jpdb counts particles and aux verbs as known grammar, so its
                # denominator is words_total. Reported alongside for
                # comparability with the numbers jpdb publishes.
                words_total = (vocab.get(bid) or {}).get("words_total") or 0
                if words_total and tok_total:
                    function_words = max(0, words_total - tok_total)
                    entry["known_pct_words"] = round(
                        100 * (tok_known + function_words) / words_total, 1
                    )

            stats[bid] = entry
    save_json(cfg.known_stats, stats)
    print(
        f"Known stats [{lang}]: {len(stats)} books against {len(studied)} studied "
        f"+ {len(exposure)} exposed orth / {len(lemma)} lemma keys "
        f"({loaded} with reading load, {len(read)} books read)."
    )
    return stats


def recompute_moved(cfg: Config) -> tuple[dict, int]:
    """recompute(), plus how many books' Known % or strain it actually changed — what a
    mark on /known can honestly promise ("moved on 12 books"), where len(stats) is every
    book scored."""
    before: dict = load_json(cfg.known_stats, {})
    stats = recompute(cfg)
    moved = sum(
        1 for bid, entry in stats.items()
        if any((before.get(bid) or {}).get(k) != entry.get(k) for k in ("known_pct", "hard_pct"))
    )
    return stats, moved


def recompute_all(cfg: Config, languages: set[str] | None = None) -> dict[str, dict]:
    """recompute() for every profile — or only those learning one of `languages`
    ("ja"/"en"; None = all). Use after any change to the shared library
    (analyze, delete, a pipeline run): each profile's known_stats.json is
    derived from the same wordlists, so they all go stale together.
    Request-scoped actions (mark, import, read toggle) only affect the
    requester and keep calling recompute().

    Returns {slug: stats}."""
    from dataclasses import replace

    from . import profiles

    out: dict[str, dict] = {}
    for p in profiles.list_profiles(cfg)["profiles"]:
        if languages is not None and p["language"] not in languages:
            continue
        out[p["slug"]] = recompute(replace(cfg, active_user=p["slug"]))
    return out


def summary(cfg: Config, library: dict | None = None) -> dict:
    """Vocabulary totals for the /known and /profile pages.

    `books_read` counts only books still in the library (see read_split);
    `orphan_reads` is the rest. Exposure is untouched: it reads each read book's
    stored wordlist, and a deleted book's wordlist is purged with it
    (purge_book), so an orphaned mark contributes nothing unless a wordlist
    from before deletes purged it survives — and then its words really were met.
    """
    studied, lemma = load_sets(cfg)
    exposure = exposure_counts(cfg)
    live, gone = read_split(cfg, library)
    return {
        "studied": len(studied),
        "exposed": len(set(exposure) - studied),
        # So /profile can say "met N+ times" without a copy of the constant.
        "exposure_min": EXPOSURE_MIN,
        "lemma_keys": len(lemma),
        "books_read": len(live),
        "orphan_reads": len(gone),
    }
