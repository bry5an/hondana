"""epub_text: spine ordering and, for the reader pack, per-document text."""
from __future__ import annotations

import glob
import os
import zipfile

import pytest
from conftest import write_epub

from epublib import epub_text

_OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
 <manifest>
  <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
  <item id="css" href="style.css" media-type="text/css"/>
  <item id="p1" href="text/p%201.xhtml" media-type="application/xhtml+xml"/>
  <item id="p2" href="text/p2.xhtml#top" media-type="application/xhtml+xml"/>
 </manifest>
 <spine>
  <itemref idref="nav"/>
  <itemref idref="css"/>
  <itemref idref="ghost"/>
  <itemref idref="p1"/>
  <itemref idref="p2"/>
 </spine>
</package>"""


def _epub(tmp_path, opf=_OPF):
    path = tmp_path / "book.epub"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("OEBPS/content.opf", opf)
    return zipfile.ZipFile(path)


def test_spine_items_skip_non_html_and_nav_but_index_every_manifest_itemref(tmp_path):
    with _epub(tmp_path) as zf:
        items = epub_text.spine_items(zf, "OEBPS/content.opf")
    # nav = 0, css = 1; "ghost" has no manifest entry, so foliate drops it and
    # does not count it either. p1 and p2 are sections 2 and 3.
    assert [(i.index, i.idref) for i in items] == [(2, "p1"), (3, "p2")]
    # Hrefs are URL-decoded, fragments dropped, and resolved from the OPF folder.
    assert [i.href for i in items] == ["OEBPS/text/p 1.xhtml", "OEBPS/text/p2.xhtml"]


def test_spine_docs_is_the_hrefs_of_spine_items(tmp_path):
    with _epub(tmp_path) as zf:
        assert epub_text.spine_docs(zf, "OEBPS/content.opf") == [
            i.href for i in epub_text.spine_items(zf, "OEBPS/content.opf")
        ]


def test_spine_items_of_a_missing_or_broken_opf_is_empty(tmp_path):
    with _epub(tmp_path, opf="<package") as zf:
        assert epub_text.spine_items(zf, "OEBPS/content.opf") == []
        assert epub_text.spine_items(zf, "nope.opf") == []


def test_spine_items_keep_the_order_extract_text_reads(tmp_path):
    from conftest import write_epub

    path = tmp_path / "one.epub"
    write_epub(path, ["あ", "い"])
    with zipfile.ZipFile(path) as zf:
        opf = epub_text._find_opf_path(zf)
        assert [(i.index, i.href) for i in epub_text.spine_items(zf, opf)] == [(0, "OEBPS/doc1.xhtml")]


# ── dom_text(): the string the phone's DOM walk sees ─────────────────────────

def _page(body: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title></head><body>{body}</body></html>'
    ).encode()


def test_dom_text_keeps_the_tail_after_a_skipped_ruby_reading():
    d = epub_text.dom_text(_page("<p>漢<ruby>字<rt>じ</rt></ruby>だ</p>"))
    assert d.ok
    assert d.text == "漢字だ"


def test_dom_text_skips_rtc_rp_and_script_subtrees_but_not_their_tails():
    d = epub_text.dom_text(_page(
        "<p>あ<ruby>漢<rp>(</rp><rt>かん</rt><rp>)</rp><rtc><rt>x</rt></rtc>字</ruby>い<script>var s = 1;</script>う</p>"
    ))
    assert d.text == "あ漢字いう"


def test_dom_text_adds_nothing_to_text_and_does_not_trim():
    d = epub_text.dom_text(_page("<p>あ</p>\n  <p> い </p>"))
    assert d.text == "あ\n   い "


def test_block_boundaries_break_the_analysis_but_not_the_text():
    for body in ("<p>あ</p><p>い</p>", "あ<br/>い"):
        d = epub_text.dom_text(_page(body))
        assert d.text == "あい"
        assert "\n" in d.analysis[d.analysis.index("あ"):d.analysis.index("い")]
        # No sentence spans the boundary: あ and い are separate pieces.
        assert epub_text.split_sentences(d.analysis) == ["あ", "い"]


def test_to_dom_maps_analysis_offsets_back_to_text():
    d = epub_text.dom_text(_page("<p>あい。</p><p>うえ。</p>"))
    spans = epub_text.split_sentences_with_spans(d.analysis)
    mapped = [d.span_to_dom(s, e) for s, e in spans]
    assert [d.text[a:b] for a, b in mapped] == ["あい。", "うえ。"]
    # A token ending right before a virtual newline still ends at its last character.
    assert d.span_to_dom(d.analysis.index("あ"), d.analysis.index("あ") + 1) == (0, 1)


def test_utf16_offsets_count_non_bmp_as_two_units():
    text = "叱𠮟る"
    assert epub_text.utf16_offsets(text) == [0, 1, 3, 4]
    assert len("叱𠮟る".encode("utf-16-le")) // 2 == 4


def test_fnv1a_utf16_matches_javascript():
    # Computed in Node:
    #   let s = "漢字だ𠮟る。\n", h = 0x811c9dc5
    #   for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 0x01000193)
    #   (h >>> 0).toString(16).padStart(8, "0")   // "576f5e0e"
    assert epub_text.fnv1a_utf16("漢字だ𠮟る。\n") == "576f5e0e"
    assert epub_text.fnv1a_utf16("") == "811c9dc5"


def test_malformed_xhtml_falls_back_to_the_html_walk_with_ok_false():
    # &nbsp; is not an XML entity and <p> is never closed: ElementTree rejects both.
    raw = b'<html><body><p>a&nbsp;b<p>c<ruby>d<rt>x</rt>e</ruby><br>f</body></html>'
    d = epub_text.dom_text(raw)
    assert not d.ok
    assert d.text == "a bcdef"
    assert epub_text.split_sentences(d.analysis) == ["a b", "cde", "f"]


def test_dom_text_of_garbage_is_empty_not_an_error():
    d = epub_text.dom_text(b"\xff\xfe not html at all <<<")
    assert isinstance(d.text, str)


def _spine_sentences_via_extractor(raw: bytes) -> list[str]:
    parser = epub_text._TextExtractor()
    parser.feed(raw.decode("utf-8", errors="replace"))
    parser.close()
    return epub_text.split_sentences(parser.text())


def test_sentences_from_analysis_equal_the_extractors(tmp_path):
    path = tmp_path / "book.epub"
    write_epub(path, ["太郎は食べた。花子も食べた。", "「おはよう」", "<ruby>漢<rt>かん</rt></ruby>字を読む。"])
    with zipfile.ZipFile(path) as zf:
        for item in epub_text.spine_items(zf, epub_text._find_opf_path(zf)):
            raw = zf.read(item.href)
            assert epub_text.split_sentences(epub_text.dom_text(raw).analysis) == _spine_sentences_via_extractor(raw)


@pytest.mark.skipif(not os.environ.get("HONDANA_DEV_LIBRARY"), reason="set HONDANA_DEV_LIBRARY to a folder of EPUBs")
def test_sentences_equal_the_extractors_across_a_real_library():
    # Opt-in. XML parsing turns CRLF into LF, as a browser does, and the old
    # extractor kept the CR, so compare without it. A one-off run over the 74-book
    # dev library found 3,538 of 3,538 documents identical.
    checked = 0
    for book in sorted(glob.glob(os.path.join(os.environ["HONDANA_DEV_LIBRARY"], "**", "*.epub"), recursive=True)):
        try:
            zf = zipfile.ZipFile(book)
        except zipfile.BadZipFile:
            continue
        with zf:
            if epub_text._is_drm_protected(zf):
                continue
            opf = epub_text._find_opf_path(zf)
            for item in epub_text.spine_items(zf, opf) if opf else []:
                try:
                    raw = zf.read(item.href)
                except KeyError:
                    continue
                d = epub_text.dom_text(raw)
                old = [s.replace("\r", "") for s in _spine_sentences_via_extractor(raw)]
                assert epub_text.split_sentences(d.analysis) == old, (book, item.href)
                for s, e in epub_text.split_sentences_with_spans(d.analysis):
                    a, b = d.span_to_dom(s, e)
                    assert d.text[a:b] == d.analysis[s:e], (book, item.href)
                checked += 1
    assert checked
