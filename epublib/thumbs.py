"""Downscaled cover thumbnails, generated on first request and cached on disk.

Extracted covers are whatever resolution the publisher shipped — in this library
that runs to 2.1 MB for a single JPEG, against a maximum display width of 180px
(the book detail hero). Serving the originals meant ~27 MB per cold page load and
covers that never finished painting.

Generation is lazy rather than a pipeline stage: there is nothing to backfill for
covers that already exist, and no CLI/admin wiring to keep in sync. The cost is
one ~50-100ms resize per cover, once, ever.
"""
from __future__ import annotations

import os
from pathlib import Path

# Widest on-screen use is the 180px book detail hero; 400px covers that at 2x DPI.
MAX_WIDTH = 400
QUALITY = 82


def thumb_path(data_dir: Path, book_id: str) -> Path:
    return data_dir / "covers" / "thumbs" / f"{book_id}.webp"


def ensure_thumb(data_dir: Path, book_id: str, src: Path, dest: Path | None = None) -> Path | None:
    """Return a cached thumbnail for `src`, generating it if needed (at `dest`,
    default `covers/thumbs/{book_id}.webp`).

    Returns None if the thumbnail can't be produced (Pillow missing, undecodable
    image); callers fall back to the original file rather than failing the
    request — a cover is never worth a 500.
    """
    dest = dest or thumb_path(data_dir, book_id)
    try:
        if dest.exists() and dest.stat().st_mtime >= src.stat().st_mtime:
            return dest
    except OSError:
        pass

    try:
        from PIL import Image
    except ImportError:
        return None

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(src) as im:
            im.draft("RGB", (MAX_WIDTH, MAX_WIDTH))  # cheap JPEG downscale on decode
            im = im.convert("RGB")
            if im.width > MAX_WIDTH:
                height = round(im.height * MAX_WIDTH / im.width)
                im = im.resize((MAX_WIDTH, height), Image.LANCZOS)
            # Temp name is per-process so two concurrent requests for the same
            # cover can't interleave writes; os.replace publishes atomically.
            tmp = dest.with_suffix(f".{os.getpid()}.tmp")
            im.save(tmp, "WEBP", quality=QUALITY, method=4)
        os.replace(tmp, dest)
        return dest
    except Exception:
        return None
