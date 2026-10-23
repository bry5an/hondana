"""The OPF fields the scanner captures beyond title/author: kana readings via
file-as (EPUB2 attribute and EPUB3 refines), publisher, date, description, and the
series volume number from calibre or an EPUB3 collection."""
import json
import zipfile

import pytest

from conftest import _CONTAINER_XML
from epublib import build, epub_meta, scanner
from epublib.index_store import load_json, save_json

OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
 <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
  <dc:identifier id="bookid">urn:uuid:test</dc:identifier>
  <dc:language>ja</dc:language>
{metadata}
 </metadata>
 <manifest><item id="doc1" href="doc1.xhtml" media-type="application/xhtml+xml"/></manifest>
 <spine><itemref idref="doc1"/></spine>
</package>"""


def make_epub(path, metadata: str):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml", _CONTAINER_XML)
        zf.writestr("OEBPS/content.opf", OPF.format(metadata=metadata))
        zf.writestr("OEBPS/doc1.xhtml",
                    '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>あ</p></body></html>')
    return path


def meta_of(tmp_path, metadata):
    return epub_meta.extract(make_epub(tmp_path / "b.epub", metadata))


def test_epub3_refines_give_the_title_and_author_readings(tmp_path):
    m = meta_of(tmp_path, """
  <dc:title id="t1">アノ花</dc:title>
  <meta refines="#t1" property="file-as">アノハナ</meta>
  <dc:creator id="a1">塩見 夏恵</dc:creator>
  <meta refines="#a1" property="file-as">シオミ ナツエ</meta>""")
    assert (m.title, m.title_reading) == ("アノ花", "アノハナ")
    assert (m.author, m.author_reading, m.author_file_as) == ("塩見 夏恵", "シオミ ナツエ", "シオミ ナツエ")


def test_epub2_opf_file_as_attribute_and_halfwidth_kana_are_normalized(tmp_path):
    m = meta_of(tmp_path, """
  <dc:title opf:file-as="ｱﾉﾊﾅ">アノ花</dc:title>
  <dc:creator opf:role="aut" opf:file-as="Bryson, Bill">Bill Bryson</dc:creator>""")
    assert m.title_reading == "アノハナ"              # NFKC: half-width → full-width
    assert m.author_file_as == "Bryson, Bill"
    assert m.author_reading is None                   # a Western sort key is not a reading


@pytest.mark.parametrize("file_as", ["Anohana", "あの 花 2", "花"])
def test_a_file_as_that_is_not_pure_kana_is_not_a_reading(tmp_path, file_as):
    m = meta_of(tmp_path, f'<dc:title opf:file-as="{file_as}">T</dc:title>')
    assert m.title_reading is None


def test_a_reading_may_end_in_a_volume_number(tmp_path):
    m = meta_of(tmp_path, '<dc:title opf:file-as="ホメカタ００１">ほめ方</dc:title>')
    assert m.title_reading == "ホメカタ001"        # full-width digits fold to ASCII


def test_publisher_date_and_description(tmp_path):
    m = meta_of(tmp_path, """
  <dc:title>T</dc:title>
  <dc:publisher>KADOKAWA</dc:publisher>
  <dc:date>2019-03-01T00:00:00+00:00</dc:date>
  <dc:description>&lt;p&gt;第一章 &amp;amp; 続き&lt;/p&gt;
