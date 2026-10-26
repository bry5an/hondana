"""Resumable audiobook uploads: the bytes of one file arrive in sequential chunks and wait in
staging until `intake` files them.

A browser multipart upload is the wrong tool here. Starlette spools the whole body to a temp
file, there is no way to resume a 2 GB transfer that dropped at 90 %, and proxies cap request
bodies. So a file is sent as a series of small PUTs (routes in server.py):

    POST   /api/audiobooks/uploads                 {filename, size} -> {id, chunk_size, offset}
    PUT    /api/audiobooks/uploads/{id}            Content-Range: bytes S-E/TOTAL, raw body
    GET    /api/audiobooks/uploads/{id}            -> {state, offset, ...}  (where to resume)
    DELETE /api/audiobooks/uploads/{id}            abort
    POST   /api/audiobooks/uploads/{id}/complete   queue the filing (intake.IntakeJobs)

Staging is `<first audiobook folder>/.hondana-staging/`, on the same filesystem as the library,
so filing a finished upload is a rename and a reader never sees half a file. The scanner skips
dot-folders, so staging is never indexed. Per upload id:

    {id}.json    what was announced: file name, size, when
    {id}.part    the bytes received so far; its length *is* the resume offset
    {id}/        the item while it is being filed: the audio file, or a zip's extracted tracks

Bytes are written in order and kept as they arrive, so the part's length is always a valid
prefix of the file and the offset a resuming client reads is one it can continue from: a chunk
cut off by a dropped connection costs only what had not yet arrived. The newest request for an
upload takes over from an older one (see `chunk`).
"""
from __future__ import annotations

import contextlib
import os
import re
import secrets
import shutil
import threading
import time
import zipfile
import zlib
from pathlib import Path

from . import audio_tags
from .audiobook_scanner import COVER_EXTS, COVER_NAMES
from .config import Config
from .index_store import load_json, save_json

STAGING = ".hondana-staging"
CHUNK_SIZE = 16 << 20       # what the server asks clients to send
MAX_CHUNK = 64 << 20        # what it will take in one PUT
MAX_BYTES = 32 << 30        # one file
FREE_MARGIN = 1 << 30       # always leave this much free on the library's disk
STALE_AFTER = 7 * 86400     # an upload nobody touched for a week is abandoned
ZIP_MAX_FILES = 2000        # audio files in one zipped book
ZIP_MAX_ENTRIES = 20000     # entries of any kind: a zip of a million empty files is a bomb too
ZIP_MAX_RATIO = 20          # audio barely compresses; a bomb expands a thousandfold
ACCEPTED_EXTS = audio_tags.AUDIO_EXTS + (".zip",)

_ID_RE = re.compile(r"[0-9a-f]{24}")
_RANGE_RE = re.compile(r"bytes (\d+)-(\d+)/(\d+)$")
# What a file's first bytes say it is, by extension. Only a *recognised* mismatch is refused
# (an HTML error page saved as .mp3, a zip named .m4b); an audio file nothing here recognises
# is left to mutagen, which has the last word when the item is filed.
_KIND_OF_EXT = {".zip": "zip", ".m4b": "mp4", ".m4a": "mp4", ".mp3": "mp3",
                ".flac": "flac", ".ogg": "ogg", ".opus": "ogg"}
_MP4_ATOMS = (b"ftyp", b"moov", b"mdat", b"free", b"wide", b"skip")

class UploadError(Exception):
    """A refusal with an HTTP status, a machine-readable `code` and words for a person.
    `extra` rides along in the JSON body (the offset a client should resume from)."""

    def __init__(self, status: int, code: str, message: str, **extra) -> None:
        super().__init__(message)
        self.status, self.code, self.extra = status, code, extra


# ── staging ──────────────────────────────────────────────────────────────────

def staging_dir(cfg: Config) -> Path:
    return cfg.audiobook_root / STAGING


