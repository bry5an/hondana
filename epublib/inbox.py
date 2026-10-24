"""The inbox: a folder new books are dropped into, filed into the library without anyone
opening the web app.

    inbox/
      Some Book.epub                      an EPUB, filed like an upload on Add
      Another [B0XXXXXXXX].m4b            an audiobook, filed like an upload on Add
      Spoken Book.zip                     the tracks of one audiobook
      Spoken Book/01.mp3, 02.mp3, …       a folder of tracks (or one file) is one book
      Author/Series/Title/Title.m4b       a tree such as Libation writes: every folder that
                                          holds audio is a book (several .m4b files side by
                                          side are separate books)
      _failed/                            what could not be filed, each with a `.error.txt`

Rules, in the order they matter:

* **Nothing is touched until it is still.** A new file may still be copying in (over SMB, from a
  download). An item is ready when its size and newest modification time are the same on two
  polls in a row and it is at least a minute old (`ready_items`). A folder with a download or
  copy marker in it (`.part`, `.crdownload`, …) is never ready. Hidden files and `_failed/` are
  ignored; so are symlinks and any other kind of file.
* **Files are moved, not copied.** An EPUB is filed through `incoming.store_incoming_epub` (the
  same duplicate check and language folders as Add) and removed from the inbox; an audiobook goes
  through `intake.file_item` (tags, naming, duplicate and conflict rules of an upload) and is
  moved into the library. Items waiting to be filed are never deleted before they are safe.
* **A failure keeps the original.** Whatever cannot be filed, and anything the library already
  has, moves to `_failed/` with a note saying why, so the inbox never retries it forever and
  nothing is lost.
* **What a filed folder leaves behind is tidied** only when it is cover images, `.cue`/`.json`
  and the like; any other file keeps the folder where it is. Emptied parent folders go too.

The audio of an item is staged in the library's own `.hondana-staging/` first (a rename on the
same disk, a copy from another one) and filed from there, by the same worker as uploads
(`intake.IntakeJobs.submit`), so a drop and an upload never move files at once and the indexing
runs once for the batch. EPUBs are filed by the watcher itself, under the caller's index lock.

`Watcher` is the server's polling thread (`server._lifespan` starts it; it follows
`cfg.inbox_dir` on every tick, so a change in Settings applies at once). `run_once` is the same
filing done synchronously, for the `inbox` pipeline stage (`cli.py inbox`, and the first step of
`cli.py all`), which needs no server. Polling rather than inotify/FSEvents: those miss changes
on SMB and NFS shares and Docker Desktop mounts.
"""
from __future__ import annotations

import errno
import os
import secrets
import shutil
import threading
import time
import traceback
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

# `intake` first: importing `incoming` (or the scanner) before the pipeline modules trips an
# older import cycle between scanner, purge, align and annotate.
from . import intake  # isort: skip
from . import audio_tags, incoming, uploads
from .audiobook_scanner import COVER_EXTS, COVER_NAMES
from .config import Config
from .consolidate import _move_file
from .index_store import load_json

POLL_SECONDS = 30
MIN_AGE = 60          # seconds an item must have been unchanged
SETTLE_SECONDS = 3.0  # between the two looks of a one-off run (the stage)
FAILED = "_failed"
MAX_DEPTH = 6         # folders below the inbox that are looked into

# A download or copy in progress; a folder holding one is not ready.
TEMP_SUFFIXES = (".part", ".partial", ".crdownload", ".download", ".tmp", ".temp", ".filepart",
                 ".opdownload", ".aria2", ".!qb", ".swp")
# What a filed folder may leave behind that is safe to tidy away with it.
SIDECAR_EXTS = COVER_EXTS + (".gif", ".bmp", ".json", ".cue", ".nfo", ".txt", ".xml", ".m3u",
                             ".m3u8", ".sfv", ".md5", ".url")
JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}


