"""Export and import of a Hondana data folder: back it up, move it to another machine, or
recover it after a disk dies (`hondana export`, `hondana import`).

**Export** writes one zip. `manifest.json` says what is in it (`format`, `schema`, the app
version, when, the scope, and every file's path, size, mtime and sha256, plus counts of what
the archive holds); the files sit under `data/`, laid out as they are in the data folder.
`scope="full"` is everything except what never travels (`never`), so the slow caches come
along: vocabulary, reader packs, covers, the audiobook id history. `scope="curated"` is only
the hand-made files (`data_mirror.curated_files`), the same list the git mirror holds; the
rest is rebuilt by `hondana all`.

**Import** reads a zip made by export, a data folder, a checkout that holds one,
or a clone of the data mirror. It is a dry run unless told to `apply`, refuses a
folder that already holds someone's work unless told to `replace` (and then saves a full export
of it first), and writes each file through a temp file with its mtime restored. A zip is checked
before anything is written: every path relative and inside the folder, no links, nothing the
manifest doesn't list, sizes and sha256 as recorded.

Nothing is imported while a server is running on the data folder: the server holds
`<data_dir>/.server.lock` (see `cli.cmd_serve`) and so does an import in progress.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import time
import zipfile
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import data_mirror
from .config import Config
from .index_store import replace_file

FORMAT = "hondana-backup"
SCHEMA = 1
MANIFEST = "manifest.json"
PREFIX = "data/"
SERVER_LOCK = ".server.lock"
SCOPES = ("full", "curated")
REBUILDS = ("index", "all", "none")
_CHUNK = 1 << 20
_MAX_MANIFEST = 256 << 20  # a manifest bigger than this is not one of ours

# What never travels: secrets and per-machine files, logs, scratch space, the search index
# (rebuilt), and every temp file a half-finished write leaves.
_NEVER_TOP = frozenset({"auth.json", "secret.key", "hondana.lock", "hondana.url", SERVER_LOCK,
                        "consolidation_plan.json"})  # the last one holds absolute paths
_NEVER_DIRS = ("logs", ".hondana-staging", "alignments/.work", "backups", ".git")
_STORED = frozenset({".gz", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".zip", ".m4b", ".m4a",
                     ".mp3", ".flac", ".opus", ".ogg"})  # already compressed: stored, not deflated
_DRIVE = re.compile(r"^[A-Za-z]:")


class BackupError(Exception):
    """Something to tell the user and stop on (nothing half-done is left behind)."""


@dataclass
class Entry:
    path: str                  # relative to the data folder, `/`-separated
    size: int
    mtime_ns: int
    sha256: str | None = None  # known for a zip (its manifest), not for a folder


@dataclass
class Inspection:
    kind: str                  # backup | data | checkout | mirror
    source: Path
    root: Path | None          # the data folder (None for a zip)
    scope: str                 # full | curated
    entries: list[Entry]
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (path, why)
    warnings: list[str] = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    manifest: dict | None = None

    @property
    def total_bytes(self) -> int:
        return sum(e.size for e in self.entries)


@dataclass
class Result:
    applied: bool
    inspection: Inspection
    written: int = 0
    backup: Path | None = None
    before: dict = field(default_factory=dict)
    after: dict = field(default_factory=dict)
    overwrites: list[str] = field(default_factory=list)
    settings: dict | None = None  # the source's settings.json values, when it wasn't imported


# ── what never travels ───────────────────────────────────────────────────────

def never(rel: str) -> bool:
    """Whether a path under the data folder is never exported and never imported."""
    parts = rel.split("/")
    name = parts[-1]
    if name == ".DS_Store" or name.endswith((".part", ".tmp")) or name.startswith(".upload-"):
        return True
    if len(parts) == 1 and (name in _NEVER_TOP or name.startswith("search.sqlite3")):
        return True
    if any(rel == d or rel.startswith(d + "/") for d in _NEVER_DIRS):
        return True
    return len(parts) == 3 and parts[0] == "users" and name in data_mirror.USER_DERIVED


def _safe_rel(name: Any) -> str:
    """`name` as a relative `/`-separated path that stays inside the data folder, else BackupError."""
    if not isinstance(name, str) or not name or len(name) > 1024 or "\0" in name or "\\" in name:
        raise BackupError(f"unsafe path in the backup: {name!r}")
    parts = name.split("/")
    if name.startswith("/") or _DRIVE.match(parts[0]) or any(p in ("", ".", "..") for p in parts):
        raise BackupError(f"unsafe path in the backup: {name!r}")
    return name


# ── listing a folder ─────────────────────────────────────────────────────────

def _walk(root: Path, skipped: list[tuple[str, str]] | None = None,
          warnings: list[str] | None = None) -> Iterator[Entry]:
    """Every regular file under `root` that may travel, by relative path. Links are never
    followed or copied."""
    for here, dirs, files in os.walk(root, followlinks=False):
        base = Path(here)
        rel_base = "" if base == root else base.relative_to(root).as_posix()
        keep = []
        for d in sorted(dirs):
            rel = f"{rel_base}/{d}" if rel_base else d
            if (base / d).is_symlink():
                if warnings is not None:
                    warnings.append(f"{rel}: a link, left out")
            elif never(rel + "/x"):
                if skipped is not None:
                    skipped.append((rel + "/", "never copied"))
            else:
                keep.append(d)
        dirs[:] = keep
        for name in sorted(files):
            rel = f"{rel_base}/{name}" if rel_base else name
            if never(rel):
                if skipped is not None and name != ".DS_Store":
                    skipped.append((rel, "never copied"))
                continue
            try:
                st = os.lstat(base / name)
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode):
                if warnings is not None:
                    warnings.append(f"{rel}: a link, left out")
            elif stat.S_ISREG(st.st_mode):
                yield Entry(rel, st.st_size, st.st_mtime_ns)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# ── counts ───────────────────────────────────────────────────────────────────

def _empty(value: Any) -> bool:
    if isinstance(value, dict):
        return all(_empty(v) for v in value.values())
    if isinstance(value, (list, str)):
        return len(value) == 0
    return value is None


def _counts(names: Iterable[str], read: Callable[[str], Any]) -> dict:
    """What a data folder holds, from its file names and a way to read one as JSON. The one
    definition behind the numbers export, import and the cutover checks print."""
    names = set(names)

    def load(rel: str, default: Any) -> Any:
        if rel not in names:
            return default
        try:
            value = read(rel)
        except (OSError, ValueError):
            return default
        return default if value is None else value

    def size(rel: str) -> int:
        value = load(rel, {})
        return len(value) if isinstance(value, (dict, list)) else 0

    def mapping(rel: str) -> dict:
        value = load(rel, {})
        return value if isinstance(value, dict) else {}

    registry = load("profiles.json", {})
    slugs = [p["slug"] for p in registry.get("profiles", []) if isinstance(p, dict) and p.get("slug")] \
        if isinstance(registry, dict) else []
    if not slugs:
        slugs = sorted({n.split("/")[1] for n in names if n.startswith("users/") and n.count("/") == 2})
    languages = {p.get("slug"): p.get("language") for p in registry.get("profiles", [])
                 if isinstance(p, dict)} if isinstance(registry, dict) else {}

    profiles = {}
    for slug in slugs:
        u = f"users/{slug}/"
        words = mapping(u + "known_words.json")
        read_books = mapping(u + "read_books.json").get("books")
        learning = mapping(u + "learning.json").get("words")
        profiles[slug] = {
            "language": languages.get(slug),
            "known_words": len(words.get("words") or []),
            "marked": len(words.get("marked") or []),
            "read_books": sum(1 for e in (read_books.values() if isinstance(read_books, dict) else ())
                              if isinstance(e, dict) and e.get("read")),
            "reader_progress": size(u + "reader_progress.json"),
            "audio_progress": size(u + "audio_progress.json"),
            "bookmarks": sum(1 for e in mapping(u + "bookmarks.json").values()
                             if isinstance(e, dict) and not e.get("deleted")),
            "learning": len(learning) if isinstance(learning, dict) else 0,
        }
    catalog = set(mapping("audiobook_catalog_base.json")) | set(mapping("audiobook_catalog_extra.json"))
    books = {n.split("/")[1].split(".")[0] for n in names
             if n.startswith("alignments/") and n.endswith((".srt", ".anchors.jsonl")) and n.count("/") == 1}
    return {"books": size("epub_library.json"), "audiobook_files": size("audiobook_files.json"),
            "catalog_rows": len(catalog), "alignments": len(books), "profiles": profiles}


def counts(data_dir: Path) -> dict:
    """What the data folder holds (books, audiobook files, catalog rows, alignments, and per
    profile its known words, read books, progress, bookmarks and learning words)."""
    names = [e.path for e in _walk(data_dir)]
    return _counts(names, lambda rel: _read_json(data_dir / rel))


def format_counts(c: dict) -> list[str]:
    """`counts` as lines for a person to read."""
    lines = [f"  {c['books']} books · {c['audiobook_files']} audiobook files · {c['catalog_rows']} "
             f"catalog rows · {c['alignments']} aligned"]
    for slug, p in c["profiles"].items():
        who = f"{slug} ({p['language']})" if p.get("language") else slug
        marked = f" (+{p['marked']} marked)" if p["marked"] else ""
        lines.append(f"  {who}: {p['known_words']} known words{marked} · {p['read_books']} read · "
                     f"{p['reader_progress']} reading positions · {p['audio_progress']} listening positions · "
                     f"{p['bookmarks']} bookmarks · {p['learning']} learning")
    return lines


def user_data(data_dir: Path) -> list[str]:
    """Why the folder holds someone's own work ([] for a new or empty one): what an import
    must not silently overwrite. Rebuildable indexes don't count."""
    if not data_dir.is_dir():
        return []
    entries = list(_walk(data_dir))
    names = {e.path for e in entries}
    c = _counts(names, lambda rel: _read_json(data_dir / rel))
    why = []
    for slug, p in c["profiles"].items():
        for key, label in (("known_words", "known words"), ("marked", "words marked known"),
                           ("read_books", "books marked read"), ("reader_progress", "reading positions"),
                           ("audio_progress", "listening positions"), ("bookmarks", "bookmarks"),
                           ("learning", "learning words")):
            if p[key]:
                why.append(f"{slug}: {p[key]} {label}")
    for name in data_mirror.ROOT_FILES:
        if name in ("profiles.json", "settings.json") or name not in names:
            continue
        try:
            if not _empty(_read_json(data_dir / name)):
                why.append(f"hand-made data in {name}")
        except (OSError, ValueError):
            why.append(f"{name} (unreadable)")
    for rel, label in (("covers/custom/", "hand-set covers"), ("covers/audiobooks/custom/", "hand-set audiobook covers"),
                       ("alignments/", "audio sync files")):
        if any(data_mirror.is_curated(n) and n.startswith(rel) for n in names):
            why.append(label)
    return why


