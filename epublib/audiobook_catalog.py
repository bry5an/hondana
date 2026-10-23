"""The audiobook catalog: an optional base file (`cfg.audiobook_data`, a
hand-curated catalog if you keep one) plus whatever has been pulled by ASIN.

The base file is immutable and per-machine (`data/audiobook_catalog_base.json`;
on a fresh clone it does not exist and the catalog starts
as just the pulled rows). A title bought
or ripped after that carries its ASIN in the file name (`… [B07QMQHLVH].m4b`) but
had no metadata, so it showed with a filename-derived title and no cover, series
order or genre, and xref could never link an EPUB to it. `fetch_missing()` looks
those ASINs up in Audible's public catalog API (no login) and keeps the result in
`data/audiobook_catalog_extra.json`, a generated per-machine file like every other
index. Everything that used to read `cfg.audiobook_data` reads `load()`/`cached()`
instead, which lay the extras over the base per field.

Rows pulled this way follow the base file's shape (`series` always populated,
`order` a zero-padded volume number or None). `genre` is Audible's own category,
mapped onto the existing vocabulary where one fits and kept as Audible names it
otherwise.
"""
from __future__ import annotations

import html
import re
import threading
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import requests

from . import audiobook_scanner
from .audiobook_scanner import AUDIBLE_KEY_RE
from .config import Config
from .index_store import load_json, save_json

API_URL = "https://api.audible.co.jp/1.0/catalog/products/{asin}"
RESPONSE_GROUPS = ("product_desc,product_attrs,series,contributors,media,rating,"
                   "category_ladders,product_extended_attrs")
RETRY_DAYS = 14        # an ASIN Audible didn't know is asked about again after this
GIVE_UP_AFTER = 3      # consecutive request errors (not 404s) before the run stops
SAVE_EVERY = 25

# ── reading ──────────────────────────────────────────────────────────────────


def _merge(base: dict, extra: dict) -> dict:
    merged = dict(base)
    for key, fields in extra.items():
        if fields.get("title") is None:  # a recorded miss, not a book
            continue
        merged[key] = {**base.get(key, {}), **fields}
    return merged


def load(cfg: Config) -> dict:
    """The full catalog, keyed by ASIN / source id: base rows with the pulled
    rows (and any per-field overlay) applied. A fresh dict — safe to mutate."""
    return _merge(load_json(cfg.audiobook_data, {}), load_json(cfg.audiobook_catalog_extra, {}))


_cache: dict = {}
_cache_lock = threading.Lock()


def _stamp(path) -> tuple | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def cached(cfg: Config) -> dict:
    """`load()` for request paths: re-merged only when either file changed.
    Read-only, like index_store.cached_json."""
    version = (_stamp(cfg.audiobook_data), _stamp(cfg.audiobook_catalog_extra))
    key = (str(cfg.audiobook_data), str(cfg.audiobook_catalog_extra))
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] == version:
            return hit[1]
    data = load(cfg)
    with _cache_lock:
        _cache[key] = (version, data)
    return data


# ── Audible product → catalog row ────────────────────────────────────────────

# Audible's category ladders → the catalog's existing genre words, checked in
# this order against every name on the first ladder. Anything unmatched keeps
# its top-level Audible name (歴史, ビジネス…), which is the more accurate label.
_GENRE_RULES = (
    ("ライトノベル", "ライトノベル"),
    ("ミステリー", "ミステリー・サスペンス"),
    ("ホラー", "ホラー"),
    ("SF・ファンタジー", "SF・ファンタジー"),
    ("絵本・児童書", "児童書"),
    ("歴史小説", "時代小説"),
    ("文学・フィクション", "文芸作品"),
)


def _genre(product: dict) -> str | None:
    ladders = product.get("category_ladders") or []
    names = [c.get("name") or "" for c in (ladders[0].get("ladder") or [])] if ladders else []
    for needle, genre in _GENRE_RULES:
        if any(needle in n for n in names):
            return genre
    return names[0] if names else None


def _order(sequence: str | None) -> str | None:
    """"第3巻", "16巻・下・前編", "4 忙しいですか？ 第4話" → "03", "16", "04"; no number → None."""
    m = re.search(r"\d+", unicodedata.normalize("NFKC", sequence or ""))
    return m.group(0).zfill(2) if m else None


def _names(people) -> str:
    return ", ".join(p["name"] for p in people or [] if p.get("name"))


