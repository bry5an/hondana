"""EPUB ↔ audiobook xref: volume-aware matching, author gate, overrides."""
from epublib import audiobook_xref as xref
from epublib.index_store import load_json, save_json

CATALOG = {
    "B000000001": {"title": "[1巻] 銭天堂", "author": "廣嶋 玲子", "series": "銭天堂", "order": "01"},
    "B000000013": {"title": "銭天堂13", "author": "廣嶋 玲子", "series": "銭天堂", "order": "13"},
    "B000000020": {"title": "図解 心理学の話", "author": "色々", "series": "", "order": ""},
    "B000000030": {"title": "最後の晩ごはん お兄さんとホットケーキ", "author": "椹野 道流",
                   "series": "最後の晩ごはん", "order": "03"},
    "B000000040": {"title": "嫌われる勇気", "author": "岸見 一郎", "series": "", "order": ""},
}


def _book(bid, title, author="", language="ja", **kw):
    return {"book_id": bid, "title": title, "author": author, "language": language, **kw}


def _setup(cfg, books):
    save_json(cfg.audiobook_data, CATALOG)
    save_json(cfg.library_index, {b["book_id"]: b for b in books})


def test_unnumbered_epub_is_volume_one_not_volume_thirteen(cfg):
    _setup(cfg, [_book("a", "銭天堂", "廣嶋玲子")])
    assert xref.xref(cfg)["a"]["audible_isbn"] == "B000000001"


def test_no_link_when_only_a_different_volume_exists(cfg):
    save_json(cfg.audiobook_data, {k: v for k, v in CATALOG.items() if k != "B000000001"})
    save_json(cfg.library_index, {"a": _book("a", "銭天堂", "廣嶋玲子")})
    assert xref.xref(cfg) == {}


def test_numbered_epub_meets_its_own_volume(cfg):
    _setup(cfg, [_book("a", "銭天堂 13", "廣嶋玲子")])
    assert xref.xref(cfg)["a"]["audible_isbn"] == "B000000013"


def test_subtitle_volume_matches_despite_catalog_order(cfg):
    _setup(cfg, [_book("a", "最後の晩ごはん お兄さんとホットケーキ", "椹野道流")])
    assert xref.xref(cfg)["a"]["audible_isbn"] == "B000000030"


def test_author_gate_rejects_a_near_title_by_someone_else(cfg):
    _setup(cfg, [_book("a", "図解 宇宙の話", "渡部潤一")])
    assert xref.xref(cfg) == {}


def test_author_waived_for_an_identical_title(cfg):
    _setup(cfg, [_book("a", "嫌われる勇気", "古賀史健")])  # co-author
    assert xref.xref(cfg)["a"]["audible_isbn"] == "B000000040"


def test_english_books_are_not_linked(cfg):
    _setup(cfg, [_book("a", "銭天堂", "廣嶋玲子", language="en")])
    assert xref.xref(cfg) == {}


def test_override_pins_unlinks_and_resets(cfg):
    _setup(cfg, [_book("a", "銭天堂", "廣嶋玲子")])
    save_json(cfg.audiobook_link_overrides, {"a": "B000000013"})
    link = xref.xref(cfg)["a"]
    assert link["audible_isbn"] == "B000000013" and link["override"] is True

    save_json(cfg.audiobook_link_overrides, {"a": None})
    assert "a" not in xref.xref(cfg)

    save_json(cfg.audiobook_link_overrides, {"a": "NOT-IN-CATALOG"})
    assert xref.xref(cfg)["a"]["audible_isbn"] == "B000000001"  # stale override ignored


def test_override_applies_to_a_non_japanese_book(cfg):
    _setup(cfg, [_book("a", "Some English Edition", language="en")])
    save_json(cfg.audiobook_link_overrides, {"a": "B000000001"})
    assert xref.xref(cfg)["a"]["audible_isbn"] == "B000000001"


def test_xref_one_touches_only_that_book(cfg):
    _setup(cfg, [_book("a", "銭天堂", "廣嶋玲子"), _book("b", "嫌われる勇気", "古賀史健")])
    save_json(cfg.audiobook_links, {"b": {"audible_isbn": "sentinel"}})
    link = xref.xref_one(cfg, "a")
    assert link["audible_isbn"] == "B000000001"
    links = load_json(cfg.audiobook_links, {})
    assert links["b"] == {"audible_isbn": "sentinel"}  # untouched
    assert links["a"] == link


# ── 第N部「…」 part/volume titles ─────────────────────────────────────

