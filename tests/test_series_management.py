"""Series-level edits: bulk genre, bulk series assignment/reset, rename/merge."""
import asyncio
import json

import pytest
from fastapi import HTTPException

from epublib import schemas
from epublib.index_store import load_json, save_json


def _book(bid, title, **extra):
    return {"book_id": bid, "title": title, "raw_title": title, "author": "A", "language": "ja",
            "lang_source": "default", "path": f"{bid}.epub", "filesize": 1, "mtime": 1.0, **extra}


def _seed(cfg, *books):
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    save_json(cfg.library_index, {b["book_id"]: b for b in books})


def _call(coro):
    return json.loads(asyncio.run(coro).body)


def _error(coro) -> HTTPException:
    with pytest.raises(HTTPException) as e:
        asyncio.run(coro)
    return e.value


def _count_builds(server, monkeypatch, real=False):
    builds = []
    real_build = server.build_mod.build
    monkeypatch.setattr(server.build_mod, "build",
                        lambda c: (builds.append(1), real_build(c) if real else None)[1])
    return builds


def _lib(cfg):
    return load_json(cfg.library_index, {})


# ── bulk genre ───────────────────────────────────────────────────────────────

def test_bulk_genre_writes_once_builds_once_and_skips_noops(server, monkeypatch):
    cfg = server.cfg
    _seed(cfg, _book("a", "A"), _book("b", "B"), _book("c", "C"))
    builds = _count_builds(server, monkeypatch)

    r = _call(server.set_books_genre(schemas.BulkGenreRequest(book_ids=["a", "b", "a"], genre=" ホラー ")))
    assert (r["genre"], r["changed"]) == ("ホラー", 2) and len(builds) == 1
    assert load_json(cfg.genre_overrides, {}) == {"a": "ホラー", "b": "ホラー"}

    r = _call(server.set_books_genre(schemas.BulkGenreRequest(book_ids=["a", "b"], genre="ホラー")))
    assert r["changed"] == 0 and len(builds) == 1            # nothing changed → no rebuild

    r = _call(server.set_books_genre(schemas.BulkGenreRequest(book_ids=["a", "c"], genre="")))
    assert (r["genre"], r["changed"]) == (None, 1)           # only "a" had one to clear
    assert load_json(cfg.genre_overrides, {}) == {"b": "ホラー"}


def test_bulk_genre_validates_before_writing_anything(server, monkeypatch):
    cfg = server.cfg
    _seed(cfg, _book("a", "A"))
    builds = _count_builds(server, monkeypatch)
    assert _error(server.set_books_genre(schemas.BulkGenreRequest(book_ids=["a"], genre="nope"))).status_code == 400
    e = _error(server.set_books_genre(schemas.BulkGenreRequest(book_ids=["a", "zz"], genre="ホラー")))
    assert e.status_code == 404 and "zz" in e.detail
    assert load_json(cfg.genre_overrides, {}) == {} and builds == []


# ── bulk series assignment ───────────────────────────────────────────────────

def _assign(server, *items):
    return _call(server.set_books_series(schemas.BulkSeriesRequest(
        assignments=[schemas.SeriesAssignment(**i) for i in items])))


def test_bulk_series_assigns_renumbers_and_resets_in_one_build(server, monkeypatch):
    cfg = server.cfg
    _seed(cfg, _book("a", "One"), _book("b", "Two"), _book("c", "Three"))
    builds = _count_builds(server, monkeypatch, real=True)

    r = _assign(server,
                {"book_id": "a", "series_name": "S", "series_order": "02"},
                {"book_id": "b", "series_name": "S", "series_order": "01"},
                {"book_id": "c", "series_name": "", "series_order": "5"})
    assert r["changed"] == 3 and len(builds) == 1
    ov = load_json(cfg.series_overrides, {})
    assert ov["a"] == {"series_name": "S", "series_order": "02"}
    assert ov["c"] == {"series_name": None, "series_order": None}   # pinned standalone
    lib = _lib(cfg)
    assert [(lib[b]["series_name"], lib[b]["series_order"]) for b in "abc"] == [("S", "02"), ("S", "01"), (None, None)]

    r = _assign(server, {"book_id": "a", "reset": True}, {"book_id": "b", "series_name": "S", "series_order": "01"})
    assert r["changed"] == 1 and len(builds) == 2                    # b unchanged; a dropped
    assert "a" not in load_json(cfg.series_overrides, {})
    assert _lib(cfg)["a"]["series_name"] is None                     # nothing derives one for it

    assert _assign(server, {"book_id": "a", "reset": True})["changed"] == 0 and len(builds) == 2


def test_bulk_series_rejects_unknown_books_without_writing(server, monkeypatch):
    cfg = server.cfg
    _seed(cfg, _book("a", "A"))
    builds = _count_builds(server, monkeypatch)
    e = _error(server.set_books_series(schemas.BulkSeriesRequest(assignments=[
        schemas.SeriesAssignment(book_id="a", series_name="S"),
        schemas.SeriesAssignment(book_id="zz", series_name="S")])))
    assert e.status_code == 404
    assert load_json(cfg.series_overrides, {}) == {} and builds == []


# ── rename / merge ───────────────────────────────────────────────────────────

def _rename(server, old, new):
    return _call(server.rename_series(schemas.SeriesRenameRequest(from_name=old, to_name=new)))


def test_rename_moves_every_volume_keeping_order_whatever_put_it_there(server, monkeypatch):
    cfg = server.cfg
    # a: derived from the OPF (no override yet), b: already hand-set, c: another series
    _seed(cfg, _book("a", "Vol one", series="ABC"), _book("b", "Vol two"), _book("c", "Other", series="XYZ"))
    save_json(cfg.series_overrides, {"b": {"series_name": "ABC", "series_order": "02"}})
    server.build_mod.build(cfg)       # the served index always carries series_name/source
    builds = _count_builds(server, monkeypatch, real=True)

    r = _rename(server, "ABC", "DEF")
    assert (r["series_name"], r["renamed"]) == ("DEF", 2) and len(builds) == 1
    lib = _lib(cfg)
    assert [(lib[b]["series_name"], lib[b]["series_order"]) for b in "abc"] == \
        [("DEF", None), ("DEF", "02"), ("XYZ", None)]
    assert load_json(cfg.series_overrides, {})["a"] == {"series_name": "DEF", "series_order": None}

    # merging into an existing series is the same operation
    assert _rename(server, "DEF", "XYZ")["renamed"] == 2
    assert {lib["series_name"] for lib in _lib(cfg).values()} == {"XYZ"}


def test_rename_skips_pair_inherited_volumes_and_validates(server, monkeypatch):
    cfg = server.cfg
    _seed(cfg,
          _book("jp", "JP", series_name="S", series_order="01", series_source="calibre"),
          _book("en", "EN", language="en", series_name="S", series_order="01", series_source="pair"))
    builds = _count_builds(server, monkeypatch)       # build stubbed: the library stays as seeded
    assert _rename(server, "S", "T")["renamed"] == 1
    assert list(load_json(cfg.series_overrides, {})) == ["jp"] and len(builds) == 1

    assert _rename(server, "S", "S")["renamed"] == 1            # same name: counted, but no write or build
    assert len(builds) == 1
    assert _error(server.rename_series(schemas.SeriesRenameRequest(from_name="Nope", to_name="T"))).status_code == 404
    assert _error(server.rename_series(schemas.SeriesRenameRequest(from_name="S", to_name="  "))).status_code == 400
