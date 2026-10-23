"""Carry hand-curated state across a book_id change.

book_id = sha1(rel_path + filesize), so renaming a file, moving it between
folders or replacing it with a corrected copy gives it a new id, orphaning
everything keyed by the old one. Two ways back, both here:

- match_moves() + rekey_book(): the scanner pairs each vanished entry with a
  freshly added one (same path, asin/isbn, file name + size, OPF
  identifier, or title+author, strongest first) and moves
  the old id's state onto the new id before dropping the old one.
- recover_orphans(): a one-off sweep for ids already orphaned. The old
  metadata is gone, so it only recovers the case it can reconstruct: the same
  filename and size under a different folder (a "tidy the root into
  Japanese/" move) by recomputing the old book_id.

Moved: read status in every profile, title/series/genre/cover overrides (and
the hand-set cover image), the Natively match. pairing_overrides.json is keyed by stable key and needs
nothing. Never overwrites state already present for the new id.

The caller holds `server._index_lock` (or is a pipeline stage that does).
"""
from __future__ import annotations

from pathlib import Path

from .config import Config
from .index_store import load_json, save_json

# {book_id: value} dicts under data_dir whose entries should follow a book.
_INDEXES = ("title_overrides", "series_overrides", "genre_overrides",
            "author_overrides", "language_overrides",
            "audiobook_link_overrides", "cover_overrides", "natively_index")


def _rekey_in(path: Path, old: str, new: str, *, nested: str | None = None) -> bool:
    if not path.exists():
        return False
    data = load_json(path, {})
    target = data.get(nested) if nested else data
    if not isinstance(target, dict) or old not in target:
        return False
    value = target.pop(old)
    target.setdefault(new, value)
    save_json(path, data)
    return True


def _user_dirs(cfg: Config) -> list[Path]:
    return sorted(p for p in cfg.users_dir.glob("*") if p.is_dir())


def rekey_book(cfg: Config, old: str, new: str) -> list[str]:
    """Move everything curated under `old` to `new`. Returns the stores touched."""
    if old == new:
        return []
    touched = []
    for attr in _INDEXES:
        path = getattr(cfg, attr)
        if _rekey_in(path, old, new):
            touched.append(path.name)
    for d in _user_dirs(cfg):
        if _rekey_in(d / "read_books.json", old, new, nested="books"):
            touched.append(f"users/{d.name}/read_books.json")
    # The hand-set cover image follows its flag. Before the caller purges
    # the old id; never overwrites a cover the new id already has.
    for src in cfg.custom_covers_dir.glob(f"{old}.*"):
        dst = src.with_name(new + src.suffix)
        if not dst.exists():
            src.rename(dst)
            touched.append(f"covers/custom/{dst.name}")
    # The alignment SRTs follow too; the map is derived and is re-matched
    # from them on the next request, so it is left for purge to drop.
    for src in cfg.alignments_dir.glob(f"{old}.*srt"):
        dst = src.with_name(new + src.name[len(old):])
        if not dst.exists():
            src.rename(dst)
            touched.append(f"alignments/{dst.name}")
    return touched


def _identity_keys(entry: dict) -> list[tuple]:
    """Signals that the same book was re-added under another id, strongest first."""
    ids = entry.get("identifiers") or {}
    keys = [("path", entry.get("path"))]
    for k in ("asin", "isbn"):
        if ids.get(k):
            keys.append((k, ids[k]))
    if entry.get("path") and entry.get("filesize"):
        keys.append(("file", (Path(entry["path"]).name, entry["filesize"])))
    if entry.get("opf_identifier"):
        keys.append(("opf", entry["opf_identifier"]))
    title = (entry.get("raw_title") or entry.get("title") or "").strip().lower()
    # raw_*: what the file says, not what a hand correction made of it (build()
    # overwrites author/language with the override and keeps the original here).
    author = (entry["raw_author"] if "raw_author" in entry else entry.get("author") or "").strip().lower()
    language = entry["raw_language"] if "raw_language" in entry else entry.get("language")
    if title and author:
        keys.append(("title+author", (language, title, author)))
    return keys


_KINDS = ("path", "asin", "isbn", "file", "opf", "title+author")


def match_moves(vanished: dict[str, dict], fresh: dict[str, dict], *,
                keys=_identity_keys, kinds: tuple[str, ...] = _KINDS) -> dict[str, str]:
    """Pair vanished entries with freshly added ones: {old_id: new_id}.

    Tier by tier (path, asin, isbn, file name+size, OPF identifier,
    title+author), a pair is made only when the signal picks out exactly one vanished and one fresh entry among those still
    unmatched — an ambiguous signal (a series sharing an author and title, two
    files with one ISBN) migrates nothing rather than guessing. `keys` and
    `kinds` swap in other signals (the audiobook scan uses its own)."""
    matches: dict[str, str] = {}
    for kind in kinds:
        by_old: dict = {}
        by_new: dict = {}
        for table, entries in ((by_old, vanished), (by_new, fresh)):
            for bid, entry in entries.items():
                if bid in matches or bid in matches.values():
                    continue
                for k, v in keys(entry):
                    if k == kind:
                        table.setdefault(v, []).append(bid)
        for key, olds in by_old.items():
            news = by_new.get(key, [])
            if len(olds) == 1 and len(news) == 1:
                matches[olds[0]] = news[0]
    return matches


def recover_orphans(cfg: Config, library: dict) -> dict[str, str]:
    """One-off: re-attach curated state orphaned by a folder move. Returns
    {old_id: new_id} for what was recovered."""
    from .scanner import book_id_for  # scanner imports this module

    known: set[str] = set()
    for attr in _INDEXES:
        known |= set(load_json(getattr(cfg, attr), {}))
    for d in _user_dirs(cfg):
        known |= set((load_json(d / "read_books.json", {}).get("books")) or {})
    orphans = known - set(library)
    if not orphans:
        return {}

    # The old path is unknown, but the file name and size survive a move, so try
    # it under every folder the library has. (A rename can't be recovered.)
    folders = {""} | {
        p.relative_to(cfg.epub_dir).as_posix() + "/"
        for p in cfg.epub_dir.rglob("*") if p.is_dir()
    }
    recovered: dict[str, str] = {}
    for new_id, entry in library.items():
        name = Path(entry["path"]).name
        for folder in folders:
            old_id = book_id_for(folder + name, entry["filesize"])
            if old_id in orphans and old_id not in recovered:
                rekey_book(cfg, old_id, new_id)
                recovered[old_id] = new_id
    return recovered