_HON = "本好きの下剋上～司書になるためには手段を選んでいられません～"
PARTS = {
    "B100000001": {"title": f"[1巻] {_HON}第一部「兵士の娘1」", "author": "香月 美夜",
                   "series": "本好きの下剋上", "order": "01"},
    "B100000002": {"title": f"[2巻] {_HON}第一部「兵士の娘2」", "author": "香月 美夜",
                   "series": "本好きの下剋上", "order": "02"},
    "B100000003": {"title": f"[3巻] {_HON}第一部「兵士の娘3」", "author": "香月 美夜",
                   "series": "本好きの下剋上", "order": "03"},
    "B100000004": {"title": f"[4巻] {_HON}第二部「神殿の巫女見習い1」", "author": "香月 美夜",
                   "series": "本好きの下剋上", "order": "04"},
}


def test_part_volume_parses_arabic_roman_and_kanji_parts():
    assert xref._part_volume(f"{_HON}第一部「兵士の娘III」") == (1, 3)
    assert xref._part_volume(f"{_HON} 第一部「兵士の娘II」") == (1, 2)
    assert xref._part_volume(f"[4巻] {_HON}第二部「神殿の巫女見習い1」") == (2, 1)
    assert xref._part_volume("第十二部「x4」") == (12, 4)
    assert xref._part_volume("第五部「女神の化身4」. TO Books.") == (5, 4)
    assert xref._part_volume("銭天堂13") is None
    assert xref._part_volume("第一部「兵士の娘」") is None  # no volume in the part


def test_roman_numeral_part_volumes_each_link_to_their_own_audiobook(cfg):
    save_json(cfg.audiobook_data, PARTS)
    save_json(cfg.library_index, {b["book_id"]: b for b in [
        _book("a", f"{_HON}第一部「兵士の娘I」", "香月美夜"),
        _book("b", f"{_HON}第一部「兵士の娘II」", "香月美夜"),
        _book("c", f"{_HON} 第一部「兵士の娘III」", "香月美夜"),
    ]})
    links = xref.xref(cfg)
    assert {k: v["audible_isbn"] for k, v in links.items()} == {
        "a": "B100000001", "b": "B100000002", "c": "B100000003"}


# ── the book page's Audiobook card: /api/books/{id}/audiobook-link ───────────

def _link_client(server):
    from fastapi.testclient import TestClient

    cfg = server.cfg
    _setup(cfg, [{**_book("a", "銭天堂", "廣嶋玲子"), "raw_title": "銭天堂", "path": "a.epub",
                  "filesize": 1, "mtime": 1.0, "lang_source": "default"}])
    xref.xref(cfg)
    return TestClient(server.app)


def test_link_endpoints_pin_unlink_and_reset(server):
    c = _link_client(server)
    url = "/api/books/a/audiobook-link"
    state = c.get(url).json()
    assert state["mode"] == "auto" and state["catalog"]["isbn"] == "B000000001"

    state = c.post(url, json={"isbn": "B000000013"}).json()
    assert state["mode"] == "manual" and state["catalog"]["title"] == "銭天堂13"
    assert load_json(server.cfg.audiobook_link_overrides, {}) == {"a": "B000000013"}

    state = c.post(url, json={"isbn": None}).json()
    assert state["mode"] == "none" and state["catalog"] is None

    state = c.delete(url).json()
    assert state["mode"] == "auto" and state["catalog"]["isbn"] == "B000000001"
    assert load_json(server.cfg.audiobook_link_overrides, {}) == {}


def test_link_endpoints_reject_unknown_books_and_isbns(server):
    c = _link_client(server)
    assert c.get("/api/books/nope/audiobook-link").status_code == 404
    assert c.post("/api/books/nope/audiobook-link", json={"isbn": "B000000001"}).status_code == 404
    assert c.delete("/api/books/nope/audiobook-link").status_code == 404
    r = c.post("/api/books/a/audiobook-link", json={"isbn": "B999999999"})
    assert r.status_code == 400
    assert not server.cfg.audiobook_link_overrides.exists()


def test_catalog_search_for_the_link_picker(server):
    from epublib import search_index

    c = _link_client(server)
    search_index.build(server.cfg)
    results = c.get("/api/audiobook-catalog/search", params={"q": "銭天堂"}).json()["results"]
    assert {r["isbn"] for r in results} == {"B000000001", "B000000013"}
    assert {"isbn", "title", "author", "series", "order", "local"} <= set(results[0])
    assert c.get("/api/audiobook-catalog/search", params={"q": " "}).json() == {"results": []}
