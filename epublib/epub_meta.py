"""Extract metadata + cover image from an EPUB using only the stdlib.

An EPUB is a ZIP. We read META-INF/container.xml to find the OPF package
document, then parse its <metadata> (Dublin Core) for title/creator/language/
identifier, publisher/date/description, the `file-as` sort keys (kana readings
for Japanese books), the calibre or EPUB3 series (+ volume number), and locate the cover image via the EPUB3
`properties="cover-image"` manifest item or the EPUB2 `<meta name="cover">`
pointer.

Everything is defensive: any missing/garbled field yields None rather than an
exception, so a single malformed EPUB never aborts a whole scan.
"""
from __future__ import annotations

import html
import io
import posixpath
import re
import struct
import unicodedata
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

DC_NS = "http://purl.org/dc/elements/1.1/"
OPF_NS = "http://www.idpf.org/2007/opf"
CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"

# A real Amazon ASIN is "B" + 9 alphanumerics. Calibre sometimes parks a UUID
# in the MOBI-ASIN slot, so we only treat values matching this as ASINs.
_ASIN_RE = re.compile(r"^B[0-9A-Z]{9}$")
_ISBN_RE = re.compile(r"^\d{9}[\dXx]$|^\d{13}$")


@dataclass
class EpubMeta:
    title: str | None = None
    author: str | None = None
    language: str | None = None  # raw OPF dc:language, e.g. "ja", "en", "eng"
    identifier: str | None = None
    # Normalized identifiers keyed by kind: "asin"/"isbn"/"uuid". These DO NOT
    # bridge EN/JP editions (different products per edition) but are useful for
    # display and as stable override keys.
    identifiers: dict = field(default_factory=dict)
    series: str | None = None
    # Volume number of `series` as a build-ready order string ("01", "2.5"), from
    # calibre:series_index or the EPUB3 collection's group-position.
    series_index: str | None = None
    # `file-as` is the sort key an EPUB carries for a title/creator. For a Japanese
    # book it is usually the kana reading (シオミナツエ) — kept as *_reading only
    # when it really is kana. author_file_as keeps the raw value either way, since
    # for Western names it is the "Last, First" form.
    title_reading: str | None = None
    author_reading: str | None = None
    author_file_as: str | None = None
    publisher: str | None = None
    pub_date: str | None = None      # "2019", "2019-03" or "2019-03-01"
    description: str | None = None   # plain text, markup stripped
    # cover image bytes + a file extension hint (".jpg"/".png"); None if absent
    cover_bytes: bytes | None = None
    cover_ext: str | None = None


# A reading: nothing but kana (either script, half-width included), the long-vowel
# mark, middle dot, spaces and digits (a volume number rides along: …カタ001).
_KANA_ONLY = re.compile(r"[0-9぀-ゟ゠-ヿー・\s　ｦ-ﾟ]+")
_DATE_RE = re.compile(r"\b([12]\d{3})(?:-(\d{2}))?(?:-(\d{2}))?")
_TAG_RE = re.compile(r"<[^>]+>")
DESCRIPTION_MAX = 5000


def _reading(value: str | None) -> str | None:
    """`value` NFKC-normalized if it is a pure-kana reading, else None."""
    v = unicodedata.normalize("NFKC", value or "").strip()
    return v if v and _KANA_ONLY.fullmatch(v) else None


def _series_index(raw: str | None) -> str | None:
    """"1.0" → "01", "12" → "12", "2.5" → "2.5"; anything unusable → None."""
    try:
        n = float((raw or "").strip())
    except ValueError:
        return None
    if n < 0 or n >= 1000:
        return None
    return f"{int(n):02d}" if n == int(n) else f"{n:g}"


def _pub_date(raw: str | None) -> str | None:
    m = _DATE_RE.search(raw or "")
    return "-".join(g for g in m.groups() if g) if m else None


