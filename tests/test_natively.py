"""Natively lookup against saved responses (no network).

The fixtures under tests/fixtures/natively/ are synthetic, shaped like
learnnatively.com's responses (as of 2026-10): the autocomplete API for a series
and for one of its volumes, the series page (books #1–#4), and that volume's book
page around the ratings JSON. If ratings or volume links stop populating in the
real pipeline, Natively changed its markup: update RATINGS_RE /
fetch_series_volume_url, and these fixtures to the new shape.
"""
import json
import re
from pathlib import Path

import pytest

from epublib import natively

FIXTURES = Path(__file__).parent / "fixtures" / "natively"
SERIES = "星降る町の図書係"
VOLUME = f"{SERIES}3"
SERIES_PATH = "/series/5e1f0a7c2b/"
VOL3_URL = f"{natively.BASE_URL}/book/3c4d5e6f7a/"


def _text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class _Resp:
    def __init__(self, text: str, status_code: int = 200):
        self.text, self.status_code = text, status_code

    def json(self):
        return json.loads(self.text)


@pytest.fixture
def site(monkeypatch):
    """Route requests.get to the fixtures; records every URL asked for."""
    pages = {
        VOL3_URL: _text("book_page.html"),
        f"{natively.BASE_URL}{SERIES_PATH}": _text("series_page.html"),
    }
    autocomplete = {
        SERIES: _text("autocomplete_series.json"),
        VOLUME: _text("autocomplete_volume.json"),
    }
    asked: list[str] = []

    def get(url, params=None, **kwargs):
        if url == natively.AUTOCOMPLETE_URL:
            asked.append(f"q={params['q']}")
            return _Resp(autocomplete.get(params["q"], "[]"))
        asked.append(url)
        return _Resp(pages[url]) if url in pages else _Resp("", 404)

    monkeypatch.setattr(natively.requests, "get", get)
    return asked


def test_ratings_are_read_from_the_book_page(site):
    assert natively.fetch_ratings(VOL3_URL) == {
        "overall_rating": 3.75, "overall_rating_count": 12,
        "entertainment_rating": 4.25, "entertainment_rating_count": 4,
        "language_learning_rating": 5.0, "language_learning_rating_count": 4,
    }


def test_a_rating_with_no_votes_is_none_not_zero(monkeypatch):
    page = re.sub(r'(\\"overall\\":\{\\"count\\":)\d+', r"\g<1>0", _text("book_page.html"))
    monkeypatch.setattr(natively.requests, "get", lambda url, **kw: _Resp(page))
    ratings = natively.fetch_ratings(VOL3_URL)
    assert ratings["overall_rating_count"] == 0 and ratings["overall_rating"] is None
    assert ratings["entertainment_rating"] == 4.25


def test_ratings_fetch_failures_return_nothing(site):
    assert natively.fetch_ratings(f"{natively.BASE_URL}/book/0000000000/") == {}  # 404


def test_series_page_gives_each_volumes_own_url(site):
    assert natively.fetch_series_volume_url(SERIES_PATH, 3) == VOL3_URL
    vol1 = natively.fetch_series_volume_url(SERIES_PATH, 1)
    assert vol1 and vol1.startswith(f"{natively.BASE_URL}/book/") and vol1 != VOL3_URL
    assert natively.fetch_series_volume_url(SERIES_PATH, 99) is None


def test_item_match_prefers_the_novel_over_the_manga_of_the_same_name():
    results = json.loads(_text("autocomplete_volume.json"))
    assert {(r["item"]["bookType"]) for r in results} == {"light_novel", "manga"}
    item = natively.best_item_match(results, VOLUME, SERIES, natively.DEFAULT_MIN_SCORE)
    assert item["url"] == "/book/3c4d5e6f7a/" and item["level"] == 25


def test_series_match_skips_manga_series():
    results = json.loads(_text("autocomplete_series.json"))
    ser = natively.best_series_match(results, SERIES)
    assert ser["url"] == SERIES_PATH and ser["level"] == 25
    assert natively.best_item_match(results, SERIES, SERIES, natively.DEFAULT_MIN_SCORE) is None


def test_lookup_item_match_end_to_end(site):
    r = natively.lookup(VOLUME, SERIES, 3, delay=0)
    assert r["match_type"] == "item" and r["natively_level"] == 25
    assert r["natively_url"] == VOL3_URL and r["confidence"] == 1.0
    assert r["overall_rating"] == 3.75 and r["language_learning_rating_count"] == 4
    assert site == [f"q={VOLUME}", VOL3_URL]


def test_lookup_falls_back_to_the_series_and_its_volume_page(site):
    # A title the autocomplete doesn't know: the series search finds the
    # series, and its page gives volume 3's own URL (and so its ratings).
    r = natively.lookup(f"{SERIES} 特装版 第3巻", SERIES, 3, delay=0)
    assert r["match_type"] == "series" and r["natively_level"] == 25
    assert r["natively_url"] == VOL3_URL and r["overall_rating_count"] == 12
    assert site[1:] == [f"q={SERIES}", f"{natively.BASE_URL}{SERIES_PATH}", VOL3_URL]


def test_lookup_miss_returns_the_empty_result(site):
    r = natively.lookup("存在しない本", "", 0, delay=0)
    assert r["natively_level"] is None and r["confidence"] == 0 and r["match_type"] is None
    assert len(site) == 1  # one autocomplete, no page fetches
