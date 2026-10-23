"""One lock serializes every writer of data/, pipeline stage runs included."""
import json
import threading

from epublib import pipeline


def _run_blocking_stage(server, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def stage(_cfg):
        entered.set()
        assert release.wait(10)

    monkeypatch.setitem(server.ADMIN_STAGES, "slow", ("Slow", "test", stage))
    t = threading.Thread(target=server._admin_worker, args=("slow",), daemon=True)
    t.start()
    assert entered.wait(5)
    return t, release


def test_stage_run_blocks_index_writers_but_not_status(server, monkeypatch):
    t, release = _run_blocking_stage(server, monkeypatch)
    try:
        # A writer endpoint queues behind the running stage...
        writer_done = threading.Event()

        def writer():
            with server._index_lock:
                writer_done.set()

        w = threading.Thread(target=writer, daemon=True)
        w.start()
        assert not writer_done.wait(0.3)

        # ...but status polling and profile switching do not.
        status_done = threading.Event()
        threading.Thread(
            target=lambda: (server.admin_status(), status_done.set()), daemon=True
        ).start()
        assert status_done.wait(2)
        with server._profiles_lock:
            pass
    finally:
        release.set()
    t.join(5)
    assert writer_done.wait(5)


def test_status_shows_the_tail_of_a_running_stage(server, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def stage(_cfg):
        for i in range(12):
            print(f"step {i}")
        entered.set()
        assert release.wait(10)

    monkeypatch.setitem(server.ADMIN_STAGES, "chatty", ("Chatty", "test", stage))
    t = threading.Thread(target=server._admin_worker, args=("chatty",), daemon=True)
    t.start()
    assert entered.wait(5)
    try:
        tail = json.loads(server.admin_status().body)["tail"].splitlines()
        assert tail[-1] == "step 11" and len(tail) <= 8
    finally:
        release.set()
    t.join(5)
    assert json.loads(server.admin_status().body)["tail"] is None


def test_delete_book_waits_for_stage_and_leaves_cached_dict_alone(server, monkeypatch):
    cfg = server.cfg
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.epub_dir.mkdir(parents=True, exist_ok=True)
    epub = cfg.epub_dir / "a.epub"
    epub.write_bytes(b"x")
    cfg.library_index.write_text(
        json.dumps({"b1": {"path": "a.epub", "title": "A"}, "b2": {"path": "b.epub"}})
    )
    monkeypatch.setattr(server.build_mod, "build", lambda c: None)
    # Everything else keyed by b1 goes with it.
    (cfg.wordlists_dir).mkdir(parents=True, exist_ok=True)
    (cfg.wordlists_dir / "b1.json").write_text("{}")
    server.save_json(cfg.natively_index, {"b1": {}, "b2": {}})
    server.save_json(cfg.title_overrides, {"b1": "T"})
    server.save_json(cfg.read_books, {"books": {"b1": {"read": True}}})
    cached = server._load_library()

    t, release = _run_blocking_stage(server, monkeypatch)
    result = {}
    d = threading.Thread(
        target=lambda: result.update(r=server.delete_book("b1")), daemon=True
    )
    d.start()
    d.join(0.3)
    assert d.is_alive() and epub.exists()  # still queued behind the stage

    release.set()
    t.join(5)
    d.join(5)
    assert not d.is_alive() and not epub.exists()
    assert set(json.loads(cfg.library_index.read_text())) == {"b2"}
    assert set(cached) == {"b1", "b2"}  # the shared cached dict was never mutated
    assert not (cfg.wordlists_dir / "b1.json").exists()
    assert set(json.loads(cfg.natively_index.read_text())) == {"b2"}
    assert json.loads(cfg.title_overrides.read_text()) == {}
    assert json.loads(cfg.read_books.read_text())["books"] == {}


def test_save_json_uses_per_writer_temp_names(tmp_path, monkeypatch):
    from epublib import index_store

    seen = []
    real_replace = index_store.os.replace
    monkeypatch.setattr(
        index_store.os, "replace", lambda a, b: (seen.append(str(a)), real_replace(a, b))
    )
    index_store.save_json(tmp_path / "x.json", {"a": 1})
    assert seen and seen[0] != str(tmp_path / "x.json.tmp")
    assert json.loads((tmp_path / "x.json").read_text()) == {"a": 1}


def test_audiobook_link_endpoints_pin_unlink_and_reset(server, monkeypatch):
    cfg = server.cfg
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    server.save_json(cfg.audiobook_data, {
        "B1": {"title": "銭天堂", "author": "廣嶋玲子", "series": "銭天堂", "order": "01"},
        "B2": {"title": "銭天堂13", "author": "廣嶋玲子", "series": "銭天堂", "order": "13"},
    })
    server.save_json(cfg.library_index, {"a": {
        "book_id": "a", "title": "銭天堂", "author": "廣嶋玲子", "language": "ja"}})
    monkeypatch.setattr(server.build_mod, "build", lambda c: None)

    assert server._apply_link_override("a", None, False)["link"]["audible_isbn"] == "B1"
    st = server._apply_link_override("a", "B2", True)
    assert (st["mode"], st["link"]["audible_isbn"], st["catalog"]["order"]) == ("manual", "B2", "13")
    st = server._apply_link_override("a", None, True)
    assert (st["mode"], st["link"], st["catalog"]) == ("none", None, None)
    st = server._apply_link_override("a", None, False)
    assert (st["mode"], st["link"]["audible_isbn"]) == ("auto", "B1")


def test_scan_stage_rebuilds_the_index_afterwards(server, monkeypatch):
    # scan rewrites changed entries without the fields build derives (series,
    # cleaned title, ...); a bare scan stage used to leave the library that way.
    calls = []
    monkeypatch.setattr(pipeline.scanner_mod, "scan", lambda c: calls.append("scan"))
    monkeypatch.setattr(pipeline.build_mod, "build", lambda c: calls.append("build"))
    server.ADMIN_STAGES["scan"][2](server.cfg)
    assert calls == ["scan", "build"]
