"""Regression tests for build.py's title volume-marker regexes and the
series-derivation precedence chain (override > calibre > audiobook > title >
pair partner).
"""
from __future__ import annotations

import pytest

from epublib.build import _derive_series, _title_series


# ── _title_series(): trailing volume markers ────────────────────────────────

@pytest.mark.parametrize(
    "title, expected",
    [
        # "合格2400" is part of the title (a 4-digit run with no non-digit
        # break in its last 3 chars) -- not a volume marker.
        ("合格2400", None),
        # "話。2" -- the digit is a real trailing volume marker.
        ("話。2", ("話。", "02")),
        ("合格2400 話。2", ("合格2400 話。", "02")),
        ("なろう(1)", ("なろう", "01")),
        ("なろう１２", ("なろう", "12")),  # fullwidth digits
        ("なろう第3巻", ("なろう", "03")),
        ("シリーズその2", ("シリーズその", "02")),
        ("シリーズ 上", ("シリーズ", "01")),
        ("シリーズ 中", ("シリーズ", "02")),
        ("シリーズ 下", ("シリーズ", "03")),
        # Trailing bracket is a publisher/edition note, not a volume marker,
        # and stripping it leaves nothing numeric behind.
        ("シリーズ(ガガガ文庫)", None),
        ("シリーズ2(ガガガ文庫)", ("シリーズ", "02")),
    ],
)
def test_title_series(title, expected):
    assert _title_series(title) == expected


# ── _derive_series(): the precedence chain ──────────────────────────────────

def test_derive_series_precedence_chain():
    library = {
        "b1": {"title": "whatever", "book_id": "b1"},
        "b2": {"title": "CalibreSeries 3", "series": "CalibreSeries", "book_id": "b2"},
        "b3": {"title": "シリーズ2", "book_id": "b3"},
        "b4": {"title": "CatSeries", "book_id": "b4"},
        "b5": {"title": "MySeries3", "book_id": "b5"},
        "b6": {"title": "MySeries", "book_id": "b6"},
        "b7": {"title": "English Edition", "language": "en", "book_id": "b7"},
    }
    links = {
        "b2": {"audible_isbn": "X"},
        "b3": {"audible_isbn": "Y"},
        "b4": {"audible_isbn": "Z"},
    }
    catalog = {
        "X": {"series": "IgnoredCatSeries", "order": "07"},
        "Y": {"series": "CatSeries", "order": "09"},
        "Z": {"series": "CatSeries", "order": "02"},
    }
    pair_of = {"b7": ("pair_0000", "b5")}
    overrides = {"b1": {"series_name": "OverrideSeries", "series_order": "05"}}

    derived = _derive_series(library, links, catalog, pair_of, overrides)

    # override wins outright, ignoring title/calibre entirely.
    assert derived["b1"] == ["OverrideSeries", "05", "override"]
    # calibre wins over the audiobook catalog match -- but never sets an
    # order (only the title/audiobook paths do).
    assert derived["b2"] == ["CalibreSeries", None, "calibre"]
    # audiobook-derived series, but the EPUB's own title volume marker
    # outranks the (possibly different-volume) matched catalog entry's order.
    assert derived["b3"] == ["CatSeries", "02", "audiobook"]
    # titled exactly like the matched series name -> volume 1, not the
    # catalog's order.
    assert derived["b4"] == ["CatSeries", "01", "audiobook"]
    # no calibre/audiobook match; falls back to the title's own volume marker.
    assert derived["b5"] == ["MySeries", "03", "title"]
    # titled exactly like a series name derived from another book -> vol 1.
    assert derived["b6"] == ["MySeries", "01", "title"]
    # last resort: inherits the pair partner's derived series.
    assert derived["b7"] == ["MySeries", "03", "pair"]


def test_derive_series_solo_book_has_no_series():
    library = {"b1": {"title": "A Standalone Book", "book_id": "b1"}}
    derived = _derive_series(library, {}, {}, {}, {})
    assert derived["b1"] == [None, None, None]


def test_derive_series_colliding_catalog_orders_are_dropped():
    # Three EPUBs with no recognisable volume marker all xref to the same
    # catalog entry: its order can't be right for all of them.
    library = {f"b{i}": {"title": f"第一部「兵士の娘{r}」", "book_id": f"b{i}"}
               for i, r in enumerate(["I", "II", "III"], 1)}
    library["b4"] = {"title": "Series 4", "book_id": "b4"}  # title-derived, trusted
    links = {bid: {"audible_isbn": "X"} for bid in library}
    catalog = {"X": {"series": "Series", "order": "02"}}
    derived = _derive_series(library, links, catalog, {}, {})
    for bid in ("b1", "b2", "b3"):
        assert derived[bid] == ["Series", None, "audiobook"]
    assert derived["b4"] == ["Series", "04", "audiobook"]


