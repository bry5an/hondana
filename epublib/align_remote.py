"""Align a server's book from another machine.

The library (EPUBs and audiobooks) often lives on a server with no GPU, while a
laptop (e.g. an Apple Silicon Mac) has the GPU but not the library. `cli.py
align-remote` borrows that machine's GPU: it fetches the book's sentences and its
audio from the server, runs this machine's own
`subplz_cmd` on them (the same per-file step as the server's `align_jobs`), and
uploads the SRTs to the server, which matches them on arrival. The server stays
the source of truth, so nothing here needs the EPUB or a matcher.

A run works in `~/.cache/hondana/align/<host>/<book_id>/`:

    book.txt   the sentences the transcripts were made from (+ text.sha: the EPUB's sha256)
    audio/     downloaded files, resumable (`name.part` until complete)
    out-NNN/   one finished transcript per audio file

Everything but the upload is resumable, and nothing reaches the server until
every file has an SRT, so a failure changes nothing there. If the EPUB changed in
between (a different sha256), the transcripts are discarded: they were made from
the old text.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

from . import align_jobs, discovery

CACHE_ROOT = Path("~/.cache/hondana/align").expanduser()
# This machine's own settings, so `align-remote` needs no --config:
#   server = "http://my-server.local"
#   token = "hnd_..."          # only if the server has an admin password (Settings › Script tokens)
#   [alignment]
#   transcribe_cmd = "subplz gen --audio {audio} --output-dir {out} ..."   (preferred)
#   subplz_cmd = "subplz sync --audio {audio} --text {text} --output-dir {out} ..."
#   (or an array of arguments: ["C:\\Tools\\subplz.exe", "sync", "--audio", "{audio}", ...])
USER_CONFIG = Path("~/.config/hondana/align.toml").expanduser()
CHUNK = 1 << 20
TIMEOUT = (10, 120)  # connect, read


class RemoteError(Exception):
    """Something to tell the user and stop on (the exit is non-zero)."""


def user_settings(path: Path | None = None) -> dict[str, str | list[str]]:
    """`server`, `transcribe_cmd` and `subplz_cmd` from USER_CONFIG, leaving out what
    it doesn't set ({} when there is no such file). A file that isn't TOML is a RemoteError."""
    path = path or USER_CONFIG
    if not path.is_file():
        return {}
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise RemoteError(f"{path} isn't valid TOML: {exc}") from exc
    found: dict[str, str | list[str]] = {
        k: str(raw[k]).strip() for k in ("server", "token") if str(raw.get(k) or "").strip()}
    section = raw.get("alignment", {})
    for name in ("subplz_cmd", "transcribe_cmd"):
        cmd = section.get(name)
        if isinstance(cmd, list) and cmd:  # an argv array, as in the server's config
            found[name] = [str(a) for a in cmd]
        elif cmd and str(cmd).strip():
            found[name] = str(cmd).strip()
    return found


def find_server(given: str | None, browse: Callable[[], list[dict]] = discovery.browse) -> str:
    """`given` (from --server or align.toml), else the one Hondana server that
    Bonjour finds on this network. None, or more than one, is a RemoteError."""
    if given:
        return given
    found = browse()
    if len(found) == 1:
        print(f"Using {found[0]['name']} at {found[0]['url']} (found on this network)")
        return found[0]["url"]
    hint = f"pass --server http://<host>:8000, or set `server` in {USER_CONFIG}"
    if not found:
        raise RemoteError(f"no Hondana server found on this network; {hint}")
    listed = ", ".join(f"{s['name']} ({s['url']})" for s in found)
    raise RemoteError(f"{len(found)} Hondana servers found ({listed}); {hint}")


@dataclass
class Plan:
    """What aligning one book takes."""
    book: dict
    ab_id: str
    names: list[str]            # the server's audio file names, in play order
    parts: list[str] | None     # the mp3 names for the upload; None for a single m4b
    runtime_min: int | None
    filesize: int | None
    local: dict[str, Path] = field(default_factory=dict)  # name -> a copy already on this machine

    @property
    def title(self) -> str:
        return self.book.get("title") or self.book["book_id"]

    def to_download(self) -> int | None:
        """Bytes still to fetch, to the best the server's total tells."""
        if self.filesize is None:
            return None
        if len(self.local) == len(self.names):
            return 0
        return max(0, self.filesize - sum(p.stat().st_size for p in self.local.values()))


