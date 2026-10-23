"""Reader packs (annotate.py): offsets, word keys, ruby, refusals, cache, route.

Uses the real fugashi tagger on small synthetic EPUBs (long enough to clear
analyze.MIN_WORD_CHARS), like test_analyze.py.
"""
from __future__ import annotations

import gzip
import json
import zipfile
from collections import Counter

import pytest

from epublib import analyze, annotate, epub_text
from epublib.index_store import save_json

_CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""

_ENCRYPTION = """<?xml version="1.0"?>
<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"
            xmlns:enc="http://www.w3.org/2001/04/xmlenc#">
  <enc:EncryptedData>
    <enc:EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes128-cbc"/>
    <enc:CipherData><enc:CipherReference URI="OEBPS/text/d0.xhtml"/></enc:CipherData>
  </enc:EncryptedData>
</encryption>"""

# Enough distinct prose to clear MIN_WORD_CHARS in the first document.
_PROSE = "".join(
    f"<p>私は{n}日前に朝ごはんを食べた。今日は人々が三ヶ月ぶりに取り扱い所へ行った。"
    "猫が好きだ。庭に花が咲いている。</p>"
    for n in "一二三四五六七八九十" * 3)


def _doc(body: str) -> str:
    return ('<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml">'
            f"<head><title>t</title></head><body>{body}</body></html>")


_DOCS = [
    _doc(_PROSE + "<p>漢<ruby>字<rt>じ</rt></ruby>を読む。</p>"),
    # 𠮟 is two UTF-16 units: every offset after it is one more than its code-point index.
    _doc("<p>𠮟られた。</p><p>東</p><p>京に行く。</p><p>本を読む<br/>京都へ行く。</p>"
         "<p>言葉より大切なもの。</p><p>君子はより高いものを目指す。</p>"
         "<p>言う方も言われた方も嬉しくなる言葉ですね。</p><p>人にとって大切なものは、仁──思いやりの気持ち──という考え方で一貫しています。</p>"),
]


def write_book(path, docs=_DOCS, *, drm: bool = False) -> None:
    items = "".join(
        f'<item id="d{i}" href="text/d{i}.xhtml" media-type="application/xhtml+xml"/>'
        for i in range(len(docs)))
    spine = "".join(f'<itemref idref="d{i}"/>' for i in range(len(docs)))
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        f"<manifest>{items}</manifest><spine>{spine}</spine></package>")
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml", _CONTAINER)
        if drm:
            zf.writestr("META-INF/encryption.xml", _ENCRYPTION)
        zf.writestr("OEBPS/content.opf", opf)
        for i, d in enumerate(docs):
            zf.writestr(f"OEBPS/text/d{i}.xhtml", d)


def _book(cfg, book_id="b1", *, language="ja", docs=_DOCS, drm=False) -> dict:
    write_book(cfg.epub_dir / f"{book_id}.epub", docs, drm=drm)
    return {"book_id": book_id, "title": "T", "language": language, "path": f"{book_id}.epub",
            "sha256": "ab" * 32, "identifiers": {"asin": "B0TEST"}}


def _utf16_slice(text: str, start: int, length: int) -> str:
    raw = text.encode("utf-16-le")
    return raw[2 * start: 2 * (start + length)].decode("utf-16-le")


# ── offsets ──────────────────────────────────────────────────────────────────

def test_every_token_slices_its_surface_out_of_the_dom_text(cfg):
    pack = annotate.build_pack(cfg, _book(cfg))
    assert pack["stable_key"] == "asin:B0TEST"
    assert [d["index"] for d in pack["docs"]] == [0, 1]
    checked = 0
    for doc, raw in zip(pack["docs"], _DOCS):
        dt = epub_text.dom_text(raw.encode())
        assert doc["len"] == len(dt.text.encode("utf-16-le")) // 2
        assert doc["fnv"] == epub_text.fnv1a_utf16(dt.text)
        # What the tagger sees, sentence by sentence, in order.
        surfaces = [
            seg_tok.surface
            for s, e in epub_text.split_sentences_with_spans(dt.analysis)
            for _, _, seg_tok in analyze.iter_tokens(dt.analysis[s:e])
            if analyze.word_def(seg_tok) is not None
        ]
        got = [_utf16_slice(dt.text, start, n) for start, n, _ in doc["tokens"]]
        assert got == surfaces
        checked += len(got)
        for s, e in doc["sents"]:
            assert 0 <= s < e <= doc["len"]
    assert checked > 100


