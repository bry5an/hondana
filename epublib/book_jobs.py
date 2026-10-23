"""Server-side per-book pipeline, queued and serialized.

A book that has just been added (upload, acquire) or that the reader asks to
analyze runs its whole chain here instead of in a browser request:

    analyze → enrich → pair → link (xref_one) → build

(`pipeline.BOOK_CHAIN`, built from the same stage registry as the CLI and /admin)
so it gets its difficulty numbers, Natively level, EN/JP pairing and audiobook
link without anyone keeping a tab open or visiting /admin. Every stage is
library-wide but incremental, so the real cost is the one new book (xref_one is
~20 ms against the full pass's ~1.5 s; pair is a cheap deterministic pass).
`analyze` ends with `known.recompute_all()`, so every profile has its stats.

One worker thread drains a FIFO queue and takes the caller's index lock around
each run, so jobs and admin stage runs never write `data/` concurrently.
Everything queued when the worker wakes runs as ONE batch: the stages are
library-wide, so analyze/enrich/pair/build run once however many books are in it
and only the per-book link step repeats. A multi-file upload `defer`s its books
and releases them together; an abandoned batch is released after DEFER_TTL.
State lives in memory — it is what `/add` and the book page poll; after a
restart a finished book is recognisable by its vocab entry, not by a job.
"""
from __future__ import annotations

import queue
import threading
import time
import traceback
from collections.abc import Callable
from typing import ContextManager

from .config import Config
from .pipeline import run_book_chain


class BookJobs:
    # How long a deferred (mid-batch) book waits for its release before it is
    # queued anyway, so closing the tab halfway through a batch loses nothing.
    DEFER_TTL = 120.0

    def __init__(self, cfg: Config, lock: Callable[[], ContextManager]) -> None:
        self._cfg = cfg
        self._lock = lock
        self._queue: queue.Queue[str] = queue.Queue()
        self._state: dict[str, dict] = {}
        self._worker: threading.Thread | None = None
        self._held: set[str] = set()
        self._held_timer: threading.Timer | None = None
        self._mu = threading.Lock()  # guards _state, _held and worker start

    def enqueue(self, book_id: str) -> dict:
        """Queue the book's pipeline. Idempotent while one is already queued or
        running (the add flow and the page's "Analyze now" can both ask)."""
        return self.enqueue_many([book_id])[book_id]

    def enqueue_many(self, book_ids) -> dict[str, dict]:
        """Queue several books atomically, so the worker sees them together and
        runs them as one batch. Returns {book_id: state}."""
        out = {}
        with self._mu:
            for book_id in book_ids:
                self._held.discard(book_id)
                cur = self._state.get(book_id)
                if not (cur and cur["state"] in ("queued", "running")):
                    self._state[book_id] = {"state": "queued", "step": None, "error": None,
                                            "queued_at": time.time(), "finished_at": None}
                    self._queue.put(book_id)
                out[book_id] = dict(self._state[book_id])
            if self._queue.qsize() and (self._worker is None or not self._worker.is_alive()):
                self._worker = threading.Thread(target=self._loop, daemon=True,
                                                name="book-jobs")
                self._worker.start()
        return out

    def defer(self, book_id: str) -> None:
        """Hold a just-added book until release() — more of its batch is coming."""
        with self._mu:
            self._held.add(book_id)
            if self._held_timer:
                self._held_timer.cancel()
            self._held_timer = threading.Timer(self.DEFER_TTL, self.release)
            self._held_timer.daemon = True
            self._held_timer.start()

    def release(self, extra=()) -> dict[str, dict]:
        """Queue every held book (plus `extra`) as one batch."""
        with self._mu:
            ids = sorted(self._held)
            if self._held_timer:
                self._held_timer.cancel()
                self._held_timer = None
        return self.enqueue_many(ids + [b for b in extra if b not in ids])

    def status(self, book_id: str) -> dict | None:
        with self._mu:
            cur = self._state.get(book_id)
            return dict(cur) if cur else None

    def _set(self, book_ids, **fields) -> None:
        with self._mu:
            for book_id in book_ids:
                self._state[book_id].update(fields)

    def _loop(self) -> None:
        while True:
            batch = [self._queue.get()]
            while True:  # everything already waiting rides along
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            self._set(batch, state="running", step=None)
            try:
                with self._lock():
                    errors = run_book_chain(self._cfg, batch,
                                          lambda s: self._set(batch, step=s))
            except BaseException as exc:  # noqa: BLE001 — never kill the worker
                traceback.print_exc()
                errors = [f"{type(exc).__name__}: {exc}"]
            self._set(batch, state="error" if errors else "done", step=None,
                      error="; ".join(errors) or None, finished_at=time.time())
