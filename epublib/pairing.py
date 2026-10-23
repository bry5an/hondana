"""Pair English and Japanese editions of the same work.

The bridge across scripts is the **author**: difflib scores 住野よる vs
"Yoru Sumino" at ~0, so we romanize the Japanese author (offline, via romaji.py)
and compare order-independent token sets. Titles can't be matched across scripts
in general, so they're only a weak tie-breaker (a Latin substring embedded in a
JP title, e.g. the JP Harry Potter title, or a romaji-vs-English title bonus).

To avoid wrong links we **only auto-pair confident, unambiguous matches**: a high
author score where each side has exactly one strong candidate. Pen-name readings
(雨穴 → "ame ana", not "Uketsu") and same-author/multi-book cases stay unpaired
and surface on the /pair review page for the user to confirm.

Overrides (`pairing_overrides.json`) are keyed by **stable identifiers**
(asin/isbn/path), not the volatile book_id, so manual links survive rescans:
`force` (pairs the reader linked), `block` (pairs the reader unlinked or rejected) and
`none` (books the reader said have no other edition: never auto-paired or suggested).
Output (`pairings.json`): { pair_id: {en, jp, score, source} }.
"""
from __future__ import annotations

import difflib
import re

from . import romaji
from .config import Config
from .index_store import load_json, save_json

AUTHOR_AUTO = 0.9   # author score required to auto-pair
SUGGEST_MIN = 0.45  # author score to surface as a review suggestion
TOP_N = 4           # candidates shown per JP book on the review page

_LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9'’.\- ]{3,}")


# ── stable keys (survive rescans) ────────────────────────────────────────────

def stable_key(book: dict) -> str:
    ids = book.get("identifiers") or {}
    if ids.get("asin"):
        return f"asin:{ids['asin']}"
    if ids.get("isbn"):
        return f"isbn:{ids['isbn']}"
    return f"path:{book.get('path')}"


def _key_to_id(library: dict) -> dict:
    return {stable_key(b): b["book_id"] for b in library.values()}


# ── scoring ──────────────────────────────────────────────────────────────────

def _author_score(en: dict, jp: dict) -> float:
    et = romaji.author_tokens(en.get("author"))
    jt = romaji.author_tokens(jp.get("author"))
    if not et or not jt:
        return 0.0
    jaccard = len(et & jt) / len(et | jt)
    seq = difflib.SequenceMatcher(None, " ".join(sorted(et)), " ".join(sorted(jt))).ratio()
    return max(jaccard, seq)


def _title_bonus(en: dict, jp: dict) -> float:
    en_title = (en.get("title") or "").lower()
    if not en_title:
        return 0.0
    best = 0.0
    # Latin text embedded in the JP title (some files carry the English title).
    for m in _LATIN_RE.findall(jp.get("title") or ""):
        best = max(best, difflib.SequenceMatcher(None, m.strip().lower(), en_title).ratio())
    # Romaji of the JP title vs the English title (usually weak, occasionally helps).
    jr = romaji.to_romaji(jp.get("title") or "")
    if jr:
        best = max(best, difflib.SequenceMatcher(None, jr, en_title).ratio())
    return best


def _combined(en: dict, jp: dict) -> tuple[float, float]:
    """Return (author_score, combined_score)."""
    a = _author_score(en, jp)
    t = _title_bonus(en, jp)
    return a, round(a * 0.8 + t * 0.2, 3)


# ── core pairing ─────────────────────────────────────────────────────────────

def _split(library: dict) -> tuple[list, list]:
    en = [b for b in library.values() if b.get("language") == "en"]
    jp = [b for b in library.values() if b.get("language") == "ja"]
    return en, jp


def _resolve_overrides(library: dict, overrides: dict):
    """Return (forced_pairs[(en_id,jp_id)], blocked_set[frozenset(ids)])."""
    k2id = _key_to_id(library)
    forced = []
    for p in overrides.get("force", []):
        en_id, jp_id = k2id.get(p.get("en")), k2id.get(p.get("jp"))
        if en_id and jp_id:
            forced.append((en_id, jp_id))
    blocked = set()
    for p in overrides.get("block", []):
        en_id, jp_id = k2id.get(p.get("en")), k2id.get(p.get("jp"))
        if en_id and jp_id:
            blocked.add(frozenset((en_id, jp_id)))
    return forced, blocked


