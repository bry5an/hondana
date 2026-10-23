"""Natively (learnnatively.com) lookup logic.

Refactored from the audiobook project's original scraper (scripts/fetch_natively_levels.py,
since retired): `lookup(title, series)` performs the two-attempt search (item match,
then series match + volume URL + ratings) and returns the result dict. It takes
title/series as arguments and has no hard-coded paths; `enrich.py` drives it for both
the EPUB library and the audiobook catalog.
"""
from __future__ import annotations

import difflib
import re
import time

try:
    import requests
except ImportError:  # pragma: no cover
    raise SystemExit("Install requests first:  pip3 install requests")

from .textnorm import natively_query

AUTOCOMPLETE_URL = "https://learnnatively.com/api/autocomplete-api/"
BASE_URL = "https://learnnatively.com"
DEFAULT_MIN_SCORE = 0.55
SERIES_MIN_SCORE = 0.6

# These are audiobooks/novels; exclude manga/comic to avoid wrong matches.
EXCLUDED_BOOK_TYPES = {"manga", "comic"}
EXCLUDED_SERIES_TYPES = {"manga_series", "comic_series"}

RATINGS_RE = re.compile(
    r'\\"reviewData\\":\{\\"overall\\":\{\\"count\\":(\d+),\\"sum\\":[\d]+,\\"avg\\":\\"([\d.]+)\\"'
    r'\},\\"entertainment\\":\{\\"count\\":(\d+),\\"sum\\":[\d]+,\\"avg\\":\\"([\d.]+)\\"'
    r'\},\\"language\\":\{\\"count\\":(\d+),\\"sum\\":[\d]+,\\"avg\\":\\"([\d.]+)\\"'
)

_NULL_RATINGS = {
    "overall_rating": None,
    "overall_rating_count": None,
    "entertainment_rating": None,
    "entertainment_rating_count": None,
    "language_learning_rating": None,
    "language_learning_rating_count": None,
}


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def autocomplete(query: str) -> list:
    try:
        r = requests.get(
            AUTOCOMPLETE_URL,
            params={"q": query, "language": "jpn"},
            headers={"Accept": "application/json"},
            timeout=12,
        )
        if r.status_code == 200:
            return r.json()
    except Exception as exc:
        print(f"    request error: {exc}")
    return []


def fetch_ratings(url: str) -> dict:
    try:
        r = requests.get(url, timeout=15)
        if r.status_code != 200:
            return {}
        m = RATINGS_RE.search(r.text)
        if m:
            def _val(count_str, avg_str):
                count = int(count_str)
                return count, (float(avg_str) if count else None)

            oc, oa = _val(m.group(1), m.group(2))
            ec, ea = _val(m.group(3), m.group(4))
            lc, la = _val(m.group(5), m.group(6))
            return {
                "overall_rating": oa,
                "overall_rating_count": oc,
                "entertainment_rating": ea,
                "entertainment_rating_count": ec,
                "language_learning_rating": la,
                "language_learning_rating_count": lc,
            }
    except Exception as exc:
        print(f"    ratings fetch error: {exc}")
    return {}


def fetch_series_volume_url(series_path: str, order: int) -> str | None:
    try:
        r = requests.get(f"{BASE_URL}{series_path}", timeout=15)
        if r.status_code != 200:
            return None
        content = r.text
        for m in re.finditer(
            r'item-type-notice[^>]*>\(<!-- -->Book<!-- --> #<!-- -->(\d+)<!-- -->\)', content
        ):
            if int(m.group(1)) == order:
                preceding = content[max(0, m.start() - 2000): m.start()]
                urls = re.findall(r'href="(/book/[a-f0-9]{10}/)"', preceding)
                if urls:
                    return f"{BASE_URL}{urls[-1]}"
    except Exception as exc:
        print(f"    series page fetch error: {exc}")
    return None


