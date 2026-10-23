"""Scan audiobook directories and build the served audiobook index.

Two stages, mirroring the scanner.py / build.py split:

`scan` walks every root in cfg.audiobook_dirs and records disk facts in
audiobook_files.json. Entries are keyed by the Audible ISBN embedded in the
filename (`[B0XXXXXXXX]`) when present, else a surrogate hash. A root that is
not mounted (e.g. the SSD is unplugged) keeps its existing entries — only
files that vanished from a *mounted* root are dropped.

`build_index` joins the disk scan with the audiobook catalog (data.json,
read-only) and Natively difficulty data into audiobook_library.json: one entry
per catalog ISBN (local or not) plus one per local file the catalog doesn't
know about.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

from . import romaji
from .config import Config
from .index_store import load_json, save_json

SCHEMA_VERSION = 1
AUDIBLE_PD = "https://www.audible.co.jp/pd/"

ISBN_RE = re.compile(r"\[(B0[A-Z0-9]{8})\]")
AUDIBLE_KEY_RE = re.compile(r"B0[A-Z0-9]{8}")
# Catalog keys embedded in filenames: Audible ISBNs plus the non-Audible
# source tags data.json also uses as keys ("kikubon 389", "audiobook.jp 1234").
CATALOG_KEY_RE = re.compile(
    r"\[(B0[A-Z0-9]{8}|(?:kikubon|audiobook\.jp|dwango) \d+)\]"
)
# "[01] Title" or "[1巻] Title" volume prefixes used by the collection.
VOL_PREFIX_RE = re.compile(r"^\[(\d+)(?:巻)?\]\s*")
AUTHOR_DIR_RE = re.compile(r"^\[(?P<author>.+?)\]\s*(?P<series>.+)$")


def _is_junk(path: Path) -> bool:
    return path.name.startswith("._") or path.name == ".DS_Store"


def ab_id_for(rel_path: str, filesize: int) -> str:
    # "f" prefix keeps surrogate ids out of the ISBN namespace.
    h = hashlib.sha1(f"{rel_path}\0{filesize}".encode("utf-8"))
    return "f" + h.hexdigest()[:15]


def _entry(root: Path, rel: str, name: str, filesize: int, mtime: float,
           fmt: str, files: list[str] | None) -> dict:
    m = CATALOG_KEY_RE.search(name)
    key = m.group(1) if m else None
    return {
        "ab_id": key or ab_id_for(rel, filesize),
        "schema": SCHEMA_VERSION,
        "catalog_key": key,
        "root": str(root),
        "path": rel,
        "filename": name,
        "filesize": filesize,
        "mtime": mtime,
        "format": fmt,
        "files": files,
        "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _scan_root(root: Path) -> list[dict]:
    entries: list[dict] = []
    mp3_dirs: dict[Path, list[Path]] = {}

    for path in sorted(root.rglob("*")):
        if not path.is_file() or _is_junk(path):
            continue
        if path.suffix.lower() == ".m4b":
            stat = path.stat()
            rel = path.relative_to(root).as_posix()
            entries.append(
                _entry(root, rel, path.name, stat.st_size, stat.st_mtime, "m4b", None)
            )
        elif path.suffix.lower() == ".mp3":
            mp3_dirs.setdefault(path.parent, []).append(path)

    # A directory of loose mp3s (and no m4b) is one book.
    for d, mp3s in mp3_dirs.items():
        if any(p.suffix.lower() == ".m4b" for p in d.iterdir() if p.is_file()):
            continue
        rel = d.relative_to(root).as_posix() if d != root else "."
        filesize = sum(p.stat().st_size for p in mp3s)
        mtime = max(p.stat().st_mtime for p in mp3s)
        entries.append(
            _entry(root, rel, d.name, filesize, mtime, "mp3-folder",
                   sorted(p.name for p in mp3s))
        )

    return entries


def scan(cfg: Config) -> dict:
    """Walk cfg.audiobook_dirs and write audiobook_files.json."""
    index: dict = load_json(cfg.audiobook_files_index, {})

    new_index: dict = {}
    for root in cfg.audiobook_dirs:
        if not root.exists():
            kept = {k: e for k, e in index.items() if e.get("root") == str(root)}
            print(f"  ! root not mounted, keeping {len(kept)} cached entries: {root}")
            new_index.update(kept)
            continue
        for entry in _scan_root(root):
            key = entry["ab_id"]
            if key in new_index:
                # Same ISBN under two roots: keep the earlier-listed (canonical
                # first) root and surface the duplicate for cleanup.
                print(f"  ! duplicate {key}: keeping {new_index[key]['path']}, "
                      f"ignoring {entry['root']}/{entry['path']}")
                continue
            new_index[key] = entry

    save_json(cfg.audiobook_files_index, new_index)
    n_key = sum(1 for e in new_index.values() if e.get("catalog_key"))
    print(f"Audiobook scan complete: {len(new_index)} files "
          f"({n_key} with catalog key, {len(new_index) - n_key} without).")
    return new_index


def _title_from_filename(name: str) -> tuple[str, str | None]:
    """Best-effort (title, order) for a file the catalog doesn't know."""
    stem = name[:-4] if name.lower().endswith(".m4b") else name
    order = None
    m = VOL_PREFIX_RE.match(stem)
    if m:
        order = m.group(1).zfill(2)
        stem = stem[m.end():]
    stem = CATALOG_KEY_RE.sub("", stem)
    return re.sub(r"\s+", " ", stem).strip(), order


