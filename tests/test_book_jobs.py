"""The server-side per-book pipeline."""
import asyncio
import json
import threading
import time

from epublib import book_jobs, pipeline
from epublib.index_store import save_json


def _stub(monkeypatch, calls, fail=()):
    def step(name):
        def run(*_a, **_k):
            calls.append(name)
            if name in fail:
                raise RuntimeError("boom")
        return run
    monkeypatch.setattr(pipeline.analyze_mod, "analyze", step("analyze"))
    monkeypatch.setattr(pipeline.enrich_mod, "enrich", step("enrich"))
    monkeypatch.setattr(pipeline.pairing_mod, "pair", step("pair"))
    monkeypatch.setattr(pipeline.xref_mod, "xref_one", step("link"))
    monkeypatch.setattr(pipeline.build_mod, "build", step("build"))
    monkeypatch.setattr(pipeline.epub_catalog_mod, "fetch_missing", step("epub-catalog"))


def _library(cfg, language="ja"):
    save_json(cfg.library_index, {"b1": {"book_id": "b1", "language": language}})


def test_runs_every_step_in_order_and_reports_progress(cfg, monkeypatch):
    calls, seen = [], []
    _stub(monkeypatch, calls)
    _library(cfg)
    assert pipeline.run_book_chain(cfg, "b1", seen.append) == []
    assert calls == seen == ["analyze", "enrich", "pair", "link", "build", "epub-catalog"]


def test_a_failed_step_does_not_stop_the_rest(cfg, monkeypatch):
    calls = []
    _stub(monkeypatch, calls, fail={"analyze"})
    _library(cfg)
    errors = pipeline.run_book_chain(cfg, "b1")
    assert calls == ["analyze", "enrich", "pair", "link", "build", "epub-catalog"]
    assert len(errors) == 1 and errors[0].startswith("analyze: RuntimeError")


def test_unanalyzable_language_skips_vocab_steps_only(cfg, monkeypatch):
    calls = []
    _stub(monkeypatch, calls)
    _library(cfg, language="fr")
    pipeline.run_book_chain(cfg, "b1")
    assert calls == ["pair", "link", "build", "epub-catalog"]


def test_vanished_book_is_reported_not_run(cfg, monkeypatch):
    calls = []
    _stub(monkeypatch, calls)
    save_json(cfg.library_index, {})
    assert pipeline.run_book_chain(cfg, "b1") == ["book is no longer in the library"]
    assert calls == []


def test_queue_is_idempotent_serialized_and_reports_state(cfg, monkeypatch):
    _library(cfg)
    gate, entered = threading.Event(), threading.Event()
    held = []
    lock = threading.Lock()

    def slow_analyze(_cfg, **_k):
        held.append(lock.locked())  # the worker holds the index lock for the job
        entered.set()
        assert gate.wait(5)

    _stub(monkeypatch, [])
    monkeypatch.setattr(pipeline.analyze_mod, "analyze", slow_analyze)
    jobs = book_jobs.BookJobs(cfg, lambda: lock)

    assert jobs.status("b1") is None
    assert jobs.enqueue("b1")["state"] == "queued"
    assert entered.wait(5)
    running = jobs.status("b1")
    assert running["state"] == "running" and running["step"] == "analyze"
    assert jobs.enqueue("b1")["state"] == "running"  # idempotent while in flight

    gate.set()
    for _ in range(100):
        if jobs.status("b1")["state"] == "done":
            break
        time.sleep(0.05)
    done = jobs.status("b1")
    assert done["state"] == "done" and done["error"] is None and held == [True]
    # a finished book can be queued again — and that run must finish before the
    # test ends, or its worker fires after the stubs are undone and its pair/link/
    # build calls land in whichever test's stubs are installed next (CI flake)
    assert jobs.enqueue("b1")["state"] == "queued"
    _wait(jobs, ["b1"])


def test_job_error_state(cfg, monkeypatch):
    _library(cfg)
    _stub(monkeypatch, [], fail={"pair"})
    jobs = book_jobs.BookJobs(cfg, lambda: threading.Lock())
    jobs.enqueue("b1")
    for _ in range(100):
        if jobs.status("b1")["state"] in ("done", "error"):
            break
        time.sleep(0.05)
    st = jobs.status("b1")
    assert st["state"] == "error" and "pair: RuntimeError" in st["error"]


def test_server_endpoints_enqueue_and_report(server, monkeypatch):
    cfg = server.cfg
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    save_json(cfg.library_index, {
        "ja1": {"book_id": "ja1", "language": "ja"},
        "fr1": {"book_id": "fr1", "language": "fr"},
    })
    queued = []
    monkeypatch.setattr(server._book_jobs, "enqueue",
                        lambda bid: queued.append(bid) or {"state": "queued"})

    r = json.loads(asyncio.run(server.analyze_book("ja1")).body)
    assert r["status"] == "queued" and queued == ["ja1"]
    r = json.loads(asyncio.run(server.analyze_book("fr1")).body)
    assert (r["status"], r["reason"]) == ("skipped", "unsupported_language")
    assert queued == ["ja1"]

    st = server.book_status("ja1", server.cfg)
    assert st["job"] is None and st["analyzable"] is True and st["vocab_status"] is None
    assert st["known"] is None
    assert server.book_status("fr1", server.cfg)["analyzable"] is False

    # Once analyze has written its results the status carries them, before `build` has
    # attached `vocab` to the library entry.
    save_json(cfg.vocab_index, {"ja1": {"status": "ok"}})
    save_json(cfg.known_stats, {"ja1": {"hard_pct": 12}})
    st = server.book_status("ja1", server.cfg)
    assert st["vocab_status"] == "ok" and st["known"] == {"hard_pct": 12}