def _ensure_staging(cfg: Config) -> Path:
    """The staging folder, made if need be. The library folder itself is never made here: a
    configured path that isn't there is usually a drive that isn't mounted, and a folder
    invented on the wrong disk would swallow the upload."""
    try:
        root = cfg.audiobook_root
    except SystemExit as exc:
        raise UploadError(503, "no_audiobook_folder", str(exc)) from None
    if not root.is_dir():
        raise UploadError(503, "no_audiobook_folder",
                          f"The audiobook folder isn't available: {root}. Check that it is "
                          "mounted, or change it in Settings.")
    d = root / STAGING
    try:
        d.mkdir(exist_ok=True)
    except OSError as exc:
        raise UploadError(503, "no_audiobook_folder",
                          f"Can't write to the audiobook folder: {exc}") from None
    return d


def _check_id(upload_id: str) -> str:
    if not _ID_RE.fullmatch(upload_id or ""):
        raise UploadError(404, "not_found", "No such upload")
    return upload_id


def _meta_path(cfg: Config, upload_id: str) -> Path:
    return staging_dir(cfg) / f"{_check_id(upload_id)}.json"


def part_path(cfg: Config, upload_id: str) -> Path:
    return staging_dir(cfg) / f"{_check_id(upload_id)}.part"


def item_dir(cfg: Config, upload_id: str) -> Path:
    return staging_dir(cfg) / _check_id(upload_id)


def _load(cfg: Config, upload_id: str) -> dict:
    meta = load_json(_meta_path(cfg, upload_id))  # a missing file reads as {}
    if not meta:
        raise UploadError(404, "not_found", "No such upload (it may have been cleared; start it again)")
    return meta


def _offset(cfg: Config, upload_id: str) -> int:
    try:
        return part_path(cfg, upload_id).stat().st_size
    except FileNotFoundError:
        return 0


def clean_name(filename: str | None) -> str:
    """The name an upload is known by: its basename, which has to be a kind we file."""
    name = Path((filename or "").replace("\\", "/")).name.strip()
    if not name or name.startswith("."):
        raise UploadError(400, "bad_name", "That file has no usable name")
    if not name.lower().endswith(ACCEPTED_EXTS):
        raise UploadError(415, "unsupported",
                          "Audiobooks can be " + ", ".join(audio_tags.AUDIO_EXTS)
                          + " files, or a .zip holding the tracks of one book")
    return name


def _free(path: Path) -> int:
    return shutil.disk_usage(path).free


# ── the protocol ─────────────────────────────────────────────────────────────

def create(cfg: Config, filename: str | None, size: int) -> dict:
    """Announce an upload. Refuses early what could only fail late: a kind of file we don't
    file, a size that can't fit, a disk that is nearly full (507)."""
    name = clean_name(filename)
    if not isinstance(size, int) or size <= 0:
        raise UploadError(400, "bad_size", "The file is empty")
    if size > MAX_BYTES:
        raise UploadError(413, "too_large", f"Too large (the limit is {MAX_BYTES >> 30} GB a file)")
    d = _ensure_staging(cfg)
    # A zip sits beside its extracted tracks until it is unpacked.
    need = size * (2 if name.lower().endswith(".zip") else 1) + FREE_MARGIN
    if _free(d) < need:
        raise UploadError(507, "no_space", "Not enough free space on the audiobook drive for this file")
    upload_id = secrets.token_hex(12)
    save_json(_meta_path(cfg, upload_id), {"id": upload_id, "filename": name, "size": size,
                                           "created": time.time()})
    return get(cfg, upload_id)


def get(cfg: Config, upload_id: str) -> dict:
    """What was announced and how much has arrived; `offset` is where to continue."""
    meta = _load(cfg, upload_id)
    return {**meta, "offset": _offset(cfg, upload_id), "chunk_size": CHUNK_SIZE}


def parse_content_range(header: str | None) -> tuple[int, int, int]:
    """`bytes 0-16777215/52428800` -> (start, end, total), end inclusive."""
    m = _RANGE_RE.match((header or "").strip())
    if not m:
        raise UploadError(400, "bad_range", "Send each chunk with a Content-Range: bytes START-END/TOTAL header")
    start, end, total = (int(g) for g in m.groups())
    if start > end or end >= total:
        raise UploadError(400, "bad_range", "That Content-Range doesn't describe a chunk of the file")
    return start, end, total