def _none_ids(library: dict, overrides: dict) -> set:
    """book_ids the reader marked "no other edition"."""
    k2id = _key_to_id(library)
    return {k2id[k] for k in overrides.get("none", []) if k in k2id}


def _build_pairings(library: dict, overrides: dict) -> dict:
    en_books, jp_books = _split(library)
    forced, blocked = _resolve_overrides(library, overrides)
    none = _none_ids(library, overrides)

    pairings: dict = {}
    used_en: set[str] = set()
    used_jp: set[str] = set()
    counter = 0

    def add(en_id, jp_id, score, source):
        nonlocal counter
        pid = f"pair_{counter:04d}"
        counter += 1
        pairings[pid] = {"en": en_id, "jp": jp_id, "score": round(score, 3), "source": source}
        used_en.add(en_id)
        used_jp.add(jp_id)

    # 1. Forced pairs win unconditionally.
    for en_id, jp_id in forced:
        if en_id not in used_en and jp_id not in used_jp:
            add(en_id, jp_id, 1.0, "override")

    # 2. Auto-pair confident + mutually-unique author matches.
    # Candidate author matches above the auto threshold (excluding blocked/used).
    strong: list[tuple[float, str, str]] = []  # (combined, en_id, jp_id)
    en_strong: dict[str, list[str]] = {}
    jp_strong: dict[str, list[str]] = {}
    for en in en_books:
        if en["book_id"] in used_en or en["book_id"] in none:
            continue
        for jp in jp_books:
            if jp["book_id"] in used_jp or jp["book_id"] in none:
                continue
            if frozenset((en["book_id"], jp["book_id"])) in blocked:
                continue
            a, combined = _combined(en, jp)
            if a >= AUTHOR_AUTO:
                strong.append((combined, en["book_id"], jp["book_id"]))
                en_strong.setdefault(en["book_id"], []).append(jp["book_id"])
                jp_strong.setdefault(jp["book_id"], []).append(en["book_id"])

    # Only link where both sides have exactly one strong candidate (unambiguous).
    for combined, en_id, jp_id in sorted(strong, reverse=True):
        if en_id in used_en or jp_id in used_jp:
            continue
        if len(en_strong.get(en_id, [])) == 1 and len(jp_strong.get(jp_id, [])) == 1:
            add(en_id, jp_id, combined, "auto")

    return pairings


def pair(cfg: Config) -> dict:
    library: dict = load_json(cfg.library_index, {})
    overrides: dict = load_json(cfg.pairing_overrides, {"force": [], "block": []})
    pairings = _build_pairings(library, overrides)
    save_json(cfg.pairings_index, pairings)

    auto = sum(1 for v in pairings.values() if v["source"] == "auto")
    forced = sum(1 for v in pairings.values() if v["source"] == "override")
    if not romaji.available():
        print("  (note: pykakasi not installed — JP author romanization disabled)")
    print(f"Pairing complete: {len(pairings)} pairs ({forced} forced, {auto} auto).")
    return pairings


# ── review-page support ──────────────────────────────────────────────────────

def _slim(book: dict) -> dict:
    return {
        "book_id": book["book_id"],
        "stable_key": stable_key(book),
        "title": book.get("title"),
        "author": book.get("author"),
        "author_romaji": romaji.to_romaji(book.get("author") or "") if book.get("author") else None,
        "language": book.get("language"),
        "cover": book.get("cover"),
        "cover_v": book.get("cover_v"),
        "series": book.get("series"),
    }


