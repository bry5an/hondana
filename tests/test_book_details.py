"""Hand corrections to a book's details: author/language overrides in
build(), the one-request /details endpoint, and the series suggestion."""
import asyncio
import json
from collections import Counter

from epublib import analyze, build
from epublib.build import suggest_series
from epublib.index_store import load_json, save_json


def _seed(cfg, **extra):
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    entry = {"book_id": "b1", "title": "Bryson Bill", "raw_title": "Bryson Bill",
             "author": "Bryson Bill", "language": "en", "lang_source": "default",
             "path": "b1.epub", "filesize": 1, "mtime": 1.0}
    entry.update(extra)
    save_json(cfg.library_index, {"b1": entry})


def _book(cfg):
    return load_json(cfg.library_index, {})["b1"]


# ── build(): overrides apply from raw_*, and reset restores them ────────────

def test_author_and_language_overrides_apply_and_reset_to_the_files_values(cfg):
    _seed(cfg)
    build.build(cfg)
    assert _book(cfg)["author"] == "Bryson Bill"

    save_json(cfg.author_overrides, {"b1": "Bill Bryson"})
    save_json(cfg.language_overrides, {"b1": "ja"})
    build.build(cfg)
    b = _book(cfg)
    assert (b["author"], b["language"], b["lang_source"]) == ("Bill Bryson", "ja", "override")
    assert (b["raw_author"], b["raw_language"]) == ("Bryson Bill", "en")

    # build rewrites author/language every run, so reset must come from raw_*.
    save_json(cfg.author_overrides, {})
    save_json(cfg.language_overrides, {})
    build.build(cfg)
    b = _book(cfg)
    assert (b["author"], b["language"], b["lang_source"]) == ("Bryson Bill", "en", "default")


def test_pair_partner_language_does_not_depend_on_iteration_order(cfg):
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    mk = lambda bid, lang: {"book_id": bid, "title": bid, "raw_title": bid, "language": lang,  # noqa: E731
                            "path": f"{bid}.epub"}
    save_json(cfg.library_index, {"a": mk("a", "en"), "z": mk("z", "ja")})
    save_json(cfg.pairings_index, {"p": {"en": "a", "jp": "z"}})
    save_json(cfg.language_overrides, {"z": "other"})
    build.build(cfg)
    lib = load_json(cfg.library_index, {})
    assert lib["a"]["pair_partner"]["language"] == "other"


# ── analyze: a language change must not be skipped as "unchanged" ───────────

def test_analyze_skips_unchanged_but_reanalyzes_after_a_language_change(cfg, monkeypatch):
    from conftest import write_epub
    write_epub(cfg.epub_dir / "b1.epub", ["これはにほんごのぶんしょうです。" * 40])
    st = (cfg.epub_dir / "b1.epub").stat()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    save_json(cfg.library_index, {"b1": {"book_id": "b1", "path": "b1.epub", "title": "t",
                                         "language": "ja", "mtime": st.st_mtime,
                                         "filesize": st.st_size}})
    calls = []

    def fake(lang):
        def run(text):
            calls.append(lang)
            return ({"content_tokens": 1, "words_total": 1, "unique_words": 1, "difficulty_avg": 1},
                    Counter(), [])
        return run

    monkeypatch.setattr(analyze, "_analyze_text", fake("ja"))
    monkeypatch.setattr(analyze, "_analyze_text_en", fake("en"))
    monkeypatch.setattr(analyze, "save_sentences", lambda path, *a, **k: path.write_bytes(b""))
    monkeypatch.setattr(analyze, "MIN_WORD_CHARS", 1)

    analyze.analyze(cfg)
    analyze.analyze(cfg)
    assert calls == ["ja"]  # second run: unchanged → skipped

    lib = load_json(cfg.library_index, {})
    lib["b1"]["language"] = "en"
    save_json(cfg.library_index, lib)
    analyze.analyze(cfg)
    assert calls == ["ja", "en"]


# ── suggest_series ───────────────────────────────────────────────────────────

def test_suggest_series_picks_the_longest_stem_and_reads_the_volume():
    names = ["最弱", "最弱テイマー", "Other"]
    assert suggest_series("最弱テイマー 3 王都へ", names) == ("最弱テイマー", "03")
    assert suggest_series("最弱テイマー　外伝", names) == ("最弱テイマー", None)
    assert suggest_series("最弱テイマー", names) is None        # exactly the name: build handles it
    assert suggest_series("まったく別の本", names) is None


# ── the endpoint ─────────────────────────────────────────────────────────────

def _server(server, monkeypatch):
    cfg = server.cfg
    _seed(cfg)
    builds, queued = [], []
    real_build = server.build_mod.build
    monkeypatch.setattr(server.build_mod, "build", lambda c: (builds.append(1), real_build(c))[1])
    monkeypatch.setattr(server._book_jobs, "enqueue", lambda bid: queued.append(bid) or {"state": "queued"})
    monkeypatch.setattr(server.known_mod, "recompute_all", lambda c, languages=None: {})
    return cfg, builds, queued


def _post(server, body):
    from epublib import schemas
    return json.loads(asyncio.run(
        server.set_book_details("b1", schemas.BookDetailsRequest(**body))).body)