def _plain_text(el: ET.Element) -> str | None:
    """dc:description is free text, XHTML children, or (calibre) escaped HTML."""
    text = "".join(el.itertext())
    text = html.unescape(_TAG_RE.sub(" ", text))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:DESCRIPTION_MAX] or None


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _classify_identifier(value: str, scheme: str | None, id_attr: str | None) -> tuple[str, str] | None:
    """Classify a dc:identifier into (kind, clean_value).

    kind is one of "asin"/"isbn"/"uuid"; returns None if unrecognized.
    """
    raw = (value or "").strip()
    if not raw:
        return None
    low = raw.lower()
    scheme = (scheme or "").lower()
    id_attr = (id_attr or "").lower()
    hint = f"{scheme} {id_attr}"

    # Strip common urn prefixes for clean values.
    bare = raw
    for prefix in ("urn:isbn:", "urn:uuid:", "isbn:", "asin:", "mobi-asin:"):
        if low.startswith(prefix):
            bare = raw[len(prefix):]
            break

    # ISBN: by scheme/id hint or by digit pattern.
    digits = re.sub(r"[^0-9Xx]", "", bare)
    if "isbn" in hint or low.startswith(("urn:isbn", "isbn")):
        if _ISBN_RE.match(digits):
            return "isbn", digits
    if _ISBN_RE.match(digits) and ("isbn" in hint or len(bare.strip()) == len(digits)):
        return "isbn", digits

    # ASIN: only when it actually looks like an ASIN (not a UUID in the slot).
    if "asin" in hint or "amazon" in hint:
        if _ASIN_RE.match(bare.strip()):
            return "asin", bare.strip()
        # Otherwise fall through — likely a UUID parked in the MOBI-ASIN slot.
    if _ASIN_RE.match(bare.strip()):
        return "asin", bare.strip()

    # UUID: by prefix/scheme/hint.
    if low.startswith("urn:uuid") or "uuid" in hint or "calibre" in hint:
        return "uuid", bare.strip()

    return None


def _find_opf_path(zf: zipfile.ZipFile) -> str | None:
    try:
        with zf.open("META-INF/container.xml") as f:
            root = ET.parse(f).getroot()
    except (KeyError, ET.ParseError):
        return None
    for rootfile in root.iter(f"{{{CONTAINER_NS}}}rootfile"):
        path = rootfile.get("full-path")
        if path:
            return path
    # Fallback: any .opf in the archive
    for name in zf.namelist():
        if name.lower().endswith(".opf"):
            return name
    return None


def _text(el: ET.Element | None) -> str | None:
    if el is None or el.text is None:
        return None
    t = el.text.strip()
    return t or None


def _ext_for(name: str) -> str:
    lower = name.lower()
    if lower.endswith(".svg"):
        return ".svg"
    if lower.endswith(".png"):
        return ".png"
    if lower.endswith((".jpeg", ".jpg")):
        return ".jpg"
    if lower.endswith(".gif"):
        return ".gif"
    if lower.endswith(".webp"):
        return ".webp"
    return ".jpg"


def _parse_opf(zf: zipfile.ZipFile, opf_path: str) -> EpubMeta:
    meta = EpubMeta()
    try:
        with zf.open(opf_path) as f:
            root = ET.parse(f).getroot()
    except (KeyError, ET.ParseError):
        return meta

    metadata_el = next((c for c in root if _localname(c.tag) == "metadata"), None)
    manifest_el = next((c for c in root if _localname(c.tag) == "manifest"), None)

    authors: list[tuple[str, str | None, str | None]] = []   # (name, file-as attr, id)
    cover_meta_id: str | None = None
    title_el: ET.Element | None = None
    # EPUB3 refinements: <meta refines="#id" property="file-as">…</meta>
    refines: dict[tuple[str, str], str] = {}
    collections: list[tuple[str | None, str]] = []   # (id, name)
    calibre_index: str | None = None

    if metadata_el is not None:
        for child in metadata_el:
            name = _localname(child.tag)
            if name == "title" and meta.title is None:
                meta.title = _text(child)
                title_el = child if meta.title else None
            elif name == "creator":
                val = _text(child)
                if val:
                    # Prefer aut role when present, but keep order otherwise.
                    role = child.get(f"{{{OPF_NS}}}role")
                    entry = (val, child.get(f"{{{OPF_NS}}}file-as"), child.get("id"))
                    if role == "aut":
                        authors.insert(0, entry)
                    else:
                        authors.append(entry)
            elif name == "publisher" and meta.publisher is None:
                meta.publisher = _text(child)
            elif name == "date" and meta.pub_date is None:
                meta.pub_date = _pub_date(_text(child))
            elif name == "description" and meta.description is None:
                meta.description = _plain_text(child)
            elif name == "language" and meta.language is None:
                meta.language = (_text(child) or "").lower() or None
            elif name == "identifier":
                val = _text(child)
                if val and meta.identifier is None:
                    meta.identifier = val
                if val:
                    scheme = child.get(f"{{{OPF_NS}}}scheme") or child.get("scheme")
                    classified = _classify_identifier(val, scheme, child.get("id"))
                    if classified:
                        kind, clean = classified
                        # First-seen wins per kind (OPF order is meaningful).
                        meta.identifiers.setdefault(kind, clean)
            elif name == "meta":
                mname = child.get("name")
                prop = child.get("property")
                if mname == "cover":
                    cover_meta_id = child.get("content")
                elif mname == "calibre:series" and meta.series is None:
                    meta.series = child.get("content")
                elif mname == "calibre:series_index" and calibre_index is None:
                    calibre_index = child.get("content")
                elif prop:
                    ref = (child.get("refines") or "").lstrip("#")
                    if ref:
                        refines.setdefault((ref, prop), (child.text or "").strip())
                    elif prop == "belongs-to-collection" and _text(child):
                        collections.append((child.get("id"), _text(child)))

    if authors:
        meta.author, author_fa, author_id = authors[0]
        meta.author_file_as = (author_fa or refines.get((author_id or "", "file-as")) or "").strip() or None
        meta.author_reading = _reading(meta.author_file_as)
    if title_el is not None:
        title_fa = title_el.get(f"{{{OPF_NS}}}file-as") or refines.get((title_el.get("id") or "", "file-as"))
        meta.title_reading = _reading(title_fa)

    # Series: calibre's wins; otherwise the first EPUB3 series collection.
    if meta.series is not None:
        meta.series_index = _series_index(calibre_index)
    else:
        for cid, name in collections:
            if refines.get((cid or "", "collection-type"), "series") == "series":
                meta.series = name
                meta.series_index = _series_index(refines.get((cid or "", "group-position")))
                break

    # Resolve the cover image path from the manifest.
    cover_href = _resolve_cover_href(manifest_el, cover_meta_id)
    if cover_href:
        opf_dir = posixpath.dirname(opf_path)
        cover_path = posixpath.normpath(posixpath.join(opf_dir, cover_href))
        try:
            meta.cover_bytes = zf.read(cover_path)
            meta.cover_ext = _ext_for(cover_path)
        except KeyError:
            pass

    return meta