# ── batches ────────────────────────────────────────────────────────────

def _wait(jobs, ids, states=("done", "error"), n=100):
    for _ in range(n):
        if all((jobs.status(i) or {}).get("state") in states for i in ids):
            return
        time.sleep(0.05)
    raise AssertionError({i: jobs.status(i) for i in ids})


def test_pipeline_for_a_batch_runs_library_wide_steps_once_and_links_each(cfg, monkeypatch):
    calls = []
    _stub(monkeypatch, calls)
    monkeypatch.setattr(pipeline.xref_mod, "xref_one", lambda c, bid: calls.append(f"link:{bid}"))
    save_json(cfg.library_index, {b: {"book_id": b, "language": "ja"} for b in ("a", "b", "c")})
    assert pipeline.run_book_chain(cfg, ["a", "b", "c", "gone"]) == []
    assert calls == ["analyze", "enrich", "pair", "link:a", "link:b", "link:c", "build", "epub-catalog"]


def test_batch_with_only_unanalyzable_books_skips_vocab_steps(cfg, monkeypatch):
    calls = []
    _stub(monkeypatch, calls)
    save_json(cfg.library_index, {"a": {"book_id": "a", "language": "fr"}})
    pipeline.run_book_chain(cfg, ["a"])
    assert calls == ["pair", "link", "build", "epub-catalog"]


def test_everything_queued_together_runs_as_one_batch(cfg, monkeypatch):
    save_json(cfg.library_index, {b: {"book_id": b, "language": "ja"} for b in ("a", "b", "c")})
    calls = []
    _stub(monkeypatch, calls)
    jobs = book_jobs.BookJobs(cfg, lambda: threading.Lock())
    states = jobs.enqueue_many(["a", "b", "c"])
    assert {s["state"] for s in states.values()} == {"queued"}
    _wait(jobs, "abc")
    assert calls.count("analyze") == calls.count("build") == 1


def test_deferred_books_wait_for_release_then_run_together(cfg, monkeypatch):
    save_json(cfg.library_index, {b: {"book_id": b, "language": "ja"} for b in ("a", "b")})
    calls = []
    _stub(monkeypatch, calls)
    jobs = book_jobs.BookJobs(cfg, lambda: threading.Lock())
    jobs.defer("a")
    jobs.defer("b")
    time.sleep(0.2)
    assert calls == [] and jobs.status("a") is None  # held, not running
    out = jobs.release()
    assert set(out) == {"a", "b"}
    _wait(jobs, "ab")
    assert calls.count("analyze") == 1
    assert jobs.release() == {}  # nothing left held


def test_release_also_queues_named_books_and_enqueue_unholds(cfg, monkeypatch):
    save_json(cfg.library_index, {b: {"book_id": b, "language": "ja"} for b in ("a", "b", "c")})
    _stub(monkeypatch, [])
    jobs = book_jobs.BookJobs(cfg, lambda: threading.Lock())
    jobs.defer("a")
    jobs.defer("b")
    jobs.enqueue("a")                      # a runs by itself, no longer held
    assert set(jobs.release(["c"])) == {"b", "c"}
    _wait(jobs, "abc")


def test_abandoned_batch_is_released_after_the_ttl(cfg, monkeypatch):
    save_json(cfg.library_index, {"a": {"book_id": "a", "language": "ja"}})
    calls = []
    _stub(monkeypatch, calls)
    monkeypatch.setattr(book_jobs.BookJobs, "DEFER_TTL", 0.2)
    jobs = book_jobs.BookJobs(cfg, lambda: threading.Lock())
    jobs.defer("a")                        # the tab closes; no analyze-batch ever comes
    _wait(jobs, "a")
    assert jobs.status("a")["state"] == "done" and calls.count("analyze") == 1


def test_unanalyzed_ids_skips_analyzed_and_other_languages(cfg):
    save_json(cfg.library_index, {
        "new_ja": {"language": "ja"}, "new_en": {"language": "en"},
        "done": {"language": "ja"}, "no_text": {"language": "ja"},
        "err": {"language": "en"}, "fr": {"language": "fr"},
    })
    save_json(cfg.vocab_index, {"done": {"status": "ok"}, "no_text": {"status": "no_text"},
                                "err": {"status": "error"}})
    assert pipeline.unanalyzed_ids(cfg) == ["new_en", "new_ja"]


def test_scan_stage_queues_unanalyzed_books_as_one_batch(server, monkeypatch):
    cfg = server.cfg
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    save_json(cfg.library_index, {"a": {"language": "ja"}, "b": {"language": "en"}})
    save_json(cfg.vocab_index, {"b": {"status": "ok"}})
    queued = []
    monkeypatch.setattr(server._book_jobs, "enqueue_many", lambda ids: queued.append(list(ids)))
    monkeypatch.setitem(server.ADMIN_STAGES, "scan", ("Scan", "test", lambda _cfg: None))
    monkeypatch.setitem(server.ADMIN_STAGES, "analyze", ("Analyze", "test", lambda _cfg: None))
    server._admin_worker("analyze")
    assert queued == []  # only a scan finds new files
    server._admin_worker("scan")
    assert queued == [["a"]]
    assert server._admin_run["ok"] is not False