@dataclass(frozen=True)
class Item:
    """Something in the inbox that is one book: a file, or a folder of tracks."""
    path: Path
    kind: str         # "epub" | "zip" | "file" (one audio file) | "folder"
    size: int         # bytes (a folder: its direct files)
    mtime: float      # newest modification time (a folder: of it and its direct files)
    busy: bool = False  # a download/copy marker sits in it

    @property
    def key(self) -> str:
        return str(self.path)

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def sig(self) -> tuple[int, float]:
        return (self.size, self.mtime)


# ── finding items ────────────────────────────────────────────────────────────

def _ignored(name: str) -> bool:
    return name.startswith((".", "~$")) or name.lower() in JUNK_NAMES


def _is_temp(name: str) -> bool:
    """A download or copy in progress: a known suffix, or rsync's `.name.XXXXXX` scratch file."""
    return (name.lower().endswith(TEMP_SUFFIXES)
            or (name.startswith(".") and not name.startswith("._") and name.count(".") >= 2))


def _ext(name: str) -> str:
    return os.path.splitext(name)[1].lower()


def _entries(directory: Path) -> tuple[list[os.DirEntry], list[os.DirEntry]]:
    """(files, folders) directly inside, by name; symlinks and unreadable entries skipped."""
    files, dirs = [], []
    with os.scandir(directory) as it:
        for e in sorted(it, key=lambda e: e.name):
            try:
                if e.is_symlink():
                    continue
                (dirs if e.is_dir() else files if e.is_file() else []).append(e)
            except OSError:
                continue
    return files, dirs


def _file_item(e: os.DirEntry, kind: str) -> Item | None:
    try:
        st = e.stat(follow_symlinks=False)
    except OSError:
        return None
    return Item(Path(e.path), kind, st.st_size, st.st_mtime)


def describe(path: Path, kind: str) -> Item | None:
    """The item as it is now (None when it is gone): what a worker re-checks before it
    moves anything, in case the folder changed after the poll that found it."""
    try:
        if kind != "folder":
            st = path.stat()
            return Item(path, kind, st.st_size, st.st_mtime)
        files, _ = _entries(path)
        size, mtime = 0, path.stat().st_mtime
        for e in files:
            if _ignored(e.name):  # Finder rewrites .DS_Store whenever the folder is opened
                continue
            st = e.stat(follow_symlinks=False)
            size += st.st_size
            mtime = max(mtime, st.st_mtime)
        return Item(path, kind, size, mtime, any(_is_temp(e.name) for e in files))
    except OSError:
        return None


def _walk(directory: Path, root: Path, depth: int, out: list[Item]) -> None:
    files, dirs = _entries(directory)
    top = directory == root
    if top:
        dirs = [e for e in dirs if e.name != FAILED]
    files = [e for e in files if not _ignored(e.name)]
    dirs = [e for e in dirs if not _ignored(e.name)]

    audio = [e for e in files if _ext(e.name) in audio_tags.AUDIO_EXTS]
    for e in files:
        kind = {".epub": "epub", ".zip": "zip"}.get(_ext(e.name))
        if kind or (top and e in audio):  # loose in the inbox: each audio file is its own book
            item = _file_item(e, kind or "file")
            if item:
                out.append(item)
    if not top and audio:
        m4bs = [e for e in audio if _ext(e.name) == ".m4b"]
        if len(m4bs) > 1:  # several audiobooks side by side
            out.extend(i for i in (_file_item(e, "file") for e in m4bs) if i)
        else:
            item = describe(directory, "folder")
            if item:
                out.append(item)
        return  # a book's own subfolders belong to it
    if depth < MAX_DEPTH:
        for e in dirs:
            _walk(Path(e.path), root, depth + 1, out)


def find_items(root: Path) -> list[Item]:
    """Every item below `root`. Raises OSError when the inbox itself can't be read."""
    out: list[Item] = []
    _walk(root, root, 0, out)
    return out


def ready_items(prev: dict[str, tuple[int, float]], items: list[Item], now: float,
                min_age: float = MIN_AGE) -> list[Item]:
    """The items to file now: unchanged since the previous poll (`prev` maps an item's key to
    the `sig` seen then), at least `min_age` seconds old, and with nothing still arriving."""
    return [i for i in items if not i.busy and prev.get(i.key) == i.sig and now - i.mtime >= min_age]


