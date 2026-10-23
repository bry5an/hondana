"""Remove everything keyed by a book_id once the book is gone.

Called from the delete endpoint and from the scanner when a file vanishes, so
neither leaves orphans behind. Two tiers:

- derived data (cover/thumbnail, wordlist, sentence index, reader pack, vocab entry,
  Natively match, audiobook link, each profile's known_stats entry) is rebuilt
  by the pipeline, so it goes on every removal.
- curated state (read status in every profile, title/series/genre/cover
  overrides, the hand-set cover image and the audiobook alignment SRTs) is hand-made. It is dropped only on an explicit delete (`curated=True`). A file
  the scanner merely stopped seeing may have been renamed or moved (new
  book_id), and `rekey`'s move detection needs the old id's curated state to
  carry it over — so the scanner leaves it alone.

`pairing_overrides.json` is deliberately untouched: it is keyed by stable key
(asin/isbn/path), which is what lets a re-added file re-pair.

The caller holds `server._index_lock` (or is a pipeline stage that does), and
afterwards runs `known.recompute_all()` if read-book exposure may have changed.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from . import align, thumbs
from .config import Config
from .index_store import load_json, save_json

# {book_id: value} dicts under data_dir. Derived — rebuilt by enrich/xref.
_DERIVED_INDEXES = ("natively_index", "vocab_index", "audiobook_links")
# Hand-curated {book_id: value} dicts under data_dir.
_CURATED_INDEXES = ("title_overrides", "series_overrides", "genre_overrides",
                    "author_overrides", "language_overrides",
                    "audiobook_link_overrides", "cover_overrides")


def _drop_key(path: Path, book_id: str, *, nested: str | None = None) -> bool:
    """Remove `book_id` from the JSON dict at `path` (or from its `nested`
    sub-dict). Writes only if something was removed. Returns whether it was."""
    if not path.exists():
        return False
    data = load_json(path, {})
    target = data.get(nested) if nested else data
    if not isinstance(target, dict) or book_id not in target:
        return False
    del target[book_id]
    save_json(path, data)
    return True


def purge_book(cfg: Config, book_id: str, *, cover: str | None = None,
               curated: bool = False) -> list[str]:
    """Delete everything keyed by `book_id`. `cover` is the entry's
    data_dir-relative cover path (the library entry may already be gone).
    Returns the names of the stores that held something, for reporting."""
    touched: list[str] = []

    for rel in filter(None, [cover]):
        try:
            (cfg.data_dir / rel).resolve().unlink(missing_ok=True)
        except OSError:
            pass
    for f in (
        thumbs.thumb_path(cfg.data_dir, book_id),
        cfg.wordlists_dir / f"{book_id}.json",
        cfg.sentences_dir / f"{book_id}.json.gz",
        cfg.annotations_dir / f"{book_id}.json.gz",
    ):
        if f.exists():
            f.unlink(missing_ok=True)
            touched.append(f.parent.name)

    # The alignment map is derived; the SRT under it is expensive to make again
    # (a Whisper run elsewhere), so it goes with the curated state.
    for f in cfg.alignments_dir.glob(f"{book_id}.*"):
        if curated or f.suffix == ".json":
            f.unlink(missing_ok=True)
            touched.append("alignments")
    if curated:  # half-finished SubPlz run
        shutil.rmtree(align.work_dir(cfg, book_id), ignore_errors=True)

    for attr in _DERIVED_INDEXES:
        path = getattr(cfg, attr)
        if _drop_key(path, book_id):
            touched.append(path.name)

    user_dirs = sorted(p for p in cfg.users_dir.glob("*") if p.is_dir())
    for d in user_dirs:
        if _drop_key(d / "known_stats.json", book_id):
            touched.append(f"users/{d.name}/known_stats.json")

    if curated:
        for attr in _CURATED_INDEXES:
            path = getattr(cfg, attr)
            if _drop_key(path, book_id):
                touched.append(path.name)
        for custom in cfg.custom_covers_dir.glob(f"{book_id}.*"):
            custom.unlink(missing_ok=True)
            touched.append("covers/custom")
        for d in user_dirs:
            if _drop_key(d / "read_books.json", book_id, nested="books"):
                touched.append(f"users/{d.name}/read_books.json")

    return touched