# ── export ───────────────────────────────────────────────────────────────────

def _version() -> str:
    try:
        from importlib.metadata import version
        return version("hondana")
    except Exception:  # noqa: BLE001 — a source checkout that isn't installed
        return "unknown"


def _unique(path: Path) -> Path:
    """`path`, or with `-2`, `-3`… before the suffix when something is already there: a backup
    named by the second must never replace another (an import may be reading it)."""
    n, candidate = 1, path
    while candidate.exists():
        n += 1
        candidate = path.with_name(f"{path.stem}-{n}{path.suffix}")
    return candidate


def default_out(cfg: Config, scope: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return _unique(cfg.data_dir / "backups" / f"hondana-{'curated-' if scope == 'curated' else ''}{stamp}.zip")


def _zip_time(mtime: float) -> tuple[int, ...]:
    t = time.localtime(mtime)[:6]
    return t if t[0] >= 1980 else (1980, 1, 1, 0, 0, 0)


def _add(zf: zipfile.ZipFile, rel: str, src: Path) -> dict:
    """One file into the zip, hashed as it goes. FileNotFoundError when it vanished."""
    st = os.stat(src)
    info = zipfile.ZipInfo(PREFIX + rel, date_time=_zip_time(st.st_mtime))
    info.compress_type = zipfile.ZIP_STORED if Path(rel).suffix.lower() in _STORED else zipfile.ZIP_DEFLATED
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    h, n = hashlib.sha256(), 0
    with open(src, "rb") as fh, zf.open(info, "w", force_zip64=st.st_size >= (1 << 31) - 1) as dst:
        while chunk := fh.read(_CHUNK):
            h.update(chunk)
            dst.write(chunk)
            n += len(chunk)
    return {"path": rel, "size": n, "mtime_ns": st.st_mtime_ns, "sha256": h.hexdigest()}


def export_data(cfg: Config, out: Path | str | None = None, scope: str = "full",
                say: Callable[[str], None] = print) -> Path:
    """Write the data folder to a zip (see the module docs) and return its path. The zip is
    written to `<out>.part` and renamed, so a failed export never leaves a half-written
    archive under the final name. Files a running server changes meanwhile are each
    consistent, but not with each other: stop the server for a backup that is."""
    if scope not in SCOPES:
        raise BackupError(f"scope is one of {', '.join(SCOPES)}")
    data_dir = cfg.data_dir
    if not data_dir.is_dir():
        raise BackupError(f"there is no data folder at {data_dir}")
    target = Path(out).expanduser() if out else default_out(cfg, scope)
    if out and target.is_dir():
        target = target / default_out(cfg, scope).name
    target = target.resolve()
    part = target.with_name(target.name + ".part")

    if scope == "curated":
        items = list(data_mirror.curated_files(data_dir))
    else:
        items = [(e.path, data_dir / e.path) for e in _walk(data_dir)]
    with contextlib.suppress(ValueError):  # the archive sits inside the folder: never pack it into itself
        own = {p.relative_to(data_dir.resolve()).as_posix() for p in (target, part)}
        items = [(rel, src) for rel, src in items if rel not in own]

    target.parent.mkdir(parents=True, exist_ok=True)
    files: list[dict] = []
    gone: list[str] = []
    try:
        with zipfile.ZipFile(part, "w", allowZip64=True) as zf:
            for rel, src in items:
                try:
                    files.append(_add(zf, rel, src))
                except FileNotFoundError:
                    gone.append(rel)  # removed while we were working
                except OSError as exc:
                    raise BackupError(f"can't read {rel}: {exc.strerror or exc}") from exc
            inside = {f["path"] for f in files}
            manifest = {
                "format": FORMAT, "schema": SCHEMA, "app_version": _version(),
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "scope": scope, "files": files,
                "counts": _counts(inside, lambda rel: _read_json(data_dir / rel)),
            }
            zf.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=1),
                        compress_type=zipfile.ZIP_DEFLATED)
        replace_file(part, target)
    except BaseException:
        with contextlib.suppress(OSError):
            part.unlink()
        raise
    total = sum(f["size"] for f in files)
    say(f"Wrote {target} ({_human(target.stat().st_size)}): {len(files)} files, "
        f"{_human(total)} of data ({scope}).")
    for line in format_counts(manifest["counts"]):
        say(line)
    if gone:
        say(f"  {len(gone)} file(s) vanished while the export ran and were left out.")
    if scope == "curated":
        say("  Only your hand-made data: run `hondana all` after importing it to rebuild the rest.")
    return target


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


