"""Extract the readable text of an EPUB in spine order, using only the stdlib.

The OPF <spine> lists the reading-order document ids; each id resolves through
the <manifest> to an XHTML file inside the zip. Text is pulled out with an
HTMLParser subclass (tolerant of the non-well-formed XHTML found in the wild,
which ElementTree would reject) that drops <rt>/<rp> ruby annotations so
furigana readings are not counted twice.

Also home to the sentence/character counting rules shared by the analyze
stage, so all text-shape decisions live in one place:
- a "character" is any Unicode letter or number (categories L*/N*)
- sentences end at 。．！？ etc. (trailing quotes/brackets stay attached), and
  a line without terminal punctuation (e.g. quoted dialogue 「おはよう」)
  counts as a sentence too if it contains at least one word character
"""
from __future__ import annotations

import posixpath
import re
import unicodedata
import zipfile
from bisect import bisect_right
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import NamedTuple
from urllib.parse import unquote
from xml.etree import ElementTree as ET

from .epub_meta import _find_opf_path, _localname

# Content inside these elements is never part of the prose.
_SKIP_TAGS = {"rt", "rp", "script", "style", "head", "title", "svg"}
# Opening/closing one of these implies a text break (mapped to a newline).
_BLOCK_TAGS = {
    "p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "td", "th", "tr", "blockquote", "section", "article", "table",
}

# encryption.xml entries using only these algorithms are obfuscated embedded
# fonts, not DRM — the text documents are still readable.
_FONT_OBFUSCATION_ALGOS = {
    "http://www.idpf.org/2008/embedding",
    "http://ns.adobe.com/pdf/enc#RC",
}

_SENT_END = re.compile(r"[。．！？‼⁉⁈⁇]+[」』）】〉》〕＞>\"'\)\]]*")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_startendtag(self, tag: str, attrs) -> None:
        # Self-closing tags (<br/>) must not unbalance the skip counter.
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if data and not self._skip_depth:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def _is_drm_protected(zf: zipfile.ZipFile) -> bool:
    """True when encryption.xml encrypts real content (not just fonts)."""
    try:
        raw = zf.read("META-INF/encryption.xml")
    except KeyError:
        return False
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return True  # unreadable encryption manifest → assume protected
    for el in root.iter():
        if _localname(el.tag) != "EncryptionMethod":
            continue
        if (el.get("Algorithm") or "") not in _FONT_OBFUSCATION_ALGOS:
            return True
    return False


class SpineItem(NamedTuple):
    """One reading-order document of an EPUB's spine.

    `index` counts every spine itemref that names a manifest item, whatever its
    media type. foliate-js builds its `sections` from the same set, so a reader
    can match this index to the section it renders."""
    index: int
    idref: str
    href: str  # archive path


def spine_items(zf: zipfile.ZipFile, opf_path: str) -> list[SpineItem]:
    """The spine's (X)HTML documents, in reading order, with their spine index."""
    try:
        with zf.open(opf_path) as f:
            root = ET.parse(f).getroot()
    except (KeyError, ET.ParseError):
        return []
    manifest_el = next((c for c in root if _localname(c.tag) == "manifest"), None)
    spine_el = next((c for c in root if _localname(c.tag) == "spine"), None)
    if manifest_el is None or spine_el is None:
        return []

    items: dict[str, tuple[str, str, str]] = {}
    for item in manifest_el:
        if _localname(item.tag) != "item":
            continue
        iid, href = item.get("id"), item.get("href")
        if iid and href:
            items[iid] = (href, item.get("media-type", ""), item.get("properties", ""))

    opf_dir = posixpath.dirname(opf_path)
    docs: list[SpineItem] = []
    index = 0
    for itemref in spine_el:
        if _localname(itemref.tag) != "itemref":
            continue
        idref = itemref.get("idref") or ""
        entry = items.get(idref)
        if not entry:
            continue
        here, index = index, index + 1
        href, media, props = entry
        if "html" not in media.lower():  # matches xhtml+xml and text/html
            continue
        if "nav" in props.split():
            continue
        href = unquote(href.split("#", 1)[0])
        docs.append(SpineItem(here, idref, posixpath.normpath(posixpath.join(opf_dir, href))))
    return docs