def test_derive_series_catalog_order_kept_when_unique():
    library = {"b1": {"title": "第一部「兵士の娘I」", "book_id": "b1"}}
    derived = _derive_series(library, {"b1": {"audible_isbn": "X"}},
                             {"X": {"series": "Series", "order": "02"}}, {}, {})
    assert derived["b1"] == ["Series", "02", "audiobook"]


# ── description fallback ──────────────────────────────────────────────

def _built(cfg, library, links=None, catalog=None):
    from epublib import build
    from epublib.index_store import load_json, save_json
    save_json(cfg.library_index, library)
    save_json(cfg.audiobook_links, links or {})
    save_json(cfg.audiobook_data, catalog or {})
    build.build(cfg)
    return load_json(cfg.library_index, {})


def test_description_prefers_the_epub_then_the_linked_audiobook(cfg):
    cat = {"X": {"title": "t", "description_full": "audiobook blurb"}}
    lib = _built(cfg, {
        "own": {"book_id": "own", "title": "a", "description": "opf blurb"},
        "linked": {"book_id": "linked", "title": "b", "description": None},
        "none": {"book_id": "none", "title": "c"},
    }, links={"own": {"audible_isbn": "X"}, "linked": {"audible_isbn": "X"}}, catalog=cat)
    assert (lib["own"]["description"], lib["own"]["description_source"]) == ("opf blurb", "epub")
    assert (lib["linked"]["description"], lib["linked"]["description_source"]) == ("audiobook blurb", "audiobook")
    assert (lib["none"]["description"], lib["none"]["description_source"]) == (None, None)


def test_the_audiobook_listing_is_shown_as_plain_text(cfg):
    # The catalog breaks lines with <br />; the book page escapes HTML, so a tag
    # left in would print literally.
    cat = {"X": {"title": "t", "description_full": "一行目。<br />二行目<br>&amp;三行目<br/>"}}
    lib = _built(cfg, {"b": {"book_id": "b", "title": "b", "description": None}},
                 links={"b": {"audible_isbn": "X"}}, catalog=cat)
    assert lib["b"]["description"] == "一行目。\n二行目\n&三行目"


def test_the_fallback_is_rederived_so_unlinking_does_not_keep_it(cfg):
    from epublib import build
    from epublib.index_store import load_json, save_json
    cat = {"X": {"title": "t", "description_full": "audiobook blurb"}}
    lib = _built(cfg, {"b": {"book_id": "b", "title": "b", "description": None}},
                 links={"b": {"audible_isbn": "X"}}, catalog=cat)
    assert lib["b"]["description"] == "audiobook blurb" and lib["b"]["raw_description"] is None
    save_json(cfg.audiobook_links, {})                  # the link goes away…
    build.build(cfg)
    b = load_json(cfg.library_index, {})["b"]
    assert (b["description"], b["description_source"]) == (None, None)   # …and so does the borrowed text


# ── gojūon sort keys ─────────────────────────────────────────────────

def test_sort_reading_orders_kanji_by_reading_not_code_point():
    from epublib.romaji import sort_reading
    # 夜 (よる) < 風 (かぜ) by code point; by reading か < よ.
    keys = {t: sort_reading(t) for t in ["夜のピクニック", "風が強く吹いている", "あした"]}
    assert sorted(keys, key=keys.get) == ["あした", "風が強く吹いている", "夜のピクニック"]
    assert keys["夜のピクニック"] == "よるのぴくにっく"      # katakana folded to hiragana


def test_sort_reading_prefers_the_files_own_reading_and_keeps_latin():
    from epublib.romaji import sort_reading
    assert sort_reading("異世界", "いせかい") == "いせかい"
    assert sort_reading("ｲｾｶｲ", "イセカイ") == "いせかい"       # half-width / katakana readings fold
    assert sort_reading("The Hobbit") == "The Hobbit"            # no Japanese: its own key
    assert sort_reading("") is None and sort_reading(None) is None
    assert sort_reading("「頭のゴミ」を捨てれば") == "あたまのごみ」をすてれば"   # leading brackets don't lead the shelf


def test_sort_readings_series_borrows_volume_one_reading():
    from epublib.build import _sort_readings
    library = {
        "v2": {"title": "風の谷 2", "series_name": "風の谷", "series_order": "02",
               "title_reading": "かぜのたに2"},
        "v1": {"title": "風の谷 1", "series_name": "風の谷", "series_order": "01",
               "title_reading": "ふうのたに1"},       # the OPF's reading wins over pykakasi's
        "solo": {"title": "夜のピクニック", "series_name": None, "series_order": None},
        "odd": {"title": "Unrelated subtitle", "series_name": "海の本", "series_order": "01",
                "title_reading": "ignored"},          # title doesn't start with the series name
    }
    _sort_readings(library)
    assert library["v1"]["series_sort"] == library["v2"]["series_sort"] == "ふうのたに1"
    assert library["v1"]["title_sort"] == "ふうのたに1"
    assert library["solo"]["series_sort"] is None
    assert library["solo"]["title_sort"] == "よるのぴくにっく"
    assert library["odd"]["series_sort"] == "うみのほん"
