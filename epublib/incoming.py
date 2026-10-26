"""Filing a newly uploaded/downloaded EPUB into the library.

Both `/api/upload` and `/api/acquire` end here. Given the raw bytes it reads the
OPF metadata first, decides the language folder from it (`lang.detect`, the same
call the scanner makes afterwards, so a book is filed where the scan would
classify it), refuses duplicates, and writes the file — it does not scan or
build; the caller does that under its own lock.
"""
from __future__ import annotations

import hashlib
import re
import tempfile
from pathlib import Path

from . import epub_meta, lang
from .config import Config
from .index_store import sha256_of
from .textnorm import author_key, compact_key, display_title

MAX_BYTES = 100 * 1024 * 1024

# Language → folder under epub_dir. "other" (and anything unknown) stays in the root.
_LANG_DIRS = {"ja": "Japanese", "en": "English"}

# Identifier kinds that name a product; a calibre uuid is minted per conversion,
# so it says nothing about whether two files are the same book.
_PRODUCT_IDS = ("isbn", "asin")


class DuplicateEpub(Exception):
    def __init__(self, book_id: str, title: str, reason: str):
        super().__init__(reason)
        self.book_id, self.title, self.reason = book_id, title, reason


class UnsafeFilename(ValueError):
    pass


def sanitize_filename(name: str | None) -> str:
    name = Path(name or "book.epub").name  # strip any directory components
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip("._") or "book"
    if not name.lower().endswith(".epub"):
        name += ".epub"
    return name


def find_duplicate(cfg: Config, library: dict, content: bytes, meta: epub_meta.EpubMeta
                   ) -> DuplicateEpub | None:
    """The library entry `content` duplicates, if any: identical bytes, then a
    shared ISBN/ASIN, then the same cleaned title + author."""
    digest = hashlib.sha256(content).hexdigest()
    for bid, entry in library.items():
        stored = entry.get("sha256")
        if stored is None and entry.get("filesize") == len(content):
            # Entry predates the hash column: only hash files that could match.
            try:
                stored = sha256_of(cfg.epub_dir / entry["path"])
            except (OSError, KeyError):
                stored = None
        if stored == digest:
            return DuplicateEpub(bid, entry.get("title") or bid, "identical file")

    ids = {(k, v) for k, v in (meta.identifiers or {}).items() if k in _PRODUCT_IDS and v}
    if ids:
        for bid, entry in library.items():
            theirs = {(k, v) for k, v in (entry.get("identifiers") or {}).items() if k in _PRODUCT_IDS}
            if ids & theirs:
                kind = sorted(ids & theirs)[0][0].upper()
                return DuplicateEpub(bid, entry.get("title") or bid, f"same {kind}")

    title, author = compact_key(display_title(meta.title or "")), author_key(meta.author)
    if title and author:
        for bid, entry in library.items():
            if (compact_key(display_title(entry.get("raw_title") or entry.get("title") or "")) == title
                    and author_key(entry["raw_author"] if "raw_author" in entry else entry.get("author")) == author):
                return DuplicateEpub(bid, entry.get("title") or bid, "same title and author")
    return None


def store_incoming_epub(cfg: Config, library: dict, content: bytes, filename: str | None,
                        language: str = "auto", force: bool = False) -> Path:
    """Write `content` into the library and return its path.

    `language` is "ja"/"en" to file explicitly, anything else to detect it from
    the file's own metadata. Raises DuplicateEpub unless `force`, UnsafeFilename,
    ValueError (over the size cap) or OSError (write failed).
    """
    if len(content) > MAX_BYTES:
        raise ValueError("File exceeds the 100 MB limit")
    filename = sanitize_filename(filename)

    # extract() wants a path; keep the scratch copy out of epub_dir so a scan
    # can never pick it up.
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe.epub"
        probe.write_bytes(content)
        meta = epub_meta.extract(probe)

    if not force:
        dup = find_duplicate(cfg, library, content, meta)
        if dup:
            raise dup

    if language not in _LANG_DIRS:
        language, _ = lang.detect(filename, meta.language)
    save_dir = cfg.epub_dir / _LANG_DIRS[language] if language in _LANG_DIRS else cfg.epub_dir
    save_dir.mkdir(parents=True, exist_ok=True)

    # Never overwrite: "Add anyway" of a same-named file becomes name_1.epub.
    if (save_dir / filename).exists():
        stem, i = filename[:-5], 1
        while (save_dir / f"{stem}_{i}.epub").exists():
            i += 1
        filename = f"{stem}_{i}.epub"

    save_path = (save_dir / filename).resolve()
    if cfg.epub_dir.resolve() not in save_path.parents:
        raise UnsafeFilename("Unsafe filename — rejected")
    save_path.write_bytes(content)
    return save_path