def test_offsets_after_a_non_bmp_character_are_utf16_units(cfg):
    pack = annotate.build_pack(cfg, _book(cfg))
    doc = pack["docs"][1]
    # 𠮟 yields no word, so the first token is the られ after it, at unit 2.
    assert doc["tokens"][0][0] == 2


def test_ruby_in_the_book_is_not_text_and_its_tail_stays(cfg):
    pack = annotate.build_pack(cfg, _book(cfg))
    doc = pack["docs"][0]
    dt = epub_text.dom_text(_DOCS[0].encode())
    assert "じ" not in dt.text[-12:] and dt.text.endswith("漢字を読む。")
    assert doc["len"] == len(dt.text)


# ── word keys ────────────────────────────────────────────────────────────────

def test_content_orth_keys_equal_the_wordlist(cfg):
    book = _book(cfg)
    text = epub_text.extract_text(cfg.epub_dir / book["path"])
    _, words, _ = analyze._analyze_text(text)  # what analyze writes to wordlists/{id}.json
    pack = annotate.build_pack(cfg, book)
    table = pack["words"]
    got = Counter(table[wid][0] for doc in pack["docs"] for _, _, wid in doc["tokens"]
                  if table[wid][6] == "content")
    assert got == words
    assert sum(got.values()) > 50


def test_function_words_are_tokens_and_punctuation_is_not(cfg):
    pack = annotate.build_pack(cfg, _book(cfg))
    kinds = {w[0]: w[6] for w in pack["words"]}
    assert kinds["は"] == "function" and kinds["猫"] == "content"
    assert "。" not in kinds
    entry = next(w for w in pack["words"] if w[0] == "猫")
    assert entry[:2] == ["猫", "猫"] and entry[2] == "ねこ" and entry[3] == "ねこ"
    assert entry[4] == [["猫", "ねこ"]] and entry[5] == 0 and entry[7] == "名詞"


def test_a_word_is_interned_once_per_surface_and_row(cfg):
    words = annotate.build_pack(cfg, _book(cfg))["words"]
    assert {w[2] for w in words if w[0] == "食べる"} == {"たべ"}  # 食べ twice -> one row
    assert len({json.dumps(w) for w in words}) == len(words)


def test_one_orth_and_lemma_with_two_classes_get_two_word_rows(cfg):
    # より is a particle in 言葉より and an adverb in より高い; 方 is a noun, or a
    # free affix in 先生方. Interning on (surface, orth, lemma, kana) alone would
    # merge them and change the counts (it did, by ~0.1% on every dev book).
    rows = annotate.build_pack(cfg, _book(cfg))["words"]
    assert {w[6] for w in rows if w[0] == "より"} == {"function", "content"}
    assert {w[5] for w in rows if w[0] == "方"} == {0, 1}


# ── ruby ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("surface, kana, ruby", [
    ("食べた", "タベタ", [["食", "た"], ["べた", ""]]),
    ("人々", "ヒトビト", [["人々", "ひとびと"]]),
    ("ヶ月", "カゲツ", [["ヶ月", "かげつ"]]),
    ("今日", "キョウ", [["今日", "きょう"]]),
    ("取り扱い", "トリアツカイ", [["取", "と"], ["り", ""], ["扱", "あつか"], ["い", ""]]),
    ("ひらがな", "ヒラガナ", []),
    ("カタカナ", "カタカナ", []),
    ("食べた", "ゼンゼン", [["食べた", "ぜんぜん"]]),  # can't align: all reading over all surface
    ("食べた", "", []),
])
def test_ruby_covers_the_whole_surface(surface, kana, ruby):
    assert annotate.ruby_for(surface, kana) == ruby
    if ruby:
        assert "".join(base for base, _ in ruby) == surface


def test_the_widened_kanji_test_is_the_packs_alone():
    assert analyze._align_readings("人々", "ヒトビト") == ()  # the stats keep the narrow test
    assert analyze._align_readings("人々", "ヒトビト", True) == (("人々", "ヒトビト"),)


