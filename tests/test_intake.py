"""Filing an uploaded audiobook: where it goes, what counts as the same book twice, hostile
names, and the whole path from the first chunk to a row in the library (intake.py)."""
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

import audio_fixtures as af
from conftest import make_cfg
from epublib import audible, audiobook_catalog, audiobook_scanner as scanner, intake, pipeline, uploads
from epublib.index_store import load_json, save_json

UP = "/api/audiobooks/uploads"


@pytest.fixture
def lib(tmp_path):
    root = tmp_path / "audiobooks"
    root.mkdir()
    return make_cfg(tmp_path, audiobook_dirs=[root]), root


def _stage(root, name="x"):
    d = root / uploads.STAGING / name
    d.mkdir(parents=True)
    return d


def _scan(cfg):
    files = scanner.scan(cfg)
    scanner.build_index(cfg)
    return files


def _row(cfg, path):
    """The catalog row the scanner makes for the book filed at `path`."""
    ab_id = next(k for k, e in _scan(cfg).items() if e["path"] == path)
    return audiobook_catalog.load(cfg)[ab_id]


# ── where a book goes ────────────────────────────────────────────────────────

def test_a_book_the_catalog_knows_is_named_after_its_row(lib):
    cfg, root = lib
    save_json(cfg.audiobook_data, {
        "B0AAAAAAAA": {"title": "夜のピクニック", "author": "恩田 陸", "series": "夜のピクニック", "order": "01"},
        "B0BBBBBBBB": {"title": "Other", "author": "A", "new_path": "Hand Made/Other [B0BBBBBBBB].m4b"}})
    d = _stage(root)
    af.m4b(d / "rip.m4b", seconds=5, asin="B0AAAAAAAA", title="tag title", artist="tag author")
    f = intake.file_item(cfg, d, "rip.m4b")
    assert (f.status, f.key, f.unsorted) == ("filed", "B0AAAAAAAA", False)
    assert f.path == "[恩田 陸] 夜のピクニック/[01] 夜のピクニック [B0AAAAAAAA].m4b"
    assert (root / f.path).is_file() and not (d / "rip.m4b").exists()

    d = _stage(root, "y")
    af.m4b(d / "rip.m4b", seconds=5, asin="B0BBBBBBBB")
    assert intake.file_item(cfg, d, "rip.m4b").path == "Hand Made/Other [B0BBBBBBBB].m4b"  # its new_path


def test_a_tagged_book_is_named_from_its_tags_with_the_asin_if_it_has_one(lib):
    cfg, root = lib
    d = _stage(root)
    af.m4b(d / "a.m4b", seconds=5, title="Title", artist="Ann Author", series="The Series", part="2",
           asin="B0TAGGED01")
    assert intake.file_item(cfg, d, "a.m4b").path == "[Ann Author] The Series/[02] Title [B0TAGGED01].m4b"

    d = _stage(root, "y")
    af.m4b(d / "b.m4b", seconds=5, title="Solo", artist="Ann Author")
    f = intake.file_item(cfg, d, "b.m4b")
    assert (f.path, f.key) == ("[Ann Author] Solo/Solo.m4b", None)

    d = _stage(root, "z")
    af.m4b(d / "c.m4b", seconds=5, title="No Author")
    assert intake.file_item(cfg, d, "c.m4b").path == "No Author/No Author.m4b"


def test_a_key_in_the_file_name_beats_the_tag(lib):
    cfg, root = lib
    d = _stage(root)
    af.m4b(d / "a.m4b", seconds=5, title="T", artist="A", asin="B0FROMTG01")
    f = intake.file_item(cfg, d, "T [B0FROMNM01].m4b")
    assert f.key == "B0FROMNM01" and f.path.endswith("[B0FROMNM01].m4b")


def test_a_book_that_says_nothing_about_itself_waits_in_unsorted(lib):
    cfg, root = lib
    d = _stage(root)
    af.m4b(d / "x.m4b", seconds=5)
    f = intake.file_item(cfg, d, "rip 03.m4b")
    assert (f.path, f.unsorted) == ("_unsorted/rip 03.m4b", True)
    assert (root / f.path).is_file()
    assert _row(cfg, f.path)["source"] == "filename"  # the scanner says it needs details