class _ChunkWriter:
    """Appends one chunk's bytes, in order, to the part file. `write` blocks (call it from a
    thread). Whatever has been written stays, even if the chunk never finishes: it is a prefix of
    the file, so the part's length is always an offset a client can continue from.

    A writer can be taken over (`kill`); it then refuses every further write, under the same lock
    that guards the file, so a stalled request that wakes up late cannot add bytes behind its
    successor's back."""

    def __init__(self, path: Path, start: int, length: int) -> None:
        self.path, self.start, self.length, self.received = path, start, length, 0
        self.dead = False
        self._lock = threading.Lock()
        self._f = open(path, "ab")

    def write(self, data: bytes) -> None:
        with self._lock:
            if self.dead:
                raise UploadError(409, "superseded", "A newer request took over this upload")
            if self.received + len(data) > self.length:
                # More than its own Content-Range says: a broken client. Undo the whole chunk.
                self._f.close()
                with contextlib.suppress(OSError):
                    os.truncate(self.path, self.start)
                self.dead = True
                raise UploadError(400, "bad_range", "The body is longer than its Content-Range says")
            self._f.write(data)
            self._f.flush()  # the file's length is the offset a resuming client reads
            self.received += len(data)

    def kill(self) -> None:
        with self._lock:
            self.dead = True
            with contextlib.suppress(OSError):
                self._f.close()

    def close(self) -> None:
        with self._lock, contextlib.suppress(OSError):
            self._f.close()


_writers_lock = threading.Lock()
_writers: dict[str, _ChunkWriter] = {}


@contextlib.contextmanager
def chunk(cfg: Config, upload_id: str, start: int, end: int, total: int):
    """Take one chunk: `with uploads.chunk(...) as w: ... w.write(data)`. It must begin exactly
    where the file stands (a 409 carrying the real `offset` otherwise, which is how a client
    that missed a response finds its place).

    The newest request for an upload wins. A client sends one chunk at a time, so a second
    request means it gave up on the first (a connection that stalled, then was abandoned); the
    server may not have noticed, because a dead TCP connection can look alive for minutes. The old
    writer is taken over rather than waited for, and what it had written is kept. A chunk that
    ends early (a dropped connection) keeps what arrived too, so a link that dies every 10 MB
    still gets a 16 MB chunk across, 10 MB at a time."""
    meta = _load(cfg, upload_id)
    if total != meta["size"]:
        raise UploadError(400, "bad_range", "The total in Content-Range isn't the size announced")
    length = end - start + 1
    if length > MAX_CHUNK:
        raise UploadError(413, "chunk_too_large", f"Send chunks of at most {MAX_CHUNK >> 20} MB")
    with _writers_lock:
        older = _writers.pop(upload_id, None)
    if older:
        older.kill()
    offset = _offset(cfg, upload_id)  # after the takeover: what the old writer wrote counts
    if start != offset:
        raise UploadError(409, "offset_mismatch",
                          f"This chunk starts at {start} but the upload is at {offset}", offset=offset)
    if _free(staging_dir(cfg)) < length + FREE_MARGIN:
        raise UploadError(507, "no_space", "Not enough free space on the audiobook drive", offset=offset)
    writer = _ChunkWriter(part_path(cfg, upload_id), start, length)
    with _writers_lock:
        _writers[upload_id] = writer
    try:
        yield writer
        if writer.dead:  # taken over while it was still arriving
            raise UploadError(409, "superseded", "A newer request took over this upload")
        if writer.received != length:
            raise UploadError(400, "short_chunk", "The chunk ended before its Content-Range did",
                              offset=start + writer.received)
    finally:
        writer.close()
        with _writers_lock:
            if _writers.get(upload_id) is writer:
                del _writers[upload_id]