# ── inspecting a source ──────────────────────────────────────────────────────

def inspect(source: Path | str, verify: bool = True) -> Inspection:
    """What `source` holds and whether it can be imported: a backup zip, a data folder, a
    checkout with a `data/` in it, or a clone of the data mirror. Raises BackupError for
    anything else, or a zip that isn't safe or isn't intact (`verify` reads every file to
    check its sha256). Never writes."""
    source = Path(source).expanduser()
    if not source.exists():
        raise BackupError(f"{source} doesn't exist")
    if source.is_file():
        if not zipfile.is_zipfile(source):
            raise BackupError(f"{source} is not a zip file, a data folder or a checkout")
        return _inspect_zip(source, verify)
    root, kind = source, "data"
    if not _is_data(source):
        if _is_data(source / "data"):
            root, kind = source / "data", "checkout"
        else:
            raise BackupError(f"{source} doesn't look like a Hondana data folder (no profiles.json or "
                              "epub_library.json), a checkout with one in data/, or a backup zip")
    elif (source / ".git").exists():
        kind = "mirror"
    skipped: list[tuple[str, str]] = []
    warnings: list[str] = []
    entries = list(_walk(root, skipped, warnings))
    if kind == "mirror":
        kept = []
        for e in entries:
            if data_mirror.is_curated(e.path):
                kept.append(e)
            else:
                skipped.append((e.path, "not hand-made data"))
        entries = kept
    entries = _drop_strays(entries, skipped, lambda rel: _read_json(root / rel), warnings)
    scope = "curated" if kind == "mirror" else "full"
    c = _counts({e.path for e in entries}, lambda rel: _read_json(root / rel))
    return Inspection(kind, source, root, scope, entries, skipped, warnings, c)


