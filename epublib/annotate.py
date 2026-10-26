"""Reader packs: what the phone needs to colour, annotate and hit-test the words
of one Japanese book, with no tokenizer on the device.

For each spine document the pack holds token offsets into the text the
browser's DOM walk sees (epub_text.dom_text), UTF-16 code units so they index a
JS string. Each token points at an interned word with the same keys the
library counts and matches on (analyze.word_def: NFKC orthBase and UniDic
lemma), so a word is known on the phone exactly when it is known on the
server. The pack is profile-independent: word status is resolved on the phone
from a vocab snapshot, so marking a word recolours the page offline.

    {"schema": 1, "book_id": …, "sha256": …, "stable_key": "asin:…",
     "docs": [{"index": 3, "href": "OEBPS/text/p-003.xhtml", "len": 12345,
               "fnv": "9a3f01bc", "ok": true,
               "sents":  [[start, end], …],
               "tokens": [[start, len, wid], …]}, …],
     "words": [[orth, lemma, kana, kanaBase, ruby, free, kind, pos1, goshu], …]}

- `fnv` is epub_text.fnv1a_utf16 of the document's text, `len` its UTF-16
  length: the phone checks both before placing a token. `ok` is False when the
  document wasn't well-formed XML (the phone may then disagree with the walk).
- A `wid` interns a distinct word row plus its surface, because the reading
  varies by inflection (and one orth/lemma can be both content and function). `kana` is the surface reading (ruby, TTS), `kanaBase`
  the dictionary-form reading (lookup, Anki), both hiragana.
- `ruby` covers the whole surface: 食べた → [["食","た"],["べた",""]]. Kana-only
  words have none ([]). When the reading can't be aligned to the kanji, the
  whole reading goes over the whole surface.
- `free`: 1 for proper nouns, numerals and affixes. `kind`: "content" or
  "function" (助詞/助動詞). Punctuation is not a token.

The pack is cached at data/annotations/{book_id}.json.gz, written atomically
and rebuilt when ANNOTATE_SCHEMA or the book's sha256 changes. The first bytes
of the JSON are always schema, book_id, sha256, so freshness is checked
without inflating the whole file.
"""
from __future__ import annotations

import gzip
import itertools
import json
import re
import threading
import zipfile
from pathlib import Path

from . import analyze, epub_text, pairing
from .config import Config
from .epub_meta import _find_opf_path
from .index_store import load_json, sha256_of, write_bytes_atomic

ANNOTATE_SCHEMA = 1


class NoPack(Exception):
    """The book can't have a pack. `reason`: drm | not_ja | no_text."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ── readings ─────────────────────────────────────────────────────────────────

def _to_hira(s: str) -> str:
    return "".join(chr(ord(c) - 0x60) if 0x30A1 <= ord(c) <= 0x30F6 else c for c in s)


def _is_kanji(ch: str) -> bool:
    return analyze._is_kanji(ch) or ch in analyze._KANJI_LIKE


def ruby_for(surface: str, kana: str) -> list[list[str]]:
    """`[[base, reading], …]` covering all of `surface`, from its katakana
    reading. Kana runs between the kanji come out with an empty reading."""
    if not kana or not any(_is_kanji(c) for c in surface):
        return []
    pairs = analyze._align_readings(surface, kana, True)
    if not pairs:
        return [[surface, _to_hira(kana)]]
    readings = iter(r for _, r in pairs)
    out: list[list[str]] = []
    for is_kanji, chars in itertools.groupby(surface, key=_is_kanji):
        text = "".join(chars)
        out.append([text, _to_hira(next(readings)) if is_kanji else ""])
    return out


# ── building ─────────────────────────────────────────────────────────────────

def book_sha(cfg: Config, book: dict) -> str:
    return book.get("sha256") or sha256_of(cfg.epub_dir / book["path"])


def build_pack(cfg: Config, book: dict) -> dict:
    """The pack of one library entry (the full record: `identifiers` feeds the
    stable key). Raises NoPack for DRM, non-Japanese and text-less books."""
    if book.get("language") != "ja":
        raise NoPack("not_ja")
    path = cfg.epub_dir / book["path"]
    docs: list[dict] = []
    word_ids: dict[tuple, int] = {}
    words: list[list] = []
    chars = 0
    try:
        with zipfile.ZipFile(path) as zf:
            if epub_text._is_drm_protected(zf):
                raise NoPack("drm")
            opf = _find_opf_path(zf)
            for item in epub_text.spine_items(zf, opf) if opf else []:
                try:
                    raw = zf.read(item.href)
                except KeyError:
                    continue
                doc = _build_doc(item, raw, word_ids, words)
                chars += doc.pop("chars")
                docs.append(doc)
    except (zipfile.BadZipFile, OSError):
        raise NoPack("no_text") from None
    if chars < analyze.MIN_WORD_CHARS:
        raise NoPack("no_text")
    # Key order matters: cached_header() reads the first three keys.
    return {
        "schema": ANNOTATE_SCHEMA,
        "book_id": book["book_id"],
        "sha256": book_sha(cfg, book),
        "stable_key": pairing.stable_key(book),
        "docs": docs,
        "words": words,
    }


def _sentence_spans(dt: epub_text.DomText) -> list[tuple[int, int, int, int]]:
    """`(s_start, s_end, d0, d1)` per sentence: its span in `dt.analysis` and in
    `dt.text`. The one place sentences are cut, shared by the pack and by
    `sentence_texts`, so an alignment lines up with the pack's `sents[i]`."""
    return [(a, b, *dt.span_to_dom(a, b))
            for a, b in epub_text.split_sentences_with_spans(dt.analysis)]