def _rank_candidates(book: dict, others: list, blocked: set, none: set) -> list:
    """`others` (the opposite-language books still unpaired) that could be `book`'s other
    edition, best first. A blocked pair or a book marked "no other edition" is never offered."""
    out = []
    for other in others:
        if other["book_id"] in none or frozenset((book["book_id"], other["book_id"])) in blocked:
            continue
        en, jp = (book, other) if book.get("language") == "en" else (other, book)
        a, combined = _combined(en, jp)
        if a >= SUGGEST_MIN or combined >= SUGGEST_MIN:
            out.append({
                **_slim(other), "score": combined, "author_score": round(a, 3),
                # What the UI says about the guess: the author names match as well as
                # an auto-pair needs (it was left out only because it wasn't
                # unambiguous), or it's a weak guess. Below AUTHOR_AUTO a score
                # can't tell a real match from noise, so none is shown.
                "signal": "author" if a >= AUTHOR_AUTO else "weak",
            })
    out.sort(key=lambda c: c["score"], reverse=True)
    return out[:TOP_N]


def compute_suggestions(cfg: Config) -> dict:
    """Data for the /pair review UI: current pairs, unpaired JP books with their
    top EN candidates, and the list of unpaired EN books."""
    library: dict = load_json(cfg.library_index, {})
    overrides: dict = load_json(cfg.pairing_overrides, {"force": [], "block": []})
    pairings = _build_pairings(library, overrides)
    by_id = {b["book_id"]: b for b in library.values()}

    paired_en = {p["en"] for p in pairings.values()}
    paired_jp = {p["jp"] for p in pairings.values()}
    _, blocked = _resolve_overrides(library, overrides)
    none = _none_ids(library, overrides)

    pairs = []
    for pid, p in pairings.items():
        en, jp = by_id.get(p["en"]), by_id.get(p["jp"])
        if en and jp:
            pairs.append({
                "pair_id": pid,
                "source": p["source"],
                "score": p["score"],
                "en": _slim(en),
                "jp": _slim(jp),
            })

    en_books, jp_books = _split(library)
    unpaired_en = [b for b in en_books if b["book_id"] not in paired_en]

    suggestions = []
    for jp in jp_books:
        if jp["book_id"] in paired_jp or jp["book_id"] in none:
            continue
        suggestions.append({"jp": _slim(jp), "candidates": _rank_candidates(jp, unpaired_en, blocked, none)})

    # Show JP books that have at least one candidate first.
    suggestions.sort(key=lambda s: (len(s["candidates"]) == 0, s["jp"]["title"] or ""))

    k2id = _key_to_id(library)
    blocks = []
    for b in overrides.get("block", []):
        en, jp = by_id.get(k2id.get(b.get("en"))), by_id.get(k2id.get(b.get("jp")))
        if en and jp:
            blocks.append({"en": _slim(en), "jp": _slim(jp)})
    blocks.sort(key=lambda b: (b["jp"]["title"] or "", b["en"]["title"] or ""))

    no_edition = sorted((_slim(by_id[i]) for i in none), key=lambda b: (b["title"] or ""))

    return {
        "pairs": pairs,
        "blocked": blocks,
        "no_edition": no_edition,
        "suggestions": suggestions,
        "unpaired_en": [_slim(b) for b in sorted(unpaired_en, key=lambda b: (b.get("title") or ""))],
        "romaji_available": romaji.available(),
    }