def _is_data(folder: Path) -> bool:
    return folder.is_dir() and ((folder / "profiles.json").is_file() or (folder / "epub_library.json").is_file())


def _drop_strays(entries: list[Entry], skipped: list[tuple[str, str]],
                 read: Callable[[str], Any], warnings: list[str]) -> list[Entry]:
    """Leave out `users/<slug>/` folders the source's own `profiles.json` doesn't list (an
    old registry's leftovers), by name. A source with no registry keeps them all."""
    if not any(e.path == "profiles.json" for e in entries):
        return entries
    try:
        registry = read("profiles.json")
        slugs = {p["slug"] for p in registry.get("profiles", [])}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        warnings.append("profiles.json can't be read, so every users/ folder is kept")
        return entries
    if not slugs:
        return entries
    kept, dropped = [], set()
    for e in entries:
        parts = e.path.split("/")
        if parts[0] == "users" and len(parts) > 2 and parts[1] not in slugs:
            dropped.add(parts[1])
            skipped.append((e.path, "not in profiles.json"))
        else:
            kept.append(e)
    if dropped:
        warnings.append("left out users/" + ", users/".join(sorted(dropped)) + " (not in the source's profiles.json)")
    return kept


def _inspect_zip(path: Path, verify: bool) -> Inspection:
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise BackupError(f"{path} isn't a readable zip file: {exc}") from exc
    with zf:
        infos = zf.infolist()
        names = [i.filename for i in infos if not i.is_dir()]
        if len(set(names)) != len(names):
            raise BackupError("the zip lists a file twice")
        if MANIFEST not in names:
            raise BackupError(f"{path.name} has no {MANIFEST}: it isn't a Hondana backup")
        if zf.getinfo(MANIFEST).file_size > _MAX_MANIFEST:
            raise BackupError("the manifest is too big to be a Hondana backup's")
        try:
            manifest = json.loads(zf.read(MANIFEST).decode("utf-8"))
        except (ValueError, zipfile.BadZipFile) as exc:
            raise BackupError(f"the manifest can't be read: {exc}") from exc
        if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
            raise BackupError(f"{path.name} isn't a Hondana backup (format {manifest.get('format')!r})"
                              if isinstance(manifest, dict) else f"{path.name} isn't a Hondana backup")
        schema = manifest.get("schema")
        if not isinstance(schema, int) or schema > SCHEMA:
            raise BackupError(f"this backup is schema {schema!r}, made by a newer Hondana "
                              f"(version {manifest.get('app_version')}): update before importing it")
        scope = manifest.get("scope")
        if scope not in SCOPES:
            raise BackupError(f"the manifest's scope {scope!r} isn't one of {', '.join(SCOPES)}")
        listed = manifest.get("files")
        if not isinstance(listed, list):
            raise BackupError("the manifest has no list of files")
        entries: list[Entry] = []
        seen: set[str] = set()
        for f in listed:
            if not isinstance(f, dict):
                raise BackupError("the manifest's file list is malformed")
            rel = _safe_rel(f.get("path"))
            size, mtime, sha = f.get("size"), f.get("mtime_ns"), f.get("sha256")
            if (not isinstance(size, int) or size < 0 or not isinstance(mtime, int)
                    or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha)):
                raise BackupError(f"the manifest's entry for {rel} is malformed")
            if rel in seen:
                raise BackupError(f"the manifest lists {rel} twice")
            seen.add(rel)
            entries.append(Entry(rel, size, mtime, sha))
        in_zip = {PREFIX + e.path for e in entries} | {MANIFEST}
        extra = sorted(set(names) - in_zip)
        if extra:
            raise BackupError(f"the zip holds files the manifest doesn't list (refusing it): {', '.join(extra[:3])}")
        missing = sorted(in_zip - set(names))
        if missing:
            raise BackupError(f"the zip is missing files its manifest lists: {', '.join(missing[:3])}")
        for i in infos:
            if i.is_dir():
                continue
            if i.flag_bits & 0x1:
                raise BackupError(f"{i.filename} is encrypted")
            if stat.S_ISLNK(i.external_attr >> 16):
                raise BackupError(f"{i.filename} is a link: refusing the backup")
        sizes = {PREFIX + e.path: e.size for e in entries}
        for i in infos:
            if i.filename in sizes and i.file_size != sizes[i.filename]:
                raise BackupError(f"{i.filename[len(PREFIX):]} is {i.file_size} bytes in the zip but the manifest "
                                  f"says {sizes[i.filename]}")
        if verify:
            bad = _verify(zf, entries)
            if bad:
                raise BackupError("the backup is damaged (sha256 differs): " + ", ".join(bad[:3])
                                  + (f" and {len(bad) - 3} more" if len(bad) > 3 else ""))
        skipped: list[tuple[str, str]] = []
        warnings: list[str] = []
        kept = []
        for e in entries:
            if never(e.path):
                skipped.append((e.path, "never copied"))
            else:
                kept.append(e)
        kept = _drop_strays(kept, skipped, lambda rel: json.loads(zf.read(PREFIX + rel).decode("utf-8")), warnings)
        c = _counts({e.path for e in kept}, lambda rel: json.loads(zf.read(PREFIX + rel).decode("utf-8")))
    return Inspection("backup", path, None, scope, kept, skipped, warnings, c, manifest)