def human_bytes(n: int | None) -> str:
    if n is None:
        return "?"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def human_time(seconds: float) -> str:
    s = int(seconds)
    h, rest = divmod(s, 3600)
    m, s = divmod(rest, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"


# ── the server ───────────────────────────────────────────────────────────────

def _why(resp: requests.Response) -> str:
    """The server's own words for a refusal."""
    try:
        body = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code}: {resp.text[:200].strip()}"
    if isinstance(body, dict):
        reason, detail = body.get("reason"), body.get("detail")
        if reason and detail:
            return f"{reason}: {detail}"
        if reason or detail:
            return str(reason or detail)
    return f"HTTP {resp.status_code}"


class Server:
    def __init__(self, base: str, session: requests.Session | None = None, token: str = "") -> None:
        self.base = base.rstrip("/")
        self.http = session or requests.Session()
        if token:  # a server with an admin password wants it for the upload
            self.http.headers["Authorization"] = f"Bearer {token}"

    @property
    def host(self) -> str:
        return re.sub(r"[^A-Za-z0-9._-]", "_", urlparse(self.base).netloc or self.base)

    def url(self, path: str) -> str:
        return self.base + path

    def _send(self, method: str, path: str, **kw) -> requests.Response:
        kw.setdefault("timeout", TIMEOUT)
        try:
            return self.http.request(method, self.url(path), **kw)
        except requests.RequestException as exc:
            raise RemoteError(f"cannot reach {self.base}: {exc}") from exc

    def json(self, path: str) -> object:
        r = self._send("GET", path)
        if r.status_code != 200:
            raise RemoteError(f"GET {path}: {_why(r)}")
        return r.json()

    def books(self) -> list[dict]:
        return self.json("/api/books")  # type: ignore[return-value]

    def status(self, book_id: str) -> dict:
        return self.json(f"/api/books/{book_id}/status")  # type: ignore[return-value]

    def audiobook(self, ab_id: str) -> dict | None:
        return next((e for e in self.json("/api/audiobooks") if e.get("ab_id") == ab_id), None)  # type: ignore[union-attr]

    def text(self, book_id: str) -> tuple[str, str]:
        """`(sentences, sha256 of the EPUB they came from)`."""
        r = self._send("GET", f"/api/books/{book_id}/alignment/text")
        if r.status_code != 200:
            raise RemoteError(f"the server will not give the book's text: {_why(r)}")
        return r.content.decode("utf-8"), r.headers["X-Book-Sha256"]

    def audio_path(self, ab_id: str, part: str | None) -> str:
        return f"/download/audiobook/{ab_id}" + (f"?part={quote(part)}" if part else "")

    def download(self, path: str, dest: Path, progress: Callable[[int, int | None], None] | None = None) -> None:
        """Fetch `path` into `dest`, resuming a partial `dest.part` (or checking a
        finished `dest` against the server). A finished file is left in place."""
        part = dest.with_name(dest.name + ".part")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():  # confirm with the server below: a 416 saying "that is all of it"
            os.replace(dest, part)
        for attempt in (0, 1):
            have = part.stat().st_size if part.exists() else 0
            r = self._send("GET", path, stream=True, headers={"Range": f"bytes={have}-"} if have else {})
            with r:
                if r.status_code == 416:
                    total = _range_total(r.headers.get("Content-Range", ""))
                    if have and total == have:
                        os.replace(part, dest)
                        return
                    part.unlink(missing_ok=True)  # the file on the server is not the one we have
                    if attempt:
                        raise RemoteError(f"GET {path}: the server rejected the range twice")
                    continue
                if r.status_code not in (200, 206):
                    raise RemoteError(f"GET {path}: {_why(r)}")
                if r.status_code == 206:
                    start = _range_start(r.headers.get("Content-Range", ""))
                    if start != have:
                        part.unlink(missing_ok=True)
                        if attempt:
                            raise RemoteError(f"GET {path}: the server resumed at the wrong byte")
                        continue
                    mode = "ab"
                else:  # the server ignored the range
                    have, mode = 0, "wb"
                length = r.headers.get("Content-Length")
                total = have + int(length) if length and length.isdigit() else None
                got = have
                try:
                    with open(part, mode) as f:
                        for chunk in r.iter_content(CHUNK):
                            f.write(chunk)
                            got += len(chunk)
                            if progress:
                                progress(got, total)
                except requests.RequestException as exc:
                    raise RemoteError(f"download of {dest.name} broke off at {human_bytes(got)}: {exc}") from exc
            if total is not None and got != total:
                raise RemoteError(f"download of {dest.name} stopped at {human_bytes(got)} of {human_bytes(total)}")
            os.replace(part, dest)
            return

    def upload(self, book_id: str, srts: list[tuple[str | None, str]], sha: str, kind: str = "synced") -> dict:
        """All the SRTs in one request: the server stores them together or not at all."""
        files = [("file", (f"{part or 'book'}.srt", text.encode("utf-8"), "application/x-subrip"))
                 for part, text in srts]
        data = [("part", part) for part, _ in srts if part]
        if kind != "synced":  # an older server reads only file and part
            data.append(("kind", kind))
        r = self._send("POST", f"/api/books/{book_id}/alignment", files=files, data=data,
                       headers={"X-Book-Sha256": sha}, timeout=(10, 600))
        if r.status_code != 200:
            raise RemoteError(f"the server refused the alignment: {_why(r)}")
        return r.json()


