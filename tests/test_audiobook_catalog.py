"""The audiobook catalog: data.json + ASIN-pulled extras merged for every
reader, Audible products turned into catalog rows, only unknown ASINs fetched."""
import pytest

from epublib import audiobook_catalog as catalog
from epublib import audiobook_scanner
from epublib.index_store import load_json, save_json

BASE = {"B000000001": {"title": "Old", "series": "Old", "order": "01", "genre": "ライトノベル",
                       "release_date": "2016-05-23", "cover": "old.jpg"}}


def product(**kw):
    return {
        "asin": "B000000101",
        "title": "[3巻] 新刊シリーズ 第3巻",
        "authors": [{"name": "香月 美夜"}, {"name": "someone"}],
        "narrators": [{"name": "井口 裕香"}],
        "series": [{"title": "新刊シリーズ", "sequence": "第3巻"}],
        "product_images": {"500": "https://img/500.jpg"},
        "rating": {"num_reviews": 4, "overall_distribution": {"average_rating": 4.375, "num_ratings": 8}},
        "release_date": "2026-09-01",
        "runtime_length_min": 590,
        "merchandising_summary": "短い紹介",
        "publisher_summary": "<p>長い&amp;紹介</p>\n<p>二段落</p>",
        "category_ladders": [{"ladder": [{"name": "ティーン"}, {"name": "文学・フィクション・ライトノベル"}]}],
        **kw,
    }


def test_entry_from_product_follows_the_data_json_shape():
    e = catalog.entry_from_product(product())
    assert e == {
        "title": "[3巻] 新刊シリーズ 第3巻", "author": "香月 美夜, someone", "series": "新刊シリーズ",
        "order": "03", "cover": "https://img/500.jpg", "audible_rating": "4.38",
        "audible_rating_count": "8", "audible_reviews": "4", "release_date": "2026-09-01",
        "genre": "ライトノベル", "runtime_length_min": "590", "description_short": "短い紹介",
        "description_full": "長い&紹介 二段落", "narrators": "井口 裕香",
    }


def test_a_standalone_repeats_its_title_as_series_with_no_order():
    e = catalog.entry_from_product(product(series=[], title="一冊"))
    assert (e["series"], e["order"]) == ("一冊", None)


@pytest.mark.parametrize("sequence, order", [
    ("第3巻", "03"), ("16巻・下・前編", "16"), ("４ 忙しいですか？ 第4話", "04"), ("下", None), (None, None),
])
def test_order_is_the_first_number_zero_padded(sequence, order):
    assert catalog._order(sequence) == order


@pytest.mark.parametrize("ladder, genre", [
    (["SF・ファンタジー", "ファンタジー"], "SF・ファンタジー"),
    (["ミステリー・スリラー・サスペンス", "ミステリー"], "ミステリー・サスペンス"),
    (["文学・フィクション", "ホラー"], "ホラー"),              # the more specific word wins
    (["文学・フィクション", "大衆小説", "現代文学"], "文芸作品"),
    (["絵本・児童書", "童話・民話・神話"], "児童書"),
    (["歴史", "世界"], "歴史"),                              # no existing word: Audible's own
])
def test_genre_maps_onto_the_existing_vocabulary_or_keeps_audibles(ladder, genre):
    p = product(category_ladders=[{"ladder": [{"name": n} for n in ladder]}])
    assert catalog.entry_from_product(p)["genre"] == genre


def test_load_overlays_extras_per_field_and_ignores_recorded_misses(cfg):
    save_json(cfg.audiobook_data, BASE)
    save_json(cfg.audiobook_catalog_extra, {
        "B000000001": {"title": "Old", "cover": "new.jpg"},          # overlay: only what it names
        "B000000101": {"title": "New"},
        "B000000102": {"title": None, "unavailable_at": "2026-10-01T00:00:00+00:00"},
    })
    merged = catalog.load(cfg)
    assert merged["B000000001"] == {**BASE["B000000001"], "cover": "new.jpg"}
    assert set(merged) == {"B000000001", "B000000101"}
    assert catalog.load(cfg) is not merged          # a fresh dict each time
    assert catalog.cached(cfg) is catalog.cached(cfg)
    save_json(cfg.audiobook_catalog_extra, {})       # a write invalidates the cache
    assert set(catalog.cached(cfg)) == {"B000000001"}


def _files(cfg, *keys):
    save_json(cfg.audiobook_files_index,
              {f"f{i}": {"catalog_key": k, "filename": f"{k}.m4b"} for i, k in enumerate(keys)})


