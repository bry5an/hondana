"""The cheapest vocabulary path into one book.

A book that is out of reach is not equally out of reach in every direction:
its unknown words are Zipf-distributed, so a few hundred of the most frequent
ones account for most of the lookups. This module ranks a book's unknown
words by in-book frequency and reports what learning the top N would do to
its reading load — turning "too hard for now" into a countable distance.

Each word is shown with an example sentence drawn from the book itself, and
specifically from a sentence where that word is the *only* unknown, so the
surrounding context is fully comprehensible. Words stick when they arrive
attached to a sentence the reader can actually parse; a bare frequency list
does not do that.

Sentence text is not stored — each sentence's (start, end) character offsets
into the extracted text are (in `spans`, recorded at analyze time), so an
example is a direct slice of a fresh extraction (~30 ms) rather than a
re-split that has to land on the same boundaries the analyzer used. That
distinction matters for English: spaCy's sentence boundaries don't line up
with `split_sentences`' JP-punctuation regex, so indexing a fresh JP-style
split by position used to return an unrelated chunk of text. The
book's mtime/filesize are checked against what the analyze stage recorded,
and examples are omitted rather than risking offsets into a changed file.
"""
from __future__ import annotations

from . import epub_text, known as known_mod
from .analyze import load_sentences
from .config import Config
from .index_store import load_json

# Points on the "what if I learned the top N" curve.
CURVE_POINTS = (0, 25, 50, 100, 250, 500, 1000, 2000)
# An example sentence is only useful while the reader can parse the rest of
# it; beyond this many other unknowns it stops being an example and becomes
# another puzzle.
MAX_EXAMPLE_UNKNOWNS = 3
# ...and it has to be a sentence. A chapter heading (「3 フグ料理」) or the bare
# word as its own line (「引く。」) contains the word but teaches nothing about
# how it is used, so require some surrounding context.
MIN_EXAMPLE_TOKENS = 5
# Past this length a sentence stops being a crisp example, so extra words earn
# no further preference.
IDEAL_EXAMPLE_TOKENS = 20


def _curve(sents: list, known: list[bool], ranked: list[int]) -> list[dict]:
    """Reading load after learning the top N words, for N in CURVE_POINTS.

    Uses the same walk as the library-wide numbers, so the N=0 point equals
    the book's reported flow/hard percentages.
    """
    out = []
    flags = list(known)
    learned = 0
    for n in CURVE_POINTS:
        if n > len(ranked):
            break
        for wid in ranked[learned:n]:
            flags[wid] = True
        learned = n
        out.append({"n": n, **known_mod.profile(sents, flags)})
    return out


def study_path(cfg: Config, book_id: str, limit: int = 100) -> dict:
    """Top unknown words of one book, with example sentences and the curve."""
    sent_index = load_sentences(cfg.sentences_dir / f"{book_id}.json.gz")
    if not sent_index or not sent_index.get("sents"):
        return {"book_id": book_id, "status": "not_analyzed", "words": [],
                "curve": []}

    defs: list = sent_index["words"]
    sents: list = sent_index["sents"]
    # Same vocabulary the library-wide numbers use: studied words plus what
    # was met in other books already read.
    studied, lemma = known_mod.load_sets(cfg)
    wordlist = load_json(cfg.wordlists_dir / f"{book_id}.json", {})
    exposed = known_mod.exposure_for(
        known_mod.exposure_counts(cfg), wordlist.get("words")
    )
    orth = studied | exposed
    if not orth:
        return {"book_id": book_id, "status": "no_known_words", "words": [],
                "curve": []}
    known = known_mod.known_mask(defs, orth, lemma)

    # One pass: occurrence counts, plus the best example sentence per unknown
    # word (fewest competing unknowns, earliest in the book on a tie).
    counts = [0] * len(defs)
    best: dict[int, tuple[tuple, int]] = {}
    for si, ids in enumerate(sents):
        unknown = []
        for wid in ids:
            counts[wid] += 1
            if not known[wid]:
                unknown.append(wid)
        if not unknown or len(unknown) > MAX_EXAMPLE_UNKNOWNS:
            continue
        if len(ids) < MIN_EXAMPLE_TOKENS:
            continue
        # Fewest competing unknowns first, then the most context, then
        # earliest in the book.
        rank = (len(unknown), -min(len(ids), IDEAL_EXAMPLE_TOKENS))
        for wid in set(unknown):
            if wid not in best or rank < best[wid][0]:
                best[wid] = (rank, si)

    ranked = sorted(
        (w for w in range(len(defs)) if not known[w] and counts[w]),
        key=lambda w: -counts[w],
    )
    curve = _curve(sents, known, ranked)
    top = ranked[:limit]

    # Resolve example sentences, but only if the file still matches what was
    # analyzed — otherwise the stored offsets no longer line up.
    texts: dict[int, str] = {}
    library = load_json(cfg.library_index, {})
    vocab = load_json(cfg.vocab_index, {})
    book = library.get(book_id) or {}
    entry = vocab.get(book_id) or {}
    stale = (
        not book.get("path")
        or entry.get("mtime") != book.get("mtime")
        or entry.get("filesize") != book.get("filesize")
    )
    spans: list = sent_index.get("spans") or []
    if not stale and spans:
        wanted = {best[w][1] for w in top if w in best}
        if wanted:
            text = epub_text.extract_text(cfg.epub_dir / book["path"])
            if text:
                for si in wanted:
                    if si < len(spans):
                        start, end = spans[si]
                        texts[si] = text[start:end].strip()

    words = []
    for wid in top:
        base, lm, proper, kana, difficulty = defs[wid][:5]
        si = best.get(wid, (0, None))[1]
        words.append({
            "word": base,
            "reading": kana,
            "count": counts[wid],
            "difficulty": difficulty if difficulty >= 0 else None,
            "example": texts.get(si) if si is not None else None,
        })

    return {
        "book_id": book_id,
        "status": "ok",
        "stale": stale,
        "unknown_types": len(ranked),
        "words": words,
        "curve": curve,
    }
