"""Hand-edited audiobook details: what `/api/audiobooks/{ab_id}/details` reads
and writes.

Edits live in `audiobook_metadata_overrides.json`, `{ab_id: {field: value}}`,
which `audiobook_catalog.load()` lays over the row per field, so they hold for
any row (an Audible one with a wrong volume number as much as a file known only
by its name) and survive every rescan and re-fetch. A hand-set cover rides the
same entry as `cover: {ext, v}`, its image in
`covers/audiobooks/custom/{ab_id}{ext}`. Both follow the audiobook when its id
changes (audiobook_rekey).

The caller holds `server._index_lock` and rebuilds afterwards (`rebuild`).
"""
from __future__ import annotations

import os
import re

from . import audiobook_catalog, audiobook_scanner, audiobook_xref, build, custom_cover
from .config import Config
from .index_store import load_json, save_json

# API field → catalog row field. Everything a hand edit may change.
FIELDS = {
    "title": "title", "author": "author", "series": "series", "order": "order",
    "narrators": "narrators", "genre": "genre", "description": "description_full",
    "release_date": "release_date",
}
MAX_LEN = {"description": 5000, "order": 12}
DATE_RE = re.compile(r"\d{4}(-\d{2}(-\d{2})?)?")


class InvalidDetails(ValueError):
    """A value the form can't save; the message is shown to the user."""


def _values(row: dict) -> dict:
    return {api: row.get(field) for api, field in FIELDS.items()}


def custom_cover_path(cfg: Config, ab_id: str):
    flag = (load_json(cfg.audiobook_metadata_overrides, {}).get(ab_id) or {}).get("cover")
    if not isinstance(flag, dict) or not flag.get("ext"):
        return None
    path = cfg.audiobook_custom_covers_dir / f"{ab_id}{flag['ext']}"
    return path if path.exists() else None


def state(cfg: Config, ab_id: str) -> dict | None:
    """The details form's state, or None for an id no catalog row has."""
    rows = audiobook_catalog.load(cfg)
    row = rows.get(ab_id)
    if row is None:
        return None
    plain = audiobook_catalog.load(cfg, edits=False).get(ab_id) or {}
    edits = load_json(cfg.audiobook_metadata_overrides, {}).get(ab_id) or {}
    entry = load_json(cfg.audiobook_files_index, {}).get(ab_id)
    own = audiobook_scanner.cover_url(ab_id, entry)
    return {
        "ab_id": ab_id,
        "values": _values(row),
        "auto": _values(plain),
        "overridden": [api for api, field in FIELDS.items() if field in edits],
        "source": row.get("source", "catalog"),
        "local": entry is not None,
        "path": entry.get("path") if entry else None,
        "series_names": sorted({r["series"] for r in rows.values() if r.get("series")}),
        "cover": {
            "url": row.get("cover"),
            "overridden": isinstance(edits.get("cover"), dict),
            # What a reset goes back to: the catalog's cover, else the file's own.
            "auto_url": plain.get("cover") or own,
        },
    }


def _clean(api: str, value) -> str:
    text = re.sub(r"\s+", " ", str(value)).strip() if api != "description" else str(value).strip()
    if len(text) > MAX_LEN.get(api, 500):
        raise InvalidDetails(f"{api} is too long")
    if api == "release_date" and text and not DATE_RE.fullmatch(text):
        raise InvalidDetails("Release date must be YYYY, YYYY-MM or YYYY-MM-DD")
    return text


def apply(cfg: Config, ab_id: str, sent: dict, genres: list[str]) -> bool:
    """Write the sent fields (empty or None = back to automatic). Returns
    whether anything changed. Raises InvalidDetails before writing anything."""
    clean = {api: _clean(api, v) if v is not None else "" for api, v in sent.items() if api in FIELDS}
    if clean.get("genre") and clean["genre"] not in genres:
        raise InvalidDetails(f"Unknown genre: {clean['genre']}")
    data = load_json(cfg.audiobook_metadata_overrides, {})
    before = dict(data.get(ab_id) or {})
    after = dict(before)
    for api, value in clean.items():
        field = FIELDS[api]
        if value:
            after[field] = value
        else:
            after.pop(field, None)
    if after == before:
        return False
    if after:
        data[ab_id] = after
    else:
        data.pop(ab_id, None)
    save_json(cfg.audiobook_metadata_overrides, data)
    return True


def _drop_cover_files(cfg: Config, ab_id: str) -> None:
    d = cfg.audiobook_custom_covers_dir
    for old in d.glob(f"{ab_id}.*"):
        old.unlink(missing_ok=True)
    (d / "thumbs" / f"{ab_id}.webp").unlink(missing_ok=True)


def store_cover(cfg: Config, ab_id: str, raw: bytes) -> None:
    """Make `raw` the audiobook's cover. Raises custom_cover.InvalidCover first."""
    ext = custom_cover.detect_ext(raw)
    d = cfg.audiobook_custom_covers_dir
    d.mkdir(parents=True, exist_ok=True)
    _drop_cover_files(cfg, ab_id)
    dest = d / f"{ab_id}{ext}"
    tmp = dest.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_bytes(raw)
    os.replace(tmp, dest)
    data = load_json(cfg.audiobook_metadata_overrides, {})
    data[ab_id] = {**(data.get(ab_id) or {}), "cover": {"ext": ext, "v": dest.stat().st_mtime_ns // 1_000_000}}
    save_json(cfg.audiobook_metadata_overrides, data)


def clear_cover(cfg: Config, ab_id: str) -> bool:
    """Back to the catalog's (or the file's) cover. True if one was removed."""
    data = load_json(cfg.audiobook_metadata_overrides, {})
    entry = dict(data.get(ab_id) or {})
    had = entry.pop("cover", None) is not None
    _drop_cover_files(cfg, ab_id)
    if had:
        if entry:
            data[ab_id] = entry
        else:
            data.pop(ab_id, None)
        save_json(cfg.audiobook_metadata_overrides, data)
    return had


def rebuild(cfg: Config) -> None:
    """After an edit: the audiobook index, then the EPUB links (a corrected
    title or volume can link a book, or stop it linking), then the library."""
    audiobook_scanner.build_index(cfg)
    audiobook_xref.xref(cfg)
    build.build(cfg)