def test_ruby_for_a_real_token(cfg):
    pack = annotate.build_pack(cfg, _book(cfg))
    today = next(w for w in pack["words"] if w[0] == "今日")
    assert today[4] == [["今日", "きょう"]]


# ── boundaries ───────────────────────────────────────────────────────────────

def test_no_token_spans_a_block_boundary(cfg):
    pack = annotate.build_pack(cfg, _book(cfg))
    doc = pack["docs"][1]
    dt = epub_text.dom_text(_DOCS[1].encode())
    text = dt.text
    surfaces = [_utf16_slice(text, s, n) for s, n, _ in doc["tokens"]]
    # 東</p><p>京 and 東<br/>京 would be 東京 if joined; they must stay apart.
    assert "東京" not in surfaces and "東" in surfaces
    def unit(i: int) -> int:  # code point index -> UTF-16 unit (𠮟 is two)
        return len(text[:i].encode("utf-16-le")) // 2

    boundary = unit(text.index("東") + 1)  # </p><p> sits right after 東 (nothing is inserted)
    assert not any(s < boundary < s + n for s, n, _ in doc["tokens"])
    br = unit(text.index("読む") + 2)  # <br/> right after 読む
    assert not any(s < br < s + n for s, n, _ in doc["tokens"])
    assert not any(s < br < e for s, e in doc["sents"])


# ── refusals ─────────────────────────────────────────────────────────────────

def test_refusals(cfg):
    with pytest.raises(annotate.NoPack) as e:
        annotate.build_pack(cfg, _book(cfg, "d", drm=True))
    assert e.value.reason == "drm"
    with pytest.raises(annotate.NoPack) as e:
        annotate.build_pack(cfg, _book(cfg, "e", language="en"))
    assert e.value.reason == "not_ja"
    with pytest.raises(annotate.NoPack) as e:
        annotate.build_pack(cfg, _book(cfg, "n", docs=[_doc("<p>短い。</p>")]))
    assert e.value.reason == "no_text"


# ── cache ────────────────────────────────────────────────────────────────────

def _count_tagger_calls(monkeypatch) -> list[int]:
    calls = [0]
    real = analyze._get_tagger

    def counting():
        calls[0] += 1
        return real()

    monkeypatch.setattr(analyze, "_get_tagger", counting)
    return calls


def test_pack_is_stored_gzipped_and_reused_until_the_sha_or_schema_changes(cfg, monkeypatch):
    book = _book(cfg)
    calls = _count_tagger_calls(monkeypatch)
    path = annotate.ensure_pack(cfg, book)
    assert path == cfg.annotations_dir / "b1.json.gz"
    stored = json.loads(gzip.decompress(path.read_bytes()))
    assert list(stored)[:4] == ["schema", "book_id", "sha256", "stable_key"]
    assert annotate.cached_header(path) == (1, "ab" * 32)
    assert not list(cfg.annotations_dir.glob("*.tmp"))

    built = calls[0]
    assert built > 0
    annotate.ensure_pack(cfg, book)
    assert calls[0] == built  # cached

    annotate.ensure_pack(cfg, {**book, "sha256": "cd" * 32})
    assert calls[0] > built and annotate.cached_header(path) == (1, "cd" * 32)

    again = calls[0]
    monkeypatch.setattr(annotate, "ANNOTATE_SCHEMA", 2)
    annotate.ensure_pack(cfg, {**book, "sha256": "cd" * 32})
    assert calls[0] > again and annotate.cached_header(path)[0] == 2


def test_a_corrupt_stored_pack_is_rebuilt(cfg):
    book = _book(cfg)
    path = annotate.pack_path(cfg, "b1")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not gzip")
    assert annotate.cached_header(path) is None
    annotate.ensure_pack(cfg, book)
    assert annotate.cached_header(path) == (1, "ab" * 32)


def test_build_all_packs_ja_books_skips_refusals_and_drops_stale_files(cfg):
    ok = _book(cfg, "ok")
    en = _book(cfg, "en", language="en")
    drm = _book(cfg, "drm", drm=True)
    save_json(cfg.library_index, {b["book_id"]: b for b in (ok, en, drm)})
    stale = annotate.pack_path(cfg, "gone")
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"x")
    annotate.build_all(cfg)
    assert sorted(p.name for p in cfg.annotations_dir.iterdir()) == ["ok.json.gz"]