def _fake_api(monkeypatch, products):
    calls = []

    def fake(asin):
        calls.append(asin)
        out = products[asin]
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(catalog, "fetch_product", fake)
    monkeypatch.setattr(audiobook_scanner, "build_index", lambda cfg: calls.append("index"))
    return calls


def test_fetch_missing_pulls_only_asins_on_disk_that_no_catalog_knows(cfg, monkeypatch):
    save_json(cfg.audiobook_data, BASE)
    _files(cfg, "B000000001", "B000000101", "B000000102", "audiobook.jp 123", None)
    calls = _fake_api(monkeypatch, {"B000000101": product(), "B000000102": product(runtime_length_min=None)})
    extra = catalog.fetch_missing(cfg, delay=0)
    assert calls == ["B000000101", "B000000102", "index"]          # base ASIN and non-ASIN keys skipped
    assert extra["B000000101"]["title"] == "[3巻] 新刊シリーズ 第3巻"
    # 102 came back as a runtime-less container (a series page), so it is recorded as not a book
    assert extra["B000000102"]["title"] is None and "unavailable_at" in extra["B000000102"]
    assert load_json(cfg.audiobook_catalog_extra, {}) == extra
    calls.clear()
    catalog.fetch_missing(cfg, delay=0)                              # nothing new, an unavailable one is recent
    assert calls == []


def test_an_unavailable_asin_is_asked_about_again_after_two_weeks(cfg, monkeypatch):
    save_json(cfg.audiobook_data, {})
    save_json(cfg.audiobook_catalog_extra, {"B000000102": {"title": None, "unavailable_at": "2020-01-01T00:00:00+00:00"}})
    _files(cfg, "B000000102")
    calls = _fake_api(monkeypatch, {"B000000102": product(asin="B000000102")})
    catalog.fetch_missing(cfg, delay=0)
    assert calls == ["B000000102", "index"]


def test_request_errors_are_not_recorded_as_misses_and_stop_the_run(cfg, monkeypatch):
    save_json(cfg.audiobook_data, {})
    keys = [f"B00000010{i}" for i in range(1, 6)]
    _files(cfg, *keys)
    calls = _fake_api(monkeypatch, {k: ConnectionError("offline") for k in keys})
    extra = catalog.fetch_missing(cfg, delay=0)
    assert calls == keys[:catalog.GIVE_UP_AFTER]                     # gave up, no index rebuild
    assert extra == {}


def test_the_audiobook_index_gets_a_pulled_book_with_its_metadata(cfg):
    save_json(cfg.audiobook_data, BASE)
    save_json(cfg.audiobook_catalog_extra, {"B000000101": catalog.entry_from_product(product())})
    save_json(cfg.audiobook_files_index, {"B000000101": {
        "ab_id": "B000000101", "catalog_key": "B000000101", "filename": "x [B000000101].m4b",
        "path": "x.m4b", "root": "/r", "filesize": 1, "format": "m4b", "files": None}})
    row = audiobook_scanner.build_index(cfg)["B000000101"]
    assert (row["title"], row["series"], row["order"], row["genre"], row["runtime_min"]) == (
        "[3巻] 新刊シリーズ 第3巻", "新刊シリーズ", "03", "ライトノベル", 590)
    assert row["local"] and row["cover_url"] == "https://img/500.jpg"
    # the author sorts by reading like the series does (/admin's prune list)
    assert row["author_reading"] == audiobook_scanner.romaji.sort_reading(row["author"])


def test_the_genre_list_is_derived_from_the_catalog_most_common_first_without_ero(server):
    cfg = server.cfg
    save_json(cfg.audiobook_data, {
        "a": {"genre": "ホラー"}, "b": {"genre": "ライトノベル"}, "c": {"genre": "ライトノベル"},
        "d": {"genre": "エロ"}, "e": {"genre": None}})
    save_json(cfg.audiobook_catalog_extra, {"f": {"title": "新", "genre": "歴史"}, "g": {"title": "新2", "genre": "歴史"},
                                            "h": {"title": "新3", "genre": "歴史"}})
    assert server.genre_list() == {"genres": ["歴史", "ライトノベル", "ホラー"]}


def test_a_missing_base_file_is_an_empty_start_not_an_error(cfg):
    """the base catalog is per-machine and absent on a fresh clone."""
    assert not cfg.audiobook_data.exists()
    assert catalog.load(cfg) == {}
    save_json(cfg.audiobook_catalog_extra, {"B000000101": catalog.entry_from_product(product())})
    assert list(catalog.load(cfg)) == ["B000000101"]
    assert list(catalog.cached(cfg)) == ["B000000101"]
