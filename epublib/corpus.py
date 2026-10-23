"""The words missing from the known list that cost the most across the library.

study.py answers "what should I learn to read *this* book". This answers the
prior question: which gaps in the known list are dragging down every book at
once. A word met in seventy books is worth marking off before a name that
appears in one, however often it recurs there — so the ranking is by breadth
first (how many books use it) and only then by total occurrences.

In practice the top of this list is not vocabulary to study at all. It is
ただ, おく, 持つ, すぐ, わけ, もらう — words the reader has known for years and
never made a flashcard for, which is exactly why they are absent from a deck
export and counted as unknown in every book. Marking them off is a correction
to the measurement, not a study plan.

Read from the per-sentence index rather than wordlists/: only it carries the
lemma (needed for the second matching space), the reading and difficulty shown
in the UI, and the free-word flag that keeps proper nouns out of the list.
A cold sweep is ~0.6-0.8 s (gunzip + JSON parse of every index); the reader-independent
tallies (`_aggregate`) and the final ranked list (`_ranked`) are cached in memory, so
repeat calls are a slice of an already-sorted list.
"""
from __future__ import annotations

import threading
from bisect import bisect_right
from pathlib import Path

from . import known as known_mod
from .analyze import load_sentences
from .config import Config


def _hira(text: str) -> str:
    """Katakana → hiragana, so a reading (トル) compares with a spelling (とる)."""
    return "".join(
        chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in text or ""
    )


def _is_kana(text: str) -> bool:
    return bool(text) and all(
        "ぁ" <= c <= "ゖ" or "ァ" <= c <= "ヶ" or c == "ー" for c in text
    )


# The sweep over every sentence index is ~0.6 s of gunzip + JSON parse, and
# none of it depends on the reader: it is word-type tallies per book. Only the
# known/unknown test does, and that is cheap. So the tallies are cached in
# memory and the known sets are applied fresh on every request — marking a
# word is visible on the very next call, with no invalidation to get wrong.
# Keyed by the files' (name, mtime, size), so a re-analyze or a language
# switch rebuilds it by itself.
_agg_lock = threading.Lock()
_agg_cache: tuple[tuple, tuple[int, dict]] | None = None


def _aggregate(cfg: Config, lang_ids: set[str] | None) -> tuple[tuple, int, dict]:
    """(signature, books scanned, {(orthBase, lemma): [total, book ids,
    reading, difficulty, loan]}) over every non-free word, in first-seen
    order. The signature names the exact set of files this was built from."""
    global _agg_cache
    paths = []
    for path in sorted(cfg.sentences_dir.glob("*.json.gz")):
        # Filename is {book_id}.json.gz; book_id is hex, so split on the first
        # dot. Skip other-language books before paying to decompress them.
        if lang_ids is not None and path.name.split(".", 1)[0] not in lang_ids:
            continue
        st = path.stat()
        paths.append((path, st.st_mtime_ns, st.st_size))
    sig = (str(cfg.sentences_dir), tuple((p.name, m, z) for p, m, z in paths))

    with _agg_lock:
        if _agg_cache and _agg_cache[0] == sig:
            return (sig, *_agg_cache[1])

        agg: dict[tuple[str, str], list] = {}
        scanned = 0
        for path, _, _ in paths:
            index = load_sentences(path)
            defs: list = index.get("words") or []
            sents: list = index.get("sents") or []
            if not defs or not sents:
                continue
            scanned += 1

            counts = [0] * len(defs)
            for ids in sents:
                for wid in ids:
                    counts[wid] += 1

            for wid, d in enumerate(defs):
                # d[5] (goshu) only exists from analyze schema 5 on.
                base, lm, free, kana, difficulty = d[:5]
                if free or not counts[wid]:
                    continue
                e = agg.get((base, lm))
                if e is None:
                    e = agg[(base, lm)] = [
                        0, set(), kana,
                        difficulty if difficulty >= 0 else None, False,
                    ]
                e[0] += counts[wid]
                if len(d) > 5 and d[5] == "外":
                    e[4] = True
                e[1].add(scanned)

        _agg_cache = (sig, (scanned, agg))
        return (sig, *_agg_cache[1])