def _range_total(header: str) -> int | None:
    m = re.fullmatch(r"bytes \*/(\d+)", header.strip())
    return int(m.group(1)) if m else None


def _range_start(header: str) -> int | None:
    m = re.match(r"bytes (\d+)-", header.strip())
    return int(m.group(1)) if m else None


# ── choosing books ───────────────────────────────────────────────────────────

def find_book(books: list[dict], query: str) -> dict:
    """A server book by `book_id` or a title substring; ambiguous → RemoteError listing them."""
    for b in books:
        if b["book_id"] == query:
            return b
    q = query.casefold()
    hits = [b for b in books if q in (b.get("title") or "").casefold()]
    exact = [b for b in hits if (b.get("title") or "").casefold() == q]
    hits = exact if len(exact) == 1 else hits
    if not hits:
        raise RemoteError(f"no book on the server matches {query!r}")
    if len(hits) > 1:
        shown = "\n".join(f"  {b['book_id']}  {b.get('title')}" for b in hits[:10])
        more = f"\n  … and {len(hits) - 10} more" if len(hits) > 10 else ""
        raise RemoteError(f"{len(hits)} books match {query!r}; pass a book_id:\n{shown}{more}")
    return hits[0]


def ineligible(book: dict) -> str | None:
    """Why the book cannot be aligned, from its library entry (the same refusals as
    the server's `generate`); None when it can."""
    if book.get("language") != "ja":
        return "not_ja: only Japanese books have sentences to align"
    link = book.get("audiobook") or {}
    if not (link.get("audible_isbn") and link.get("audiobook_local")):
        return "no_audiobook: the book has no audiobook on the server"
    return None


def candidates(server: Server) -> list[dict]:
    """JP books on the server with a local audiobook and no alignment, in title order."""
    out = []
    for b in server.books():
        if ineligible(b):
            continue
        if (server.status(b["book_id"]).get("alignment") or {}).get("state") == "none":
            out.append(b)
    return out


def make_plan(server: Server, book: dict, entries: dict[str, dict] | None = None,
              audio: Sequence[Path] = (), audio_dir: Path | None = None) -> Plan:
    problem = ineligible(book)
    if problem:
        raise RemoteError(f"{book.get('title') or book['book_id']}: {problem}")
    ab_id = book["audiobook"]["audible_isbn"]
    entry = entries.get(ab_id) if entries is not None else server.audiobook(ab_id)
    if not entry or not entry.get("local"):
        raise RemoteError(f"no_audiobook: {ab_id} is not on the server")
    mp3 = bool(entry.get("files"))  # a folder of tracks (mp3 or otherwise)
    names = list(entry["files"]) if mp3 else [Path(entry["path"]).name]
    if not names:
        raise RemoteError(f"no_audiobook: {ab_id} has no audio files")
    plan = Plan(book, ab_id, names, names if mp3 else None, entry.get("runtime_min"), entry.get("filesize"))
    given = {p.name: p for p in audio}
    for name in names:
        found = given.get(name) or (audio_dir / name if audio_dir else None)
        if found and found.is_file():
            plan.local[name] = found
    if not mp3 and plan.local and plan.filesize and next(iter(plan.local.values())).stat().st_size != plan.filesize:
        got = next(iter(plan.local.values())).stat().st_size
        raise RemoteError(f"{names[0]} on this machine is {human_bytes(got)}, the server's is "
                          f"{human_bytes(plan.filesize)}; the cues would not line up")
    return plan


def describe(plan: Plan) -> str:
    hours = f"{plan.runtime_min / 60:.1f} h" if plan.runtime_min else "? h"
    where = f", {len(plan.local)}/{len(plan.names)} already here" if plan.local else ""
    return (f"{plan.book['book_id']}  {plan.title}  ({len(plan.names)} file{'s' * (len(plan.names) != 1)}, "
            f"{hours}, {human_bytes(plan.to_download())} to download{where})")


# ── a run ────────────────────────────────────────────────────────────────────

@dataclass
class Result:
    book_id: str
    title: str
    skipped: str | None = None
    response: dict | None = None
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.skipped is not None or bool(self.response and self.response.get("ok"))