# ── where the inbox may be ───────────────────────────────────────────────────

def overlap(inbox: Path, roots: list[Path]) -> str | None:
    """Why `inbox` can't sit with these library folders: inside one, the same as one, or
    holding one (the inbox would file the library into itself)."""
    a = inbox.resolve()
    for root in roots:
        b = root.resolve()
        if a == b or a.is_relative_to(b) or b.is_relative_to(a):
            return f"The inbox can't be inside, or hold, a library folder ({root})"
    return None


def problem(cfg: Config) -> str | None:
    """Why the configured inbox can't be used right now, or None (also None when unset)."""
    root = cfg.inbox_dir
    if root is None:
        return None
    clash = overlap(root, [cfg.epub_dir, *cfg.audiobook_dirs])
    if clash:
        return clash
    if not root.is_dir():
        return (f"The inbox folder isn't available: {root}. Check that it is mounted, "
                "or change it in Settings.")
    if not os.access(root, os.R_OK | os.W_OK | os.X_OK):
        return f"Hondana can't read and write the inbox folder: {root}"
    return None


# ── what happens to an item ──────────────────────────────────────────────────

# Items being worked on, by key, in this process: the server's watcher and a pipeline stage
# run from /admin share the inbox, and must not both pick up the same one.
_claims_lock = threading.Lock()
_claims: set[str] = set()


def _claim(key: str) -> bool:
    with _claims_lock:
        if key in _claims:
            return False
        _claims.add(key)
        return True


def _release(key: str) -> None:
    with _claims_lock:
        _claims.discard(key)


def _unique(directory: Path, name: str) -> Path:
    target = directory / name
    stem, ext = os.path.splitext(name)
    n = 2
    while target.exists():
        target = directory / f"{stem} ({n}){ext}"
        n += 1
    return target