def test_purge_and_gc_know_about_annotations(cfg):
    from epublib import orphans, purge

    path = annotate.pack_path(cfg, "b1")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"x")
    assert orphans.collect(cfg)["annotations/"] == {"b1"}
    assert "annotations" in purge.purge_book(cfg, "b1")
    assert not path.exists()


# ── HTTP ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def client(server):
    from fastapi.testclient import TestClient

    scfg = server.cfg
    books = {
        "b1": _book(scfg, "b1"),
        "en": _book(scfg, "en", language="en"),
        "drm": _book(scfg, "drm", drm=True),
    }
    save_json(scfg.library_index, books)
    return TestClient(server.app)


def test_pack_route_serves_stored_gzip_with_an_etag_and_304s(client, monkeypatch):
    r = client.get("/api/reader/books/b1/pack")
    assert r.status_code == 200
    assert r.headers["content-encoding"] == "gzip"
    assert r.headers["content-type"].startswith("application/json")
    assert r.headers["etag"] == '"1-' + "ab" * 8 + '"'
    pack = r.json()  # the client inflated it
    assert pack["schema"] == 1 and pack["book_id"] == "b1" and pack["docs"]

    r304 = client.get("/api/reader/books/b1/pack", headers={"If-None-Match": r.headers["etag"]})
    assert r304.status_code == 304 and not r304.content
    assert client.get("/api/reader/books/b1/pack", headers={"If-None-Match": '"0-x"'}).status_code == 200

    # A client that can't take gzip gets the JSON itself.
    plain = client.get("/api/reader/books/b1/pack", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in plain.headers and plain.json() == pack

    assert client.head("/api/reader/books/b1/pack").status_code == 200


def test_a_second_request_is_served_from_cache_without_the_tagger(client, monkeypatch):
    assert client.get("/api/reader/books/b1/pack").status_code == 200

    def no_tagger():
        raise AssertionError("tagger called")

    monkeypatch.setattr(analyze, "_get_tagger", no_tagger)
    assert client.get("/api/reader/books/b1/pack").status_code == 200


def test_pack_route_errors(client):
    assert client.get("/api/reader/books/nope/pack").status_code == 404
    r = client.get("/api/reader/books/drm/pack")
    assert r.status_code == 409 and r.json() == {"reason": "drm"}
    r = client.get("/api/reader/books/en/pack")
    assert r.status_code == 409 and r.json() == {"reason": "not_ja"}


def test_building_a_pack_does_not_wait_for_the_index_lock(client, server):
    with server._index_lock:  # a stage run holds this for minutes
        assert client.get("/api/reader/books/b1/pack").status_code == 200


def test_annotate_is_a_standalone_maintenance_stage():
    from epublib import pipeline

    stage = pipeline.STAGE_BY_ID["annotate"]
    assert not stage.in_chain and stage.group == "maintenance"
    assert "annotate" not in pipeline.CHAIN


# ── stable_key on the book routes ─────────────────────────────────────

def test_book_routes_carry_the_stable_key_the_pack_does(client, server):
    """The phone files a position under it; a book with no pack has no other source."""
    from epublib import pairing

    scfg = server.cfg
    lib = {
        "b1": _book(scfg, "b1"),  # asin
        "isbn": {**_book(scfg, "isbn"), "identifiers": {"isbn": "9784000000000"}},
        "path": {**_book(scfg, "path"), "identifiers": {}, "path": "Japanese/x.epub"},
    }
    save_json(scfg.library_index, lib)
    listed = {b["book_id"]: b for b in client.get("/api/books").json()}
    for bid, book in lib.items():
        want = pairing.stable_key(book)
        assert listed[bid]["stable_key"] == want
        assert client.get(f"/api/books/{bid}").json()["stable_key"] == want
    assert [listed[b]["stable_key"] for b in ("b1", "isbn", "path")] == [
        "asin:B0TEST", "isbn:9784000000000", "path:Japanese/x.epub"]
    # The list still leaves its source fields out.
    assert "identifiers" not in listed["b1"] and "path" not in listed["b1"]
    assert client.get("/api/reader/books/b1/pack").json()["stable_key"] == listed["b1"]["stable_key"]