def best_item_match(results: list, title: str, series: str, min_score: float) -> dict | None:
    best, best_score = None, 0.0
    for result in results:
        if result.get("widget") != "item":
            continue
        obj = result.get("item") or {}
        if obj.get("bookType") in EXCLUDED_BOOK_TYPES:
            continue
        level = (obj.get("rating") or {}).get("lvl")
        if level is None:
            continue
        obj_title = obj.get("title", "")
        obj_series = (obj.get("series") or {}).get("title", "")
        t_score = similarity(title, obj_title)
        s_score = similarity(series, obj_series) if series and obj_series else t_score
        score = t_score * 0.6 + s_score * 0.4
        if score > best_score:
            best_score = score
            best = {"score": score, "title": obj_title, "url": obj.get("url"), "level": level}
    return best if best_score >= min_score else None


def best_series_match(results: list, series_name: str) -> dict | None:
    best, best_score = None, 0.0
    for result in results:
        if result.get("widget") != "series":
            continue
        obj = result.get("series") or {}
        if obj.get("mediaType") in EXCLUDED_SERIES_TYPES:
            continue
        level = (obj.get("rating") or {}).get("lvl")
        if level is None:
            continue
        obj_title = obj.get("title", "")
        score = similarity(series_name, obj_title)
        if score > best_score:
            best_score = score
            best = {"score": score, "title": obj_title, "url": obj.get("url"), "level": level}
    return best if best_score >= SERIES_MIN_SCORE else None


def lookup(
    title: str,
    series: str = "",
    order: int = 0,
    *,
    min_score: float = DEFAULT_MIN_SCORE,
    delay: float = 0.4,
) -> dict:
    """Look up a single book on Natively. Returns a result dict matching the
    schema the audiobook scraper emits (natively_level/url/ratings/...).

    `delay` is slept after each network request to be polite.
    """
    series = series or ""
    cleaned = natively_query(title)

    book_url = None
    level = None
    match_title = None
    confidence = 0.0
    match_type = None

    # attempt 1: search by cleaned title, prefer non-manga items
    results = autocomplete(cleaned)
    time.sleep(delay)
    item = best_item_match(results, cleaned, series, min_score)
    if item:
        book_url = f"{BASE_URL}{item['url']}"
        level = item["level"]
        match_title = item["title"]
        confidence = round(item["score"], 2)
        match_type = "item"

    # attempt 2: series search (also catches manga-only item results)
    if not item and series:
        search_query = series if series != title else cleaned
        results2 = autocomplete(search_query)
        time.sleep(delay)
        item2 = best_item_match(results2, cleaned, series, min_score)
        if item2:
            book_url = f"{BASE_URL}{item2['url']}"
            level = item2["level"]
            match_title = item2["title"]
            confidence = round(item2["score"], 2)
            match_type = "item"
        else:
            ser = best_series_match(results2, series)
            if ser:
                level = ser["level"]
                match_title = ser["title"]
                confidence = round(ser["score"], 2)
                match_type = "series"
                if order:
                    time.sleep(delay)
                    book_url = fetch_series_volume_url(ser["url"], order)
                if not book_url:
                    book_url = f"{BASE_URL}{ser['url']}"

    # attempt 3: for standalone books (no series metadata), check if title matches a
    # Natively series entry — reuses attempt-1 results, no extra request
    if level is None and not series:
        ser = best_series_match(results, cleaned)
        if ser:
            level = ser["level"]
            match_title = ser["title"]
            confidence = round(ser["score"], 2)
            match_type = "series"
            book_url = f"{BASE_URL}{ser['url']}"

    if level is None:
        return {
            "natively_level": None,
            "natively_url": None,
            "matched_title": None,
            "confidence": 0,
            "match_type": None,
            **_NULL_RATINGS,
        }

    ratings = {}
    if book_url and "/book/" in book_url:
        ratings = fetch_ratings(book_url)
        time.sleep(delay)

    return {
        "natively_level": level,
        "natively_url": book_url,
        "matched_title": match_title,
        "confidence": confidence,
        "match_type": match_type,
        "overall_rating": ratings.get("overall_rating"),
        "overall_rating_count": ratings.get("overall_rating_count"),
        "entertainment_rating": ratings.get("entertainment_rating"),
        "entertainment_rating_count": ratings.get("entertainment_rating_count"),
        "language_learning_rating": ratings.get("language_learning_rating"),
        "language_learning_rating_count": ratings.get("language_learning_rating_count"),
    }
