"""Cross-reference EPUBs against the audiobook project's data.json.

For each EPUB we fuzzy-match its title/series against the audiobook catalog
(keyed by Audible ISBN). On a confident match we record whether the audiobook
file actually exists on disk (per the audio-scan in audiobook_files.json —
data.json's `new_path` is stale) and the Audible URL to buy it.

Matching keeps volume numbers (every volume of a series shares its stem, so
without them the "best" match is an arbitrary volume) and requires the authors
to agree unless the titles are essentially identical. A wrong or missing link
can be corrected by hand: `audiobook_link_overrides.json` ({book_id: isbn |
null}) is applied last — an ISBN pins the link, null means "no audiobook".

Output (`audiobook_links.json`): { book_id: {audible_isbn, match_score,
audiobook_local, audiobook_path, audible_url[, override]} }.
"""
from __future__ import annotations

import difflib
import re
import unicodedata

from . import audiobook_catalog
from .audiobook_scanner import AUDIBLE_KEY_RE
from .build import _title_series
from .config import Config
from .index_store import load_json, save_json
from .textnorm import match_key, strip_volume_prefix

AUDIBLE_PD = "https://www.audible.co.jp/pd/"

# Titles this similar are the same book even if the catalog credits a
# co-author (嫌われる勇気 lists 岸見一郎; the EPUB 古賀史健).
_AUTHOR_WAIVED_AT = 0.98
_AUTHOR_MIN_SIM = 0.5
def _volume(title: str | None) -> int | None:
    """The explicit trailing volume marker in a title ("…銭天堂13" → 13)."""
    hit = _title_series(strip_volume_prefix(unicodedata.normalize("NFKC", title or "")))
    return int(hit[1]) if hit and hit[1].isdigit() else None


_KANJI_DIGITS = {c: i for i, c in enumerate("一二三四五六七八九", 1)}
_ROMAN = {"I": 1, "V": 5, "X": 10}
# 第一部「兵士の娘I」 / 第三部「領主の養女4」: a part number, then a volume
# *within the part* in the subtitle's last characters (Arabic in the catalog,
# often roman in an EPUB). Neither is a trailing marker `_title_series` sees,
# and the catalog's global `order` can't be derived from them.
_PART_VOL = re.compile(
    r"第\s*([0-9]{1,2}|[一二三四五六七八九十]{1,3})\s*部\s*[「『]"
    r"[^」』]*?([0-9]{1,3}|[IVX]{1,6})[」』]")


def _kanji_int(s: str) -> int | None:
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if s.startswith("十"):
        return 10 + _KANJI_DIGITS.get(s[1:], 0) if len(s) == 2 else None
    if s.endswith("十"):
        return _KANJI_DIGITS[s[0]] * 10 if len(s) == 2 else None
    if len(s) == 1:
        return _KANJI_DIGITS.get(s)
    if len(s) == 3 and s[1] == "十":
        return _KANJI_DIGITS[s[0]] * 10 + _KANJI_DIGITS[s[2]]
    return None


def _roman_int(s: str) -> int | None:
    vals = [_ROMAN[c] for c in s]
    total = sum(-v if i + 1 < len(vals) and v < vals[i + 1] else v
                for i, v in enumerate(vals))
    return total or None


def _part_volume(title: str | None) -> tuple[int, int] | None:
    """(part, volume within the part) from a `第N部「…N」` title, else None."""
    t = strip_volume_prefix(unicodedata.normalize("NFKC", title or ""))
    m = _PART_VOL.search(t)
    if not m:
        return None
    part = _kanji_int(m.group(1))
    sub = int(m.group(2)) if m.group(2).isdigit() else _roman_int(m.group(2))
    return (part, sub) if part and sub else None


def _to_int(order: str | None) -> int | None:
    return int(order) if order and order.isdigit() else None


def _volume_ok(book_vol: int | None, cand_marker: int | None,
               cand_order: int | None) -> bool:
    """Can this catalog volume be this EPUB's volume?

    An EPUB with a number must meet the same number. An EPUB without one is
    volume 1 (or a standalone) as far as the title says, so a catalog title
    that carries a different number is some other volume — but a catalog
    `order` alone doesn't disqualify it: volumes titled by subtitle
    ("最後の晩ごはん お兄さんとホットケーキ") carry an order and no number."""
    if book_vol is not None:
        cand = cand_marker if cand_marker is not None else cand_order
        return cand is None or cand == book_vol
    return cand_marker is None or cand_marker == 1


def _author_ok(book_author: str, cand_author: str, title_score: float) -> bool:
    if title_score >= _AUTHOR_WAIVED_AT or not book_author or not cand_author:
        return True
    if book_author in cand_author or cand_author in book_author:
        return True
    return difflib.SequenceMatcher(None, book_author, cand_author).ratio() >= _AUTHOR_MIN_SIM


class _Candidate:
    __slots__ = ("isbn", "title", "series", "author", "marker", "order", "part")

    def __init__(self, isbn: str, ab: dict) -> None:
        self.isbn = isbn
        raw = strip_volume_prefix(ab.get("title") or "")
        self.title = match_key(raw)
        self.series = match_key(ab.get("series"))
        self.author = match_key(ab.get("author"))
        self.marker = _volume(ab.get("title"))
        self.order = _to_int(ab.get("order"))
        self.part = _part_volume(ab.get("title"))