def _verify(zf: zipfile.ZipFile, entries: list[Entry]) -> list[str]:
    bad = []
    for e in entries:
        h, n = hashlib.sha256(), 0
        with zf.open(PREFIX + e.path) as fh:
            while chunk := fh.read(_CHUNK):
                n += len(chunk)
                if n > e.size:
                    break
                h.update(chunk)
        if n != e.size or h.hexdigest() != e.sha256:
            bad.append(e.path)
    return bad


# ── the server guard ─────────────────────────────────────────────────────────

@contextlib.contextmanager
def hold_server_lock(data_dir: Path) -> Iterator[None]:
    """Hold `<data_dir>/.server.lock` for the block, or BackupError when a server (or another
    import) has it. A running desktop app holds `hondana.lock` and is refused too."""
    from .desktop import LOCK_NAME, InstanceLock

    app = InstanceLock(data_dir / LOCK_NAME)
    if (data_dir / LOCK_NAME).exists():
        if not app.acquire():
            raise BackupError("the Hondana app is running on this data folder: quit it first")
        app.release()
    lock = InstanceLock(data_dir / SERVER_LOCK)
    if not lock.acquire():
        raise BackupError(f"a Hondana server is running on {data_dir}: stop the server first")
    try:
        yield
    finally:
        lock.release()