def _resolve_cover_href(manifest_el: ET.Element | None, cover_meta_id: str | None) -> str | None:
    if manifest_el is None:
        return None
    items = [c for c in manifest_el if _localname(c.tag) == "item"]

    # EPUB3: <item properties="cover-image" .../>
    # Skip SVG covers — they're typically tiny wrappers or vector assets
    # that aren't useful as raster thumbnails.
    for item in items:
        props = item.get("properties", "")
        if "cover-image" in props.split():
            media = item.get("media-type", "")
            if not media.startswith("image/svg+xml"):
                return item.get("href")

    # EPUB2: <meta name="cover" content="ID"> → matching manifest item id
    if cover_meta_id:
        for item in items:
            if item.get("id") == cover_meta_id:
                return item.get("href")

    # Last resort: an image item whose id/href mentions "cover"
    for item in items:
        media = item.get("media-type", "")
        if not media.startswith("image/"):
            continue
        if media.startswith("image/svg+xml"):
            continue
        blob = f"{item.get('id', '')} {item.get('href', '')}".lower()
        if "cover" in blob:
            return item.get("href")

    # Fall back to SVG EPUB3 cover-image if nothing else is available.
    for item in items:
        props = item.get("properties", "")
        if "cover-image" in props.split():
            return item.get("href")

    return None


class _FallbackZipReader:
    """Minimal ZIP reader that parses local file headers.

    Used when the central directory is missing or corrupt (truncated ZIPs).
    Only methods needed by ``_find_opf_path`` and ``_parse_opf`` are provided.
    """

    def __init__(self, path: Path) -> None:
        self._files: dict[str, bytes] = {}
        data = path.read_bytes()
        pos = 0
        while True:
            idx = data.find(b"PK\x03\x04", pos)
            if idx == -1:
                break
            if idx + 30 > len(data):
                break
            _sig, _ver, flags, method, _mod_time, _mod_date, _crc, comp_size, _uncomp_size, name_len, extra_len = struct.unpack(
                "<IHHHHHIIIHH", data[idx : idx + 30]
            )
            name = data[idx + 30 : idx + 30 + name_len].decode("utf-8", errors="replace")
            data_start = idx + 30 + name_len + extra_len
            comp_data = data[data_start : data_start + comp_size]

            if method == 8:
                try:
                    raw = zlib.decompress(comp_data)
                except Exception:
                    try:
                        raw = zlib.decompress(comp_data, -15)
                    except Exception:
                        raw = None
            elif method == 0:
                raw = comp_data
            else:
                raw = None

            if raw is not None:
                self._files[name] = raw
            pos = data_start + comp_size

    def namelist(self) -> list[str]:
        return list(self._files.keys())

    def open(self, name: str) -> io.BytesIO:
        return io.BytesIO(self._files[name])

    def read(self, name: str) -> bytes:
        return self._files[name]


def extract(path: Path) -> EpubMeta:
    """Return EpubMeta for the EPUB at `path`. Never raises on bad input."""
    try:
        with zipfile.ZipFile(path) as zf:
            opf_path = _find_opf_path(zf)
            if not opf_path:
                return EpubMeta()
            return _parse_opf(zf, opf_path)
    except (zipfile.BadZipFile, OSError):
        pass

    try:
        zf = _FallbackZipReader(path)
        opf_path = _find_opf_path(zf)
        if not opf_path:
            return EpubMeta()
        return _parse_opf(zf, opf_path)
    except Exception:
        return EpubMeta()
