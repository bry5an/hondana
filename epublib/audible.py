"""Audible's public catalog API: one product by ASIN, or a search.

No login and no account: the same `/1.0/catalog/products` endpoint the Audible
site's own pages use. Everything that talks to Audible goes through here, so the
store (`MARKETPLACES`), the User-Agent and the error messages are in one place:

- `fetch_product(asin, marketplace)` — the product, or None if Audible has no
  such book (404). Any other failure raises `AudibleError`, so an outage is
  never recorded as "Audible doesn't know it". (A bad argument, an unknown
  store or an empty search, is a `ValueError`.)
- `search(...)` — products matching a title / author / keywords, cached for ten
  minutes (a dialog retyped three times costs one request).
- `pd_url(asin, marketplace)` — the book's page on the Audible site.
- `parse_asin(text)` — an ASIN from what someone pasted, a bare ASIN or an
  Audible link.

An ASIN belongs to a store: the Japanese catalog has books the American one
doesn't, and the same book can carry different ASINs. `audible.marketplace` in
the settings says which store this library is bought from.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass

import requests

DEFAULT_MARKETPLACE = "jp"
TIMEOUT = 15
CACHE_SECONDS = 600
CACHE_SIZE = 100
SEARCH_LIMIT = 10
RESPONSE_GROUPS = ("product_desc,product_attrs,series,contributors,media,rating,"
                   "category_ladders,product_extended_attrs")
# Audible ASINs: B0… for most titles, an ISBN-10 for some older ones.
ASIN_RE = re.compile(r"B0[A-Z0-9]{8}|\d{9}[\dX]")
_ASIN_IN_TEXT = re.compile(r"(?<![A-Z0-9])B0[A-Z0-9]{8}(?![A-Z0-9])", re.IGNORECASE)


@dataclass(frozen=True)
class Marketplace:
    tld: str
    label: str

    @property
    def api(self) -> str:
        return f"https://api.audible.{self.tld}/1.0/catalog/products"

    @property
    def site(self) -> str:
        return f"https://www.audible.{self.tld}"


MARKETPLACES: dict[str, Marketplace] = {
    "jp": Marketplace("co.jp", "Japan"),
    "us": Marketplace("com", "United States"),
    "uk": Marketplace("co.uk", "United Kingdom"),
    "de": Marketplace("de", "Germany"),
    "fr": Marketplace("fr", "France"),
    "ca": Marketplace("ca", "Canada"),
    "au": Marketplace("com.au", "Australia"),
    "in": Marketplace("in", "India"),
    "it": Marketplace("it", "Italy"),
    "es": Marketplace("es", "Spain"),
}


class AudibleError(RuntimeError):
    """Audible couldn't be reached or answered badly. The message is meant to be
    shown to the person who clicked."""


def market(code: str | None) -> Marketplace:
    try:
        return MARKETPLACES[(code or DEFAULT_MARKETPLACE).strip().lower()]
    except KeyError:
        raise ValueError(f"Unknown Audible marketplace: {code}") from None


def pd_url(asin: str, marketplace: str | None = None) -> str:
    """The book's page on the Audible site."""
    return f"{market(marketplace).site}/pd/{asin}"


def parse_asin(text: str | None) -> str | None:
    """The ASIN in `text`: a bare one, or one inside an Audible link
    (`…/pd/Title/B0CCN9M612?ref=…`). None when there isn't one."""
    text = (text or "").strip()
    if not text:
        return None
    m = _ASIN_IN_TEXT.search(text)
    if m:
        return m.group(0).upper()
    bare = text.upper()
    return bare if ASIN_RE.fullmatch(bare) else None


def _user_agent() -> str:
    try:
        from importlib.metadata import version
        v = version("hondana")
    except Exception:  # not installed (a checkout)
        v = "dev"
    return f"Hondana/{v} (+https://github.com/bry5an/hondana)"


def _get(url: str, params: dict) -> dict | None:
    """The JSON at `url`, None for a 404. Everything else wrong is an AudibleError."""
    try:
        r = requests.get(url, params=params, timeout=TIMEOUT,
                         headers={"User-Agent": _user_agent(), "Accept": "application/json"})
    except requests.Timeout:
        raise AudibleError("Audible didn't answer in time. Try again in a moment.") from None
    except requests.RequestException:
        raise AudibleError("Couldn't reach Audible. Check this computer's internet connection.") from None
    if r.status_code == 404:
        return None
    if r.status_code == 429:
        raise AudibleError("Audible is limiting requests right now. Wait a minute and try again.")
    if r.status_code >= 400:
        raise AudibleError(f"Audible answered with an error ({r.status_code}). Try again later.")
    try:
        return r.json()
    except ValueError:
        raise AudibleError("Audible sent a reply that can't be read.") from None


def fetch_product(asin: str, marketplace: str | None = None) -> dict | None:
    """The Audible product for `asin`, or None if it doesn't exist (404).
    Any other failure raises, so a network outage isn't recorded as a miss."""
    data = _get(f"{market(marketplace).api}/{asin}", {"response_groups": RESPONSE_GROUPS})
    return (data or {}).get("product")


# ── search ───────────────────────────────────────────────────────────────────

_now = time.monotonic
_cache: dict[tuple, tuple[float, list[dict]]] = {}
_cache_lock = threading.Lock()


def _cached(key: tuple, fetch) -> list[dict]:
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > _now():
            return hit[1]
    value = fetch()
    with _cache_lock:
        if len(_cache) >= CACHE_SIZE:
            for old in sorted(_cache, key=lambda k: _cache[k][0])[:CACHE_SIZE // 4]:
                del _cache[old]
        _cache[key] = (_now() + CACHE_SECONDS, value)
    return value


def _query(marketplace: str | None, title: str, author: str, keywords: str, limit: int) -> list[dict]:
    params = {"response_groups": RESPONSE_GROUPS, "num_results": limit,
              "products_sort_by": "Relevance"}
    for name, value in (("title", title), ("author", author), ("keywords", keywords)):
        if value:
            params[name] = value
    data = _get(market(marketplace).api, params) or {}
    # A series page comes back as a "product" with no runtime: not a book.
    return [p for p in data.get("products") or [] if p.get("title") and p.get("runtime_length_min")]


def search(title: str = "", author: str = "", keywords: str = "", *,
           marketplace: str | None = None, limit: int = SEARCH_LIMIT) -> list[dict]:
    """Products matching the title / author / keywords, best first. Tags and file
    names spell things differently than Audible does, so a title search that
    finds nothing is widened twice: without the author, then as keywords (which
    forgive a trailing "(series 1)" or a volume marker that `title` does not)."""
    title, author, keywords = (" ".join(s.split()) for s in (title, author, keywords))
    if not (title or author or keywords):
        raise ValueError("Give a title, an author or some keywords to search for.")
    code = (marketplace or DEFAULT_MARKETPLACE).strip().lower()
    market(code)  # fail on an unknown store before anything is cached

    def run(t: str, a: str, k: str) -> list[dict]:
        return _cached((code, t, a, k, limit), lambda: _query(code, t, a, k, limit))

    found = run(title, author, keywords)
    if not found and title and author:
        found = run(title, "", keywords)
    if not found and title and not keywords:
        found = run("", "", title)
    return found