def spine_docs(zf: zipfile.ZipFile, opf_path: str) -> list[str]:
    """Archive paths of the spine's (X)HTML documents, in reading order."""
    return [item.href for item in spine_items(zf, opf_path)]


def extract_text(path: Path) -> str | None:
    """Full spine-ordered text of the EPUB, or None if none is extractable
    (bad zip, no OPF/spine, DRM-encrypted content)."""
    try:
        with zipfile.ZipFile(path) as zf:
            if _is_drm_protected(zf):
                return None
            opf_path = _find_opf_path(zf)
            if not opf_path:
                return None
            docs = spine_docs(zf, opf_path)
            if not docs:
                return None
            chunks: list[str] = []
            for doc in docs:
                try:
                    raw = zf.read(doc)
                except KeyError:
                    continue
                parser = _TextExtractor()
                try:
                    parser.feed(raw.decode("utf-8", errors="replace"))
                    parser.close()
                except Exception:
                    continue  # one broken document never kills the book
                chunks.append(parser.text())
            text = "\n".join(chunks)
            return text if text.strip() else None
    except (zipfile.BadZipFile, OSError):
        return None


# ── Per-document text for the reader pack ───────────────────────────────────
#
# The phone renders each spine document in a WebView and places the pack's
# tokens by walking <body>'s text nodes, so the pack's offsets must index
# exactly the string the browser sees. `extract_text` can't give that: it adds
# a newline at block tags and works on the whole book.

# Subtrees the phone's walk skips: the ruby readings (and their container).
_DOM_SKIP_TAGS = _SKIP_TAGS | {"rtc"}


@dataclass(frozen=True)
class DomText:
    """One document's text, as the browser's text nodes concatenate.

    - `text`: the concatenation of the body's text nodes, minus ruby readings
      and the other skipped subtrees. No whitespace normalization, nothing
      inserted. Offsets into it are what the pack stores.
    - `analysis`: the same text with a virtual "\n" at every block tag's open
      and close, so sentence splitting and tokenizing never join words across
      `<br>` or `</p><p>`. Analyze this, then map back with `to_dom`.
    - `inserted`: the positions in `analysis` of those virtual newlines.
    - `ok`: False when the document wasn't well-formed XML and a tolerant HTML
      walk produced the text instead; the phone may then disagree with it.
    """
    text: str
    analysis: str
    inserted: tuple[int, ...]
    ok: bool

    def to_dom(self, i: int) -> int:
        """Offset into `text` of the real character at `analysis[i]` (for a
        virtual newline: of the character after it)."""
        return i - bisect_right(self.inserted, i)

    def span_to_dom(self, start: int, end: int) -> tuple[int, int]:
        """`(start, end)` in `analysis` -> the same span in `text`. The end is
        mapped from its last character, because an exclusive end that falls on
        a virtual newline would otherwise land past the span."""
        if end <= start:
            d = self.to_dom(start)
            return d, d
        return self.to_dom(start), self.to_dom(end - 1) + 1


class _DomTextBuilder:
    def __init__(self) -> None:
        self._text: list[str] = []
        self._analysis: list[str] = []
        self._inserted: list[int] = []
        self._len = 0  # length of `analysis` so far

    def data(self, s: str) -> None:
        if s:
            self._text.append(s)
            self._analysis.append(s)
            self._len += len(s)

    def block_break(self) -> None:
        self._inserted.append(self._len)
        self._analysis.append("\n")
        self._len += 1

    def build(self, ok: bool) -> DomText:
        return DomText("".join(self._text), "".join(self._analysis), tuple(self._inserted), ok)


def _walk_dom(el: ET.Element, out: _DomTextBuilder) -> None:
    name = _localname(el.tag) if isinstance(el.tag, str) else ""
    if name in _DOM_SKIP_TAGS:
        return  # the caller still emits this element's tail
    block = name in _BLOCK_TAGS
    if block:
        out.block_break()
    out.data(el.text or "")
    for child in el:
        _walk_dom(child, out)
        out.data(child.tail or "")
    if block:
        out.block_break()