def _num(value, cast):
    try:
        return cast(value)
    except (TypeError, ValueError):
        return None


def build_index(cfg: Config) -> dict:
    """Join disk scan + catalog + Natively data into audiobook_library.json."""
    files: dict = load_json(cfg.audiobook_files_index, {})
    from . import audiobook_catalog  # late: that module imports this one

    catalog: dict = audiobook_catalog.load(cfg)
    natively: dict = load_json(cfg.audiobook_natively, {})

    # Manually pruned books. The catalog is a scrape of everything the account
    # ever owned, so it carries hundreds of titles the reader has no intention
    # of reading, and deleting a downloaded file only demotes its row to
    # "Audible-only" rather than removing it. Pruning is kept here, as a
    # suppression list applied at join time, instead of by editing data.json —
    # that file is the immutable scrape output, and a re-scrape would undo any
    # edit to it. Hiding is therefore reversible and survives a re-scrape.
    hidden: set = set(load_json(cfg.audiobook_hidden, {}).get("ab_ids") or [])

    out: dict = {}

    for key, ab in catalog.items():
        if key in hidden:
            continue
        local = files.get(key)
        nat = natively.get(key) or {}
        out[key] = {
            "ab_id": key,
            "catalog_key": key,
            "title": ab.get("title"),
            "author": ab.get("author"),
            "author_reading": romaji.sort_reading(ab.get("author")),
            "series": ab.get("series"),
            "series_reading": romaji.sort_reading(ab.get("series")),
            "order": ab.get("order"),
            "local": local is not None,
            "format": local["format"] if local else None,
            "path": local["path"] if local else None,
            "root": local["root"] if local else None,
            "filesize": local["filesize"] if local else None,
            "files": local["files"] if local else None,
            "cover_url": ab.get("cover"),
            "runtime_min": _num(ab.get("runtime_length_min"), int),
            "audible_rating": _num(ab.get("audible_rating"), float),
            "release_date": ab.get("release_date"),
            "genre": ab.get("genre"),
            # Non-Audible sources (kikubon/audiobook.jp keys) have no pd page.
            "audible_url": f"{AUDIBLE_PD}{key}" if AUDIBLE_KEY_RE.fullmatch(key) else None,
            "natively_level": nat.get("natively_level"),
            "natively_url": nat.get("natively_url"),
            "language_learning_rating": nat.get("language_learning_rating"),
            "natively_match_type": nat.get("match_type"),
            "natively_confidence": nat.get("confidence"),
            "natively_matched_title": nat.get("matched_title"),
        }

    # Local files the catalog doesn't know about (kikubon rips etc.).
    for key, entry in files.items():
        if key in out or key in hidden:
            continue
        title, order = _title_from_filename(entry["filename"])
        author = series = None
        parent = Path(entry["path"]).parent.name
        m = AUTHOR_DIR_RE.match(parent)
        if m:
            author, series = m.group("author"), m.group("series")
        out[key] = {
            "ab_id": key,
            "catalog_key": entry.get("catalog_key"),
            "title": title,
            "author": author,
            "author_reading": romaji.sort_reading(author),
            "series": series or title,
            "series_reading": romaji.sort_reading(series or title),
            "order": order,
            "local": True,
            "format": entry["format"],
            "path": entry["path"],
            "root": entry["root"],
            "filesize": entry["filesize"],
            "files": entry["files"],
            "cover_url": None,
            "runtime_min": None,
            "audible_rating": None,
            "release_date": None,
            "audible_url": None,
            "natively_level": None,
            "natively_url": None,
            "language_learning_rating": None,
            "natively_match_type": None,
            "natively_confidence": None,
            "natively_matched_title": None,
        }

    save_json(cfg.audiobook_index, out)
    n_local = sum(1 for e in out.values() if e["local"])
    print(f"Audiobook index built: {len(out)} entries "
          f"({n_local} local, {len(out) - n_local} Audible-only"
          f"{f', {len(hidden)} hidden' if hidden else ''}).")
    return out