def test_an_m4b_stays_a_file_but_everything_else_gets_a_folder_of_its_own(lib):
    cfg, root = lib
    # Two flac books of one series, no album tag: side by side they would be ONE book.
    for i, title in enumerate(("First", "Second"), 1):
        d = _stage(root, f"s{i}")
        af.flac(d / "x.flac", seconds=5, title=title, artist="Ann", series="Saga", part=str(i))
        f = intake.file_item(cfg, d, "x.flac")
        assert f.path == f"[Ann] Saga/[0{i}] {title}/[0{i}] {title}.flac"
    files = _scan(cfg)
    assert sorted(e["path"] for e in files.values()) == [
        "[Ann] Saga/[01] First/[01] First.flac", "[Ann] Saga/[02] Second/[02] Second.flac"]


def test_the_tracks_of_a_zip_are_one_book_in_a_folder(lib):
    cfg, root = lib
    d = _stage(root)
    for n in (1, 2, 3):
        af.mp3(d / f"{n:02d}.mp3", seconds=2, album="Spoken", artist="Ann", track=n)
    (d / "cover.jpg").write_bytes(af.PNG)
    f = intake.file_item(cfg, d, "Spoken.zip")
    assert f.path == "[Ann] Spoken/Spoken"
    assert sorted(p.name for p in (root / f.path).iterdir()) == ["01.mp3", "02.mp3", "03.mp3", "cover.jpg"]
    [entry] = _scan(cfg).values()
    assert (entry["format"], entry["path"], entry["files"]) == ("mp3-folder", f.path, ["01.mp3", "02.mp3", "03.mp3"])
    assert entry["cover"]  # the folder's cover came with it


def test_a_zipped_single_track_keeps_its_cover(lib):
    cfg, root = lib
    d = _stage(root)
    af.flac(d / "only.flac", seconds=3, title="Short", artist="Ann")
    (d / "cover.png").write_bytes(af.PNG)
    f = intake.file_item(cfg, d, "Short.zip")
    assert sorted(p.name for p in (root / f.path).parent.iterdir()) == ["Short.flac", "cover.png"]


# ── the same book twice ──────────────────────────────────────────────────────

def test_the_same_keyed_file_again_is_a_duplicate_and_moves_nothing(lib):
    cfg, root = lib
    d = _stage(root)
    af.m4b(d / "a.m4b", seconds=5, pad=500, title="T", artist="A", asin="B0DUPLIC01")
    first = intake.file_item(cfg, d, "a.m4b")
    [ab_id] = _scan(cfg)
    d = _stage(root, "again")
    af.m4b(d / "a.m4b", seconds=5, pad=500, title="T", artist="A", asin="B0DUPLIC01")
    again = intake.file_item(cfg, d, "a.m4b")
    assert (again.status, again.existing, again.path) == ("duplicate", ab_id, first.path)
    assert (d / "a.m4b").exists()  # left for the caller to clear away


def test_a_different_file_for_a_key_already_there_is_a_conflict_not_a_second_copy(lib):
    cfg, root = lib
    d = _stage(root)
    af.m4b(d / "a.m4b", seconds=5, pad=500, title="T", artist="A", asin="B0CONFLC01")
    intake.file_item(cfg, d, "a.m4b")
    _scan(cfg)
    d = _stage(root, "other")
    af.m4b(d / "a.m4b", seconds=5, pad=900, title="T", artist="A", asin="B0CONFLC01")
    with pytest.raises(intake.IntakeError) as err:
        intake.file_item(cfg, d, "a.m4b")
    assert err.value.reason == "conflict" and (d / "a.m4b").exists()
    # The path itself is taken too, even when nothing has scanned the first one yet.
    save_json(cfg.audiobook_files_index, {})
    d = _stage(root, "third")
    af.m4b(d / "a.m4b", seconds=5, pad=700, title="T", artist="A", asin="B0CONFLC01")
    with pytest.raises(intake.IntakeError):
        intake.file_item(cfg, d, "a.m4b")


def test_books_without_a_key_that_share_a_name_get_a_number(lib):
    cfg, root = lib
    paths = []
    for i, pad in enumerate((100, 200, 100)):
        d = _stage(root, f"s{i}")
        af.m4b(d / "x.m4b", seconds=5, pad=pad, title="Same", artist="Ann")
        paths.append(intake.file_item(cfg, d, "x.m4b"))
    assert [p.path for p in paths[:2]] == ["[Ann] Same/Same.m4b", "[Ann] Same/Same (2).m4b"]
    # The third is byte-for-byte the first.
    assert (paths[2].status, paths[2].path) == ("duplicate", "[Ann] Same/Same.m4b")


