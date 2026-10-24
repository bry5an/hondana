"""Filing an uploaded audiobook into the library, and the queue that does it.

`uploads` gets the bytes to staging; `file_item` decides what the book is and where it lives:

1. Read its tags. The key is a `[B0…]` in the file's name, else the tag ASIN; the metadata is
   the catalog's row for that key, else the tags (title, author, series, part), else the name.
2. Give it the library's own layout, `[Author] Series/[NN] Title [KEY].m4b`
   (`consolidate._dest_rel`, in its portable mode: names made from tags nobody has looked at).
   A book with no key and no title tag goes to `_unsorted/<its file name>` and shows up flagged
   "Needs details", like any other guessed row.
3. Same book twice: a file already in the library under that key with the same size is a
   duplicate and nothing is stored; with another size it is a conflict (the scanner keeps one
   file per key, so storing the second would silently hide it). Without a key, a taken path
   gets ` (2)`, ` (3)`.
4. Move it into place. Staging is inside the library's root, so this is a rename.

Everything but `.m4b` is filed in a folder of its own, because the scanner groups the files of
a folder into books: two flac files side by side with no album tag would become one book. An
`.m4b` stays a plain file in its series folder, as the library has always had them.

`IntakeJobs` runs this for finished uploads, one at a time, then `pipeline.AUDIO_CHAIN` once for
the whole batch. Moving files takes no lock (a rename is atomic, and the scanner skips
staging); only the indexing holds `_index_lock`, the same lock every other writer of `data/`
takes. Job state is in memory, like `book_jobs`.

The inbox (`inbox.py`) files through the same worker, so a drop and an upload never move files
at once: `submit()` takes a `Source` in place of an upload id, which stages its own item and is
told how filing went (it keeps the original until then, so a failure can put it somewhere safe).
"""
from __future__ import annotations

import queue
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import ContextManager, Protocol

from . import audio_tags, audiobook_catalog, uploads
from .audiobook_scanner import CATALOG_KEY_RE, _title_from_filename
from .config import Config
from .consolidate import NAME_BYTES, _clip, _dest_rel, _move_dir, _move_file, _part, _portable_name
from .index_store import load_json
from .pipeline import run_audio_chain


