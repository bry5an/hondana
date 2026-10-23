"""Genre and description for EPUBs, by ISBN.

An EPUB's OPF rarely carries a description (about 1 in 6) and never a genre. A
linked audiobook supplies both when there is one (build.py). For the rest that
have an ISBN in their OPF identifiers, `fetch_missing()` asks the Google Books API
and keeps the answer in `data/epub_catalog_extra.json` — generated and per-machine
like `audiobook_catalog_extra.json` — keyed by ISBN so it survives a rename. `build`
uses it last: OPF, then the linked audiobook, then this.

Needs a free API key (`GOOGLE_BOOKS_API_KEY`, see .env.example): keyless access
shares one exhausted daily quota. Without a key the stage does nothing and says so.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import requests

from .config import Config
from .index_store import load_json, save_json
from .textnorm import plain_text

API_URL = "https://www.googleapis.com/books/v1/volumes"
RETRY_DAYS = 14      # an ISBN Google had nothing for is asked about again after this
GIVE_UP_AFTER = 3    # consecutive request errors (not "no result") before the run stops
SAVE_EVERY = 25

# Google's categories are English/BISAC style ("Fiction / Fantasy / General").
# Matched against all of a volume's categories, in this order, onto the genre
# words the catalog already uses; a non-fiction volume becomes ノンフィクション.
_GENRE_RULES = (
    ("juvenile", "児童書"),
    ("mystery", "ミステリー・サスペンス"),
    ("thriller", "ミステリー・サスペンス"),
    ("detective", "ミステリー・サスペンス"),
    ("horror", "ホラー"),
    ("fantasy", "SF・ファンタジー"),
    ("science fiction", "SF・ファンタジー"),
    ("historical", "時代小説"),
    ("fiction", "文芸作品"),
)
_SKIP_GENRE = ("comics", "graphic novels")  # a format, not a genre


def genre_from_categories(categories: list[str] | None) -> str | None:
    text = " / ".join(categories or []).lower()
    if not text or any(s in text for s in _SKIP_GENRE):
        return None
    for needle, genre in _GENRE_RULES:
        if needle in text:
            return genre
    return "ノンフィクション"


def entry_from_volume(volume: dict, now: datetime) -> dict:
    info = volume.get("volumeInfo") or {}
    return {
        "description": plain_text(info.get("description")),
        "genre": genre_from_categories(info.get("categories")),
        "categories": info.get("categories") or [],
        "fetched_at": now.isoformat(timespec="seconds"),
    }


def fetch_volume(isbn: str, api_key: str) -> dict | None:
    """The first Google Books volume for `isbn`, or None if there is none. Any
    HTTP failure (quota, bad key, outage) raises, so it isn't recorded as a miss."""
    r = requests.get(API_URL, params={"q": f"isbn:{isbn}", "maxResults": 1, "key": api_key},
                     timeout=15)
    r.raise_for_status()
    items = r.json().get("items") or []
    return items[0] if items else None


def load(cfg: Config) -> dict:
    return load_json(cfg.epub_catalog_extra, {})


def _wanted(library: dict, extra: dict, now: datetime) -> dict[str, str]:
    """{isbn: book title} for books with an ISBN that still lack a description or
    genre after the audiobook join, not yet looked up (or a miss old enough to retry)."""
    out: dict[str, str] = {}
    for book in library.values():
        isbn = (book.get("identifiers") or {}).get("isbn")
        if not isbn or isbn in out:
            continue
        if book.get("description") and book.get("genre"):
            continue
        have = extra.get(isbn)
        if have:
            try:
                age = now - datetime.fromisoformat(have["fetched_at"])
            except (KeyError, ValueError, TypeError):
                age = None
            found = have.get("description") or have.get("genre")
            if found or (age is not None and age < timedelta(days=RETRY_DAYS)):
                continue
        out[isbn] = book.get("title") or isbn
    return out


def fetch_missing(cfg: Config, *, delay: float = 0.3) -> dict:
    """Look up the wanted ISBNs and save the results; rebuild the combined index
    when something was found. Returns the extras."""
    from . import build as build_mod  # late: build reads this module's file

    extra: dict = load(cfg)
    if not cfg.google_books_api_key:
        print("EPUB catalog: no GOOGLE_BOOKS_API_KEY set (see .env.example); skipping ISBN lookup.")
        return extra

    now = datetime.now(timezone.utc)
    todo = _wanted(load_json(cfg.library_index, {}), extra, now)
    if not todo:
        print("EPUB catalog: nothing missing.")
        return extra

    print(f"EPUB catalog: {len(todo)} ISBN(s) without a description or genre.")
    found = errors = 0
    for i, (isbn, title) in enumerate(todo.items(), 1):
        try:
            volume = fetch_volume(isbn, cfg.google_books_api_key)
        except Exception as exc:  # noqa: BLE001 — quota/network: report, keep the rest
            errors += 1
            print(f"  [{i}/{len(todo)}] ! {isbn}: {exc}")
            if errors >= GIVE_UP_AFTER:
                print("  Stopping after repeated errors; run it again later.")
                break
            continue
        errors = 0
        entry = entry_from_volume(volume or {}, now)
        extra[isbn] = entry
        if entry["description"] or entry["genre"]:
            found += 1
            print(f"  [{i}/{len(todo)}] ✓ {title[:40]}  {entry['genre']}")
        else:
            print(f"  [{i}/{len(todo)}] – {title[:50]}")
        if i % SAVE_EVERY == 0:
            save_json(cfg.epub_catalog_extra, extra)
        time.sleep(delay)

    save_json(cfg.epub_catalog_extra, extra)
    print(f"EPUB catalog: {found} found.")
    if found:
        build_mod.build(cfg)
    return extra
