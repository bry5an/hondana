"""Join the per-stage caches into the denormalized index the API serves.

Reads epub_library.json + natively_epub.json + pairings.json +
audiobook_links.json + vocab_epub.json and writes a single combined view back
into epub_library.json so the server does zero matching per request. Each
book gets `natively`, `pair_id`, `pair_partner`, `audiobook`, `vocab`, and
`series_name`/`series_order`/`series_source` fields filled in (or null).

Deliberately profile-independent: `known` (the reader-specific known-%/
reading-load numbers) is *not* baked in here. It lives in each profile's own
known_stats.json and is overlaid onto a book at request time by server.py, so
this shared index never needs rebuilding just because someone's known-words
list changed, and two devices on different profiles never race to bake their
own numbers into the one file everyone reads.

Series derivation (calibre series metadata is absent from nearly every EPUB):
0. manual entry in series_overrides.json → source "override" (hand-supplied
   fix for volumes none of the heuristics below can catch, e.g. a light-novel
   series where each volume's title is an unrelated subtitle)
1. calibre:series (or an EPUB3 series collection) from the OPF, with its
   series_index / group-position as the order → source "calibre"
2. linked audiobook's catalog entry     → source "audiobook" (best data: the
   Audible catalog has reliable series + zero-padded volume order)
3. trailing volume marker in the title  → source "title" (第N巻 / その2 / (1) /
   上中下), plus: a book titled exactly like a derived series name is that
   series' volume 1
4. pair partner's derived series        → source "pair" (EN editions inherit)
"""
from __future__ import annotations

import re

from . import audiobook_catalog
from . import romaji
from . import search_index as search_index_mod
from . import textnorm
from .config import Config
from .index_store import load_json, save_json

# Volume numbers are 1–3 digits with nothing numeric right before them —
# "合格2400" is part of the title, "話。2" is a volume marker.
_PAREN_VOL = re.compile(r"[（(]([0-9０-９]{1,3})[）)][\s　]*$")
_VOL_PATTERNS = (
    re.compile(r"[\s　]*(?:第)?(?<![0-9０-９])([0-9０-９]{1,3})(?:巻)?[\s　]*$"),
    re.compile(r"[\s　]*その(?<![0-9０-９])([0-9０-９]{1,3})[\s　]*$"),
    re.compile(r"[\s　]+([上中下])[\s　]*$"),
)
_KANJI_VOL_ORDER = {"上": "01", "中": "02", "下": "03"}


def _order_str(num: str) -> str:
    if num in _KANJI_VOL_ORDER:
        return _KANJI_VOL_ORDER[num]
    ascii_num = num.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    return ascii_num.zfill(2)


def _title_series(title: str) -> tuple[str, str] | None:
    """(series_name, order) from a trailing volume marker, else None."""
    t = textnorm.peel_title(title)
    m = _PAREN_VOL.search(t)
    if not m:
        for pat in _VOL_PATTERNS:
            m = pat.search(t)
            if m:
                break
    if not m or m.start() == 0:
        return None
    stem = t[: m.start()].strip()
    if len(stem) < 2:
        return None
    return stem, _order_str(m.group(1))


def suggest_series(title: str, names) -> tuple[str, str | None] | None:
    """An existing series `title` looks like a volume of: the longest name the
    cleaned title starts with, plus a volume number if one follows it. For a
    book with no series, so the edit form can offer to join the exact existing
    spelling instead of leaving a near-duplicate to be typed."""
    t = textnorm.peel_title(title or "")
    best = max((n for n in names if len(n) >= 3 and t.startswith(n) and len(t) > len(n)),
               key=len, default=None)
    if not best:
        return None
    m = re.search(r"[0-9０-９]{1,3}|[上中下]", t[len(best):])
    return best, (_order_str(m.group(0)) if m else None)