class IntakeError(Exception):
    """An upload that can't be filed; `reason` is machine-readable, the message is for a person."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass
class Filed:
    status: str                # "filed" | "duplicate"
    path: str                  # the scanner's `path` for the book: its file, or its folder of tracks
    key: str | None
    title: str | None
    unsorted: bool             # no key and no title: filed under _unsorted/, needs details
    existing: str | None = None  # a duplicate: the ab_id already in the library


def _audio_files(staged: Path) -> list[Path]:
    return sorted((p for p in staged.iterdir()
                   if p.is_file() and p.suffix.lower() in audio_tags.AUDIO_EXTS),
                  key=lambda p: p.name)


def _size(path: Path) -> int:
    """A file's size, or the audio in a folder (what the scanner calls the book's filesize)."""
    if path.is_dir():
        return sum(p.stat().st_size for p in path.rglob("*")
                   if p.is_file() and p.suffix.lower() in audio_tags.AUDIO_EXTS)
    return path.stat().st_size


def _resolve(cfg: Config, tags: dict, original_name: str) -> tuple[str | None, dict | None]:
    """(catalog key, the row to name the book from). No row at all means nothing is known
    about the book beyond its file name."""
    m = CATALOG_KEY_RE.search(original_name)
    key = m.group(1) if m else tags.get("asin")
    row = audiobook_catalog.load(cfg, local=False).get(key) if key else None
    if row and row.get("title"):  # not a stub for an ASIN Audible has no book for
        return key, row
    name_title, name_order = _title_from_filename(original_name)
    title = tags.get("title") or (name_title if key else None)
    if not title:
        return key, None
    return key, {"title": title, "author": tags.get("author"), "series": tags.get("series"),
                 "order": audiobook_catalog._vol(tags.get("part")) or name_order}


def _paths(base: str, kind: str, ext: str) -> tuple[str, str]:
    """(the path the scanner will report for the book, what to create at it): a plain
    file for an m4b, otherwise a folder (holding the one file, or the tracks)."""
    if kind == "m4b":
        return f"{base}{ext}", f"{base}{ext}"
    if kind == "single":
        return f"{base}/{Path(base).name}{ext}", base
    return base, base


def file_item(cfg: Config, staged: Path, original_name: str) -> Filed:
    """File the audio in the folder `staged` (one file, or a zip's tracks) into the library."""
    root = cfg.audiobook_root
    files = _audio_files(staged)
    if not files:
        raise IntakeError("no_audio", "There is no audio in that upload")
    exts = {p.suffix.lower() for p in files}
    if len(exts) > 1:
        raise IntakeError("mixed_formats", "That upload mixes audio formats; send one book at a time")
    ext = exts.pop()
    if ext == ".m4b" and len(files) > 1:
        raise IntakeError("several_books", f"That upload has {len(files)} .m4b files; "
                                           "send audiobooks one at a time")
    kind = "m4b" if ext == ".m4b" else "single" if len(files) == 1 else "tracks"

    per_file = [audio_tags.read(p) for p in files]
    unreadable = [p.name for p, t in zip(files, per_file) if t is None]
    if unreadable:
        raise IntakeError("bad_audio", f"{unreadable[0]} isn't a readable audio file")
    tags = per_file[0] if len(files) == 1 else audio_tags.merge_tracks(per_file)

    key, entry = _resolve(cfg, tags, original_name)
    if entry is None:
        stem = _portable_name(_clip(_part(Path(original_name).stem), NAME_BYTES - 8))
        base = f"_unsorted/{stem}"
    else:
        base = _dest_rel(key, entry, folder=True, portable=True)

    new_size = sum(p.stat().st_size for p in files)
    index = load_json(cfg.audiobook_files_index, {})
    if key:
        for ab_id, e in index.items():
            if e.get("catalog_key") == key:
                if e.get("filesize") == new_size:
                    return Filed("duplicate", e["path"], key, (entry or {}).get("title"), False, ab_id)
                raise IntakeError(
                    "conflict", f"A different file for this book is already in your library "
                                f"({e['path']}). Delete that one first to replace it.")

    for n in range(1, 1000):
        book_rel, made = _paths(base if n == 1 else f"{base} ({n})", kind, ext)
        dest = root / made
        if not dest.resolve().is_relative_to(root.resolve()):
            raise IntakeError("bad_path", "That book's name would put it outside the audiobook folder")
        if not dest.exists():
            break
        if _size(dest) == new_size:
            existing = next((k for k, e in index.items()
                             if e.get("root") == str(root) and e.get("path") == book_rel), None)
            return Filed("duplicate", book_rel, key, (entry or {}).get("title"), False, existing)
        if key:
            raise IntakeError("conflict", f"A different file is already filed as {made}")
    else:
        raise IntakeError("conflict", "Too many books share that name")

    if kind == "m4b":
        _move_file(files[0], dest, cross=False, verify_hash=False)
    elif kind == "single":
        _move_file(files[0], root / book_rel, cross=False, verify_hash=False)
        for extra in staged.iterdir():  # a cover that came in the zip
            if extra.is_file() and extra != files[0]:
                _move_file(extra, dest / extra.name, cross=False, verify_hash=False)
    else:
        _move_dir(staged, dest, cross=False, verify_hash=False)
    return Filed("filed", book_rel, key, (entry or {}).get("title"), entry is None)


# ── the queue ────────────────────────────────────────────────────────────────

class Source(Protocol):
    """Where a queued item comes from when it is not an upload."""

    def stage(self) -> tuple[Path, str]:
        """Make the folder `file_item` takes and return it with the name the item arrived as."""

    def settle(self, filed: Filed | None, error: Exception | None) -> None:
        """Filing is over: `filed` says how it went, or `error` is why it did not. Called
        before the staging folder is cleared, so what is left in it can still be rescued."""


class IntakeJobs:
    """A FIFO of finished uploads, one worker. Everything queued when it wakes is filed, then
    indexed in one pass (audio-scan, catalog, xref and build are library-wide, so ten books cost
    one pass, not ten)."""

    def __init__(self, cfg: Config, lock: Callable[[], ContextManager]) -> None:
        self._cfg = cfg
        self._lock = lock
        self._queue: queue.Queue[str] = queue.Queue()
        self._state: dict[str, dict] = {}
        self._sources: dict[str, Source] = {}
        self._worker: threading.Thread | None = None
        self._mu = threading.Lock()  # guards _state, _sources and worker start

    def enqueue(self, upload_id: str) -> dict:
        """Queue a finished upload. Idempotent: asking again while it is queued, running or
        done returns that state instead of filing it twice."""
        with self._mu:
            return self._put(upload_id)

    def submit(self, job_id: str, source: Source) -> dict:
        """Queue an item that is not an upload. `job_id` is a fresh 24-hex id (it names the
        item's staging folder)."""
        with self._mu:
            self._sources[job_id] = source
            return self._put(job_id)

    def _put(self, job_id: str) -> dict:
        cur = self._state.get(job_id)
        if not cur or cur["state"] == "error":
            self._state[job_id] = {"state": "queued", "step": None, "error": None,
                                   "reason": None, "result": None,
                                   "queued_at": time.time(), "finished_at": None}
            self._queue.put(job_id)
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._loop, daemon=True, name="intake-jobs")
            self._worker.start()
        return dict(self._state[job_id])

    def status(self, upload_id: str) -> dict | None:
        with self._mu:
            cur = self._state.get(upload_id)
            return dict(cur) if cur else None

    def active(self, upload_id: str) -> bool:
        """Queued or running: the upload's files are being worked on and must not be touched."""
        cur = self.status(upload_id)
        return bool(cur and cur["state"] in ("queued", "running"))

    def _set(self, ids, **fields) -> None:
        with self._mu:
            for i in ids:
                self._state[i].update(fields)

    def _end(self, upload_id: str, state: str, **fields) -> None:
        self._set([upload_id], state=state, step=None, finished_at=time.time(), **fields)
        uploads.discard(self._cfg, upload_id)

    def _settle(self, source: Source | None, filed: Filed | None, error: Exception | None) -> None:
        if source is None:
            return
        try:
            source.settle(filed, error)
        except Exception:  # noqa: BLE001 — a source's bookkeeping must not stop the queue
            traceback.print_exc()

    def _fail(self, job_id: str, source: Source | None, exc: Exception, message: str, reason: str) -> None:
        self._settle(source, None, exc)  # before _end clears the staging folder
        self._end(job_id, "error", error=message, reason=reason)

    def _result(self, f: Filed) -> dict:
        cfg = self._cfg
        ab_id = f.existing
        if ab_id is None:
            ab_id = next((k for k, e in load_json(cfg.audiobook_files_index, {}).items()
                          if e.get("root") == str(cfg.audiobook_root) and e.get("path") == f.path), None)
        row = load_json(cfg.audiobook_index, {}).get(ab_id) or {}
        return {"status": f.status, "ab_id": ab_id, "path": f.path,
                "title": row.get("title") or f.title, "author": row.get("author"),
                "series": row.get("series"),
                "needs_details": f.status == "filed" and row.get("source") == "filename"}

    def _loop(self) -> None:
        while True:
            batch = [self._queue.get()]
            while True:  # everything already waiting rides along
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            self._set(batch, state="running", step="filing")
            filed: dict[str, Filed] = {}
            for upload_id in batch:
                with self._mu:
                    source = self._sources.pop(upload_id, None)
                try:
                    staged, name = source.stage() if source else uploads.stage_item(self._cfg, upload_id)
                    filed[upload_id] = file_item(self._cfg, staged, name)
                except uploads.UploadError as exc:
                    self._fail(upload_id, source, exc, str(exc), exc.code)
                except IntakeError as exc:
                    self._fail(upload_id, source, exc, str(exc), exc.reason)
                except Exception as exc:  # noqa: BLE001 — never kill the worker
                    traceback.print_exc()
                    self._fail(upload_id, source, exc, f"{type(exc).__name__}: {exc}", "error")
                else:
                    self._settle(source, filed[upload_id], None)

            new = [u for u, f in filed.items() if f.status == "filed"]
            errors: list[str] = []
            if new:
                self._set(new, step="indexing")
                try:
                    with self._lock():
                        errors = run_audio_chain(self._cfg, lambda s: self._set(new, step=s))
                except BaseException as exc:  # noqa: BLE001
                    traceback.print_exc()
                    errors = [f"{type(exc).__name__}: {exc}"]
            for upload_id, f in filed.items():
                result = self._result(f)
                if f.status == "filed" and errors:
                    self._end(upload_id, "error", result=result, reason="index_failed",
                              error="Saved, but indexing failed: " + "; ".join(errors))
                else:
                    self._end(upload_id, "done", result=result)
