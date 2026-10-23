"""Atomic JSON load/save for the generated indices.

Every index is a plain dict (usually keyed by book_id). Saves go through a
temp file + os.replace so a crash mid-write can never corrupt an index.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return {} if default is None else default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Per-writer temp name so two concurrent writers of one index can't
    # clobber each other's half-written file before the atomic replace.
    tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# mtime-keyed cache, shared by every caller (server.py's request handlers,
# profiles.py's per-request profile resolution). A save_json (os.replace)
# changes the file's mtime, so a write invalidates the cache for free — no
# manual busting needed. Keyed by path so callers reading distinct files (or
# the same per-user path under different active_user values) never collide.
_cache: dict[str, tuple] = {}
_cache_lock = threading.Lock()


def cached_json(path: Path, default: Any = None) -> Any:
    """Like load_json, but skips the re-parse if the file's (mtime, size)
    hasn't changed since the last call. Callers must treat the result as
    read-only unless they immediately overwrite the file with save_json —
    mutating a shared cached dict without saving would leak into other
    callers holding the same cache hit."""
    try:
        st = path.stat()
    except OSError:
        return {} if default is None else default
    version = (st.st_mtime_ns, st.st_size)
    key = str(path)
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] == version:
            return hit[1]
    data = load_json(path, default)
    with _cache_lock:
        _cache[key] = (version, data)
    return data