def _derive_series(
    library: dict, links: dict, catalog: dict, pair_of: dict, overrides: dict
) -> dict:
    """book_id → [series_name, series_order, series_source] (Nones if solo)."""
    derived: dict = {}
    from_catalog: set = set()  # books whose order is the matched audiobook's, not their title's
    for bid, book in library.items():
        name = order = source = None
        if bid in overrides:
            derived[bid] = [
                overrides[bid].get("series_name"),
                overrides[bid].get("series_order"),
                "override",
            ]
            continue
        if book.get("series"):
            # calibre:series, or an EPUB3 series collection — either way the OPF's
            # own series_index / group-position is the volume number.
            name, source, order = book["series"], "calibre", book.get("series_index")
        if name is None:
            link = links.get(bid) or {}
            cat = catalog.get(link.get("audible_isbn")) or {}
            if cat.get("series"):
                name, source = cat["series"], "audiobook"
                # The xref match may point at a *different volume* of the same
                # series, so the EPUB's own title outranks the matched
                # audiobook's order.
                hit = _title_series(book.get("title") or "")
                if hit:
                    order = hit[1]
                elif textnorm.peel_title(book.get("title") or "") == name:
                    order = "01"  # titled exactly like the series → volume 1
                else:
                    order = cat.get("order")
                    from_catalog.add(bid)
        if name is None:
            hit = _title_series(book.get("title") or "")
            if hit:
                (name, order), source = hit, "title"
        derived[bid] = [name, order, source]

    # The catalog's order is only a guess for an EPUB whose title says nothing
    # (xref may have matched a neighbouring volume). When several EPUBs in one
    # series land on the same number the guess is plainly wrong for at least
    # some of them, so leave those unnumbered rather than show a false tie.
    claimed: dict = {}
    for bid, (name, order, source) in derived.items():
        if source == "audiobook" and order:
            claimed.setdefault((name, order), []).append(bid)
    for ids in claimed.values():
        if len(ids) > 1:
            for bid in ids:
                if bid in from_catalog:
                    derived[bid][1] = None

    # A book titled exactly like a derived series name is its volume 1
    # (e.g. 最弱テイマー… vol 1 carries no number, vols 2/3 do).
    names = {d[0] for d in derived.values() if d[0]}
    for bid, book in library.items():
        if derived[bid][0] or bid in overrides:  # an override is final, even "standalone"
            continue
        stem = textnorm.peel_title(book.get("title") or "")
        if stem in names:
            derived[bid] = [stem, "01", "title"]

    # Last resort: inherit the pair partner's series (EN ↔ JP editions).
    for bid in library:
        if derived[bid][0] or bid in overrides:
            continue
        pid_partner = pair_of.get(bid)
        if pid_partner:
            partner = derived.get(pid_partner[1])
            if partner and partner[0]:
                derived[bid] = [partner[0], partner[1], "pair"]

    return derived


def _description(own: str | None, linked: str | None,
                 looked_up: str | None = None) -> tuple[str | None, str | None]:
    """(text, source): the EPUB's own description, else the linked audiobook's,
    else the one fetched by ISBN. The catalog's listing is HTML (about half
    break their lines with `<br />`), so it is made plain text like the other two."""
    if own:
        return own, "epub"
    linked = textnorm.plain_text(linked)
    if linked:
        return linked, "audiobook"
    if looked_up:
        return looked_up, "isbn"
    return None, None


def _sort_readings(library: dict) -> None:
    """Fill `title_sort` and `series_sort` on every book: the keys `/` orders titles
    and series by, in gojūon order (`romaji.sort_reading`). A book's own key
    prefers the OPF's reading (`title_reading`, about a quarter of the library) over
    pykakasi's guess. A series has no reading of its own, so it borrows volume 1's
    when that title starts with the series name, else it is read from the name."""
    firsts: dict[str, dict] = {}
    for book in library.values():
        name = book.get("series_name")
        if not name:
            continue
        best = firsts.get(name)
        if best is None or _order_key(book) < _order_key(best):
            firsts[name] = book

    series_key: dict[str, str | None] = {}
    for name, first in firsts.items():
        own = first.get("title_reading") if (first.get("title") or "").startswith(name) else None
        series_key[name] = romaji.sort_reading(name, own)

    for book in library.values():
        book["title_sort"] = romaji.sort_reading(book.get("title"), book.get("title_reading"))
        book["series_sort"] = series_key.get(book.get("series_name"))


def _order_key(book: dict) -> tuple:
    """Volume 1 first: numeric order, blank orders last."""
    try:
        return (0, float(book.get("series_order")))
    except (TypeError, ValueError):
        return (1, 0.0)


