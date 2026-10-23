"""Generate an audiobook alignment by running SubPlz.

`align.ingest` takes an SRT made elsewhere; this makes one on this machine, on
request. It runs the configured `subplz_cmd` once per audio file of the book's
linked local audiobook (the m4b, or each mp3 of a folder) against a plain-text
copy of the book (`annotate.sentence_texts`, one sentence per line, so the cues
that come back are the sentences `align.match_cues` compares against), then
hands every SRT to `align.ingest_parts`: the stored files and the map are
exactly what an upload would have produced.

A run is hours of Whisper, so it has its own queue and worker and never takes
`_index_lock` (it writes only `data/alignments/`, like the ingest route). One
job runs at a time. State is in memory, like `BookJobs`.

Resume: a run works in `data/alignments/.work/{book_id}/`. Each audio file's SRT
lands in its own `out-NNN/` folder only when the command finished, so a failed
or cancelled run keeps the files already done and a retry skips them. Nothing
reaches the book's stored alignment until every file has an SRT, and a failure
(a command that is missing, exits non-zero, writes no SRT, or an SRT that can't
be matched) leaves any existing alignment untouched.
"""
from __future__ import annotations

import atexit
import os
import queue
import shlex
import shutil
import signal
import subprocess
import threading
import time
import traceback
from collections.abc import Callable
from pathlib import Path

from . import align, annotate
from .config import Config

_PLACEHOLDERS = ("{audio}", "{text}", "{out}")
_TAIL_BYTES = 600  # how much of the command's output an error message carries


class JobCancelled(Exception):
    pass


def command_problem(cfg: Config) -> str | None:
    """Why `subplz_cmd` can't be run, or None if it can."""
    return template_problem(cfg.subplz_cmd)


def template_problem(template: str) -> str | None:
    if not template:
        return "subplz_cmd is not set in the config ([alignment] subplz_cmd)"
    try:
        argv = shlex.split(template)
    except ValueError as exc:
        return f"subplz_cmd is not a valid command: {exc}"
    if not argv:
        return "subplz_cmd is empty"
    if "{audio}" not in template or "{text}" not in template:
        return "subplz_cmd must contain {audio} and {text}"
    return None


def _argv(template: str, audio: Path, text: Path, out: Path) -> list[str]:
    values = {"{audio}": str(audio), "{text}": str(text), "{out}": str(out)}
    argv = []
    for arg in shlex.split(template):
        for token, value in values.items():
            arg = arg.replace(token, value)  # not str.format: other braces stay as typed
        argv.append(arg)
    return argv


def stop_process(proc: subprocess.Popen) -> None:
    """Terminate the command and anything it spawned (it runs in its own session)."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


def out_dir(work: Path, i: int) -> Path:
    """Where file `i`'s finished SRT(s) are kept in a work dir."""
    return work / f"out-{i:03d}"


def transcribe(template: str, audio: Path, text: Path, work: Path, i: int, *,
               on_start: Callable[[subprocess.Popen], None] | None = None,
               on_end: Callable[[], None] | None = None,
               cancelled: Callable[[], bool] | None = None) -> Path:
    """One run of the command on audio file `i` into a scratch folder, renamed to
    `out_dir(work, i)` only when it exited 0 and wrote an SRT, so a failed or
    interrupted run leaves nothing a retry would take for done. Shared by the
    server's job and `align_remote`. `on_start`/`on_end` bracket the
    process (the job keeps it to cancel it); `cancelled` is asked after the
    command ends. Ctrl-C (or anything else that unwinds the wait) stops the
    command's process group before it propagates. Raises RuntimeError with the
    tail of the command's output, or JobCancelled."""
    done = out_dir(work, i)
    scratch = work / f"out-{i:03d}.partial"
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True)
    log = work / "run.log"
    argv = _argv(template, audio, text, scratch)
    with open(log, "wb") as logf:
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=logf,
                                    stderr=subprocess.STDOUT, start_new_session=True,
                                    cwd=work)  # SubPlz keeps a ./config/ cache in its cwd
        except OSError as exc:
            raise RuntimeError(f"could not run {argv[0]}: {exc}") from exc
        if on_start:
            on_start(proc)
        try:
            code = proc.wait()
        except BaseException:
            stop_process(proc)
            proc.wait()
            raise
        finally:
            if on_end:
                on_end()
    if cancelled and cancelled():
        raise JobCancelled()
    if code != 0:
        raise RuntimeError(f"{Path(argv[0]).name} exited with {code}: {_tail(log)}")
    if not any(scratch.glob("*.srt")):
        raise RuntimeError(f"{Path(argv[0]).name} wrote no .srt into {{out}}: {_tail(log)}")
    shutil.rmtree(done, ignore_errors=True)
    scratch.rename(done)
    return done


def read_srt(folder: Path, audio: Path) -> str:
    found = sorted(folder.glob("*.srt"))
    # Prefer the one named after the audio file when the command wrote several.
    pick = next((f for f in found if f.stem.startswith(audio.stem)), found[0])
    return pick.read_text("utf-8-sig", errors="replace")