class _DomTextFallback(HTMLParser):
    """The same walk over tolerant HTML, for documents ElementTree rejects."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out = _DomTextBuilder()
        self._skip: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if self._skip:
            if tag == self._skip[-1]:
                self._skip.append(tag)  # nested copy of the skipped tag
        elif tag in _DOM_SKIP_TAGS:
            self._skip.append(tag)
        elif tag in _BLOCK_TAGS:
            self.out.block_break()

    def handle_endtag(self, tag: str) -> None:
        if self._skip:
            if tag == self._skip[-1]:
                self._skip.pop()
        elif tag in _BLOCK_TAGS:
            self.out.block_break()

    def handle_startendtag(self, tag: str, attrs) -> None:
        # Self-closing: opens and closes at once, so it never enters the skip stack.
        if not self._skip and tag in _BLOCK_TAGS:
            self.out.block_break()
            self.out.block_break()

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.out.data(data)


def dom_text(xhtml: bytes) -> DomText:
    """The text of one spine document as the phone's DOM walk will see it."""
    try:
        root = ET.fromstring(xhtml)
        body = next((c for c in root.iter() if isinstance(c.tag, str) and _localname(c.tag) == "body"), None)
        if body is None:
            raise ET.ParseError("no body")
    except ET.ParseError:
        parser = _DomTextFallback()
        try:
            parser.feed(xhtml.decode("utf-8", errors="replace"))
            parser.close()
        except Exception:
            pass  # keep what was read: one broken document never kills the book
        return parser.out.build(ok=False)
    out = _DomTextBuilder()
    _walk_dom(body, out)
    return out.build(ok=True)


def utf16_offsets(text: str) -> list[int]:
    """`offsets[i]` is the UTF-16 code-unit index of code point `i` (and
    `offsets[len(text)]` the total length). JS string indexes are UTF-16, and
    a character outside the BMP (𠮟) takes two units, so every later offset
    differs from the code-point one."""
    offsets = [0] * (len(text) + 1)
    units = 0
    for i, ch in enumerate(text):
        offsets[i] = units
        units += 2 if ord(ch) > 0xFFFF else 1
    offsets[len(text)] = units
    return offsets


def fnv1a_utf16(text: str) -> str:
    """32-bit FNV-1a over the UTF-16 code units of `text` (each unit folded in
    whole, as JS `charCodeAt` returns it), as 8 hex digits. The phone checks a
    document against the pack with the same function, because `crypto.subtle`
    is not available on a custom-scheme page:

        let h = 0x811c9dc5
        for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 0x01000193)
        return (h >>> 0).toString(16).padStart(8, '0')
    """
    data = text.encode("utf-16-le", "surrogatepass")
    h = 0x811C9DC5
    for i in range(0, len(data), 2):
        h = ((h ^ (data[i] | data[i + 1] << 8)) * 0x01000193) & 0xFFFFFFFF
    return f"{h:08x}"


def _has_word_char(s: str) -> bool:
    return any(unicodedata.category(ch)[0] in ("L", "N") for ch in s)


def count_chars(text: str) -> int:
    """Count of letter/number characters (Unicode L*/N*) — jpdb's 'Characters'."""
    return sum(1 for ch in text if unicodedata.category(ch)[0] in ("L", "N"))


def split_sentences_with_spans(text: str) -> list[tuple[int, int]]:
    """Like split_sentences, but returns (start, end) character offsets into
    text instead of the substrings — lets a caller store the offsets once at
    analyze time and slice the original text at request time, rather than
    re-running the splitter and hoping the result lines up positionally."""
    spans: list[tuple[int, int]] = []
    offset = 0
    for line in text.split("\n"):
        pos = 0
        for m in _SENT_END.finditer(line):
            if _has_word_char(line[pos:m.end()]):
                spans.append((offset + pos, offset + m.end()))
            pos = m.end()
        if _has_word_char(line[pos:]):
            spans.append((offset + pos, offset + len(line)))
        offset += len(line) + 1  # +1 for the '\n' split() consumed
    return spans


def split_sentences(text: str) -> list[str]:
    """Split into sentences per the module rules; only segments containing at
    least one word character count."""
    return [text[start:end] for start, end in split_sentences_with_spans(text)]