def build(cfg: Config) -> dict:
    library: dict = load_json(cfg.library_index, {})
    natively: dict = load_json(cfg.natively_index, {})
    pairings: dict = load_json(cfg.pairings_index, {})
    links: dict = load_json(cfg.audiobook_links, {})
    vocab: dict = load_json(cfg.vocab_index, {})
    catalog: dict = audiobook_catalog.load(cfg)
    by_isbn: dict = load_json(cfg.epub_catalog_extra, {})
    series_overrides: dict = load_json(cfg.series_overrides, {})
    genre_overrides: dict = load_json(cfg.genre_overrides, {})
    title_overrides: dict = load_json(cfg.title_overrides, {})
    author_overrides: dict = load_json(cfg.author_overrides, {})
    language_overrides: dict = load_json(cfg.language_overrides, {})
    cover_overrides: dict = load_json(cfg.cover_overrides, {})

    # Map each book_id → (pair_id, partner_id).
    pair_of: dict = {}
    for pid, p in pairings.items():
        en, jp = p.get("en"), p.get("jp")
        if en in library and jp in library:
            pair_of[en] = (pid, jp)
            pair_of[jp] = (pid, en)

    series = _derive_series(library, links, catalog, pair_of, series_overrides)

    # Title cleanup. Precomputed in its own pass (not mutated in the main loop
    # below) because pair_partner reads library[partner_id]["title"] there —
    # an in-place mutation would make whether a partner's title is already
    # cleaned by the time it's read depend on dict iteration order.
    #
    # `book["title"]` below gets overwritten with the cleaned/overridden value
    # every build, so it can't be re-read as the cleaning input on the next
    # run — that would bake in whatever the last override was, permanently,
    # the moment it's cleared. `raw_title` (scanner.py) is the stable source:
    # scanner only ever writes it once per (re)scan, build() never touches it.
    # Entries scanned before raw_title existed fall back to `title` once and
    # persist it from then on, since at that point it's still the untouched
    # raw OPF value (no override could have been applied to a field that
    # didn't exist yet).
    titles: dict[str, str | None] = {}
    for bid, book in library.items():
        raw = book.get("raw_title") or book.get("title")
        book["raw_title"] = raw
        cleaned = textnorm.display_title(raw) if raw else raw
        titles[bid] = title_overrides.get(bid) or (cleaned or raw)

    # Author and language: like `title`, build overwrites them every run, so what
    # the file itself says is kept in raw_author/raw_language/raw_lang_source
    # (written once per (re)scan — scanner replaces the whole entry — and never
    # touched again) and an override is re-applied from there. Without that,
    # clearing an override would be stuck with the last corrected value. Its own
    # pass, ahead of the main loop, because pair_partner reads the partner's
    # language there.
    for bid, book in library.items():
        for field in ("author", "language", "lang_source", "description"):
            book.setdefault(f"raw_{field}", book.get(field))
        # Display form drops OPF list punctuation ("James Nestor;"); author_key is
        # the spelling-independent form everything compares and groups on.
        # The raw value stays in raw_author, and a hand override is shown as typed.
        override = author_overrides.get(bid)
        book["author"] = override or textnorm.clean_author(book["raw_author"])
        book["author_key"] = textnorm.author_key(book["author"])
        forced = language_overrides.get(bid)
        book["language"] = forced or book["raw_language"]
        book["lang_source"] = "override" if forced else book["raw_lang_source"]

    # Cover: the scanner's own extraction stays in raw_cover; a hand-set cover
    # (data/covers/custom/) is laid over it here, so a rescan that rewrites
    # the entry from scratch can't lose it. `cover_v` (mtime, ms) busts the browser's
    # immutable /covers/{id} cache when the image is replaced.
    for bid, book in library.items():
        book.setdefault("raw_cover", book.get("cover"))
        book.pop("cover_v", None)
        book["cover"] = book["raw_cover"]
        custom = cover_overrides.get(bid)
        if custom:
            rel = f"covers/custom/{bid}{custom['ext']}"
            try:
                book["cover_v"] = (cfg.data_dir / rel).stat().st_mtime_ns // 1_000_000  # ms: ns overflows a JS number
                book["cover"] = rel
            except OSError:
                pass  # file missing (e.g. a mirror restore without images): EPUB's cover

    for bid, book in library.items():
        # An older epub_library.json may still carry a stale baked-in
        # `known` from an old build — drop it so a book with no per-request
        # overlay applied (e.g. direct inspection of the file) can't be
        # mistaken for current data.
        book.pop("known", None)
        book["natively"] = natively.get(bid)
        book["audiobook"] = links.get(bid)
        pid_partner = pair_of.get(bid)
        if pid_partner:
            pid, partner_id = pid_partner
            partner = library.get(partner_id, {})
            book["pair_id"] = pid
            book["pair_partner"] = {
                "book_id": partner_id,
                "title": titles.get(partner_id),
                "language": partner.get("language"),
            }
        else:
            book["pair_id"] = None
            book["pair_partner"] = None

        v = vocab.get(bid)
        book["vocab"] = (
            {k: val for k, val in v.items() if k not in ("mtime", "filesize", "schema")}
            if v
            else None
        )
        book["series_name"], book["series_order"], book["series_source"] = series[bid]


        # Title. `textnorm.display_title` peels off publisher/imprint parentheticals,
        # 【edition】 markers, and appended English translations. A hand-typed
        # override (Manage mode's ✏️ action, /api/books/{id}/title) always
        # wins over the heuristic.
        book["title"] = titles[bid]

        # Genre. EPUB metadata carries no usable genre, so it comes from the
        # linked audiobook's catalog entry when there is one, else from an ISBN
        # lookup, and a hand-picked override (the /add page's selector)
        # wins over both — it is the most deliberate.
        link = links.get(bid) or {}
        linked = catalog.get(link.get("audible_isbn"), {})
        looked_up = by_isbn.get((book.get("identifiers") or {}).get("isbn")) or {}
        book["genre"] = (genre_overrides.get(bid) or linked.get("genre")
                         or looked_up.get("genre") or None)

        # Description: the OPF's own when it has one (about 1 in 6 do),
        # else the linked audiobook's listing, else one fetched by ISBN. The file's value is kept in raw_description
        # so the fallback is re-derived each build, never baked into the entry.
        book["description"], book["description_source"] = _description(
            book["raw_description"], linked.get("description_full"), looked_up.get("description"))

    _sort_readings(library)

    save_json(cfg.library_index, library)
    grouped =sum(1 for s in series.values() if s[0])
    print(
        f"Build complete: {len(library)} books in the combined index "
        f"({grouped} in series)."
    )

    # Best-effort: the served index above is what matters. A broken search
    # rebuild shouldn't take down `build`; /api/search degrades to substring.
    try:
        search_index_mod.build(cfg)
    except Exception as exc:
        print(f"Search index rebuild failed (non-fatal): {exc}")

    return library