def work_dir(server: Server, book_id: str, root: Path | None = None) -> Path:
    return (root or CACHE_ROOT) / server.host / book_id


def align_book(server: Server, plan: Plan, subplz_cmd: align_jobs.Command, *, kind: str = "synced",
               force: bool = False,
               keep_audio: bool = False, root: Path | None = None,
               log: Callable[[str], None] = print) -> Result:
    """Take one book from the server to an uploaded alignment. `subplz_cmd` is the
    command for `kind`: `transcript` (SubPlz `gen`) or `synced` (SubPlz
    `sync`). Raises RemoteError (or whatever interrupts it: the work dir is kept
    for a retry)."""
    book, started = plan.book, time.time()
    book_id = book["book_id"]
    result = Result(book_id, plan.title)
    align = server.status(book_id).get("alignment") or {}
    # A book aligned from SubPlz sync is redone by a transcript: that is the point of one.
    done = align.get("state") == "ready" and (kind != "transcript" or align.get("source") == "transcript")
    if done and not force:
        result.skipped = f"already aligned (coverage {align.get('coverage')}); --force to redo it"
        return result
    if align.get("state") in ("queued", "running"):
        raise RemoteError("the server is already aligning this book")
    if kind == "transcript" and "transcript" not in align.get("accepts", []):
        raise RemoteError(f"{server.base} doesn't take transcripts yet (it would store this as synced); "
                          "update the server first")
    problem = (align_jobs.transcribe_problem if kind == "transcript" else align_jobs.template_problem)(subplz_cmd)
    if problem:
        raise RemoteError(problem)

    text, sha = server.text(book_id)
    work = work_dir(server, book_id, root)
    work.mkdir(parents=True, exist_ok=True)
    sha_file, text_file = work / "text.sha", work / "book.txt"
    if sha_file.exists() and sha_file.read_text().strip() != sha:
        log("The EPUB changed on the server since the last run; discarding the old transcripts.")
        for d in work.glob("out-*"):  # sync output is made from the text; a transcript is not
            shutil.rmtree(d, ignore_errors=True)
    text_file.write_text(text, encoding="utf-8")
    sha_file.write_text(sha)

    for i, name in enumerate(plan.names):
        tag = f"{i + 1}/{len(plan.names)} {name}"
        done = align_jobs.out_dir(work, i, kind)
        if any(done.glob("*.srt")):
            log(f"{tag}: transcript kept from an earlier run")
            continue
        path = plan.local.get(name)
        if path:
            log(f"{tag}: using {path}")
        else:
            path = work / "audio" / name
            log(f"{tag}: downloading")
            server.download(server.audio_path(plan.ab_id, name if plan.parts else None), path, _progress())
            _end_progress()
        log(f"{tag}: transcribing")
        t0 = time.time()
        try:
            align_jobs.transcribe(subplz_cmd, path, text_file, work, i, kind=kind)
        except RuntimeError as exc:
            raise RemoteError(str(exc)) from exc
        log(f"{tag}: done in {human_time(time.time() - t0)}")

    srts = [(name if plan.parts else None, align_jobs.read_srt(align_jobs.out_dir(work, i, kind), Path(name)))
            for i, name in enumerate(plan.names)]
    log(f"Uploading {len(srts)} {'transcript' if kind == 'transcript' else 'SRT'}{'s' * (len(srts) != 1)}…")
    result.response = server.upload(book_id, srts, sha, kind)
    result.seconds = time.time() - started
    if result.response.get("ok"):  # a poor match keeps its transcripts to look at
        if keep_audio:
            for d in [*work.glob("out-*"), *work.glob("tx-*")]:
                shutil.rmtree(d, ignore_errors=True)
        else:
            shutil.rmtree(work, ignore_errors=True)
    return result


_last = {"at": 0.0}


def _progress() -> Callable[[int, int | None], None]:
    def show(got: int, total: int | None) -> None:
        if not sys.stderr.isatty() or time.time() - _last["at"] < 0.25:
            return
        _last["at"] = time.time()
        pct = f" {got * 100 // total}%" if total else ""
        sys.stderr.write(f"\r  {human_bytes(got)}{' / ' + human_bytes(total) if total else ''}{pct}   ")
        sys.stderr.flush()
    return show


def _end_progress() -> None:
    if sys.stderr.isatty():
        sys.stderr.write("\r" + " " * 50 + "\r")


def keep_awake() -> subprocess.Popen | None:
    """macOS: don't let the laptop sleep while this process lives (`caffeinate -i -w pid`)."""
    exe = shutil.which("caffeinate")
    if not exe:
        return None
    return subprocess.Popen([exe, "-i", "-w", str(os.getpid())], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
