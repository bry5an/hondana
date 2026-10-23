"""Your own Yomitan dictionary zips, hosted to the Hondana iOS reader.

The phone imports Yomitan zips with hoshidicts, so the server only has to list
the files in `dicts_dir` and serve them: every device then gets the same set
without iTunes file sharing. The zips are personal and some are not
redistributable, so the folder is gitignored (it sits under `data/`) and
LAN-only like the books.

The manifest reads each zip's `index.json` and infers `kind` from the bank files
inside. Hashing a 40 MB zip on every request would be wasteful, so a manifest
entry is cached by the file's `(mtime_ns, size)`; replacing a zip changes one
of them.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import zipfile
from pathlib import Path

from .config import Config

# A Yomitan zip is tens of megabytes (Jitendex ~40 MB); this only stops a runaway upload.
MAX_BYTES = 1024 * 1024 * 1024

_cache: dict[str, tuple[tuple[int, int], dict | None]] = {}
_lock = threading.Lock()


def _meta_modes(zf: zipfile.ZipFile, names: list[str]) -> set[str]:
    """The `freq` / `pitch` modes in a zip's first term_meta bank. A bank is a
    list of `[term, mode, data]`; a dictionary keeps one mode per bank in
    practice, so one bank says what the zip is."""
    first = sorted(n for n in names if _bank(n) == "term_meta_bank")
    if not first:
        return set()
    try:
        rows = json.loads(zf.read(first[0]))
    except (ValueError, KeyError, zipfile.BadZipFile):
        return set()
    return {r[1] for r in rows if isinstance(r, list) and len(r) > 1 and r[1] in ("freq", "pitch")}


def _bank(name: str) -> str:
    """`term_meta_bank_1.json` -> `term_meta_bank`; "" for anything else."""
    stem = name.rsplit("/", 1)[-1]
    if not stem.endswith(".json"):
        return ""
    head, _, tail = stem[:-5].rpartition("_")
    return head if tail.isdigit() else ""


def kind_of(zf: zipfile.ZipFile) -> str:
    names = zf.namelist()
    banks = {_bank(n) for n in names}
    kinds: set[str] = set()
    if "term_bank" in banks:
        kinds.add("term")
    if "kanji_bank" in banks:
        kinds.add("kanji")
    if "kanji_meta_bank" in banks:
        kinds.add("freq")
    if "term_meta_bank" in banks:
        kinds |= _meta_modes(zf, names) or {"freq"}
    return next(iter(kinds)) if len(kinds) == 1 else "mixed"


def _entry(path: Path, size: int) -> dict | None:
    """The manifest row of one zip, or None when it isn't a Yomitan dictionary."""
    try:
        with zipfile.ZipFile(path) as zf:
            index = json.loads(zf.read("index.json"))
            kind = kind_of(zf)
    except (zipfile.BadZipFile, KeyError, ValueError, OSError):
        return None
    if not isinstance(index, dict):
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)

    def text(key: str) -> str:
        v = index.get(key)
        return v if isinstance(v, str) else ""

    return {
        "file": path.name,
        "title": text("title") or path.stem,
        "revision": text("revision"),
        # Older dictionaries carry `version` where newer ones say `format`.
        "format": index.get("format", index.get("version")),
        "author": text("author"),
        "attribution": text("attribution"),
        "url": text("url"),
        "size": size,
        "sha256": h.hexdigest(),
        "kind": kind,
    }


def manifest(cfg: Config) -> list[dict]:
    """Every readable Yomitan zip directly in `dicts_dir`, by file name. An
    absent folder is an empty manifest, not an error."""
    root = cfg.dicts_dir
    if not root.is_dir():
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for path in sorted(root.iterdir(), key=lambda p: p.name):
        if path.suffix.lower() != ".zip" or path.name.startswith(".") or not path.is_file():
            continue
        st = path.stat()
        version = (st.st_mtime_ns, st.st_size)
        key = str(path)
        seen.add(key)
        with _lock:
            hit = _cache.get(key)
        if hit and hit[0] == version:
            row = hit[1]
        else:
            row = _entry(path, st.st_size)
            with _lock:
                _cache[key] = (version, row)
        if row:
            out.append(row)
    with _lock:
        for key in [k for k in _cache if k.startswith(str(root)) and k not in seen]:
            del _cache[key]
    return out


def find(cfg: Config, file: str) -> Path | None:
    """The path of a manifest file by name. The name has to be in the manifest,
    so nothing outside `dicts_dir` can be asked for."""
    if any(d["file"] == file for d in manifest(cfg)):
        return cfg.dicts_dir / file
    return None


class BadDict(ValueError):
    """An upload that can't become a dictionary; the message is for the person who sent it."""


def clean_name(filename: str | None) -> str:
    """The file name an upload is stored under: its basename, which has to be a plain `.zip`."""
    name = Path((filename or "").replace("\\", "/")).name
    if not name.lower().endswith(".zip") or name.startswith("."):
        raise BadDict("Only Yomitan dictionary .zip files are accepted")
    return name


def store(cfg: Config, filename: str | None, fileobj) -> dict:
    """Validate an uploaded zip and put it in `dicts_dir` under its own name, replacing a zip of
    that name (that is how a dictionary is updated). The bytes go to a temp file inside
    `dicts_dir` first, so a rejected or oversized upload never touches what is there and the
    final rename is atomic: the phone can't be served half a zip. Returns its manifest row."""
    name = clean_name(filename)
    root = cfg.dicts_dir
    root.mkdir(parents=True, exist_ok=True)
    # A dot-name without `.zip` is skipped by `manifest`, so a crash leaves nothing it lists.
    fd, tmp_name = tempfile.mkstemp(prefix=".upload-", suffix=".part", dir=root)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out:
            size = 0
            while chunk := fileobj.read(1 << 20):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise BadDict(f"Too large (the limit is {MAX_BYTES >> 20} MB)")
                out.write(chunk)
        row = _entry(tmp, size)
        if row is None:
            raise BadDict("Not a Yomitan dictionary: the zip has no readable index.json")
        dest = root / name
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    row["file"] = name
    # The rename keeps mtime and size, so the row just built is the manifest's row for `dest`.
    st = dest.stat()
    with _lock:
        _cache[str(dest)] = ((st.st_mtime_ns, st.st_size), row)
    return row


def remove(cfg: Config, file: str) -> bool:
    """Delete a manifest zip by name. False when there is no such dictionary."""
    path = find(cfg, file)
    if path is None:
        return False
    path.unlink(missing_ok=True)
    with _lock:
        _cache.pop(str(path), None)
    return True