&lt;p&gt;あらすじ&lt;/p&gt;</dc:description>""")
    assert m.publisher == "KADOKAWA"
    assert m.pub_date == "2019-03-01"
    assert m.description == "第一章 & 続き あらすじ"      # markup stripped, entities decoded, whitespace folded


def test_description_may_be_xhtml_children_and_dates_may_be_just_a_year(tmp_path):
    m = meta_of(tmp_path, """
  <dc:title>T</dc:title>
  <dc:date>c. 2004</dc:date>
  <dc:description><p xmlns="http://www.w3.org/1999/xhtml">本文 <b>強調</b></p></dc:description>""")
    assert (m.pub_date, m.description) == ("2004", "本文 強調")
    assert meta_of(tmp_path, "<dc:title>T</dc:title><dc:date>unknown</dc:date>").pub_date is None


@pytest.mark.parametrize("raw, expected", [("1.0", "01"), ("12", "12"), ("2.5", "2.5"), ("x", None), ("-1", None), (None, None)])
def test_calibre_series_index_becomes_a_build_ready_order(tmp_path, raw, expected):
    idx = f'<meta name="calibre:series_index" content="{raw}"/>' if raw is not None else ""
    m = meta_of(tmp_path, f'<dc:title>T</dc:title><meta name="calibre:series" content="S"/>{idx}')
    assert (m.series, m.series_index) == ("S", expected)


def test_epub3_collection_is_the_series_when_calibre_has_none(tmp_path):
    m = meta_of(tmp_path, """
  <dc:title>T</dc:title>
  <meta property="belongs-to-collection" id="c0">Shelf</meta>
  <meta refines="#c0" property="collection-type">set</meta>
  <meta property="belongs-to-collection" id="c1">Real Series</meta>
  <meta refines="#c1" property="collection-type">series</meta>
  <meta refines="#c1" property="group-position">3</meta>""")
    assert (m.series, m.series_index) == ("Real Series", "03")
    m = meta_of(tmp_path, """
  <dc:title>T</dc:title><meta name="calibre:series" content="Calibre"/>
  <meta property="belongs-to-collection" id="c1">Other</meta>
  <meta refines="#c1" property="group-position">9</meta>""")
    assert (m.series, m.series_index) == ("Calibre", None)         # calibre's name wins, so does its (absent) index


def test_a_bare_opf_still_yields_none_for_every_new_field(tmp_path):
    m = meta_of(tmp_path, "<dc:title>T</dc:title>")
    assert (m.title_reading, m.author_reading, m.author_file_as, m.publisher, m.pub_date,
            m.description, m.series_index) == (None,) * 7


# ── scanner + build + API ────────────────────────────────────────────────────

def test_scan_stores_the_fields_and_build_uses_the_series_index(cfg):
    cfg.epub_dir.mkdir(parents=True, exist_ok=True)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    for n, order in ((1, "2.0"), (2, "10")):
        make_epub(cfg.epub_dir / f"v{n}.epub", f"""
  <dc:title>Different Subtitle {n}</dc:title><dc:publisher>Pub</dc:publisher>
  <dc:date>2020</dc:date><dc:description>desc {n}</dc:description>
  <meta name="calibre:series" content="Saga"/><meta name="calibre:series_index" content="{order}"/>""")
    scanner.scan(cfg)
    build.build(cfg)
    lib = load_json(cfg.library_index, {})
    assert {b["schema"] for b in lib.values()} == {scanner.SCHEMA_VERSION}
    by_title = {b["raw_title"]: b for b in lib.values()}
    one = by_title["Different Subtitle 1"]
    assert (one["publisher"], one["pub_date"], one["description"]) == ("Pub", "2020", "desc 1")
    # calibre-sourced series used to come out with series_order None
    assert [(b["series_name"], b["series_order"], b["series_source"]) for b in
            (one, by_title["Different Subtitle 2"])] == [("Saga", "02", "calibre"), ("Saga", "10", "calibre")]


def test_unchanged_files_are_rescanned_once_for_the_new_schema(cfg):
    cfg.epub_dir.mkdir(parents=True, exist_ok=True)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    make_epub(cfg.epub_dir / "a.epub", "<dc:title>T</dc:title><dc:publisher>Pub</dc:publisher>")
    scanner.scan(cfg)
    lib = load_json(cfg.library_index, {})
    (bid,) = lib
    old = {k: v for k, v in lib[bid].items() if k not in ("publisher", "schema")}
    save_json(cfg.library_index, {bid: {**old, "schema": 3}})        # as scanned by an older schema
    scanner.scan(cfg)
    assert load_json(cfg.library_index, {})[bid]["publisher"] == "Pub"


def test_the_list_endpoint_omits_the_synopsis_but_the_book_endpoint_keeps_it(server):
    cfg = server.cfg
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    save_json(cfg.library_index, {"b1": {"book_id": "b1", "title": "T", "language": "ja",
                                         "description": "long synopsis", "publisher": "Pub"}})
    listed = json.loads(server.api_books(req_cfg=server.cfg).body)
    assert listed[0]["publisher"] == "Pub" and "description" not in listed[0]
    assert server.api_book("b1", server.cfg)["description"] == "long synopsis"