class AlignJobs:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._queue: queue.Queue[dict] = queue.Queue()
        self._state: dict[str, dict] = {}
        self._cancel: set[str] = set()
        self._proc: tuple[str, subprocess.Popen] | None = None
        self._worker: threading.Thread | None = None
        self._mu = threading.Lock()  # guards _state, _cancel, _proc and worker start
        atexit.register(self.shutdown)

    def enqueue(self, book: dict, ab_id: str, files: list[str] | None,
                audio: list[tuple[str | None, Path]]) -> dict:
        """Queue a run for the book. `audio` is `(part name, path)` per file in play
        order (part name None for a single m4b). Idempotent while one is queued or
        running. Returns the job state."""
        book_id = book["book_id"]
        with self._mu:
            cur = self._state.get(book_id)
            if cur and cur["state"] in ("queued", "running"):
                return dict(cur)
            self._cancel.discard(book_id)
            self._state[book_id] = {"state": "queued", "step": None, "error": None,
                                    "queued_at": time.time(), "finished_at": None}
            self._queue.put({"book": book, "ab_id": ab_id, "files": files, "audio": audio,
                             "token": self._state[book_id]["queued_at"]})
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._loop, daemon=True, name="align-jobs")
                self._worker.start()
            return dict(self._state[book_id])

    def status(self, book_id: str) -> dict | None:
        with self._mu:
            cur = self._state.get(book_id)
            return dict(cur) if cur else None

    def cancel(self, book_id: str) -> bool:
        """Stop a queued or running run. Whether there was one."""
        with self._mu:
            cur = self._state.get(book_id)
            if not cur or cur["state"] not in ("queued", "running"):
                return False
            if cur["state"] == "queued":  # the worker will find it cancelled and skip it
                cur.update(state="cancelled", finished_at=time.time())
                return True
            self._cancel.add(book_id)
            if self._proc and self._proc[0] == book_id:
                stop_process(self._proc[1])
            return True

    def shutdown(self) -> None:
        """Don't leave a transcription running after the server exits."""
        with self._mu:
            if self._proc:
                stop_process(self._proc[1])

    def _set(self, book_id: str, **fields) -> None:
        with self._mu:
            self._state[book_id].update(fields)

    def _loop(self) -> None:
        while True:
            job = self._queue.get()
            book_id = job["book"]["book_id"]
            with self._mu:
                cur = self._state[book_id]
                if cur["state"] != "queued" or cur["queued_at"] != job["token"]:
                    continue  # cancelled while it waited (and maybe queued again since)
                cur.update(state="running", step="preparing")
            try:
                self._run(job)
                result: dict = {"state": "done", "error": None}
            except JobCancelled:
                result = {"state": "cancelled", "error": None}
            except (align.AlignError, annotate.NoPack) as exc:
                result = {"state": "error",
                          "error": exc.reason if str(exc) == exc.reason else f"{exc.reason}: {exc}"}
            except BaseException as exc:  # noqa: BLE001 — never kill the worker
                traceback.print_exc()
                result = {"state": "error", "error": f"{type(exc).__name__}: {exc}"}
            self._set(book_id, step=None, finished_at=time.time(), **result)

    def _run(self, job: dict) -> None:
        cfg, book = self._cfg, job["book"]
        book_id = book["book_id"]
        problem = command_problem(cfg)
        if problem:
            raise RuntimeError(problem)
        work = align.work_dir(cfg, book_id)
        work.mkdir(parents=True, exist_ok=True)

        text = work / "book.txt"
        text.write_text(align.book_text(cfg, book), encoding="utf-8")

        audio = job["audio"]
        srts: dict[str | None, str] = {}
        for i, (part, path) in enumerate(audio):
            self._check_cancelled(book_id)
            done = out_dir(work, i)
            if not any(done.glob("*.srt")):
                self._set(book_id, step=f"{i + 1}/{len(audio)} {part or path.name}")
                transcribe(cfg.subplz_cmd, path, text, work, i, on_start=self._started(book_id),
                           on_end=self._ended, cancelled=lambda: self._is_cancelled(book_id))
            srts[part] = read_srt(done, path)
        self._check_cancelled(book_id)
        self._set(book_id, step="matching")
        align.ingest_parts(cfg, book, job["ab_id"], job["files"], srts)
        shutil.rmtree(work, ignore_errors=True)

    def _is_cancelled(self, book_id: str) -> bool:
        with self._mu:
            return book_id in self._cancel

    def _check_cancelled(self, book_id: str) -> None:
        if self._is_cancelled(book_id):
            raise JobCancelled()

    def _started(self, book_id: str) -> Callable[[subprocess.Popen], None]:
        def on_start(proc: subprocess.Popen) -> None:
            with self._mu:
                self._proc = (book_id, proc)
                if book_id in self._cancel:  # cancelled between the check and the start
                    stop_process(proc)
        return on_start

    def _ended(self) -> None:
        with self._mu:
            self._proc = None


def _tail(log: Path) -> str:
    try:
        data = log.read_bytes()[-_TAIL_BYTES:]
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace").strip()