def sniff(path: Path) -> str | None:
    """What a file's first bytes say it is: zip, mp4, mp3, flac, ogg, or None."""
    with open(path, "rb") as f:
        head = f.read(12)
    if head[:2] == b"PK" and head[2:4] in (b"\x03\x04", b"\x05\x06"):
        return "zip"
    if head[4:8] in _MP4_ATOMS:
        return "mp4"
    if head[:4] == b"fLaC":
        return "flac"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        return "mp3"
    return None


def check_complete(cfg: Config, upload_id: str) -> dict:
    """The upload's record once every byte is in and the content is what its name says."""
    meta = _load(cfg, upload_id)
    offset = _offset(cfg, upload_id)
    if offset != meta["size"]:
        raise UploadError(409, "incomplete", f"Only {offset} of {meta['size']} bytes have arrived",
                          offset=offset)
    expected = _KIND_OF_EXT[Path(meta["filename"]).suffix.lower()]
    found = sniff(part_path(cfg, upload_id))
    if found != expected and (found is not None or expected == "zip"):
        raise UploadError(400, "bad_type", f"{meta['filename']} isn't "
                          + ("a zip file" if expected == "zip" else f"a real {Path(meta['filename']).suffix} file"))
    return meta


def discard(cfg: Config, upload_id: str) -> bool:
    """Remove everything staged for an upload. False when there was nothing."""
    existed = False
    for path in (_meta_path(cfg, upload_id), part_path(cfg, upload_id)):
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
            existed = True
    d = item_dir(cfg, upload_id)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
        existed = True
    return existed


# ── unpacking ────────────────────────────────────────────────────────────────

def _natural(name: str) -> list:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def _checked_name(info: zipfile.ZipInfo) -> str:
    """The entry's name with `/` separators, or a refusal: absolute and drive paths, `..`,
    symlinks and encrypted entries are never extracted, and one of them anywhere in the zip
    refuses the whole zip."""
    name = info.filename.replace("\\", "/")
    if (name.startswith("/") or "\x00" in name or ".." in name.split("/")
            or re.match(r"[A-Za-z]:", name)):
        raise UploadError(400, "unsafe_zip", f"The zip has an unsafe path ({info.filename!r}) and was refused")
    if (info.external_attr >> 16) & 0o170000 == 0o120000:
        raise UploadError(400, "unsafe_zip", "The zip contains a symbolic link and was refused")
    if info.flag_bits & 0x1:
        raise UploadError(400, "unsafe_zip", "The zip is password-protected")
    return name


def _junk(name: str) -> bool:
    base = name.rsplit("/", 1)[-1]
    return name.startswith("__MACOSX/") or base.startswith("._") or base in (".DS_Store", "Thumbs.db")