# ── names made from tags nobody has looked at ────────────────────────────────

@pytest.mark.parametrize("title,author", [
    (".hack", None), ("..", None), ("CON", "AUX"), ("Ends with dots...", "Ann"),
    ("../../etc/passwd", "../.."), ("a/b\\c:d*e?f\"g<h>i|j", "Ann"),
    ("長" * 120, "著" * 90), ("x" * 300, "y" * 300),
])
def test_hostile_titles_become_safe_names_inside_the_library(lib, title, author):
    cfg, root = lib
    d = _stage(root)
    af.m4b(d / "x.m4b", seconds=5, title=title, artist=author, series=title)
    f = intake.file_item(cfg, d, "x.m4b")
    dest = (root / f.path).resolve()
    assert dest.is_file() and dest.is_relative_to(root.resolve())
    parts = f.path.split("/")
    assert all(len(p.encode()) <= 255 and not p.startswith(".") and not p.endswith((" ", ".")) for p in parts), parts
    assert not any(p in ("", "..") for p in parts)
    # What was filed is found again by the scanner (a leading dot would hide it).
    assert [e["path"] for e in _scan(cfg).values()] == [f.path]


# ── what can't be filed ──────────────────────────────────────────────────────

def test_uploads_that_are_not_one_audiobook_are_refused_with_a_reason(lib):
    cfg, root = lib

    def staged(n, *files):
        d = _stage(root, f"s{n}")
        for make, name in files:
            make(d / name, seconds=2)
        return d

    cases = [
        (staged(0), "no_audio"),
        (staged(1, (af.m4b, "a.m4b"), (af.m4b, "b.m4b")), "several_books"),
        (staged(2, (af.mp3, "a.mp3"), (af.flac, "b.flac")), "mixed_formats"),
    ]
    for d, reason in cases:
        with pytest.raises(intake.IntakeError) as err:
            intake.file_item(cfg, d, "x.zip")
        assert err.value.reason == reason
    bad = _stage(root, "bad")
    (bad / "broken.mp3").write_bytes(b"not audio at all" * 50)
    with pytest.raises(intake.IntakeError) as err:
        intake.file_item(cfg, bad, "broken.mp3")
    assert (err.value.reason, "broken.mp3" in str(err.value)) == ("bad_audio", True)
    assert (bad / "broken.mp3").exists()  # nothing moved on a refusal


# ── the pipeline step ────────────────────────────────────────────────────────

def test_the_audio_chain_is_the_audiobook_stages_in_order(monkeypatch):
    assert pipeline.AUDIO_CHAIN == ("audio-scan", "catalog", "xref", "build")
    assert all(s in pipeline.STAGE_BY_ID for s in pipeline.AUDIO_CHAIN)
    calls = []
    for mod, fn, name in [(pipeline.audiobook_scanner_mod, "scan", "audio-scan"),
                          (pipeline.audiobook_scanner_mod, "build_index", "audio-index"),
                          (pipeline.audiobook_catalog_mod, "fetch_missing", "catalog"),
                          (pipeline.xref_mod, "xref", "xref"), (pipeline.build_mod, "build", "build")]:
        monkeypatch.setattr(mod, fn, lambda *a, _n=name, **k: calls.append(_n))
    steps = []
    assert pipeline.run_audio_chain(None, steps.append) == []
    assert steps == list(pipeline.AUDIO_CHAIN)
    assert calls == ["audio-scan", "audio-index", "catalog", "xref", "build"]


def test_a_failing_audio_step_does_not_stop_the_rest(monkeypatch):
    ran = []
    monkeypatch.setattr(pipeline.audiobook_scanner_mod, "scan", lambda *a, **k: 1 / 0)
    monkeypatch.setattr(pipeline.audiobook_scanner_mod, "build_index", lambda *a, **k: None)
    monkeypatch.setattr(pipeline.audiobook_catalog_mod, "fetch_missing", lambda *a, **k: ran.append("catalog"))
    monkeypatch.setattr(pipeline.xref_mod, "xref", lambda *a, **k: ran.append("xref"))
    monkeypatch.setattr(pipeline.build_mod, "build", lambda *a, **k: ran.append("build"))
    errors = pipeline.run_audio_chain(None)
    assert ran == ["catalog", "xref", "build"] and len(errors) == 1 and errors[0].startswith("audio-scan: ZeroDivisionError")


