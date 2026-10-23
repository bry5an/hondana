"""Walk the EPUB folder and build/maintain epub_library.json.

Incremental and resume-safe: an entry whose file is unchanged (same filesize +
mtime) is skipped without re-parsing. Entries whose files have disappeared are
dropped. Cover images are extracted to data/covers/{book_id}.{ext}.

book_id is a stable surrogate key derived from the relative path + filesize so
the same file keeps the same id (and cover filename) across runs.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from . import epub_meta, lang
from . import purge as purge_mod
from . import rekey as rekey_mod
from .config import Config
from .index_store import load_json, save_json

SAVE_EVERY = 50
# Bump when the per-entry schema changes so a re-scan backfills new fields on
# unchanged files instead of skipping them. v2 added `identifiers`, v3 `sha256`
# (what /add compares an incoming upload against to catch re-uploads). v4 added
# the extra OPF fields (readings, publisher, date, description, series index).
SCHEMA_VERSION = 4


def book_id_for(rel_path: str, filesize: int) -> str:
    h = hashlib.sha1(f"{rel_path}\0{filesize}".encode("utf-8"))
    return h.hexdigest()[:16]


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_junk(path: Path) -> bool:
    # Skip macOS AppleDouble sidecars (`._Foo.epub`) and .DS_Store. These appear
    # for every real file on macOS-written shares (e.g. the server's mounted
    # epub volume) and would otherwise be ingested as cover-less ghost entries.
    return path.name.startswith("._") or path.name == ".DS_Store"


def _iter_epubs(epub_dir: Path):
    for path in sorted(epub_dir.rglob("*.epub")):
        if path.is_file() and not _is_junk(path):
            yield path


def _clear_old_covers(covers_dir: Path, book_id: str) -> None:
    for old in covers_dir.glob(f"{book_id}.*"):
        try:
            old.unlink()
        except OSError:
            pass


def _cover_is_svg(cover_path: Path) -> bool:
    """Check if a cover file is SVG data (even if the extension is wrong).

    Books scanned before the SVG fix may have SVG covers saved with
    .jpg/.png extensions. These need re-extraction to get the raster cover.
    """
    if cover_path.suffix.lower() == ".svg":
        return True
    try:
        with open(cover_path, "rb") as f:
            header = f.read(20)
        return header.lstrip().startswith(b"<?xml") or b"<svg" in header
    except OSError:
        return False


def scan(cfg: Config) -> dict:
    if not cfg.epub_dir.exists():
        raise SystemExit(f"epub_dir does not exist: {cfg.epub_dir}")

    cfg.covers_dir.mkdir(parents=True, exist_ok=True)
    index: dict = load_json(cfg.library_index, {})

    seen_ids: set[str] = set()
    processed = 0
    added = 0
    updated = 0
    added_ids: list[str] = []

    for path in _iter_epubs(cfg.epub_dir):
        stat = path.stat()
        rel = path.relative_to(cfg.epub_dir).as_posix()
        bid = book_id_for(rel, stat.st_size)
        seen_ids.add(bid)

        existing = index.get(bid)
        # The cover file referenced by a cached entry must still exist on disk;
        # data/covers/ is gitignored, so a server seeded with an index but no
        # covers would otherwise keep dangling references and never re-extract.
        # Only consulted for a cached entry — a brand-new file (every upload) has
        # no `existing` at all.
        cover_ok = True
        # raw_cover, not cover: a hand-set cover replaces `cover` in build(),
        # but it is the EPUB's own extraction that this check is about.
        own_cover = existing.get("raw_cover", existing.get("cover")) if existing else None
        if own_cover:
            cover_path = cfg.data_dir / own_cover
            cover_ok = cover_path.exists() and not _cover_is_svg(cover_path)
        if (
            existing
            and existing.get("mtime") == stat.st_mtime
            and existing.get("filesize") == stat.st_size
            and existing.get("schema") == SCHEMA_VERSION
            and cover_ok
        ):
            continue  # unchanged, current schema, cover present → skip

        meta = epub_meta.extract(path)
        title = meta.title or path.stem
        language, lang_source = lang.detect(path.name, meta.language)

        cover_rel = None
        if meta.cover_bytes:
            _clear_old_covers(cfg.covers_dir, bid)
            ext = meta.cover_ext or ".jpg"
            cover_file = cfg.covers_dir / f"{bid}{ext}"
            try:
                cover_file.write_bytes(meta.cover_bytes)
                cover_rel = f"covers/{bid}{ext}"
            except OSError as exc:
                print(f"    cover write failed for {rel}: {exc}")

        index[bid] = {
            "book_id": bid,
            "schema": SCHEMA_VERSION,
            "path": rel,
            "filesize": stat.st_size,
            "mtime": stat.st_mtime,
            "sha256": sha256_of(path),
            "title": title,
            "raw_title": title,
            "author": meta.author,
            "language": language,
            "lang_source": lang_source,
            "opf_identifier": meta.identifier,
            "identifiers": meta.identifiers,
            "series": meta.series,
            "series_index": meta.series_index,
            "title_reading": meta.title_reading,
            "author_reading": meta.author_reading,
            "author_file_as": meta.author_file_as,
            "publisher": meta.publisher,
            "pub_date": meta.pub_date,
            "description": meta.description,
            "cover": cover_rel,
            "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

        if existing:
            updated += 1
            tag = "↺"
        else:
            added += 1
            added_ids.append(bid)
            tag = "+"
        processed += 1
        print(f"  {tag} [{language}] {title[:60]}")

        if processed % SAVE_EVERY == 0:
            save_json(cfg.library_index, index)

    # Drop entries whose files vanished.
    removed = [bid for bid in index if bid not in seen_ids]
    vanished = {bid: index.pop(bid) for bid in removed}
    # A "vanished" file may just have been renamed, moved or replaced (new
    # book_id): carry its curated state over to the matching new entry.
    moves = rekey_mod.match_moves(vanished, {bid: index[bid] for bid in added_ids})
    for old, new in moves.items():
        rekey_mod.rekey_book(cfg, old, new)
        print(f"  ⇄ {vanished[old].get('title', old)[:50]}: kept state across move")
    for bid, entry in vanished.items():
        # Derived data only; curated state was either just moved above or is
        # kept in case the file comes back (an explicit delete is what purges it).
        # raw_cover: the EPUB's own extraction — a hand-set cover is kept with its flag.
        purge_mod.purge_book(cfg, bid, cover=entry.get("raw_cover", entry.get("cover")))

    save_json(cfg.library_index, index)
    print(
        f"\nScan complete: {len(index)} books "
        f"({added} added, {updated} updated, {len(removed)} removed)."
    )
    return index