def quarantine(root: Path, item: Item, message: str, staged: Path | None = None) -> Path:
    """Move what is left of `item` (still in the inbox, or already in `staged`) into
    `_failed/`, with `<name>.error.txt` saying why."""
    failed = root / FAILED
    failed.mkdir(exist_ok=True)
    dest = _unique(failed, item.name)
    if item.kind == "folder":
        if item.path.exists():
            shutil.move(str(item.path), str(dest))
        else:
            dest.mkdir()
        if staged and staged.is_dir():
            for f in sorted(staged.iterdir()):
                if f.is_file():
                    _move_file(f, _unique(dest, f.name), cross=False, verify_hash=False)
    elif item.path.exists():
        shutil.move(str(item.path), str(dest))
    elif staged and (staged / item.name).is_file():
        _move_file(staged / item.name, dest, cross=False, verify_hash=False)
    note = dest.with_name(dest.name + ".error.txt")
    note.write_text(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\n{item.name}\n\n{message}\n", encoding="utf-8")
    return dest


def _only_sidecars(entries: list[os.DirEntry]) -> bool:
    return all(e.is_file(follow_symlinks=False)
               and (e.name.lower() in JUNK_NAMES or _ext(e.name) in SIDECAR_EXTS or e.name.startswith("._"))
               for e in entries)


def _prune(directory: Path, root: Path) -> None:
    """Remove `directory` and its parents up to (not including) `root` while they hold
    nothing but junk."""
    root = root.resolve()
    while directory.resolve() != root and directory.resolve().is_relative_to(root):
        try:
            with os.scandir(directory) as it:
                left = list(it)
            if not all(e.name.lower() in JUNK_NAMES or e.name.startswith("._") for e in left):
                return
            for e in left:
                os.unlink(e.path)
            directory.rmdir()
        except OSError:
            return
        directory = directory.parent


def consume(root: Path, item: Item) -> None:
    """The item is in the library: remove what it leaves in the inbox. An EPUB or zip goes; a
    folder goes if all that is left is cover images and the like (any other file keeps it);
    emptied parents go too."""
    if item.kind in ("epub", "zip"):
        item.path.unlink(missing_ok=True)
    elif item.kind == "folder" and item.path.is_dir():
        with os.scandir(item.path) as it:
            left = list(it)
        if _only_sidecars(left):
            shutil.rmtree(item.path, ignore_errors=True)
    _prune(item.path.parent, root)


class Changed(Exception):
    """The item changed between being found and being staged: look again next poll."""


# (item, "filed" | "duplicate" | "failed" | "changed" | "retry", note). "changed" and "retry"
# leave the item where it is for a later look: it moved under our feet, or the library's drive
# has no room or isn't there.
Settled = Callable[[Item, str, str | None], None]


def _retryable(error: Exception) -> bool:
    return (isinstance(error, uploads.UploadError) and error.status in (503, 507)) or (
        isinstance(error, OSError) and error.errno == errno.ENOSPC)


def _restore(item: Item, staged: Path) -> None:
    """Put back whatever was already staged, for an item that will be tried again."""
    if not staged.is_dir():
        return
    for f in sorted(staged.iterdir()):
        target = item.path / f.name if item.kind == "folder" else item.path
        if f.is_file() and not target.exists():
            _move_file(f, target, cross=False, verify_hash=False)


class AudioJob:
    """One audiobook from the inbox as `intake.IntakeJobs` sees it (an `intake.Source`)."""

    def __init__(self, cfg: Config, root: Path, item: Item, job_id: str, done: Settled) -> None:
        self.cfg, self.root, self.item, self.job_id, self.done = cfg, root, item, job_id, done

    @property
    def staged(self) -> Path:
        return uploads.item_dir(self.cfg, self.job_id)

    def stage(self) -> tuple[Path, str]:
        it = self.item
        now = describe(it.path, it.kind)
        if now is None or now.sig != it.sig or now.busy:
            raise Changed(it.name)
        if it.kind == "zip":
            d = uploads.fresh_item_dir(self.cfg, self.job_id)
            uploads.safe_extract(it.path, d)
            return d, it.name
        d = uploads.fresh_item_dir(self.cfg, self.job_id, need=it.size)
        if it.kind == "file":
            _move_file(it.path, d / it.name, cross=False, verify_hash=False)
            return d, it.name
        files, _ = _entries(it.path)
        wanted = [e for e in files if _ext(e.name) in audio_tags.AUDIO_EXTS
                  or (_ext(e.name) in COVER_EXTS and os.path.splitext(e.name)[0].lower() in COVER_NAMES)]
        tracks = [e for e in wanted if _ext(e.name) in audio_tags.AUDIO_EXTS]
        for e in wanted:
            _move_file(Path(e.path), d / e.name, cross=False, verify_hash=False)
        # A single file keeps its own name (Libation puts the ASIN in it), tracks the folder's.
        return d, tracks[0].name if len(tracks) == 1 else it.name

    def settle(self, filed: intake.Filed | None, error: Exception | None) -> None:
        it, note = self.item, None
        try:
            if isinstance(error, Changed):
                outcome = "changed"
            elif error is not None and _retryable(error):
                outcome, note = "retry", str(error)
                _restore(it, self.staged)
            elif error is not None:
                outcome, note = "failed", str(error) or type(error).__name__
                quarantine(self.root, it, note, self.staged)
            elif filed.status == "duplicate":
                outcome = "duplicate"
                note = "Already in your library" + (f": {filed.title}" if filed.title else f" ({filed.path})")
                quarantine(self.root, it, note, self.staged)
            else:
                outcome, note = "filed", filed.path
                consume(self.root, it)
        except Exception as exc:  # noqa: BLE001 — report it, don't lose the outcome
            traceback.print_exc()
            outcome, note = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            _release(it.key)
        self.done(it, outcome, note)


def file_audio_now(cfg: Config, root: Path, item: Item, done: Settled) -> None:
    """File one audiobook here and now (the pipeline stage; the server queues it instead)."""
    job = AudioJob(cfg, root, item, secrets.token_hex(12), done)
    try:
        staged, name = job.stage()
        filed = intake.file_item(cfg, staged, name)
    except Exception as exc:  # noqa: BLE001 — every failure is the item's, not the run's
        job.settle(None, exc)
    else:
        job.settle(filed, None)
    finally:
        uploads.discard(cfg, job.job_id)


AddEpub = Callable[[bytes, str], "str | None"]  # files the bytes; may return a note


def _store_only(cfg: Config) -> AddEpub:
    """File an EPUB and nothing else: the pipeline stage indexes afterwards."""
    def add(content: bytes, filename: str) -> None:
        incoming.store_incoming_epub(cfg, load_json(cfg.library_index, {}), content, filename)
    return add


def file_epub(root: Path, item: Item, add_epub: AddEpub, done: Settled) -> None:
    """File one EPUB with `add_epub` and settle it like an audiobook."""
    note = None
    try:
        now = describe(item.path, item.kind)
        if now is None or now.sig != item.sig:
            outcome = "changed"
        else:
            if item.size > incoming.MAX_BYTES:
                raise ValueError("File exceeds the 100 MB limit")
            note = add_epub(item.path.read_bytes(), item.name)
            consume(root, item)
            outcome = "filed"
    except incoming.DuplicateEpub as dup:
        outcome, note = "duplicate", f"Already in your library ({dup.reason}): {dup.title}"
        _quarantine_quietly(root, item, note)
    except Exception as exc:  # noqa: BLE001
        if not isinstance(exc, (ValueError, OSError, incoming.UnsafeFilename)):
            traceback.print_exc()
        outcome, note = "failed", str(exc) or type(exc).__name__
        _quarantine_quietly(root, item, note)
    finally:
        _release(item.key)
    done(item, outcome, note)


def _quarantine_quietly(root: Path, item: Item, message: str) -> None:
    try:
        quarantine(root, item, message)
    except OSError as exc:
        print(f"inbox: couldn't move {item.name} to {FAILED}/: {exc}")


# ── the pipeline stage ───────────────────────────────────────────────────────

@dataclass
class Report:
    filed_epubs: list[str] = field(default_factory=list)
    filed_audio: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    waiting: list[str] = field(default_factory=list)


def run_once(cfg: Config, *, add_epub: AddEpub | None = None, settle_seconds: float | None = None,
             clock: Callable[[], float] = time.time,
             sleep: Callable[[float], None] = time.sleep) -> Report:
    """File everything in the inbox that is ready, one item after another, and say what
    happened. Two looks `settle_seconds` apart stand in for the watcher's two polls. Does not
    index: the caller (the `inbox` stage) does."""
    report = Report()
    root = cfg.inbox_dir
    if root is None:
        print("inbox: no inbox folder is set (inbox.dir); nothing to do")
        return report
    why = problem(cfg)
    if why:
        print(f"inbox: {why}")
        return report
    add_epub = add_epub or _store_only(cfg)

    first = {i.key: i.sig for i in find_items(root)}
    sleep(SETTLE_SECONDS if settle_seconds is None else settle_seconds)
    items = find_items(root)
    ready = ready_items(first, items, clock())
    ready_keys = {i.key for i in ready}
    report.waiting = [i.name for i in items if i.key not in ready_keys]

    def done(item: Item, outcome: str, note: str | None) -> None:
        bucket = {"filed": report.filed_audio if item.kind != "epub" else report.filed_epubs,
                  "duplicate": report.duplicates, "failed": report.failed,
                  "changed": report.waiting, "retry": report.waiting}[outcome]
        bucket.append(item.name)
        print(f"inbox: {outcome} {item.name}" + (f" ({note})" if note else ""))

    for item in ready:
        if not _claim(item.key):
            continue
        if item.kind == "epub":
            file_epub(root, item, add_epub, done)
        else:
            file_audio_now(cfg, root, item, done)
    if report.waiting:
        print(f"inbox: {len(report.waiting)} still arriving or less than {MIN_AGE} s old; "
              "run again in a minute")
    return report


# ── the server's watcher ─────────────────────────────────────────────────────

class Watcher:
    """Polls the configured inbox and files what is ready. `poll()` is one tick (the thread
    calls it every `poll_seconds`; tests call it with a fake clock)."""

    def __init__(self, cfg: Config, jobs: intake.IntakeJobs, add_epub: AddEpub, *,
                 clock: Callable[[], float] = time.time, poll_seconds: float = POLL_SECONDS,
                 min_age: float = MIN_AGE) -> None:
        self.cfg, self.jobs, self.add_epub = cfg, jobs, add_epub
        self.clock, self.poll_seconds, self.min_age = clock, poll_seconds, min_age
        self._seen: dict[str, tuple[int, float]] = {}
        self._handled: dict[str, tuple[int, float]] = {}  # settled but still there: leave alone
        self._root: Path | None = None
        self._waiting: list[str] = []
        self._problem: str | None = None
        self._recent: deque[dict] = deque(maxlen=20)
        self._mu = threading.Lock()         # guards the status fields
        self._tick = threading.Lock()       # one poll at a time
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    # lifecycle ----------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="inbox-watcher")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)

    def wake(self) -> None:
        """Poll soon (the setting changed)."""
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll()
            except Exception:  # noqa: BLE001 — keep watching
                traceback.print_exc()
            self._wake.wait(self.poll_seconds)
            self._wake.clear()

    # one tick -----------------------------------------------------------------

    def poll(self) -> list[Item]:
        """Look at the inbox once and file what is ready. Returns the items it took."""
        with self._tick:
            root = self.cfg.inbox_dir
            if root != self._root:  # a different folder: start from nothing
                self._root, self._seen, self._handled = root, {}, {}
            why = problem(self.cfg) if root else None
            if root is None or why:
                with self._mu:
                    self._problem, self._waiting = why, []
                self._seen = {}
                return []
            try:
                items = find_items(root)
            except OSError as exc:
                with self._mu:
                    self._problem, self._waiting = f"Can't read the inbox: {exc}", []
                self._seen = {}
                return []
            ready = ready_items(self._seen, items, self.clock(), self.min_age)
            self._seen = {i.key: i.sig for i in items}
            with self._mu:
                self._handled = {k: v for k, v in self._handled.items() if self._seen.get(k) == v}
                ready = [i for i in ready if self._handled.get(i.key) != i.sig]

            taken = []
            for item in ready:
                if not _claim(item.key):  # a stage run from /admin has it
                    continue
                taken.append(item)
                if item.kind == "epub":
                    file_epub(root, item, self.add_epub, self._settled)
                    continue
                job_id = secrets.token_hex(12)
                try:
                    self.jobs.submit(job_id, AudioJob(
                        self.cfg, root, item, job_id,
                        lambda it, outcome, note, job=job_id: self._settled(it, outcome, note, job)))
                except BaseException:
                    _release(item.key)
                    raise
            taken_keys = {i.key for i in taken}
            with self._mu:
                self._problem = None
                self._waiting = [i.name for i in items if i.key not in taken_keys]
            return taken

    def _settled(self, item: Item, outcome: str, note: str | None, job: str | None = None) -> None:
        if outcome == "changed":
            return
        with self._mu:
            last = self._recent[0] if self._recent else None
            if outcome == "retry" and last and (last["name"], last["note"]) == (item.name, note):
                return  # still the same trouble: say it once
            if outcome != "retry" and item.path.exists():
                # Couldn't be moved or removed: don't try again until it changes.
                now = describe(item.path, item.kind)
                if now:
                    self._handled[item.key] = now.sig
            self._recent.appendleft({"name": item.name, "outcome": outcome, "note": note,
                                     "at": time.time(), "job": job})
        print(f"inbox: {outcome} {item.name}" + (f" ({note})" if note else ""))

    # what Settings shows --------------------------------------------------------

    def status(self) -> dict:
        root = self.cfg.inbox_dir
        failed = 0
        if root is not None:
            try:
                failed = sum(1 for p in (root / FAILED).iterdir() if p.name.endswith(".error.txt"))
            except OSError:
                pass
        with self._mu:
            return {"enabled": root is not None, "dir": str(root) if root else None,
                    "problem": problem(self.cfg) or self._problem, "waiting": list(self._waiting),
                    "failed": failed, "recent": list(self._recent)}