def test_details_save_is_one_build_for_any_number_of_fields(server, monkeypatch):
    cfg, builds, queued = _server(server, monkeypatch)
    r = _post(server, {"title": "Short History", "author": "Bill Bryson",
                       "genre": "ノンフィクション", "series": {"name": "Bryson", "order": "1"}})
    assert len(builds) == 1 and queued == []  # language untouched → no pipeline run
    v = r["values"]
    assert (v["title"], v["author"], v["genre"], v["series_name"], v["series_order"]) == \
        ("Short History", "Bill Bryson", "ノンフィクション", "Bryson", "1")
    assert sorted(r["overridden"]) == ["author", "genre", "series", "title"]
    assert r["auto"]["author"] == "Bryson Bill" and r["auto"]["title"] == "Bryson Bill"


def test_details_reset_to_automatic_and_standalone_pin(server, monkeypatch):
    cfg, builds, queued = _server(server, monkeypatch)
    _post(server, {"title": "X", "author": "Y", "series": {"name": "S", "order": "2"}})
    r = _post(server, {"title": None, "author": "", "series": None})
    assert r["overridden"] == []
    assert (r["values"]["title"], r["values"]["author"], r["values"]["series_name"]) == \
        ("Bryson Bill", "Bryson Bill", None)

    r = _post(server, {"series": {"name": ""}})  # pinned standalone, not a reset
    assert r["overridden"] == ["series"]
    assert load_json(cfg.series_overrides, {})["b1"] == {"series_name": None, "series_order": None}


def test_details_unchanged_save_does_not_rebuild_and_omitted_fields_are_kept(server, monkeypatch):
    cfg, builds, queued = _server(server, monkeypatch)
    _post(server, {"author": "A"})
    n = len(builds)
    _post(server, {"author": "A"})            # same value
    _post(server, {"title": "T"})             # leaves the author alone
    assert len(builds) == n + 1
    assert load_json(cfg.author_overrides, {}) == {"b1": "A"}


def test_details_language_change_drops_derived_data_and_queues_the_pipeline(server, monkeypatch):
    cfg, builds, queued = _server(server, monkeypatch)
    cfg.wordlists_dir.mkdir(parents=True, exist_ok=True)
    (cfg.wordlists_dir / "b1.json").write_text("{}")
    save_json(cfg.vocab_index, {"b1": {"status": "ok"}})
    save_json(cfg.read_books, {"books": {"b1": {}}})
    save_json(cfg.title_overrides, {"b1": "Keep me"})

    r = _post(server, {"language": "ja"})
    assert r["values"]["language"] == "ja" and sorted(r["overridden"]) == ["language", "title"]
    assert queued == ["b1"] and r["job"] == {"state": "queued"}
    assert not (cfg.wordlists_dir / "b1.json").exists()
    assert "b1" not in load_json(cfg.vocab_index, {})
    # curated state survives (this is not a delete)
    assert load_json(cfg.title_overrides, {}) == {"b1": "Keep me"}
    assert "b1" in load_json(cfg.read_books, {})["books"]


def test_details_validation(server, monkeypatch):
    import pytest
    from fastapi import HTTPException
    from epublib import schemas
    _server(server, monkeypatch)
    for body in ({"genre": "nope"}, {"language": "fr"}):
        with pytest.raises(HTTPException) as e:
            asyncio.run(server.set_book_details("b1", schemas.BookDetailsRequest(**body)))
        assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        asyncio.run(server.set_book_details("nope", schemas.BookDetailsRequest(title="x")))
    assert e.value.status_code == 404


def test_details_get_suggests_an_existing_series_for_a_seriesless_book(server, monkeypatch):
    cfg, _, _ = _server(server, monkeypatch)
    lib = load_json(cfg.library_index, {})
    lib["s1"] = {"book_id": "s1", "title": "Bryson Bill 2 more", "raw_title": "x", "series_name": "Bryson Bill",
                 "series_order": "01", "language": "en", "path": "s1.epub"}
    save_json(cfg.library_index, lib)
    st = server.get_book_details("b1")
    assert st["series_names"] == ["Bryson Bill"]
    # b1's own title is the series name exactly → build derives it; no suggestion
    assert st["suggestion"] is None


# ── GET /api/books projection ─────────────────────────────────────────

def test_book_list_drops_fields_no_page_reads_but_keeps_the_single_book_whole(server):
    cfg = server.cfg
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    row = {"book_id": "b1", "title": "T", "author": "A", "language": "ja", "series_name": "S",
           "cover": "covers/b1.jpg", "natively": {"natively_level": 20}, "vocab": {"status": "ok"},
           "audiobook": None, "pair_id": None,
           "description": "d", "raw_description": "d", "identifiers": {"isbn": "1"},
           "opf_identifier": "1", "raw_title": "T", "scanned_at": "x", "schema": 2,
           "lang_source": "default", "mtime": 1.0, "filesize": 1, "path": "b1.epub"}
    save_json(cfg.library_index, {"b1": row})

    [listed] = json.loads(server.api_books(req_cfg=cfg).body)
    assert set(listed) == {"book_id", "title", "author", "language", "series_name", "cover",
                           "natively", "vocab", "audiobook", "pair_id", "stable_key", "known"}
    assert json.loads(server.api_books(series="S", req_cfg=cfg).body) == [listed]
    assert set(server.api_book("b1", req_cfg=cfg)) >= set(row)   # the book page still gets it all
