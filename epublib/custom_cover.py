"""Hand-set book covers.

An uploaded image lives in `data/covers/custom/{book_id}{ext}` with a flag in
`cover_overrides.json`; `build.build()` lays it over the EPUB's own cover. It is
kept out of `covers/{id}.*` because the scanner clears that glob whenever an entry
is rewritten. The EPUB's own cover stays where the scanner put it, so resetting
is just dropping the flag.

The caller holds `server._index_lock` and runs `build.build()` afterwards.
"""
from __future__ import annotations

import io
import os

from . import thumbs
from .config import Config
from .index_store import load_json, save_json

MAX_BYTES = 10 * 1024 * 1024
# Pillow format → file extension. No SVG: scripts in an <img> are inert, but
# build/thumbs and every other consumer only expects a raster.
FORMATS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif"}


class InvalidCover(ValueError):
    """The upload isn't an acceptable image; the message is shown to the user."""


def detect_ext(raw: bytes) -> str:
    """File extension for `raw` by what it actually contains, not its filename."""
    if len(raw) > MAX_BYTES:
        raise InvalidCover(f"Image exceeds the {MAX_BYTES // (1024 * 1024)} MB limit")
    try:
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as im:
            fmt = im.format
            im.verify()
    except Exception:
        raise InvalidCover("That file isn't a readable image") from None
    if fmt not in FORMATS:
        raise InvalidCover("Use a JPEG, PNG, WebP or GIF image")
    return FORMATS[fmt]


def _drop_files(cfg: Config, book_id: str) -> None:
    for old in cfg.custom_covers_dir.glob(f"{book_id}.*"):
        old.unlink(missing_ok=True)
    thumbs.thumb_path(cfg.data_dir, book_id).unlink(missing_ok=True)


def _set_flag(cfg: Config, book_id: str, value: dict | None) -> None:
    flags = load_json(cfg.cover_overrides, {})
    if value is None:
        if book_id not in flags:
            return
        del flags[book_id]
    else:
        flags[book_id] = value
    save_json(cfg.cover_overrides, flags)


def store(cfg: Config, book_id: str, raw: bytes) -> None:
    """Make `raw` the book's cover. Raises InvalidCover before touching anything."""
    ext = detect_ext(raw)
    cfg.custom_covers_dir.mkdir(parents=True, exist_ok=True)
    _drop_files(cfg, book_id)  # a replacement may have a different extension
    dest = cfg.custom_covers_dir / f"{book_id}{ext}"
    tmp = dest.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_bytes(raw)
    os.replace(tmp, dest)
    _set_flag(cfg, book_id, {"ext": ext})


def clear(cfg: Config, book_id: str) -> bool:
    """Back to the EPUB's own cover. True if a hand-set cover was removed."""
    had = book_id in load_json(cfg.cover_overrides, {})
    _drop_files(cfg, book_id)
    _set_flag(cfg, book_id, None)
    return had