def _text(value: str | None) -> str | None:
    if not value:
        return None
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value))).strip() or None


def entry_from_product(product: dict) -> dict:
    """One catalog row, in the shape data.json uses, from an API `product`."""
    title = product.get("title") or ""
    series = (product.get("series") or [{}])[0]
    rating = (product.get("rating") or {}).get("overall_distribution") or {}
    images = product.get("product_images") or {}
    avg = rating.get("average_rating")
    return {
        "title": title,
        "author": _names(product.get("authors")),
        "series": series.get("title") or title,
        "order": _order(series.get("sequence")) if series.get("title") else None,
        "cover": images.get("500") or next(iter(images.values()), None),
        "audible_rating": f"{avg:.2f}" if avg else None,
        "audible_rating_count": str(rating.get("num_ratings") or 0),
        "audible_reviews": str((product.get("rating") or {}).get("num_reviews") or 0),
        "release_date": product.get("release_date"),
        "genre": _genre(product),
        "runtime_length_min": str(product["runtime_length_min"])
        if product.get("runtime_length_min") is not None else None,
        "description_short": _text(product.get("merchandising_summary")),
        "description_full": _text(product.get("publisher_summary")),
        "narrators": _names(product.get("narrators")),
    }


# ── fetching ─────────────────────────────────────────────────────────────────

def fetch_product(asin: str) -> dict | None:
    """The Audible product for `asin`, or None if it doesn't exist (404).
    Any other failure raises, so a network outage isn't recorded as a miss."""
    r = requests.get(API_URL.format(asin=asin), params={"response_groups": RESPONSE_GROUPS},
                     timeout=15)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json().get("product")


def _wanted(cfg: Config, base: dict, extra: dict, now: datetime) -> list[str]:
    """ASINs on disk that the catalog doesn't know: not in the base file, not
    pulled already, and not an unavailable one asked about recently."""
    out = []
    for key in load_json(cfg.audiobook_files_index, {}).values():
        asin = key.get("catalog_key")
        if not asin or not AUDIBLE_KEY_RE.fullmatch(asin) or asin in base or asin in out:
            continue
        have = extra.get(asin)
        if have and have.get("title") is not None:
            continue
        if have:
            try:
                if now - datetime.fromisoformat(have["unavailable_at"]) < timedelta(days=RETRY_DAYS):
                    continue
            except (KeyError, ValueError, TypeError):
                pass
        out.append(asin)
    return out


def fetch_missing(cfg: Config, *, delay: float = 0.3) -> dict:
    """Pull catalog rows for audiobook files whose ASIN is in no catalog yet,
    save them to `audiobook_catalog_extra.json`, and rebuild the audiobook index
    when anything was added. Returns the extras."""
    base: dict = load_json(cfg.audiobook_data, {})
    extra: dict = load_json(cfg.audiobook_catalog_extra, {})
    now = datetime.now(timezone.utc)
    todo = _wanted(cfg, base, extra, now)
    if not todo:
        print("Audiobook catalog: nothing missing.")
        return extra

    print(f"Audiobook catalog: {len(todo)} ASIN(s) not in the catalog.")
    added = errors = 0
    for i, asin in enumerate(todo, 1):
        try:
            product = fetch_product(asin)
        except Exception as exc:  # noqa: BLE001 — network/HTTP, report and keep going
            errors += 1
            print(f"  [{i}/{len(todo)}] ! {asin}: {exc}")
            if errors >= GIVE_UP_AFTER:
                print("  Stopping after repeated errors; run it again later.")
                break
            continue
        errors = 0
        # A series/parent ASIN comes back as a container with no runtime: not a book.
        if product and product.get("title") and product.get("runtime_length_min"):
            extra[asin] = entry_from_product(product)
            added += 1
            print(f"  [{i}/{len(todo)}] ✓ {asin} {extra[asin]['title'][:50]}")
        else:
            extra[asin] = {"title": None, "unavailable_at": now.isoformat(timespec="seconds")}
            print(f"  [{i}/{len(todo)}] – {asin} (not a book in Audible's catalog)")
        if i % SAVE_EVERY == 0:
            save_json(cfg.audiobook_catalog_extra, extra)
        time.sleep(delay)

    save_json(cfg.audiobook_catalog_extra, extra)
    print(f"Audiobook catalog: {added} added.")
    if added:
        audiobook_scanner.build_index(cfg)
    return extra