def safe_extract(zip_path: Path, dest: Path) -> list[Path]:
    """Unpack one book's tracks (and its cover image, if it has one) into the new folder
    `dest`, flat. Returns the audio files in play order.

    Everything else in the zip is ignored, not extracted. The caps are on what is *kept*: how
    many files, and how many bytes (declared by the zip, counted again as they are written,
    and at most `ZIP_MAX_RATIO` times the zip's own size, since audio doesn't compress and a
    bomb does). Tracks from several folders (CD1/, CD2/) are numbered into one folder in order;
    so are tracks whose names would not sort into play order (1, 2, 10)."""
    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError):
        raise UploadError(400, "bad_zip", "That isn't a readable zip file") from None
    with zf:
        infos = zf.infolist()
        if len(infos) > ZIP_MAX_ENTRIES:
            raise UploadError(400, "zip_bomb", "The zip has too many files in it")
        audio: list[tuple[str, zipfile.ZipInfo]] = []
        covers: list[tuple[str, zipfile.ZipInfo]] = []
        for info in infos:
            name = _checked_name(info)
            if info.is_dir() or _junk(name):
                continue
            base = name.rsplit("/", 1)[-1]
            stem, _, ext = base.rpartition(".")
            if f".{ext.lower()}" in audio_tags.AUDIO_EXTS:
                audio.append((name, info))
            elif f".{ext.lower()}" in COVER_EXTS and stem.lower() in COVER_NAMES:
                covers.append((name, info))
        if not audio:
            raise UploadError(400, "no_audio", "The zip has no audio files in it")
        if len(audio) > ZIP_MAX_FILES:
            raise UploadError(400, "zip_bomb", f"The zip has more than {ZIP_MAX_FILES} audio files")
        audio.sort(key=lambda a: _natural(a[0]))
        covers.sort(key=lambda c: (c[0].count("/"), _natural(c[0])))
        cover = covers[0][1] if covers else None
        keep = audio + covers[:1]
        declared = sum(i.file_size for _, i in keep)
        if declared > MAX_BYTES or declared > ZIP_MAX_RATIO * zip_path.stat().st_size + (1 << 20):
            raise UploadError(400, "zip_bomb", "The zip expands to far more than its size; it was refused")
        dest.mkdir(parents=True, exist_ok=True)
        if _free(dest) < declared + FREE_MARGIN:
            raise UploadError(507, "no_space", "Not enough free space on the audiobook drive to unpack the zip")

        bases = [n.rsplit("/", 1)[-1] for n, _ in audio]
        in_order = [b.lower() for b in bases] == sorted(b.lower() for b in bases)
        flat = (len({n.rpartition("/")[0] for n, _ in audio}) == 1
                and len({b.lower() for b in bases}) == len(bases) and in_order)
        width = max(3, len(str(len(audio))))
        budget = declared
        out: list[Path] = []
        try:
            for i, (name, info) in enumerate(keep, 1):
                base = name.rsplit("/", 1)[-1]
                if info is cover:
                    target = dest / f"cover{Path(base).suffix.lower()}"
                else:
                    target = dest / (base if flat else f"{i:0{width}d} {base}")
                with zf.open(info) as src, open(target, "xb") as dst:
                    while data := src.read(1 << 20):
                        budget -= len(data)
                        if budget < 0:
                            raise UploadError(400, "zip_bomb", "The zip holds more than it says; it was refused")
                        dst.write(data)
                if target.suffix.lower() in audio_tags.AUDIO_EXTS:
                    out.append(target)
        except (zipfile.BadZipFile, zlib.error, NotImplementedError, RuntimeError, EOFError):
            raise UploadError(400, "bad_zip", "The zip is damaged or uses an unsupported compression") from None
        return out


def stage_item(cfg: Config, upload_id: str) -> tuple[Path, str]:
    """Turn a finished upload into the folder `intake.file_item` takes: `staging/{id}/` holding
    the audio file, or a zip's tracks. Returns (that folder, the name the upload was sent as).
    Starts over if an earlier attempt left a folder behind."""
    meta = check_complete(cfg, upload_id)
    d = item_dir(cfg, upload_id)
    shutil.rmtree(d, ignore_errors=True)
    part = part_path(cfg, upload_id)
    if meta["filename"].lower().endswith(".zip"):
        safe_extract(part, d)
        part.unlink()  # the zip's space back, now that its tracks are out
    else:
        d.mkdir()
        os.replace(part, d / meta["filename"])
    return d, meta["filename"]


# ── housekeeping ─────────────────────────────────────────────────────────────

def sweep(cfg: Config, *, now: float | None = None, max_age: float = STALE_AFTER) -> int:
    """Remove uploads nobody has touched for `max_age` seconds (default a week), part files,
    records and half-filed folders alike; an upload is as fresh as its newest file. Returns
    how many were removed. Runs at startup, so a crash or an abandoned tab can't fill the disk."""
    try:
        d = staging_dir(cfg)
        entries = list(d.iterdir()) if d.is_dir() else []
    except (OSError, SystemExit):
        return 0
    now = time.time() if now is None else now
    def stem(p: Path) -> str:  # {id}.json, {id}.part and {id}/ are one upload
        return p.name.split(".", 1)[0]

    newest: dict[str, float] = {}
    for p in entries:
        try:
            newest[stem(p)] = max(newest.get(stem(p), 0.0), p.stat().st_mtime)
        except OSError:
            continue
    stale = {k for k, m in newest.items() if now - m > max_age}
    for p in entries:
        if stem(p) in stale:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                with contextlib.suppress(OSError):
                    p.unlink()
    return len(stale)
