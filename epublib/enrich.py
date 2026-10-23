"""Enrich Japanese books with Natively difficulty levels + ratings.

Drives natively.lookup() over the JP entries in the library. English books are
skipped entirely (they don't need Natively ranks). Resume-safe: re-running only
fetches what needs it, and progress is saved every SAVE_EVERY books like the
original scraper.

What "needs it":
* a book with no cached result — always;
* an *unmatched* book (a miss) once it is older than RETRY_DAYS, or the series it
  is looked up under has changed since (a miss is often just a missing series
  name — the book was fetched before `build` derived one);
* a *series-level* match (the series page's average level) that was cached before
  the volume number was passed, so it can now resolve the volume's own page.
A retry never replaces a match with a miss, and a hand-set `manual_url` entry is
never touched, not even by `refresh`.

Lookups use the series `build.build()` derived (`series_name`/`series_order`), not
the raw calibre `series` that almost no EPUB carries.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import audiobook_catalog, natively
from .config import Config
from .index_store import load_json, save_json

SAVE_EVERY = 50
RETRY_DAYS = 14
MANUAL = "manual_url"


def _volume(order: str | None) -> int:
    """series_order ("01", "12") as the int Natively's volume lookup takes; 0 if
    it isn't a plain whole number ("2.5", "上")."""
    return int(order) if order and order.isdigit() else 0


def _query_for(book: dict) -> dict:
    return {"series": book.get("series_name") or book.get("series") or "",
            "order": _volume(book.get("series_order"))}


def _age(entry: dict, now: datetime) -> timedelta | None:
    try:
        return now - datetime.fromisoformat(entry["fetched_at"])
    except (KeyError, ValueError, TypeError):
        return None


def wants_lookup(entry: dict | None, query: dict, now: datetime, *,
                 retry: bool = True, retry_unmatched: bool = False,
                 legacy_matches_final: bool = False) -> bool:
    """Does this cache entry (None = no entry yet) need another Natively lookup?
    `legacy_matches_final`: a match stored before entries were stamped (the
    audiobook levels, written by the old scraper — which already resolved volume
    pages) is never upgraded."""
    if entry is None:
        return True
    if entry.get("match_type") == MANUAL:
        return False
    matched = entry.get("natively_level") is not None
    if matched:
        if legacy_matches_final and "fetched_at" not in entry:
            return False
        # Only the series-level average can improve: now that the volume is known,
        # the volume's own page can be found.
        return retry and entry.get("match_type") == "series" and bool(query["order"]) \
            and "query" not in entry
    if retry_unmatched:
        return True
    if not retry:
        return False
    age = _age(entry, now)
    return age is None or age > timedelta(days=RETRY_DAYS) or entry.get("query") != query


def enrich(cfg: Config, refresh: bool = False, *, retry: bool = True,
           retry_unmatched: bool = False) -> dict:
    """`refresh`: re-fetch every JP book. `retry_unmatched`: re-fetch every miss
    now, however recent. `retry=False`: only books with no result at all (what the
    per-book add chain uses, so an upload never waits on a batch of old misses)."""
    library: dict = load_json(cfg.library_index, {})
    cache: dict = load_json(cfg.natively_index, {})

    # Derived data for books that are gone (deleted before deletes purged it).
    if library:
        stale = [bid for bid in cache if bid not in library]
        for bid in stale:
            del cache[bid]
        if stale:
            print(f"Dropped {len(stale)} Natively entries for books no longer in the library.")

    now = datetime.now(timezone.utc)
    jp_books = [b for b in library.values() if b.get("language") == "ja"]
    todo = []
    for b in jp_books:
        entry = cache.get(b["book_id"])
        if refresh and entry is not None and entry.get("match_type") != MANUAL:
            todo.append(b)
        elif wants_lookup(entry, _query_for(b), now, retry=retry, retry_unmatched=retry_unmatched):
            todo.append(b)

    print(f"Natively enrichment: {len(jp_books)} JP books, {len(todo)} to fetch.")
    _fetch(cfg, cfg.natively_index, cache,
           [(b["book_id"], b.get("title") or "", _query_for(b)) for b in todo])
    return cache


def _fetch(cfg: Config, path, cache: dict, todo: list[tuple[str, str, dict]]) -> None:
    """Look up each (key, title, query), stamp and store the result in `cache`,
    saving to `path` every SAVE_EVERY lookups and at the end."""
    processed = 0
    matched = 0

    for key, title, query in todo:
        result = natively.lookup(
            title,
            query["series"],
            query["order"],
            min_score=cfg.natively_min_score,
            delay=cfg.natively_delay,
        )
        previous = cache.get(key)
        if result.get("natively_level") is None and previous and previous.get("natively_level") is not None:
            # The retry found nothing where an earlier lookup did: keep what we had.
            result = previous
        cache[key] = {**result, "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      "query": query}
        processed += 1

        level = result.get("natively_level")
        if level is not None:
            matched += 1
            ll = result.get("language_learning_rating")
            print(f"  [{processed}/{len(todo)}] ✓ L{level} LL={ll}  {title[:50]}")
        else:
            print(f"  [{processed}/{len(todo)}] – {title[:55]}")

        if processed % SAVE_EVERY == 0:
            save_json(path, cache)

    save_json(path, cache)
    print(f"\nEnrichment complete: {matched}/{len(todo)} matched.")


def enrich_audiobooks(cfg: Config, refresh: bool = False) -> dict:
    """Natively levels for the audiobook catalog (keyed by Audible ASIN)
    → `data/natively_levels.json`, the same lookup and retry rules as the EPUB
    stage; this replaces the old standalone scripts/fetch_natively_levels.py.

    Resume-safe: a catalog entry with no result is looked up, a miss is retried
    after RETRY_DAYS, and a match is final — including the ones the old scraper
    wrote, which carry no `fetched_at` (so the first run retries every old miss
    once). `refresh` re-fetches every entry (about an hour for ~3,000). The result is a
    per-machine file under `data/`, like every other index."""
    catalog: dict = audiobook_catalog.load(cfg)
    if not catalog:
        print("The audiobook catalog is empty (run `catalog` first); nothing to enrich.")
        return {}
    cache: dict = load_json(cfg.audiobook_natively, {})

    now = datetime.now(timezone.utc)
    todo = []
    for isbn, ab in catalog.items():
        query = {"series": ab.get("series") or "", "order": _volume(ab.get("order"))}
        entry = cache.get(isbn)
        if refresh and entry is not None and entry.get("match_type") != MANUAL \
                or wants_lookup(entry, query, now, legacy_matches_final=True):
            todo.append((isbn, ab.get("title") or "", query))

    print(f"Audiobook Natively enrichment: {len(catalog)} catalog entries, {len(todo)} to fetch.")
    _fetch(cfg, cfg.audiobook_natively, cache, todo)
    return cache