def _stat(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


# The ranked review list is a pure function of the library's sentence files,
# the reader's known list, the books they have marked read (exposure) and the
# language — and each of those lives in a file. So the whole ranked list is
# cached per profile and keyed by those files' stats: nothing is recomputed
# until a book is added/removed/re-analyzed, a word is marked or imported, or
# a book is marked read. Every write goes through save_json (os.replace), which
# changes the mtime, so there are no invalidation hooks to forget to call.
_rank_lock = threading.Lock()
_rank_cache: dict[str, tuple[tuple, dict]] = {}


def _fingerprint(cfg: Config, lang: str) -> tuple:
    return (
        lang,
        _stat(cfg.known_words),
        _stat(cfg.read_books),
        _stat(cfg.library_index),
        # What exposure_counts() reads: one wordlist per book marked read.
        tuple((bid, _stat(cfg.wordlists_dir / f"{bid}.json"))
              for bid in sorted(known_mod.read_ids(cfg))),
    )


def _ranked(cfg: Config) -> dict | None:
    """Every unknown word type, broadest-reach first, cached (see above).
    None when the library has not been analyzed."""
    from . import profiles
    from .index_store import cached_json

    if not cfg.sentences_dir.exists():
        return None

    # Only the language the active profile is learning: the sentences dir is
    # global (both languages), so without this an English reader's review list
    # would be flooded with Japanese words they'll never match, and vice versa.
    # None = no library index yet, so don't filter.
    lang = profiles.active_language(cfg)
    fp = _fingerprint(cfg, lang)
    key = str(cfg.known_words)

    def fresh(sig: tuple | None) -> dict | None:
        hit = _rank_cache.get(key)
        if hit and hit[0] == (fp, sig):
            return hit[1]
        return None

    library: dict = cached_json(cfg.library_index, {})
    lang_ids: set[str] | None = (
        {bid for bid, b in library.items() if b.get("language") == lang}
        if library else None
    )
    # Cheap when warm: a stat per sentence file, no parsing.
    sig, scanned, agg = _aggregate(cfg, lang_ids)
    hit = fresh(sig)
    if hit is not None:
        return hit

    with _rank_lock:
        hit = fresh(sig)
        if hit is not None:
            return hit
        result = _rank(cfg, scanned, agg)
        _rank_cache[key] = ((fp, sig), result)
        return result


def _rank(cfg: Config, scanned: int, agg: dict) -> dict:
    studied, lemma = known_mod.load_sets(cfg)
    # No book to exclude here — this is the whole library at once — so the
    # exposed set is used flat, unlike the per-book exposure_for().
    orth = studied | set(known_mod.exposure_counts(cfg))

    # Per-spelling tallies; kana/kanji pairs are folded together below.
    spell: dict[str, dict] = {}
    known_any: set[str] = set()
    for (base, lm), (total, books, kana, difficulty, loan) in agg.items():
        if base in orth or lm in lemma:
            # Known in either space — recorded rather than skipped, since
            # the same spelling may appear under a different lemma in
            # another book and must not sneak back onto the list there.
            known_any.add(base)
        e = spell.get(base)
        if e is None:
            e = spell[base] = {
                "total": 0, "books": set(), "lemma": lm, "reading": kana,
                "difficulty": difficulty, "loan": False,
            }
        e["total"] += total
        e["loan"] = e["loan"] or loan
        # One book, one vote: a book's several entries per orthBase
        # (different lemma/POS/reading) must not count as several books.
        e["books"] |= books

    # Fold a pure-kana spelling (あたる) into the kanji spelling it is the
    # reading of (当たる): same lemma and same reading. Scoring already treats
    # them as one word (known_mask matches on lemma); this only stops the
    # review list showing one gap twice. Kanji-to-kanji variants (撮る/獲る/
    # 採る share lemma 取る) are different words and stay separate rows.
    live = {w: e for w, e in spell.items() if w not in known_any}
    by_key: dict[tuple[str, str], list[str]] = {}
    for w, e in live.items():
        if not _is_kana(w):
            by_key.setdefault((e["lemma"], _hira(e["reading"])), []).append(w)
    folded: dict[str, list[str]] = {}
    for w, e in live.items():
        if not _is_kana(w):
            continue
        hosts = by_key.get((e["lemma"], _hira(w)))
        if hosts:
            host = max(hosts, key=lambda h: live[h]["total"])
            folded.setdefault(host, []).append(w)
    absorbed = {w for ws in folded.values() for w in ws}

    rows = []
    for w, e in live.items():
        if w in absorbed:
            continue
        extra = [live[x] for x in folded.get(w, [])]
        books = set(e["books"]).union(*(x["books"] for x in extra)) if extra else e["books"]
        rows.append({
            "word": w,
            "variants": [w, *folded.get(w, [])],
            "reading": e["reading"],
            "difficulty": e["difficulty"],
            "loan": e["loan"],
            "books": len(books),
            "total": e["total"] + sum(x["total"] for x in extra),
        })

    def rank(r: dict) -> tuple[int, int, str]:
        return (-r["books"], -r["total"], r["word"])

    rows.sort(key=rank)
    loans = [r for r in rows if r["loan"]]
    return {
        "scanned": scanned,
        "unknown_types": len(rows),
        "loan_types": len(loans),
        # Parallel to each list: the rank keys, for bisecting to a cursor.
        "all": (rows, [rank(r) for r in rows]),
        "loan": (loans, [rank(r) for r in loans]),
    }


def frequent_unknown(
    cfg: Config,
    limit: int = 500,
    loan_only: bool = False,
    after: tuple[int, int, str] | None = None,
) -> dict:
    """Unknown word types across every analyzed book, broadest-reach first.

    `loan_only` keeps just the katakana loanwords (goshu 外). It must filter
    before the `limit` cut: a loanword used in one book ranks far below the
    top `limit` overall, so filtering the already-truncated list client-side
    would hide it.

    `after` is a (books, total, word) cursor — the last row already shown —
    and the page returned is the rows ranked below it. A cursor rather than
    an offset because marking words known removes them from this list
    between pages, which would shift every offset and skip rows.
    """
    ranked = _ranked(cfg)
    if ranked is None:
        return {"status": "not_analyzed", "books_scanned": 0,
                "unknown_types": 0, "words": []}

    rows, keys = ranked["loan" if loan_only else "all"]
    start = bisect_right(keys, (-after[0], -after[1], after[2])) if after else 0
    return {
        "status": "ok",
        "books_scanned": ranked["scanned"],
        "unknown_types": ranked["unknown_types"],
        "loan_types": ranked["loan_types"],
        "has_more": len(rows) - start > limit,
        "words": rows[start:start + limit],
    }