# ── import ───────────────────────────────────────────────────────────────────

def _open_source(insp: Inspection) -> tuple[Callable[[Entry], Any], Callable[[], None]]:
    """`(open an entry's bytes, close the source)`."""
    if insp.kind == "backup":
        zf = zipfile.ZipFile(insp.source)
        return (lambda e: zf.open(PREFIX + e.path)), zf.close
    root = insp.root
    return (lambda e: open(root / e.path, "rb")), (lambda: None)


def _write(insp: Inspection, open_entry: Callable[[Entry], Any], entry: Entry, data_dir: Path) -> None:
    dest = data_dir / entry.path
    dest.parent.mkdir(parents=True, exist_ok=True)
    root = data_dir.resolve()
    if root not in (dest.parent.resolve(), *dest.parent.resolve().parents):
        raise BackupError(f"{entry.path} would land outside the data folder")
    tmp = dest.with_name(f".{dest.name}.import-{os.getpid()}.tmp")
    h, n = hashlib.sha256(), 0
    try:
        with open_entry(entry) as src, open(tmp, "wb") as out:
            while chunk := src.read(_CHUNK):
                n += len(chunk)
                if insp.kind == "backup" and n > entry.size:
                    raise BackupError(f"{entry.path} is bigger than the manifest says")
                h.update(chunk)
                out.write(chunk)
        if insp.kind == "backup" and (n != entry.size or h.hexdigest() != entry.sha256):
            raise BackupError(f"{entry.path} doesn't match the manifest (damaged backup)")
        os.utime(tmp, ns=(entry.mtime_ns, entry.mtime_ns))
        replace_file(tmp, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


_KEEP_ON_REPLACE = frozenset({"backups", "logs", "auth.json", "secret.key", "settings.json",
                              SERVER_LOCK, "hondana.lock", "hondana.url"})


def _clear(data_dir: Path) -> None:
    """Empty the data folder for a replace, keeping what belongs to this machine."""
    for item in sorted(data_dir.iterdir()):
        if item.name in _KEEP_ON_REPLACE:
            continue
        if item.is_dir() and not item.is_symlink():
            shutil.rmtree(item)
        else:
            item.unlink()


def import_data(cfg: Config, source: Path | str, *, apply: bool = False, replace: bool = False,
                settings: bool = False, rebuild: str = "index",
                say: Callable[[str], None] = print) -> Result:
    """Import `source` into `cfg.data_dir` (see the module docs). A dry run unless `apply`:
    it reports what would be written and changes nothing. Raises BackupError for anything
    that should stop, before any file is written."""
    if rebuild not in REBUILDS:
        raise BackupError(f"--rebuild is one of {', '.join(REBUILDS)}")
    data_dir = cfg.data_dir
    insp = inspect(source)
    where = insp.source.resolve()
    here = data_dir.resolve()
    if insp.root is not None and (insp.root.resolve() == here or here in insp.root.resolve().parents
                                  or insp.root.resolve() in here.parents):
        raise BackupError("the source and the data folder overlap: import from a copy, not from itself")
    if replace and here in where.parents and where.relative_to(here).parts[0] != "backups":
        raise BackupError(f"{source} is inside the data folder, which --replace empties: move it out first")

    entries = [e for e in insp.entries if e.path != "settings.json" or settings]
    source_settings = None
    if not settings and any(e.path == "settings.json" for e in insp.entries):
        source_settings = _source_settings(insp)
    theirs = user_data(data_dir)
    existing = [e.path for e in entries if (data_dir / e.path).exists()]
    res = Result(applied=False, inspection=insp, overwrites=existing, settings=source_settings,
                 before=counts(data_dir) if data_dir.is_dir() else _counts([], lambda rel: None))
    _report(insp, res, entries, theirs, replace, settings, say)
    if theirs and not replace:
        raise BackupError("this data folder already holds work of its own (above); import with --replace to "
                          "replace it (a full export of it is saved first), or into an empty folder")
    needed = sum(e.size for e in entries) + (sum(e.size for e in _walk(data_dir)) if replace and data_dir.is_dir() else 0)
    probe = data_dir
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    free = shutil.disk_usage(probe).free
    if free < needed * 1.05:
        raise BackupError(f"not enough disk space: {_human(needed)} needed, {_human(free)} free")
    if not apply:
        say("\nThat was a dry run: nothing was written. Add --apply to import.")
        return res

    data_dir.mkdir(parents=True, exist_ok=True)
    with hold_server_lock(data_dir):
        if replace and any(_walk(data_dir)):
            res.backup = _unique(data_dir / "backups" / ("before-import-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".zip"))
            say(f"\nSaving what is here to {res.backup} first…")
            export_data(cfg, res.backup, "full", say=lambda _: None)
            _clear(data_dir)
        open_entry, close = _open_source(insp)
        try:
            for n, e in enumerate(entries, 1):
                _write(insp, open_entry, e, data_dir)
                if n % 500 == 0:
                    say(f"  {n}/{len(entries)} files…")
        finally:
            close()
    res.applied, res.written = True, len(entries)
    say(f"\nImported {len(entries)} files ({_human(sum(e.size for e in entries))}).")
    _rebuild(cfg, rebuild, insp, say)
    res.after = counts(data_dir)
    say("\nNow in this data folder:")
    for line in format_counts(res.after):
        say(line)
    _compare(insp.counts, res.after, say)
    return res


def _source_settings(insp: Inspection) -> dict | None:
    try:
        if insp.kind == "backup":
            with zipfile.ZipFile(insp.source) as zf:
                data = json.loads(zf.read(PREFIX + "settings.json").decode("utf-8"))
        else:
            data = _read_json(insp.root / "settings.json")
    except (OSError, ValueError, KeyError):
        return None
    values = data.get("values") if isinstance(data, dict) else None
    return values if isinstance(values, dict) else None


def _report(insp: Inspection, res: Result, entries: list[Entry], theirs: list[str], replace: bool,
            settings: bool, say: Callable[[str], None]) -> None:
    kinds = {"backup": "a Hondana backup", "data": "a data folder", "checkout": "a checkout's data/ folder",
             "mirror": "a data-mirror clone"}
    say(f"Source: {insp.source} ({kinds[insp.kind]}, {insp.scope}"
        + (f", made by Hondana {insp.manifest.get('app_version')} on {insp.manifest.get('created_at')}"
           if insp.manifest else "") + ")")
    say(f"  {len(entries)} files, {_human(sum(e.size for e in entries))} to import")
    for line in format_counts(insp.counts):
        say(line)
    say("\nThis data folder now:")
    for line in format_counts(res.before):
        say(line)
    if theirs:
        say("  it holds work of its own: " + "; ".join(theirs[:6]) + (" …" if len(theirs) > 6 else ""))
    if replace:
        say("  --replace: everything here except backups/, logs/, the admin password and settings is removed "
            "after a full export of it is saved to backups/")
    elif res.overwrites:
        say(f"  {len(res.overwrites)} existing files will be overwritten")
    if insp.skipped:
        by_reason: dict[str, int] = {}
        for _, why in insp.skipped:
            by_reason[why] = by_reason.get(why, 0) + 1
        say("Left out: " + ", ".join(f"{n} ({why})" for why, n in sorted(by_reason.items())))
    for w in insp.warnings:
        say(f"  ! {w}")
    if res.settings is not None:
        say("The source's settings.json isn't imported (it holds this machine's paths). Its values, to copy by "
            "hand or import with --settings:")
        for k, v in sorted(res.settings.items()):
            say(f"    {k} = {v!r}")
    elif settings:
        say("settings.json will be imported too (--settings): check the folders it names exist on this machine.")
    if insp.scope == "curated":
        say("This holds only hand-made data: after importing, run `hondana all` to rebuild the rest.")


def _rebuild(cfg: Config, mode: str, insp: Inspection, say: Callable[[str], None]) -> None:
    from . import known, pipeline, profiles

    profiles.ensure_initialized(cfg)
    if mode == "none":
        say("\nNot rebuilding (--rebuild none): run `hondana all` or at least `hondana audio-scan` and `hondana build`.")
        return
    if mode == "all":
        say("\nRebuilding everything (hondana all)…")
        pipeline.run_all(cfg)
        known.recompute_all(cfg)
        return
    if insp.scope == "curated":
        say("\nThis was only hand-made data, so there is no index to refresh: run `hondana all` to rebuild the rest.")
        return
    say("\nRefreshing the indexes (audio-scan, build, known words)…")
    for stage in ("audio-scan", "build"):
        pipeline.STAGE_BY_ID[stage].run(cfg)
    known.recompute_all(cfg)


def _compare(source: dict, now: dict, say: Callable[[str], None]) -> None:
    """Say plainly where the imported folder doesn't hold what the source did."""
    problems = []
    for key in ("books", "audiobook_files", "catalog_rows", "alignments"):
        if source.get(key) != now.get(key):
            problems.append(f"{key}: source {source.get(key)}, now {now.get(key)}")
    for slug, p in source.get("profiles", {}).items():
        mine = now.get("profiles", {}).get(slug)
        if mine is None:
            problems.append(f"profile {slug} is missing")
            continue
        for key, value in p.items():
            if key != "language" and mine.get(key) != value:
                problems.append(f"{slug} {key}: source {value}, now {mine.get(key)}")
    if problems:
        say("\nDIFFERENCES from the source (check these):")
        for line in problems:
            say(f"  ! {line}")
    else:
        say("\nEvery count matches the source.")