class _Matcher:
    """The catalog, normalized once, plus the per-book search."""

    def __init__(self, audiobooks: dict, min_score: float) -> None:
        self.min_score = min_score
        # A row guessed from a file name is never linked on its own (a picker
        # or a hand edit can still link it).
        self.cands = [_Candidate(isbn, ab) for isbn, ab in audiobooks.items()
                      if ab.get("source") != "filename"]

    def best(self, book: dict) -> tuple[str, float] | None:
        bt = match_key(book.get("title"))
        bs = match_key(book.get("series"))
        if not bt and not bs:
            return None
        ba = match_key(book.get("author"))
        book_vol = _volume(book.get("title"))
        book_part = _part_volume(book.get("title"))
        # One matcher per book, with the book string as seq2: SequenceMatcher
        # indexes seq2 once, so scanning thousands of rows against it is cheap.
        # The upper-bound ratios skip almost every full comparison.
        sm_title = difflib.SequenceMatcher(None, "", bt, autojunk=False)
        sm_series = difflib.SequenceMatcher(None, "", bs, autojunk=False) if bs else None

        best_isbn, best = None, self.min_score - 1e-9
        for c in self.cands:
            if not _volume_ok(book_vol, c.marker, c.order):
                continue
            if book_part and c.part and book_part != c.part:
                continue  # a different volume of the same multi-part series
            score = 0.0
            if bt and c.title:
                sm_title.set_seq1(c.title)
                if (sm_title.real_quick_ratio() > best
                        and sm_title.quick_ratio() > best):
                    score = sm_title.ratio()
            title_score = score
            if sm_series and c.series:
                sm_series.set_seq1(c.series)
                if (sm_series.real_quick_ratio() > max(best, score)
                        and sm_series.quick_ratio() > max(best, score)):
                    score = max(score, sm_series.ratio())
            if score > best and _author_ok(ba, c.author, title_score):
                best_isbn, best = c.isbn, score
        return (best_isbn, best) if best_isbn else None


def _entry(cfg_files: dict, audiobooks: dict, isbn: str, score: float,
           override: bool = False) -> dict:
    local = cfg_files.get(isbn)
    entry = {
        "audible_isbn": isbn,
        "match_score": round(score, 3),
        "audiobook_local": local is not None,
        "audiobook_path": local["path"] if local else None,
        "audible_url": (f"{AUDIBLE_PD}{isbn}"
                        if AUDIBLE_KEY_RE.fullmatch(isbn) else None),
    }
    if override:
        entry["override"] = True
    return entry


def _local_by_key(cfg: Config) -> dict:
    ab_files: dict = load_json(cfg.audiobook_files_index, {})
    if not ab_files:
        print("audiobook_files.json empty; run `python cli.py audio-scan` "
              "for local-availability data.")
    # By catalog key, else ab_id: a local file's own catalog row is keyed by its
    # ab_id (audiobook_catalog.local_row), so it links (and syncs) like any other.
    return {e.get("catalog_key") or e["ab_id"]: e for e in ab_files.values()}


def _link_for(book: dict, matcher: _Matcher | None, audiobooks: dict,
              overrides: dict, local_by_key: dict) -> dict | None:
    """The audiobook link for one EPUB, or None. A hand override wins over the
    fuzzy match; null means "no audiobook" and suppresses even a good match. An
    override naming an ISBN no longer in the catalog is ignored."""
    bid = book["book_id"]
    if bid in overrides:
        isbn = overrides[bid]
        if isbn is None:
            return None
        if isbn in audiobooks:
            return _entry(local_by_key, audiobooks, isbn, 1.0, override=True)
    if matcher is None or book.get("language") != "ja":
        return None  # audiobooks are Japanese
    hit = matcher.best(book)
    return _entry(local_by_key, audiobooks, hit[0], hit[1]) if hit else None


def xref(cfg: Config) -> dict:
    library: dict = load_json(cfg.library_index, {})
    audiobooks: dict = audiobook_catalog.load(cfg)
    if not audiobooks:
        print("audiobook catalog is empty (run `catalog` once audiobook files are scanned); skipping xref.")
        save_json(cfg.audiobook_links, {})
        return {}

    overrides: dict = load_json(cfg.audiobook_link_overrides, {})
    local_by_key = _local_by_key(cfg)
    matcher = _Matcher(audiobooks, cfg.xref_min_score)

    links: dict = {}
    for book in library.values():
        link = _link_for(book, matcher, audiobooks, overrides, local_by_key)
        if link:
            links[book["book_id"]] = link

    save_json(cfg.audiobook_links, links)
    print(f"Audiobook xref complete: {len(links)} EPUBs linked to audiobooks "
          f"({sum(1 for link in links.values() if link.get('override'))} by override).")
    return links


def xref_one(cfg: Config, book_id: str) -> dict | None:
    """Re-link a single EPUB (~20 ms vs the full pass), leaving every other
    link as is. Returns the new link, or None if it now has none."""
    library: dict = load_json(cfg.library_index, {})
    book = library.get(book_id)
    audiobooks: dict = audiobook_catalog.load(cfg)
    if not book or not audiobooks:
        return None
    overrides: dict = load_json(cfg.audiobook_link_overrides, {})
    matcher = _Matcher(audiobooks, cfg.xref_min_score)
    link = _link_for(book, matcher, audiobooks, overrides, _local_by_key(cfg))

    links: dict = load_json(cfg.audiobook_links, {})
    if link:
        links[book_id] = link
    else:
        links.pop(book_id, None)
    save_json(cfg.audiobook_links, links)
    return link
