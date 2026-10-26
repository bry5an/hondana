"""The inbox folder: what counts as an item, when one is ready, and what becomes of it
(inbox.py), driven with a fake clock and the real filing code."""
import asyncio
import contextlib
import json
import os
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

import audio_fixtures as af
from conftest import make_cfg, write_epub
from epublib import audible, inbox, intake, pipeline, settings, uploads
from epublib.incoming import DuplicateEpub
from epublib.index_store import load_json


class Clock:
    def __init__(self):
        self.t = time.time() + 5

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def wait_for(cond, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = cond()
        if got:
            return got
        time.sleep(0.02)
    raise AssertionError("timed out")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(audible, "fetch_product", lambda *a, **k: None)


@pytest.fixture
def rig(tmp_path):
    root = tmp_path / "audiobooks"
    root.mkdir()
    box = tmp_path / "inbox"
    box.mkdir()
    cfg = make_cfg(tmp_path, audiobook_dirs=[root], inbox_dir=box)
    cfg.epub_dir.mkdir(parents=True)
    added = []
    jobs = intake.IntakeJobs(cfg, contextlib.nullcontext)
    clock = Clock()
    watcher = inbox.Watcher(cfg, jobs, lambda content, name: added.append((name, content)),
                            clock=clock)

    class Rig:
        pass

    r = Rig()
    r.cfg, r.root, r.box, r.added, r.jobs, r.clock, r.watcher = cfg, root, box, added, jobs, clock, watcher

    def drop_and_wait():
        """Two polls a minute apart, as the thread would make them; returns what was taken."""
        watcher.poll()
        clock.advance(inbox.MIN_AGE + 1)
        return watcher.poll()

    def settled(n=1):
        return wait_for(lambda: len(watcher.status()["recent"]) >= n and watcher.status()["recent"])

    def finished(entry):
        """Wait for the indexing that follows a filed audiobook."""
        wait_for(lambda: (jobs.status(entry["job"]) or {}).get("state") in ("done", "error"))
        return jobs.status(entry["job"])

    r.drop_and_wait, r.settled, r.finished = drop_and_wait, settled, finished
    return r


def names(box):
    return sorted(p.name for p in box.iterdir())


# ── what is an item, and when is it ready ────────────────────────────────────

def test_an_item_is_ready_when_unchanged_since_the_last_poll_and_a_minute_old(tmp_path):
    f = tmp_path / "a.epub"
    f.write_bytes(b"x" * 10)
    [item] = inbox.find_items(tmp_path)
    now = item.mtime + 120
    assert inbox.ready_items({}, [item], now) == []                              # first sight
    assert inbox.ready_items({item.key: item.sig}, [item], now) == [item]         # still, and old
    assert inbox.ready_items({item.key: item.sig}, [item], item.mtime + 30) == []  # too young
    assert inbox.ready_items({item.key: (11, item.mtime)}, [item], now) == []     # it grew
    busy = inbox.Item(item.path, "folder", 1, item.mtime, busy=True)
    assert inbox.ready_items({busy.key: busy.sig}, [busy], now) == []


def test_what_counts_as_an_item(tmp_path):
    box = tmp_path
    (box / "Book.epub").write_bytes(b"e")
    (box / "tracks.zip").write_bytes(b"z")
    af.m4b(box / "One [B0AAAAAAAA].m4b", seconds=2)
    af.mp3(box / "single.mp3", seconds=2)
    (box / "notes.txt").write_text("not a book")
    (box / ".hidden.epub").write_bytes(b"h")
    (box / "half.epub.part").write_bytes(b"p")
    (box / inbox.FAILED).mkdir()
    (box / inbox.FAILED / "old.epub").write_bytes(b"f")
    (box / "link.m4b").symlink_to(box / "One [B0AAAAAAAA].m4b")
    for n in (1, 2):
        af.mp3(box / "Spoken" / f"{n:02d}.mp3", seconds=2)
    af.m4b(box / "Author" / "Series" / "Title [B0BBBBBBBB]" / "Title [B0BBBBBBBB].m4b", seconds=2)
    (box / "Author" / "Series" / "Title [B0BBBBBBBB]" / "cover.jpg").write_bytes(af.PNG)
    for n in ("A", "B"):
        af.m4b(box / "Two Books" / f"{n}.m4b", seconds=2)
    (box / "Deep" / "Er" / "Book.epub").parent.mkdir(parents=True)
    (box / "Deep" / "Er" / "Book.epub").write_bytes(b"d")

    got = {(i.kind, i.path.relative_to(box).as_posix()) for i in inbox.find_items(box)}
    assert got == {
        ("epub", "Book.epub"), ("zip", "tracks.zip"), ("file", "One [B0AAAAAAAA].m4b"),
        ("file", "single.mp3"), ("folder", "Spoken"),
        ("folder", "Author/Series/Title [B0BBBBBBBB]"),
        ("file", "Two Books/A.m4b"), ("file", "Two Books/B.m4b"), ("epub", "Deep/Er/Book.epub"),
    }


def test_a_folder_with_a_copy_in_progress_is_never_ready(tmp_path):
    af.mp3(tmp_path / "Book" / "01.mp3", seconds=2)
    [item] = inbox.find_items(tmp_path)
    assert not item.busy
    (tmp_path / "Book" / "02.mp3.crdownload").write_bytes(b"...")
    assert inbox.find_items(tmp_path)[0].busy
    (tmp_path / "Book" / "02.mp3.crdownload").unlink()
    (tmp_path / "Book" / ".02.mp3.Ab12Cd").write_bytes(b"...")  # rsync's scratch file
    assert inbox.find_items(tmp_path)[0].busy


# ── the watcher, end to end ──────────────────────────────────────────────────

def test_a_dropped_audiobook_is_filed_indexed_and_leaves_the_inbox(rig):
    data = af.m4b(rig.box / "rip.m4b", seconds=5, pad=50_000, title="夜のピクニック",
                  artist="恩田 陸", series="夜のピクニック", part="1").read_bytes()
    assert rig.watcher.poll() == []  # first sight: nothing moves
    assert names(rig.box) == ["rip.m4b"]
    rig.clock.advance(30)
    assert rig.watcher.poll() == []  # still too young
    rig.clock.advance(31)
    [taken] = rig.watcher.poll()
    assert taken.name == "rip.m4b"

    [entry] = rig.settled()
    assert (entry["outcome"], entry["name"]) == ("filed", "rip.m4b")
    assert rig.finished(entry)["state"] == "done"
    filed = rig.root / "[恩田 陸] 夜のピクニック" / "[01] 夜のピクニック.m4b"
    assert filed.read_bytes() == data
    assert names(rig.box) == []
    assert list((rig.root / uploads.STAGING).iterdir()) == []
    rows = load_json(rig.cfg.audiobook_index, {})
    assert [r["title"] for r in rows.values() if r.get("local")] == ["夜のピクニック"]
    assert rig.watcher.status()["waiting"] == []


def test_a_file_still_growing_is_left_alone_until_it_stops(rig):
    f = af.m4b(rig.box / "big.m4b", seconds=5, title="T", artist="A")
    rig.watcher.poll()
    rig.clock.advance(61)
    af.m4b(f, seconds=5, pad=500, title="T", artist="A")  # the copy is still going
    os.utime(f, (rig.clock.t - 5, rig.clock.t - 5))
    assert rig.watcher.poll() == []
    assert rig.watcher.status()["waiting"] == ["big.m4b"]
    rig.clock.advance(61)
    assert [i.name for i in rig.watcher.poll()] == ["big.m4b"]
    rig.settled()


def test_something_that_is_not_audio_goes_to_failed_with_a_note_and_is_not_retried(rig):
    (rig.box / "fake.mp3").write_bytes(b"this is a web page")
    [taken] = rig.drop_and_wait()
    [entry] = rig.settled()
    assert entry["outcome"] == "failed" and "readable audio" in entry["note"]
    assert names(rig.box) == [inbox.FAILED]
    assert names(rig.box / inbox.FAILED) == ["fake.mp3", "fake.mp3.error.txt"]
    assert "readable audio" in (rig.box / inbox.FAILED / "fake.mp3.error.txt").read_text()
    assert (rig.box / inbox.FAILED / "fake.mp3").read_bytes() == b"this is a web page"
    rig.clock.advance(300)
    assert rig.watcher.poll() == [] and len(rig.watcher.status()["recent"]) == 1
    assert rig.watcher.status()["failed"] == 1


def test_a_book_the_library_already_has_is_set_aside_not_filed_twice(rig):
    af.m4b(rig.box / "a.m4b", seconds=5, title="Same", artist="Ann", asin="B0DUPLICA1")
    rig.drop_and_wait()
    rig.finished(rig.settled()[0])
    filed = next(rig.root.rglob("*.m4b"))
    af.m4b(rig.box / "again.m4b", seconds=5, title="Same", artist="Ann", asin="B0DUPLICA1")
    rig.drop_and_wait()
    entry = wait_for(lambda: next((e for e in rig.watcher.status()["recent"] if e["name"] == "again.m4b"), None))
    assert entry["outcome"] == "duplicate" and "Already in your library" in entry["note"]
    assert names(rig.box / inbox.FAILED) == ["again.m4b", "again.m4b.error.txt"]
    assert list(rig.root.rglob("*.m4b")) == [filed]


def test_a_zip_of_tracks_is_one_book_and_a_broken_zip_is_kept(rig):
    for n in (1, 2):
        af.mp3(rig.box / "stage" / f"{n:02d}.mp3", seconds=2, album="Spoken", artist="Ann", track=n)
    with zipfile.ZipFile(rig.box / "Spoken.zip", "w") as zf:
        for n in (1, 2):
            zf.write(rig.box / "stage" / f"{n:02d}.mp3", f"{n:02d}.mp3")
    (rig.box / "stage" / "01.mp3").unlink()
    (rig.box / "stage" / "02.mp3").unlink()
    (rig.box / "stage").rmdir()
    (rig.box / "Broken.zip").write_bytes(b"PK\x03\x04 not really")
    rig.drop_and_wait()
    got = wait_for(lambda: len(rig.watcher.status()["recent"]) >= 2 and rig.watcher.status()["recent"])
    assert {e["name"]: e["outcome"] for e in got} == {"Spoken.zip": "filed", "Broken.zip": "failed"}
    rig.finished(next(e for e in got if e["outcome"] == "filed"))
    assert sorted(p.name for p in (rig.root / "[Ann] Spoken" / "Spoken").iterdir()) == ["01.mp3", "02.mp3"]
    assert names(rig.box) == [inbox.FAILED]  # the good zip is gone, the broken one kept
    assert names(rig.box / inbox.FAILED) == ["Broken.zip", "Broken.zip.error.txt"]


def test_a_libation_style_tree_is_filed_and_its_leftovers_tidied(rig):
    book = rig.box / "Ann Author" / "The Series" / "Title [B0LIBATIO1]"
    af.m4b(book / "Title [B0LIBATIO1].m4b", seconds=5, title="Title", artist="Ann Author")
    (book / "Title [B0LIBATIO1].jpg").write_bytes(af.PNG)
    (book / "Title [B0LIBATIO1].metadata.json").write_text("{}")
    rig.drop_and_wait()
    entry = rig.settled()[0]
    assert entry["outcome"] == "filed" and entry["note"].endswith("[B0LIBATIO1].m4b")
    rig.finished(entry)
    assert names(rig.box) == []  # the folders went too
    assert len(list(rig.root.rglob("*.m4b"))) == 1


def test_a_folder_keeps_files_that_are_not_cover_art_or_notes(rig):
    book = rig.box / "Book [B0KEEPPDF1]"
    af.m4b(book / "Book [B0KEEPPDF1].m4b", seconds=5, title="Book", artist="A")
    (book / "companion.pdf").write_bytes(b"%PDF")
    rig.drop_and_wait()
    rig.finished(rig.settled()[0])
    assert names(book) == ["companion.pdf"]
    rig.clock.advance(300)
    assert rig.watcher.poll() == []  # nothing left to file; the folder is not an item


def test_a_folder_of_tracks_arrives_with_its_cover(rig):
    book = rig.box / "Spoken Word"
    for n in (1, 2, 3):
        af.mp3(book / f"{n:02d}.mp3", seconds=2, album="Spoken Word", artist="Ann", track=n)
    (book / "cover.jpg").write_bytes(af.PNG)
    rig.drop_and_wait()
    rig.finished(rig.settled()[0])
    filed = rig.root / "[Ann] Spoken Word" / "Spoken Word"
    assert sorted(p.name for p in filed.iterdir()) == ["01.mp3", "02.mp3", "03.mp3", "cover.jpg"]
    assert names(rig.box) == []


def test_an_epub_goes_to_the_epub_filer_and_a_duplicate_is_set_aside(rig):
    (rig.box / "a.epub").write_bytes(b"epub bytes")
    (rig.box / "dupe.epub").write_bytes(b"other bytes")

    def add(content, name):
        if name == "dupe.epub":
            raise DuplicateEpub("abc", "Same Book", "same ISBN")
        rig.added.append((name, content))
        return "queued"

    rig.watcher.add_epub = add
    rig.drop_and_wait()
    got = {e["name"]: e for e in rig.watcher.status()["recent"]}
    assert rig.added == [("a.epub", b"epub bytes")]
    assert got["a.epub"]["outcome"] == "filed" and got["a.epub"]["note"] == "queued"
    assert got["dupe.epub"]["outcome"] == "duplicate"
    assert "same ISBN" in (rig.box / inbox.FAILED / "dupe.epub.error.txt").read_text()
    assert names(rig.box) == [inbox.FAILED]


def test_an_epub_over_the_size_cap_is_refused_unread(rig, monkeypatch):
    monkeypatch.setattr(inbox.incoming, "MAX_BYTES", 5)
    (rig.box / "huge.epub").write_bytes(b"123456789")
    rig.drop_and_wait()
    assert rig.watcher.status()["recent"][0]["outcome"] == "failed"
    assert rig.added == [] and "100 MB" in (rig.box / inbox.FAILED / "huge.epub.error.txt").read_text()


def test_no_room_in_the_library_is_not_the_books_fault(rig, monkeypatch):
    af.m4b(rig.box / "a.m4b", seconds=5, title="T", artist="A")
    room = {"free": 0}
    monkeypatch.setattr(uploads, "_free", lambda path: room["free"])
    rig.drop_and_wait()
    [entry] = rig.settled()
    assert entry["outcome"] == "retry" and "free space" in entry["note"]
    assert names(rig.box) == ["a.m4b"]  # stays where it was
    rig.clock.advance(31)
    rig.watcher.poll()
    wait_for(lambda: not inbox._claims)
    assert len(rig.watcher.status()["recent"]) == 1  # said once, not every poll
    room["free"] = 1 << 40
    rig.clock.advance(31)
    rig.watcher.poll()
    wait_for(lambda: rig.watcher.status()["recent"][0]["outcome"] == "filed")
    assert names(rig.box) == []


def test_an_item_that_changed_before_it_was_staged_is_left_for_the_next_look(rig):
    f = af.m4b(rig.box / "a.m4b", seconds=5, title="T", artist="A")
    [item] = inbox.find_items(rig.box)
    done = []
    assert inbox._claim(item.key)
    job = inbox.AudioJob(rig.cfg, rig.box, item, "a" * 24, lambda *a: done.append(a))
    with open(f, "ab") as h:
        h.write(b"more")
    with pytest.raises(inbox.Changed):
        job.stage()
    job.settle(None, inbox.Changed(item.name))
    assert done[0][1] == "changed" and f.exists()
    assert inbox._claim(item.key)  # released
    inbox._release(item.key)


def test_two_things_never_take_the_same_item(rig):
    af.m4b(rig.box / "a.m4b", seconds=5, title="T", artist="A")
    rig.watcher.poll()
    rig.clock.advance(61)
    [item] = inbox.find_items(rig.box)
    assert inbox._claim(item.key)  # a stage run from /admin has it
    try:
        assert rig.watcher.poll() == []
    finally:
        inbox._release(item.key)
    assert [i.name for i in rig.watcher.poll()] == ["a.m4b"]
    rig.settled()


def test_an_unset_or_unusable_inbox_does_nothing_and_says_why(rig, tmp_path):
    af.m4b(rig.box / "a.m4b", seconds=5, title="T", artist="A")
    rig.cfg.inbox_dir = None
    assert rig.drop_and_wait() == []
    st = rig.watcher.status()
    assert (st["enabled"], st["dir"], st["problem"]) == (False, None, None)

    rig.cfg.inbox_dir = tmp_path / "unmounted"
    assert rig.drop_and_wait() == []
    assert "isn't available" in rig.watcher.status()["problem"]

    rig.cfg.inbox_dir = rig.root / "inbox-inside"
    rig.cfg.inbox_dir.mkdir()
    assert "inside" in rig.watcher.status()["problem"]
    assert names(rig.box) == ["a.m4b"]


def test_the_inbox_cannot_hold_or_sit_in_a_library_folder(tmp_path):
    books, audio, box = tmp_path / "books", tmp_path / "audio", tmp_path / "box"
    for d in (books, audio, box):
        d.mkdir()
    cfg = make_cfg(tmp_path, epub_dir=books, audiobook_dirs=[audio], inbox_dir=box)
    assert inbox.problem(cfg) is None
    for bad in (books, audio, books / "new", tmp_path):
        (books / "new").mkdir(exist_ok=True)
        cfg.inbox_dir = bad
        assert "library folder" in inbox.problem(cfg), bad
    cfg.inbox_dir = None
    assert inbox.problem(cfg) is None


# ── the shared worker ────────────────────────────────────────────────────────

def test_inbox_items_are_filed_by_the_same_single_worker_as_uploads(rig, tmp_path):
    seen = []

    class Probe:
        def __init__(self, name, fails):
            self.name, self.fails = name, fails

        def stage(self):
            seen.append((self.name, "stage", __import__("threading").current_thread().name))
            d = uploads.fresh_item_dir(rig.cfg, "b" * 24 if self.fails else "c" * 24)
            if self.fails:
                (d / "left.m4b").write_bytes(b"x")
                raise RuntimeError("boom")
            af.m4b(d / "ok.m4b", seconds=3, title="Probe", artist="Ann")
            return d, "ok.m4b"

        def settle(self, filed, error):
            # The staging folder is still there to rescue from when filing failed.
            seen.append((self.name, "settle", error and str(error), (rig.root / uploads.STAGING / ("b" * 24)).exists()))

    rig.jobs.submit("b" * 24, Probe("bad", True))
    rig.jobs.submit("c" * 24, Probe("good", False))
    wait_for(lambda: all((rig.jobs.status(i) or {}).get("state") in ("done", "error") for i in ("b" * 24, "c" * 24)))
    assert rig.jobs.status("b" * 24)["state"] == "error" and "boom" in rig.jobs.status("b" * 24)["error"]
    assert rig.jobs.status("c" * 24)["state"] == "done"
    assert {t for _, k, *rest in seen if k == "stage" for t in rest} == {"intake-jobs"}
    assert ("bad", "settle", "boom", True) in seen
    assert not (rig.root / uploads.STAGING / ("b" * 24)).exists()  # cleared once settled


# ── the pipeline stage ───────────────────────────────────────────────────────

def test_the_stage_files_what_is_ready_and_indexes_it(tmp_path, monkeypatch):
    root, box = tmp_path / "audiobooks", tmp_path / "inbox"
    root.mkdir()
    box.mkdir()
    cfg = make_cfg(tmp_path, audiobook_dirs=[root], inbox_dir=box)
    write_epub(box / "Fresh Book.epub", ["雨が降っていた。" * 20])
    af.m4b(box / "rip.m4b", seconds=5, title="Spoken", artist="Ann")
    (box / "Too New.epub").write_bytes(b"x")
    old = time.time() - 600
    for p in (box / "Fresh Book.epub", box / "rip.m4b"):
        os.utime(p, (old, old))

    report = inbox.run_once(cfg, settle_seconds=0)
    assert (report.filed_epubs, report.filed_audio, report.waiting) == (
        ["Fresh Book.epub"], ["rip.m4b"], ["Too New.epub"])
    assert report.failed == [] and names(box) == ["Too New.epub"]
    assert list(cfg.epub_dir.rglob("Fresh Book*.epub"))

    # As a stage it also makes them visible, so `inbox` alone is useful.
    write_epub(box / "Second.epub", ["別の本です。" * 20])
    af.m4b(box / "two.m4b", seconds=5, title="Second", artist="Ann")
    for p in (box / "Second.epub", box / "two.m4b"):
        os.utime(p, (old, old))
    monkeypatch.setattr(inbox, "SETTLE_SECONDS", 0)
    pipeline.STAGE_BY_ID["inbox"].run(cfg)
    assert len(load_json(cfg.library_index, {})) == 2  # both EPUBs, though only `inbox` ran
    assert any(b.get("title") == "Second" for b in load_json(cfg.audiobook_index, {}).values())


def test_the_stage_does_nothing_without_an_inbox(tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    assert inbox.run_once(cfg) == inbox.Report()
    assert "no inbox folder" in capsys.readouterr().out


# ── the server ───────────────────────────────────────────────────────────────

@pytest.fixture
def web(server, tmp_path):
    root, box = tmp_path / "audio", tmp_path / "inbox"
    root.mkdir()
    box.mkdir()
    server.cfg.audiobook_dirs = [root]
    server.cfg.discovery_enabled = False
    # What a settings save re-reads, as it would be after a real start with these folders.
    settings.save_overlay(server.cfg.data_dir, {"audiobook_dirs": [str(root)]})
    return TestClient(server.app), server, root, box


def test_settings_can_turn_the_inbox_on_and_the_page_can_see_it(web):
    client, server, root, box = web
    assert client.get("/api/inbox").json()["enabled"] is False
    r = client.put("/api/settings", json={"values": {"inbox.dir": str(box)}})
    assert r.status_code == 200, r.text
    assert "inbox.dir" in r.json()["applied"] and server.cfg.inbox_dir == box
    st = client.get("/api/inbox").json()
    assert (st["enabled"], st["dir"], st["problem"]) == (True, str(box), None)
    field = next(f for f in r.json()["fields"] if f["key"] == "inbox.dir")
    assert (field["value"], field["source"], field["editable"]) == (str(box), "settings", True)
    # Cleared, it is off again.
    r = client.put("/api/settings", json={"values": {"inbox.dir": None}})
    assert r.status_code == 200 and server.cfg.inbox_dir is None


@pytest.mark.parametrize("where", ["inside the audiobooks", "holding the audiobooks"])
def test_settings_refuse_an_inbox_that_overlaps_the_library(web, tmp_path, where):
    client, server, root, box = web
    bad = root / "drop" if where.startswith("inside") else tmp_path
    bad.mkdir(exist_ok=True)
    r = client.put("/api/settings", json={"values": {"inbox.dir": str(bad)}})
    assert r.status_code == 400 and "library folder" in r.json()["errors"]["inbox.dir"]
    assert server.cfg.inbox_dir is None


def test_settings_refuse_a_library_folder_that_swallows_the_inbox(web, tmp_path):
    client, server, root, box = web
    assert client.put("/api/settings", json={"values": {"inbox.dir": str(box)}}).status_code == 200
    r = client.put("/api/settings", json={"values": {"audiobook_dirs": [str(tmp_path)]}})
    assert r.status_code == 400 and "library folder" in r.json()["errors"]["inbox.dir"]


def test_a_missing_inbox_folder_is_refused_in_settings(web, tmp_path):
    client, *_ = web
    r = client.put("/api/settings", json={"values": {"inbox.dir": str(tmp_path / "nope")}})
    assert r.status_code == 400 and "No folder" in r.json()["errors"]["inbox.dir"]


def test_the_watcher_runs_with_the_server_and_stops_with_it(web):
    client, server, root, box = web

    async def run():
        async with server._lifespan(server.app):
            assert server._inbox_watcher._thread.is_alive()
            return server._inbox_watcher._thread

    thread = asyncio.run(run())
    assert not thread.is_alive()


def test_the_inbox_status_is_for_admins(web):
    client, *_ = web
    assert client.post("/api/auth/password", json={"password": "correct horse"}).status_code == 200
    assert TestClient(web[1].app).get("/api/inbox").status_code == 401
    assert client.get("/api/inbox").status_code == 200


def test_an_epub_from_the_inbox_is_added_like_one_from_add(web, tmp_path, monkeypatch):
    client, server, root, box = web
    queued = []
    monkeypatch.setattr(server._book_jobs, "enqueue", lambda bid: queued.append(bid) or {})
    write_epub(box / "Dropped.epub", ["雨が降っていた。" * 30])
    assert server._inbox_add_epub((box / "Dropped.epub").read_bytes(), "Dropped.epub") is None
    [book] = [b for b in client.get("/api/books").json()]
    assert queued == [book["book_id"]]
    with pytest.raises(DuplicateEpub):  # a second copy is turned away
        server._inbox_add_epub((box / "Dropped.epub").read_bytes(), "Dropped.epub")


def test_the_json_for_status_is_plain(rig):
    json.dumps(rig.watcher.status())