# ── from the first chunk to a row in the library ─────────────────────────────

@pytest.fixture
def web(server, tmp_path):
    root = tmp_path / "audio"
    root.mkdir()
    server.cfg.audiobook_dirs = [root]
    return TestClient(server.app), root, server


def _send(client, name, data, chunk=40_000):
    up = client.post(UP, json={"filename": name, "size": len(data)}).json()
    for start in range(0, len(data), chunk):
        part = data[start:start + chunk]
        r = client.put(f"{UP}/{up['id']}", content=part,
                       headers={"Content-Range": f"bytes {start}-{start + len(part) - 1}/{len(data)}"})
        assert r.status_code == 200, r.text
    return up["id"]


def _finish(client, upload_id, timeout=20):
    r = client.post(f"{UP}/{upload_id}/complete")
    assert r.status_code == 202, r.text
    deadline = time.time() + timeout
    st = None
    while time.time() < deadline:
        st = client.get(f"{UP}/{upload_id}").json()
        if st["state"] in ("done", "error"):
            return st
        time.sleep(0.05)
    raise AssertionError(f"still {st}")


def test_an_upload_ends_as_a_row_in_the_library(web, tmp_path):
    client, root, _ = web
    data = af.m4b(tmp_path / "src.m4b", seconds=5, pad=150_000, title="夜のピクニック",
                  artist="恩田 陸", series="夜のピクニック", part="1").read_bytes()
    upload_id = _send(client, "rip.m4b", data)
    st = _finish(client, upload_id)
    assert st["state"] == "done", st
    r = st["result"]
    assert (r["status"], r["path"], r["needs_details"]) == ("filed", "[恩田 陸] 夜のピクニック/[01] 夜のピクニック.m4b", False)
    row = next(b for b in client.get("/api/audiobooks").json() if b["ab_id"] == r["ab_id"])
    assert (row["local"], row["title"], row["author"], row["format"]) == (True, "夜のピクニック", "恩田 陸", "m4b")
    assert (root / r["path"]).read_bytes() == data
    assert list((root / uploads.STAGING).iterdir()) == []  # nothing left behind
    # Asking again is not filing it again, and the finished upload takes no more chunks.
    assert client.post(f"{UP}/{upload_id}/complete").json()["state"] == "done"
    again = client.put(f"{UP}/{upload_id}", content=b"x", headers={"Content-Range": "bytes 0-0/9"})
    assert again.status_code == 409


def test_an_untagged_upload_says_it_needs_details(web, tmp_path):
    client, root, _ = web
    data = af.m4b(tmp_path / "src.m4b", seconds=5).read_bytes()
    st = _finish(client, _send(client, "ripped book.m4b", data))
    assert (st["result"]["path"], st["result"]["needs_details"]) == ("_unsorted/ripped book.m4b", True)
    row = next(b for b in client.get("/api/audiobooks").json() if b["ab_id"] == st["result"]["ab_id"])
    assert (row["source"], row["title"]) == ("filename", "ripped book")


def test_the_same_upload_twice_is_reported_as_already_there(web, tmp_path):
    client, root, _ = web
    data = af.m4b(tmp_path / "src.m4b", seconds=5, pad=2000, title="T", artist="A", asin="B0TWICE001").read_bytes()
    first = _finish(client, _send(client, "a.m4b", data))["result"]
    again = _finish(client, _send(client, "a.m4b", data))["result"]
    assert (again["status"], again["ab_id"]) == ("duplicate", first["ab_id"])
    assert len(list(root.rglob("*.m4b"))) == 1 and list((root / uploads.STAGING).iterdir()) == []