def sentence_texts(cfg: Config, book: dict) -> list[dict]:
    """`[{index, fnv, texts}]` per spine document, `texts[i]` being the DOM text
    of the pack's `sents[i]`. No tokenizer, so it is cheap. Same refusals
    as `build_pack`: NoPack for non-Japanese, DRM and text-less books."""
    if book.get("language") != "ja":
        raise NoPack("not_ja")
    docs: list[dict] = []
    chars = 0
    try:
        with zipfile.ZipFile(cfg.epub_dir / book["path"]) as zf:
            if epub_text._is_drm_protected(zf):
                raise NoPack("drm")
            opf = _find_opf_path(zf)
            for item in epub_text.spine_items(zf, opf) if opf else []:
                try:
                    raw = zf.read(item.href)
                except KeyError:
                    continue
                dt = epub_text.dom_text(raw)
                chars += epub_text.count_chars(dt.text)
                docs.append({"index": item.index, "fnv": epub_text.fnv1a_utf16(dt.text),
                             "texts": [dt.text[d0:d1] for _, _, d0, d1 in _sentence_spans(dt)]})
    except (zipfile.BadZipFile, OSError):
        raise NoPack("no_text") from None
    if chars < analyze.MIN_WORD_CHARS:
        raise NoPack("no_text")
    return docs


def _build_doc(item, raw: bytes, word_ids: dict, words: list) -> dict:
    dt = epub_text.dom_text(raw)
    u16 = epub_text.utf16_offsets(dt.text)
    sents: list[list[int]] = []
    tokens: list[list[int]] = []
    for s_start, s_end, d0, d1 in _sentence_spans(dt):
        sents.append([u16[d0], u16[d1]])
        for t_start, t_end, tok in analyze.iter_tokens(dt.analysis[s_start:s_end]):
            d = analyze.word_def(tok)
            if d is None:
                continue
            a, b = dt.span_to_dom(s_start + t_start, s_start + t_end)
            surface = tok.surface
            # The whole row is the key, not just (surface, orth, lemma, kana):
            # the same four can carry different classes (a content より and a
            # particle one), and merging them would change the counts.
            row = (
                d.base, d.lemma, _to_hira(d.kana), _to_hira(d.reading),
                1 if d.free else 0, d.kind, tok.feature.pos1, d.goshu,
            )
            key = (surface, *row)
            wid = word_ids.get(key)
            if wid is None:
                wid = word_ids[key] = len(words)
                words.append([*row[:4], ruby_for(surface, d.kana), *row[4:]])
            tokens.append([u16[a], u16[b] - u16[a], wid])
    return {
        "index": item.index,
        "href": item.href,
        "len": u16[-1],
        "fnv": epub_text.fnv1a_utf16(dt.text),
        "ok": dt.ok,
        "sents": sents,
        "tokens": tokens,
        "chars": epub_text.count_chars(dt.text),  # popped by the caller
    }


# ── cache ────────────────────────────────────────────────────────────────────

def pack_path(cfg: Config, book_id: str) -> Path:
    return cfg.annotations_dir / f"{book_id}.json.gz"


def etag(sha256: str) -> str:
    return f'"{ANNOTATE_SCHEMA}-{sha256[:16]}"'


_HEADER = re.compile(r'\{"schema":(\d+),"book_id":"[^"]*","sha256":"([0-9a-f]*)"')


def cached_header(path: Path) -> tuple[int, str] | None:
    """`(schema, sha256)` of a stored pack, or None when missing or unreadable."""
    try:
        with gzip.open(path, "rb") as f:
            head = f.read(400).decode("utf-8", "replace")
    except (OSError, EOFError):
        return None
    m = _HEADER.match(head)
    return (int(m.group(1)), m.group(2)) if m else None


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _book_lock(book_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(book_id, threading.Lock())


def ensure_pack(cfg: Config, book: dict, *, force: bool = False) -> Path:
    """The stored pack for `book`, built first if it is missing or stale. One
    build per book at a time, so a second request waits and then reads the
    result. Does not touch `server._index_lock`."""
    path = pack_path(cfg, book["book_id"])
    sha = book_sha(cfg, book)
    with _book_lock(book["book_id"]):
        if not force and cached_header(path) == (ANNOTATE_SCHEMA, sha):
            return path
        pack = build_pack(cfg, {**book, "sha256": sha})
        blob = json.dumps(pack, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        write_bytes_atomic(path, gzip.compress(blob, 6))
    return path


def build_all(cfg: Config, refresh: bool = False) -> None:
    """The `annotate` stage: a pack for every Japanese book that can have one,
    and none for books that left the library."""
    library: dict = load_json(cfg.library_index, {})
    books = sorted((b for b in library.values() if b.get("language") == "ja"),
                   key=lambda b: b.get("path", ""))
    built = refused = 0
    for book in books:
        title = (book.get("title") or book["book_id"])[:60]
        try:
            ensure_pack(cfg, book, force=refresh)
            built += 1
        except NoPack as e:
            refused += 1
            print(f"  ∅ {title} ({e.reason})")
        except Exception as exc:  # one broken book never aborts the run
            refused += 1
            print(f"  ✗ {title}: {type(exc).__name__}: {exc}")
    removed = 0
    if cfg.annotations_dir.is_dir():
        for f in cfg.annotations_dir.glob("*.json.gz"):
            if f.name[: -len(".json.gz")] not in library:
                f.unlink(missing_ok=True)
                removed += 1
    print(f"\nAnnotate complete: {built} packs, {refused} refused, {removed} removed.")
