"""Scan audiobook directories and build the served audiobook index.

Two stages, mirroring the scanner.py / build.py split:

`scan` walks every root in cfg.audiobook_dirs and records disk facts in
audiobook_files.json. One book is:
- an `.m4b` file;
- a folder's loose `.mp3` files, when the folder has no m4b (format `mp3-folder`);
- a folder's `.m4a`/`.flac`/`.opus`/`.ogg` files that share an album tag, when
  the folder has no m4b: one file is a single-file book (format = its
  extension), several are a `tracks` book.
Dot-folders, NAS housekeeping folders (`@eaDir`, `#recycle`) and partial
downloads are skipped. A root that is not mounted (e.g. the SSD is unplugged)
keeps its existing entries — only files that vanished from a *mounted* root are
dropped. Embedded tags (audio_tags.py) are cached per file in
audiobook_tags.json by size and mtime, so a rescan reads only what changed.

**Ids.** Every entry has a `fid`: `ab_id_for(path, size)` the first time a scan
sees it, then carried forever — across a move, a rename or a re-tag — by
`_carry` (same place; else the same name and size; else the same tag title and
duration; else the same size and the hash of 1 MiB from the middle; each only
when exactly one vanished and one new entry share the signal). Its catalog key
is the `[KEY]` in the file (or folder) name, else the book it was matched to by
hand (`audiobook_assignments.json`, by fid), else the tag ASIN. `ab_id` is the
key, else the fid, so the ids of the owner's `[ASIN]` files are their ASINs, as
before. When a keyless entry gains a key its ab_id changes, and
`audiobook_rekey.rekey_audiobook` moves everything kept under the old one.

`build_index` joins the disk scan with the audiobook catalog (audiobook_catalog,
which includes a row for every local file) and Natively difficulty data into
audiobook_library.json: one entry per catalog row, local or not.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import audible, audio_tags, romaji
from .config import Config
from .index_store import load_json, save_json

SCHEMA_VERSION = 2

ISBN_RE = re.compile(r"\[(B0[A-Z0-9]{8})\]")
AUDIBLE_KEY_RE = audible.ASIN_RE
# Catalog keys embedded in filenames: Audible ISBNs plus the non-Audible
# source tags data.json also uses as keys ("kikubon 389", "audiobook.jp 1234").
CATALOG_KEY_RE = re.compile(
    r"\[(B0[A-Z0-9]{8}|(?:kikubon|audiobook\.jp|dwango) \d+)\]"
)
# "[01] Title" or "[1巻] Title" volume prefixes used by the collection.
VOL_PREFIX_RE = re.compile(r"^\[(\d+)(?:巻)?\]\s*")
AUTHOR_DIR_RE = re.compile(r"^\[(?P<author>.+?)\]\s*(?P<series>.+)$")

TRACK_EXTS = (".m4a", ".flac", ".opus", ".ogg")
SKIP_DIRS = frozenset({"@eaDir", "#recycle", "#snapshot", "$RECYCLE.BIN",
                       "System Volume Information", "lost+found"})
# Still downloading: Safari's `X.download` bundles, browser/torrent temp files.
PARTIAL_SUFFIXES = (".part", ".partial", ".crdownload", ".download", ".tmp", ".!qb", ".aria2")
COVER_NAMES = ("cover", "folder", "front")
COVER_EXTS = (".jpg", ".jpeg", ".png", ".webp")
FP_WINDOW = 1 << 20


def _is_junk(path: Path) -> bool:
    return path.name.startswith("._") or path.name == ".DS_Store"


def _skip_dir(name: str) -> bool:
    return name.startswith(".") or name in SKIP_DIRS or name.lower().endswith(PARTIAL_SUFFIXES)


def _skip_file(name: str) -> bool:
    return name.startswith(".") or name.lower().endswith(PARTIAL_SUFFIXES)


def ab_id_for(rel_path: str, filesize: int) -> str:
    # "f" prefix keeps surrogate ids out of the ISBN namespace.
    h = hashlib.sha1(f"{rel_path}\0{filesize}".encode("utf-8"))
    return "f" + h.hexdigest()[:15]


# ── walking ──────────────────────────────────────────────────────────────────

def _items(root: Path) -> list[dict]:
    """The books under `root` before tags are read: m4b files (in path order),
    then mp3 folders, then folders of other tracks (grouped later)."""
    singles: list[dict] = []
    mp3_dirs: list[dict] = []
    track_dirs: list[dict] = []
    for dirpath, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not _skip_dir(d))
        d = Path(dirpath)
        audio = [d / n for n in sorted(names)
                 if not _skip_file(n) and n.lower().endswith(audio_tags.AUDIO_EXTS)]
        if not audio:
            continue
        rel_dir = d.relative_to(root).as_posix() if d != root else "."
        m4bs = [p for p in audio if p.suffix.lower() == ".m4b"]
        for p in m4bs:
            singles.append({"rel": p.relative_to(root).as_posix(), "name": p.name,
                            "format": "m4b", "paths": [p], "dir": d})
        if m4bs:
            continue  # a folder with an m4b is that book; loose tracks beside it are extras
        mp3s = [p for p in audio if p.suffix.lower() == ".mp3"]
        if mp3s:
            mp3_dirs.append({"rel": rel_dir, "name": d.name, "format": "mp3-folder",
                             "paths": mp3s, "dir": d})
        tracks = [p for p in audio if p.suffix.lower() in TRACK_EXTS]
        if tracks:
            track_dirs.append({"rel": rel_dir, "name": d.name, "paths": tracks, "dir": d})
    # The order the scan always had (sorted paths, m4b files before mp3 folders),
    # which decides which copy of a duplicate is kept.
    singles.sort(key=lambda i: Path(i["rel"]).parts)
    mp3_dirs.sort(key=lambda i: i["paths"][0].relative_to(root).parts)
    return singles + mp3_dirs + track_dirs


class _Tags:
    """The per-file tag cache: reuse a file's entry while its size and mtime
    hold, else read it again."""

    def __init__(self, cfg: Config) -> None:
        self.old: dict = load_json(cfg.audiobook_tags_cache, {})
        self.new: dict = {}

    def get(self, root: Path, path: Path, st: os.stat_result) -> dict:
        key = f"{root}\0{path.relative_to(root).as_posix()}"
        stamp = f"{st.st_size}:{st.st_mtime_ns}"
        hit = self.new.get(key) or self.old.get(key)
        if not hit or hit.get("stamp") != stamp:
            hit = {"stamp": stamp, "tags": audio_tags.summary(audio_tags.read(path))}
        self.new[key] = hit
        return hit

    def keep_root(self, root: Path) -> None:
        """Carry an unmounted root's cache over untouched."""
        prefix = f"{root}\0"
        self.new.update({k: v for k, v in self.old.items() if k.startswith(prefix)})

    def fingerprint(self, root: Path, path: Path, st: os.stat_result) -> str:
        rec = self.get(root, path, st)
        if "fp" not in rec:
            h = hashlib.sha1()
            try:
                with open(path, "rb") as f:
                    f.seek(max(0, st.st_size // 2 - FP_WINDOW // 2))
                    h.update(f.read(FP_WINDOW))
            except OSError:
                return ""
            rec["fp"] = h.hexdigest()[:16]
        return rec["fp"]


def _group_tracks(item: dict, stats: dict, tags: _Tags, root: Path) -> list[dict]:
    """A folder's non-mp3 tracks, one item per album tag: one file is a
    single-file book, several a `tracks` book."""
    groups: dict[str, list[Path]] = {}
    for p in item["paths"]:
        album = (tags.get(root, p, stats[p])["tags"] or {}).get("album") or ""
        groups.setdefault(album, []).append(p)
    out = []
    for album, paths in groups.items():
        if len(paths) == 1:
            p = paths[0]
            out.append({"rel": p.relative_to(root).as_posix(), "name": p.name,
                        "format": p.suffix.lower()[1:], "paths": paths, "dir": item["dir"]})
        else:
            out.append({"rel": item["rel"], "name": item["name"] if len(groups) == 1 else album or item["name"],
                        "format": "tracks", "paths": paths, "dir": item["dir"],
                        "group": album if len(groups) > 1 else None})
    return out


def _folder_cover(d: Path) -> Path | None:
    try:
        files = {p.name.lower(): p for p in d.iterdir() if p.is_file()}
    except OSError:
        return None
    for stem in COVER_NAMES:
        for ext in COVER_EXTS:
            if stem + ext in files:
                return files[stem + ext]
    return None


def _scan_root(root: Path, tags: _Tags) -> list[dict]:
    raw = _items(root)
    stats = {p: p.stat() for item in raw for p in item["paths"]}
    items: list[dict] = []
    for item in raw:
        items.extend(_group_tracks(item, stats, tags, root) if "format" not in item else [item])
    books_per_dir: dict[Path, int] = {}
    for item in items:
        books_per_dir[item["dir"]] = books_per_dir.get(item["dir"], 0) + 1

    entries: list[dict] = []
    for item in items:
        paths = item["paths"]
        sts = [stats[p] for p in paths]
        recs = [tags.get(root, p, st) for p, st in zip(paths, sts)]
        multi = item["format"] in ("mp3-folder", "tracks")
        book_tags = (audio_tags.merge_tracks([r["tags"] for r in recs]) if multi
                     else recs[0]["tags"])
        book_tags = audio_tags.summary(book_tags)
        filesize = sum(st.st_size for st in sts)
        mid = len(paths) // 2
        cover_src = next((p for p, r in zip(paths, recs) if (r["tags"] or {}).get("has_cover")), None)
        if cover_src is None and books_per_dir[item["dir"]] == 1:
            cover_src = _folder_cover(item["dir"])
        entries.append({
            "schema": SCHEMA_VERSION,
            "root": str(root),
            "path": item["rel"],
            "filename": item["name"],
            "filesize": filesize,
            "mtime": max(st.st_mtime for st in sts),
            "mtime_ns": max(st.st_mtime_ns for st in sts),
            "format": item["format"],
            "files": [p.name for p in paths] if multi else None,
            "group": item.get("group"),
            "tags": book_tags,
            "fp": None,
            "_cover_src": cover_src,
            "_fp_src": (root, paths[mid], sts[mid]),
        })
    return entries


def _ensure_fp(e: dict, tags: _Tags) -> None:
    """The size + middle-of-file hash, read only for an entry that may need to
    be recognised after a move (see `_carry`)."""
    if not e.get("fp"):
        e["fp"] = f"{e['filesize']}:{tags.fingerprint(*e['_fp_src'])}"


# ── ids ──────────────────────────────────────────────────────────────────────

def _loc(e: dict) -> tuple:
    return (e.get("root"), e.get("path"), e.get("group") or "")


def _seed(e: dict) -> str:
    """What a new entry's surrogate is made from: its path, plus the first track
    for a `tracks` book (a folder can hold two albums)."""
    if e["format"] == "tracks" and e.get("group"):
        return f"{e['path']}/{e['files'][0]}"
    return e["path"]


def _fid_of(e: dict) -> str:
    """An existing entry's fid. Entries from before fids existed: a surrogate
    ab_id is the fid; a keyed one gets the surrogate it would have had."""
    if e.get("fid"):
        return e["fid"]
    if not e.get("catalog_key"):
        return e["ab_id"]
    return ab_id_for(e["path"], e["filesize"])


def _key_source(entry: dict | None) -> str | None:
    """How a file got its catalog key (an entry from before `key_source` existed
    can only have got it from its name)."""
    if not entry or not entry.get("catalog_key"):
        return None
    return entry.get("key_source") or "name"


def _named(e: dict) -> bool:
    """Is the entry's key in its own name? Such an id is found again from the
    name alone, wherever the file goes."""
    return _key_source(e) == "name"


def _carry_keys(e: dict) -> list[tuple]:
    keys = [("file", (e.get("filename"), e.get("filesize")))]
    t = e.get("tags") or {}
    if t.get("title") and t.get("duration"):
        keys.append(("tags", (t["title"], round(t["duration"]))))
    if e.get("fp") and not e["fp"].endswith(":"):
        keys.append(("fp", e["fp"]))
    return keys


def _carry(prev: dict, fresh: list[dict], mounted: set[str], tags: _Tags) -> list[dict | None]:
    """For each fresh entry, the previous entry it continues, or None if it is new.

    The same place is the same book. Otherwise only an entry whose key isn't
    in its name (a surrogate, a hand match, a tag ASIN) is looked for: a named
    one is its key wherever it is."""
    from .rekey import match_moves

    by_loc = {_loc(e): e for e in prev.values()}
    origins: list[dict | None] = [by_loc.get(_loc(e)) for e in fresh]
    fresh_locs = {_loc(e) for e in fresh}
    vanished = {k: e for k, e in prev.items()
                if e.get("root") in mounted and _loc(e) not in fresh_locs and not _named(e)}
    new = {str(i): e for i, e in enumerate(fresh) if origins[i] is None}
    if vanished and new:
        if any(v.get("fp") for v in vanished.values()):
            for e in new.values():
                _ensure_fp(e, tags)
        for old_id, i in match_moves(vanished, new, keys=_carry_keys,
                                     kinds=("file", "tags", "fp")).items():
            origins[int(i)] = {**vanished[old_id], "_moved": True}
    return origins


def _resolve(e: dict, assignments: dict) -> tuple[str | None, str | None]:
    """(catalog key, where it came from): the name, a hand match, then the tag."""
    m = CATALOG_KEY_RE.search(e["filename"])
    if m:
        return m.group(1), "name"
    if assignments.get(e["fid"]):
        return assignments[e["fid"]], "assigned"
    asin = (e.get("tags") or {}).get("asin")
    if asin:
        return asin, "tag"
    return None, None


# ── covers ───────────────────────────────────────────────────────────────────

def _extract_cover(cfg: Config, e: dict, src: Path | None, prev: dict | None) -> None:
    """Keep `covers/audiobooks/{fid}.*` in step with the entry's cover source:
    the first track with an embedded picture, else a folder's cover.jpg."""
    e["cover"] = None
    try:
        st = src.stat() if src else None
    except OSError:
        st = None
    if st is None:
        return
    stamp = f"{src}:{st.st_size}:{st.st_mtime_ns}"
    if prev and prev.get("cover_src") == stamp and prev.get("cover") \
            and (cfg.data_dir / prev["cover"]).exists():
        e["cover"], e["cover_src"], e["cover_v"] = prev["cover"], stamp, prev.get("cover_v")
        return
    if src.suffix.lower() in COVER_EXTS:
        data, ext = None, ".jpg" if src.suffix.lower() == ".jpeg" else src.suffix.lower()
    else:
        tags = audio_tags.read(src) or {}
        data = tags.get("cover")
        if not data:
            return
        ext = ".png" if tags.get("cover_mime") == "image/png" else ".jpg"
    out_dir = cfg.audiobook_covers_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob(f"{e['fid']}.*"):
        old.unlink()
    dest = out_dir / f"{e['fid']}{ext}"
    if data is None:
        shutil.copyfile(src, dest)
    else:
        dest.write_bytes(data)
    e["cover"] = dest.relative_to(cfg.data_dir).as_posix()
    e["cover_src"] = stamp
    e["cover_v"] = st.st_mtime_ns // 1_000_000


def _sweep_covers(cfg: Config, index: dict) -> None:
    """Drop extracted covers (and their thumbnails) no entry uses any more."""
    keep = {Path(e["cover"]).stem for e in index.values() if e.get("cover")}
    for d in (cfg.audiobook_covers_dir, cfg.audiobook_covers_dir / "thumbs"):
        if d.is_dir():
            for p in d.iterdir():
                if p.is_file() and p.stem not in keep:
                    p.unlink()


# ── scan ─────────────────────────────────────────────────────────────────────

def scan(cfg: Config, *, dry_run: bool = False) -> dict:
    """Walk cfg.audiobook_dirs and write audiobook_files.json (unless dry_run,
    which only prints what would change). Returns the new index."""
    from . import audiobook_catalog, audiobook_rekey  # late: both import this module

    prev: dict = load_json(cfg.audiobook_files_index, {})
    assignments: dict = load_json(cfg.audiobook_assignments, {})
    # A row the catalog already has a cover for never shows the file's own.
    # (Not counting a hand-set cover: a reset falls back to the file's own.)
    has_cover = {k for k, r in audiobook_catalog.load(cfg, local=False, edits=False).items()
                 if r.get("cover")}
    tags = _Tags(cfg)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    new_index: dict = {}
    fresh: list[dict] = []
    mounted: set[str] = set()
    for root in cfg.audiobook_dirs:
        if not root.exists():
            kept = {k: e for k, e in prev.items() if e.get("root") == str(root)}
            print(f"  ! root not mounted, keeping {len(kept)} cached entries: {root}")
            new_index.update(kept)
            tags.keep_root(root)
            continue
        mounted.add(str(root))
        fresh.extend(_scan_root(root, tags))

    moved: list[tuple[dict, dict]] = []
    rekeys: list[tuple[str, str, bool, str | None]] = []
    for e, origin in zip(fresh, _carry(prev, fresh, mounted, tags)):
        e["fid"] = _fid_of(origin) if origin else ab_id_for(_seed(e), e["filesize"])
        key, source = _resolve(e, assignments)
        e["catalog_key"], e["key_source"] = key, source
        e["ab_id"] = key or e["fid"]
        if not _named(e):
            _ensure_fp(e, tags)
        cover_src = e.pop("_cover_src")
        del e["_fp_src"]
        key_id = e["ab_id"]
        if key_id in new_index:
            # Same key twice (two roots, or an m4b and an mp3 copy): keep the
            # earlier-listed one (canonical root first, m4b first) and say so.
            print(f"  ! duplicate {key_id}: keeping {new_index[key_id]['path']}, "
                  f"ignoring {e['root']}/{e['path']}")
            continue
        unchanged = origin and not origin.get("_moved") and origin.get("mtime_ns") == e["mtime_ns"] \
            and origin.get("filesize") == e["filesize"]
        e["scanned_at"] = origin.get("scanned_at", now) if unchanged else now
        if not dry_run:
            same_place = origin if origin and not origin.get("_moved") else None
            _extract_cover(cfg, e, None if key_id in has_cover else cover_src, same_place)
        new_index[key_id] = e
        if origin and origin.get("_moved"):
            moved.append((origin, e))
        if origin and origin["ab_id"] != e["ab_id"]:
            # Only a surrogate is moved onto the new id: a keyed id is a catalog
            # row of its own, still there (Audible-only) after the file goes.
            rekeys.append((origin["ab_id"], e["ab_id"], not origin.get("catalog_key"), source))

    verb = "would" if dry_run else "did"
    for origin, e in moved:
        print(f"  ~ moved: {origin['path']} → {e['path']}")
    for old, new, carry, source in rekeys:
        what = f"gained a key ({source}), {verb} move its state" if carry else "key changed"
        print(f"  ~ id {old} → {new}: {what}")
    if dry_run:
        print(f"Audiobook scan (dry run): {len(new_index)} books, {len(moved)} moved, "
              f"{len(rekeys)} id change(s); nothing written.")
        return new_index

    for old, new, carry, _ in rekeys:
        if carry:
            audiobook_rekey.rekey_audiobook(cfg, old, new)
    audiobook_rekey.drop_live_aliases(cfg, new_index)
    save_json(cfg.audiobook_tags_cache, tags.new)
    save_json(cfg.audiobook_files_index, new_index)
    _sweep_covers(cfg, new_index)
    n_key = sum(1 for e in new_index.values() if e.get("catalog_key"))
    print(f"Audiobook scan complete: {len(new_index)} files "
          f"({n_key} with catalog key, {len(new_index) - n_key} without"
          f"{f'; {len(moved)} moved' if moved else ''}"
          f"{f'; {len(rekeys)} id change(s)' if rekeys else ''}).")
    return new_index


# ── index ────────────────────────────────────────────────────────────────────

def _title_from_filename(name: str) -> tuple[str, str | None]:
    """Best-effort (title, order) for a file the catalog doesn't know."""
    stem = name
    if name.lower().endswith(audio_tags.AUDIO_EXTS):
        stem = name.rsplit(".", 1)[0]
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


def cover_url(ab_id: str, entry: dict | None) -> str | None:
    """The served URL of a local file's own cover (versioned, so it can be cached)."""
    if not entry or not entry.get("cover"):
        return None
    return f"/covers/audiobook/{ab_id}" + (f"?v={entry['cover_v']}" if entry.get("cover_v") else "")


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

    # Every local file has a catalog row (audiobook_catalog adds one from its
    # tags or file name when no catalog knows it), so this one loop covers both.
    for key, ab in catalog.items():
        if key in hidden:
            continue
        local = files.get(key)
        nat = natively.get(key) or {}
        source = ab.get("source", "catalog")
        out[key] = {
            "ab_id": key,
            "catalog_key": (local or {}).get("catalog_key") if source != "catalog" else key,
            "source": source,
            # How the file got its key: name | assigned (a hand match) | tag | None.
            "key_source": _key_source(local),
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
            "cover_url": ab.get("cover") or cover_url(key, local),
            "runtime_min": _num(ab.get("runtime_length_min"), int),
            "audible_rating": _num(ab.get("audible_rating"), float),
            "release_date": ab.get("release_date"),
            "genre": ab.get("genre"),
            # Non-Audible sources (kikubon/audiobook.jp keys) have no pd page.
            "audible_url": (audible.pd_url(key, cfg.audible_marketplace)
                            if AUDIBLE_KEY_RE.fullmatch(key) else None),
            "natively_level": nat.get("natively_level"),
            "natively_url": nat.get("natively_url"),
            "language_learning_rating": nat.get("language_learning_rating"),
            "natively_match_type": nat.get("match_type"),
            "natively_confidence": nat.get("confidence"),
            "natively_matched_title": nat.get("matched_title"),
        }

    save_json(cfg.audiobook_index, out)
    n_local = sum(1 for e in out.values() if e["local"])
    print(f"Audiobook index built: {len(out)} entries "
          f"({n_local} local, {len(out) - n_local} Audible-only"
          f"{f', {len(hidden)} hidden' if hidden else ''}).")
    return out