def edition_state(cfg: Config, book_id: str) -> dict:
    """One book's other-edition state, for its page (`book` is the book itself, with the
    `stable_key` its link requests need): how it is paired (`auto`: the scanner,
    `manual`: you, `none`: you said it has no other edition, `unpaired`), the partner, and
    while unpaired the ranked candidates plus every opposite-language book still unpaired
    (`others`, to pick by hand). Reads the pairings the last `pair` wrote, so it agrees with
    the `pair_partner` the page already shows, and ranks only this book, not the library."""
    library: dict = load_json(cfg.library_index, {})
    book = next((b for b in library.values() if b["book_id"] == book_id), None)
    if book is None or book.get("language") not in ("en", "ja"):
        return {"eligible": False, "book": None, "mode": None, "partner": None, "candidates": [], "others": []}

    by_id = {b["book_id"]: b for b in library.values()}
    pairings: dict = load_json(cfg.pairings_index, {})
    paired: set = set()
    partner = source = None
    for p in pairings.values():
        paired.update((p["en"], p["jp"]))
        if book_id in (p["en"], p["jp"]):
            other = by_id.get(p["jp"] if book_id == p["en"] else p["en"])
            if other:
                partner, source = _slim(other), p["source"]
    if partner:
        return {"eligible": True, "book": _slim(book), "mode": "manual" if source == "override" else "auto",
                "partner": partner, "candidates": [], "others": []}

    overrides: dict = load_json(cfg.pairing_overrides, {"force": [], "block": []})
    none = _none_ids(library, overrides)
    want = "ja" if book["language"] == "en" else "en"
    unpaired = sorted((b for b in library.values()
                       if b.get("language") == want and b["book_id"] not in paired),
                      key=lambda b: b.get("title") or "")
    others = [_slim(b) for b in unpaired]
    if book_id in none:
        return {"eligible": True, "book": _slim(book), "mode": "none", "partner": None,
                "candidates": [], "others": others}
    _, blocked = _resolve_overrides(library, overrides)
    return {"eligible": True, "book": _slim(book), "mode": "unpaired", "partner": None, "others": others,
            "candidates": _rank_candidates(book, unpaired, blocked, none)}


# ── override mutations (used by the review endpoints) ────────────────────────

def _same(a: dict, b_en: str, b_jp: str) -> bool:
    return a.get("en") == b_en and a.get("jp") == b_jp


def link(cfg: Config, en_key: str, jp_key: str) -> None:
    """Force a pair: add to overrides.force, drop any matching block entry."""
    ov = load_json(cfg.pairing_overrides, {"force": [], "block": []})
    ov.setdefault("force", [])
    ov.setdefault("block", [])
    ov["block"] = [b for b in ov["block"] if not _same(b, en_key, jp_key)]
    # Linking is a decision that there IS another edition, so it supersedes "none".
    ov["none"] = [k for k in ov.get("none", []) if k not in (en_key, jp_key)]
    if not any(_same(f, en_key, jp_key) for f in ov["force"]):
        ov["force"].append({"en": en_key, "jp": jp_key})
    save_json(cfg.pairing_overrides, ov)


def unlink(cfg: Config, en_key: str, jp_key: str, block: bool = True) -> None:
    """Remove a pair: drop from force and, unless `block` is False (undoing a link just
    made), add to block so it won't re-auto-pair."""
    ov = load_json(cfg.pairing_overrides, {"force": [], "block": []})
    ov.setdefault("force", [])
    ov.setdefault("block", [])
    ov["force"] = [f for f in ov["force"] if not _same(f, en_key, jp_key)]
    if block and not any(_same(b, en_key, jp_key) for b in ov["block"]):
        ov["block"].append({"en": en_key, "jp": jp_key})
    save_json(cfg.pairing_overrides, ov)


def unblock(cfg: Config, en_key: str, jp_key: str) -> None:
    """Lift a block (made by unlink) without forcing a pair: the scanner may
    auto-pair these two again, and they show up as a suggestion if it doesn't."""
    ov = load_json(cfg.pairing_overrides, {"force": [], "block": []})
    ov.setdefault("force", [])
    ov["block"] = [b for b in ov.get("block", []) if not _same(b, en_key, jp_key)]
    save_json(cfg.pairing_overrides, ov)


def set_none(cfg: Config, key: str) -> None:
    """Mark a book "no other edition": no auto-pairing, no suggestions, until cleared."""
    ov = load_json(cfg.pairing_overrides, {"force": [], "block": []})
    ov.setdefault("force", [])
    ov.setdefault("block", [])
    none = ov.setdefault("none", [])
    if key not in none:
        none.append(key)
    save_json(cfg.pairing_overrides, ov)


def clear_none(cfg: Config, key: str) -> None:
    """Back to automatic: the book is paired and suggested as usual again."""
    ov = load_json(cfg.pairing_overrides, {"force": [], "block": []})
    ov.setdefault("force", [])
    ov.setdefault("block", [])
    ov["none"] = [k for k in ov.get("none", []) if k != key]
    save_json(cfg.pairing_overrides, ov)