def test_a_refused_upload_reports_why_and_leaves_no_files(web, tmp_path):
    client, root, _ = web
    data = af.m4b(tmp_path / "src.m4b", seconds=5, pad=2000, title="T", artist="A", asin="B0REFUSE01").read_bytes()
    _finish(client, _send(client, "a.m4b", data))
    other = af.m4b(tmp_path / "other.m4b", seconds=5, pad=3000, title="T", artist="A", asin="B0REFUSE01").read_bytes()
    st = _finish(client, _send(client, "b.m4b", other))
    assert (st["state"], st["reason"]) == ("error", "conflict") and "already in your library" in st["error"]
    assert list((root / uploads.STAGING).iterdir()) == [] and len(list(root.rglob("*.m4b"))) == 1


def test_a_zip_of_mp3s_becomes_one_book(web, tmp_path):
    client, root, _ = web
    z = tmp_path / "spoken.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for n in (1, 2):
            track = af.mp3(tmp_path / f"t{n}.mp3", seconds=2, album="Spoken", artist="Ann", track=n)
            zf.write(track, f"Spoken/{n:02d}.mp3")
    st = _finish(client, _send(client, "spoken.zip", z.read_bytes()))
    assert st["state"] == "done", st
    row = next(b for b in client.get("/api/audiobooks").json() if b["ab_id"] == st["result"]["ab_id"])
    assert (row["format"], row["files"], row["title"]) == ("mp3-folder", ["01.mp3", "02.mp3"], "Spoken")


def test_a_hostile_zip_is_refused_and_cleaned_up(web, tmp_path):
    client, root, _ = web
    z = tmp_path / "evil.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("01.mp3", b"x")
        zf.writestr("../escape.mp3", b"x")
    st = _finish(client, _send(client, "evil.zip", z.read_bytes()))
    assert (st["state"], st["reason"]) == ("error", "unsafe_zip")
    assert not (tmp_path / "escape.mp3").exists() and not list(root.rglob("*.mp3"))
    assert list((root / uploads.STAGING).iterdir()) == []


def test_a_new_asin_is_looked_up_once_the_book_is_in(web, tmp_path, monkeypatch):
    client, root, _ = web
    asked = []
    monkeypatch.setattr(audible, "fetch_product", lambda asin, marketplace=None: asked.append(asin))
    data = af.m4b(tmp_path / "src.m4b", seconds=5, title="T", artist="A", asin="B0NEWPUR01").read_bytes()
    st = _finish(client, _send(client, "a.m4b", data))
    assert st["state"] == "done" and asked == ["B0NEWPUR01"]


def test_filing_waits_for_nobody_but_indexing_waits_for_the_lock(web, tmp_path):
    """The move is a rename and takes no lock; only the index passes queue behind a running stage."""
    client, root, server = web
    data = af.m4b(tmp_path / "src.m4b", seconds=5, title="Held", artist="Ann").read_bytes()
    upload_id = _send(client, "held.m4b", data)
    with server._index_lock:
        assert client.post(f"{UP}/{upload_id}/complete").status_code == 202
        dest = root / "[Ann] Held" / "Held.m4b"
        deadline = time.time() + 10
        while not dest.exists() and time.time() < deadline:
            time.sleep(0.02)
        assert dest.is_file()                           # filed, with the lock still held elsewhere
        time.sleep(0.2)
        st = client.get(f"{UP}/{upload_id}").json()
        assert (st["state"], st["step"]) == ("running", "indexing")
        assert client.delete(f"{UP}/{upload_id}").status_code == 409   # and it can't be aborted meanwhile
        assert load_json(server.cfg.audiobook_files_index, {}) == {}   # not indexed yet
    deadline = time.time() + 10
    while client.get(f"{UP}/{upload_id}").json()["state"] != "done" and time.time() < deadline:
        time.sleep(0.05)
    assert client.get(f"{UP}/{upload_id}").json()["state"] == "done"


def test_an_indexing_failure_still_says_the_file_is_saved(web, tmp_path, monkeypatch):
    client, root, _ = web
    monkeypatch.setattr(pipeline.audiobook_scanner_mod, "scan", lambda *a, **k: 1 / 0)
    data = af.m4b(tmp_path / "src.m4b", seconds=5, title="Saved", artist="Ann").read_bytes()
    st = _finish(client, _send(client, "a.m4b", data))
    assert (st["state"], st["reason"]) == ("error", "index_failed")
    assert st["error"].startswith("Saved, but indexing failed") and (root / "[Ann] Saved" / "Saved.m4b").is_file()
