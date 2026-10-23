"""EPUB genre/description by ISBN: Google categories → catalog genre words,
only books still missing something are looked up, misses throttled, errors never
recorded, results used by build below the OPF and the linked audiobook."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from epublib import build, epub_catalog
from epublib.index_store import load_json, save_json

NOW = datetime(2026, 10, 5, tzinfo=timezone.utc)


@pytest.mark.parametrize("categories, genre", [
    (["Fiction / Fantasy / General"], "SF・ファンタジー"),
    (["Fiction / Mystery & Detective / General"], "ミステリー・サスペンス"),
    (["Juvenile Fiction / Animals"], "児童書"),            # checked before plain "fiction"
    (["Fiction / Horror"], "ホラー"),
    (["Fiction / Literary"], "文芸作品"),
    (["Biography & Autobiography / General"], "ノンフィクション"),
    (["Comics & Graphic Novels / Manga"], None),           # a format, not a genre
    ([], None), (None, None),
])
def test_genre_from_categories(categories, genre):
    assert epub_catalog.genre_from_categories(categories) == genre


def test_entry_from_volume_strips_markup_from_the_description():
    e = epub_catalog.entry_from_volume({"volumeInfo": {
        "description": "<p>第一段落 &amp; <b>強調</b></p><p>二段落<br>三行目</p>",
        "categories": ["Fiction / Fantasy / General"]}}, NOW)
    assert e["description"] == "第一段落 & 強調\n二段落\n三行目"
    assert (e["genre"], e["categories"], e["fetched_at"]) == (
        "SF・ファンタジー", ["Fiction / Fantasy / General"], "2026-10-05T00:00:00+00:00")


def _setup(cfg, monkeypatch, library, volumes, extra=None, key="k"):
    cfg = replace(cfg, google_books_api_key=key)
    save_json(cfg.library_index, library)
    if extra is not None:
        save_json(cfg.epub_catalog_extra, extra)
    calls = []

    def fake(isbn, api_key):
        calls.append(isbn)
        out = volumes.get(isbn)
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(epub_catalog, "fetch_volume", fake)
    monkeypatch.setattr(build, "build", lambda c: calls.append("build"))
    return cfg, calls


def _book(bid, isbn=None, **kw):
    return {"book_id": bid, "title": bid, "identifiers": {"isbn": isbn} if isbn else {}, **kw}


VOL = {"volumeInfo": {"description": "d", "categories": ["Fiction / Fantasy / General"]}}


def test_only_books_with_an_isbn_that_still_lack_something_are_looked_up(cfg, monkeypatch):
    cfg, calls = _setup(cfg, monkeypatch, {
        "need": _book("need", "111"),
        "no_isbn": _book("no_isbn"),
        "complete": _book("complete", "222", description="x", genre="ホラー"),
        "half": _book("half", "333", description="x"),              # genre still missing
        "dupe": _book("dupe", "111"),
    }, {"111": VOL, "333": VOL})
    extra = epub_catalog.fetch_missing(cfg, delay=0)
    assert calls == ["111", "333", "build"]
    assert extra["111"]["genre"] == "SF・ファンタジー" and load_json(cfg.epub_catalog_extra, {}) == extra


def test_without_an_api_key_it_does_nothing(cfg, monkeypatch, capsys):
    cfg, calls = _setup(cfg, monkeypatch, {"a": _book("a", "111")}, {"111": VOL}, key="")
    assert epub_catalog.fetch_missing(cfg, delay=0) == {}
    assert calls == [] and "GOOGLE_BOOKS_API_KEY" in capsys.readouterr().out


def test_found_isbns_are_never_asked_again_and_fresh_misses_wait_two_weeks(cfg, monkeypatch):
    recent = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cfg, calls = _setup(cfg, monkeypatch, {
        "found": _book("found", "111"), "miss_new": _book("miss_new", "222"),
        "miss_old": _book("miss_old", "333")}, {"333": VOL}, extra={
            "111": {"description": "d", "genre": None, "fetched_at": "2020-01-01T00:00:00+00:00"},
            "222": {"description": None, "genre": None, "fetched_at": recent},
            "333": {"description": None, "genre": None, "fetched_at": "2020-01-01T00:00:00+00:00"}})
    epub_catalog.fetch_missing(cfg, delay=0)
    assert calls == ["333", "build"]


def test_no_result_is_recorded_as_a_miss_without_rebuilding(cfg, monkeypatch):
    cfg, calls = _setup(cfg, monkeypatch, {"a": _book("a", "111")}, {"111": None})
    extra = epub_catalog.fetch_missing(cfg, delay=0)
    assert calls == ["111"]                                       # nothing found → no build
    assert extra["111"]["description"] is None and extra["111"]["genre"] is None
    assert extra["111"]["fetched_at"]


def test_request_errors_are_not_recorded_and_stop_the_run(cfg, monkeypatch):
    isbns = [str(i) for i in range(5)]
    cfg, calls = _setup(cfg, monkeypatch, {i: _book(i, i) for i in isbns},
                        {i: RuntimeError("quota") for i in isbns})
    assert epub_catalog.fetch_missing(cfg, delay=0) == {}
    assert calls == isbns[:epub_catalog.GIVE_UP_AFTER]


def test_build_uses_the_looked_up_data_below_the_opf_and_the_audiobook(cfg):
    save_json(cfg.epub_catalog_extra, {
        "111": {"description": "from google", "genre": "SF・ファンタジー"},
        "222": {"description": "from google", "genre": "SF・ファンタジー"}})
    save_json(cfg.audiobook_data, {"A": {"title": "t", "genre": "ライトノベル", "description_full": "from audiobook"}})
    save_json(cfg.audiobook_links, {"linked": {"audible_isbn": "A"}})
    save_json(cfg.library_index, {
        "bare": _book("bare", "111"),
        "own": _book("own", "222", description="from opf"),
        "linked": _book("linked", "111"),
        "nothing": _book("nothing")})
    build.build(cfg)
    lib = load_json(cfg.library_index, {})
    assert (lib["bare"]["description"], lib["bare"]["description_source"], lib["bare"]["genre"]) == (
        "from google", "isbn", "SF・ファンタジー")
    assert (lib["own"]["description"], lib["own"]["description_source"]) == ("from opf", "epub")
    assert (lib["linked"]["description_source"], lib["linked"]["genre"]) == ("audiobook", "ライトノベル")
    assert (lib["nothing"]["description"], lib["nothing"]["genre"]) == (None, None)


def test_genre_list_includes_genres_found_by_isbn(server):
    save_json(server.cfg.epub_catalog_extra, {"1": {"genre": "ノンフィクション"}, "2": {"genre": "ノンフィクション"}})
    save_json(server.cfg.audiobook_data, {"a": {"genre": "ホラー"}})
    assert server.genre_list()["genres"] == ["ノンフィクション", "ホラー"]
