"""Resumable audiobook uploads: the chunk protocol, resuming, what is refused, and unpacking a
zipped folder of tracks safely (uploads.py and its routes)."""
import os
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

import audio_fixtures as af
from conftest import make_cfg
from epublib import uploads

UP = "/api/audiobooks/uploads"


@pytest.fixture
def lib(server, tmp_path):
    root = tmp_path / "audio"
    root.mkdir()
    server.cfg.audiobook_dirs = [root]
    return TestClient(server.app), root, server.cfg


def _put(client, upload_id, data, start, total):
    return client.put(f"{UP}/{upload_id}", content=data,
                      headers={"Content-Range": f"bytes {start}-{start + len(data) - 1}/{total}"})


def _announce(client, name, size):
    r = client.post(UP, json={"filename": name, "size": size})
    assert r.status_code == 201, r.text
    return r.json()


# ── the protocol ─────────────────────────────────────────────────────────────

def test_a_file_arrives_in_chunks_and_a_client_that_lost_its_place_resumes(lib):
    client, root, _ = lib
    data = bytes(range(256)) * 400
    up = _announce(client, "Book.m4b", len(data))
    assert (up["state"], up["offset"], up["chunk_size"]) == ("receiving", 0, uploads.CHUNK_SIZE)

    r = _put(client, up["id"], data[:30000], 0, len(data))
    assert r.status_code == 200 and r.json()["offset"] == 30000

    # Skipping ahead, or sending a chunk again after a lost response, is refused with the
    # real offset, and nothing is stored twice.
    for start in (60000, 0):
        r = _put(client, up["id"], data[start:start + 5000], start, len(data))
        assert r.status_code == 409
        assert (r.json()["reason"], r.json()["offset"]) == ("offset_mismatch", 30000)
    assert client.get(f"{UP}/{up['id']}").json()["offset"] == 30000

    r = _put(client, up["id"], data[30000:], 30000, len(data))
    assert r.json()["state"] == "received" and r.json()["offset"] == len(data)
    assert (root / uploads.STAGING / f"{up['id']}.part").read_bytes() == data


def test_a_chunk_that_stops_short_keeps_what_arrived(lib):
    client, root, _ = lib
    up = _announce(client, "Book.m4b", 1000)
    short = client.put(f"{UP}/{up['id']}", content=b"x" * 10, headers={"Content-Range": "bytes 0-99/1000"})
    assert (short.status_code, short.json()["reason"], short.json()["offset"]) == (400, "short_chunk", 10)
    assert client.get(f"{UP}/{up['id']}").json()["offset"] == 10
    # The next chunk carries on from the 10 bytes that did arrive.
    assert _put(client, up["id"], b"y" * 90, 10, 1000).json()["offset"] == 100
    assert (root / uploads.STAGING / f"{up['id']}.part").read_bytes() == b"x" * 10 + b"y" * 90


def test_a_chunk_that_runs_long_is_undone_whole(lib):
    client, _, _ = lib
    up = _announce(client, "Book.m4b", 1000)
    assert _put(client, up["id"], b"a" * 50, 0, 1000).json()["offset"] == 50
    long = client.put(f"{UP}/{up['id']}", content=b"x" * 10, headers={"Content-Range": "bytes 50-54/1000"})
    assert (long.status_code, long.json()["reason"]) == (400, "bad_range")
    assert client.get(f"{UP}/{up['id']}").json()["offset"] == 50  # a broken client's bytes are not trusted


@pytest.mark.parametrize("header", [None, "bytes 5-2/10", "bytes 0-10/10", "0-4/10", "bytes */10"])
def test_a_chunk_needs_a_sane_content_range(lib, header):
    client, _, _ = lib
    up = _announce(client, "Book.m4b", 10)
    r = client.put(f"{UP}/{up['id']}", content=b"12345", headers={"Content-Range": header} if header else {})
    assert (r.status_code, r.json()["reason"]) == (400, "bad_range")


def test_the_total_has_to_be_the_size_announced(lib):
    client, _, _ = lib
    up = _announce(client, "Book.m4b", 10)
    r = client.put(f"{UP}/{up['id']}", content=b"12345", headers={"Content-Range": "bytes 0-4/99"})
    assert (r.status_code, r.json()["reason"]) == (400, "bad_range")


def test_a_chunk_cut_off_in_flight_keeps_what_arrived(lib):
    _, root, cfg = lib
    up = uploads.create(cfg, "Book.m4b", 100)
    with uploads.chunk(cfg, up["id"], 0, 49, 100) as w:
        w.write(b"a" * 50)
    with pytest.raises(RuntimeError):
        with uploads.chunk(cfg, up["id"], 50, 99, 100) as w:
            w.write(b"b" * 20)
            raise RuntimeError("the client went away")
    assert uploads.get(cfg, up["id"])["offset"] == 70
    # A link that dies every 20 bytes still gets there: each try carries on where the last stopped.
    with uploads.chunk(cfg, up["id"], 70, 99, 100) as w:
        w.write(b"c" * 30)
    assert uploads.part_path(cfg, up["id"]).read_bytes() == b"a" * 50 + b"b" * 20 + b"c" * 30


def test_a_newer_request_takes_over_from_a_stalled_one(lib):
    """A dead connection can look alive to the server for minutes; the client's retry must not
    wait for it, and the stalled request must not be able to write once it is replaced."""
    _, _, cfg = lib
    up = uploads.create(cfg, "Book.m4b", 100)
    with pytest.raises(uploads.UploadError) as ended:  # the stalled request, when it finally ends
        with uploads.chunk(cfg, up["id"], 0, 49, 100) as stalled:
            stalled.write(b"a" * 20)  # then nothing more arrives
            # The client gives up, asks where the file stands (20), and sends from there.
            with uploads.chunk(cfg, up["id"], 20, 99, 100) as retry:
                retry.write(b"b" * 80)
            # The stalled connection wakes up late and tries to add to the file.
            with pytest.raises(uploads.UploadError) as err:
                stalled.write(b"zzz")
            assert err.value.code == "superseded"
    assert ended.value.code == "superseded"
    assert uploads.part_path(cfg, up["id"]).read_bytes() == b"a" * 20 + b"b" * 80


def test_a_chunk_that_starts_wrong_is_refused_even_after_a_takeover(lib):
    _, _, cfg = lib
    up = uploads.create(cfg, "Book.m4b", 100)
    with pytest.raises(uploads.UploadError):  # superseded, as above
        with uploads.chunk(cfg, up["id"], 0, 49, 100) as stalled:
            stalled.write(b"a" * 20)
            with pytest.raises(uploads.UploadError) as err:
                with uploads.chunk(cfg, up["id"], 0, 49, 100):  # the client didn't ask where the file stands
                    pass
            assert (err.value.code, err.value.extra["offset"]) == ("offset_mismatch", 20)


def test_the_announcement_refuses_what_could_only_fail_late(lib, server, monkeypatch):
    client, root, _ = lib
    for name, size, status, reason in [
        ("notes.txt", 10, 415, "unsupported"), ("Book.m4b", 0, 400, "bad_size"),
        ("Book.m4b", uploads.MAX_BYTES + 1, 413, "too_large"), (".hidden.m4b", 5, 400, "bad_name"),
    ]:
        r = client.post(UP, json={"filename": name, "size": size})
        assert (r.status_code, r.json()["reason"]) == (status, reason), name
    assert not (root / uploads.STAGING).exists() or not list((root / uploads.STAGING).iterdir())

    monkeypatch.setattr(uploads, "_free", lambda _p: 0)
    r = client.post(UP, json={"filename": "Book.m4b", "size": 10})
    assert (r.status_code, r.json()["reason"]) == (507, "no_space")
    # A zip needs room for its tracks beside it.
    monkeypatch.setattr(uploads, "_free", lambda _p: 10 + uploads.FREE_MARGIN)
    assert client.post(UP, json={"filename": "Book.zip", "size": 10}).status_code == 507
    assert client.post(UP, json={"filename": "Book.m4b", "size": 10}).status_code == 201


def test_a_full_disk_stops_a_chunk_and_keeps_what_arrived(lib, monkeypatch):
    client, _, _ = lib
    up = _announce(client, "Book.m4b", 200)
    assert _put(client, up["id"], b"a" * 100, 0, 200).status_code == 200
    monkeypatch.setattr(uploads, "_free", lambda _p: 0)
    r = _put(client, up["id"], b"b" * 100, 100, 200)
    assert (r.status_code, r.json()["reason"], r.json()["offset"]) == (507, "no_space", 100)
    monkeypatch.undo()
    assert client.get(f"{UP}/{up['id']}").json()["offset"] == 100


def test_the_audiobook_folder_has_to_be_there(server, tmp_path):
    server.cfg.audiobook_dirs = [tmp_path / "not-mounted"]
    r = TestClient(server.app).post(UP, json={"filename": "Book.m4b", "size": 10})
    assert (r.status_code, r.json()["reason"]) == (503, "no_audiobook_folder")
    assert not (tmp_path / "not-mounted").exists()  # and nothing was made on the wrong disk


def test_an_unknown_or_malformed_id_is_not_found(lib):
    client, _, _ = lib
    for bad in ("nope", "../../etc", "0" * 24):
        assert client.get(f"{UP}/{bad}").status_code == 404
        assert client.delete(f"{UP}/{bad}").status_code == 404
    assert client.post(f"{UP}/{'0' * 24}/complete").status_code == 404


def test_abort_throws_away_what_arrived(lib):
    client, root, _ = lib
    up = _announce(client, "Book.m4b", 100)
    _put(client, up["id"], b"a" * 40, 0, 100)
    assert client.delete(f"{UP}/{up['id']}").json() == {"status": "ok"}
    assert client.get(f"{UP}/{up['id']}").status_code == 404
    assert list((root / uploads.STAGING).iterdir()) == []


# ── completing ───────────────────────────────────────────────────────────────

def test_complete_needs_every_byte(lib):
    client, _, _ = lib
    up = _announce(client, "Book.m4b", 100)
    _put(client, up["id"], b"a" * 40, 0, 100)
    r = client.post(f"{UP}/{up['id']}/complete")
    assert (r.status_code, r.json()["reason"], r.json()["offset"]) == (409, "incomplete", 40)


def test_the_content_has_to_be_what_the_name_says(lib):
    client, _, _ = lib
    zipped = b"PK\x03\x04" + b"\0" * 30
    for name, data in [("Book.m4b", zipped), ("Book.zip", b"<html>not a zip</html>"),
                       ("Book.mp3", b"fLaC" + b"\0" * 20), ("Book.flac", b"OggS" + b"\0" * 20)]:
        up = _announce(client, name, len(data))
        _put(client, up["id"], data, 0, len(data))
        r = client.post(f"{UP}/{up['id']}/complete")
        assert (r.status_code, r.json()["reason"]) == (400, "bad_type"), name


@pytest.mark.parametrize("name,data", [
    ("a.m4b", b"\0\0\0\x20ftypM4B "), ("a.m4a", b"\0\0\0\x20ftypM4A "), ("a.mp3", b"ID3\x04" + b"\0" * 8),
    ("a.mp3", b"\xff\xfb\x90\x64" + b"\0" * 8), ("a.flac", b"fLaC" + b"\0" * 8),
    ("a.ogg", b"OggS" + b"\0" * 8), ("a.opus", b"OggS" + b"\0" * 8), ("a.zip", b"PK\x03\x04" + b"\0" * 8),
    ("a.mp3", b"\0" * 12),  # nothing recognised: left to the tags to decide when it is filed
])
def test_what_looks_right_is_accepted(lib, name, data):
    client, _, cfg = lib
    up = _announce(client, name, len(data))
    _put(client, up["id"], data, 0, len(data))
    assert uploads.check_complete(cfg, up["id"])["filename"] == name


# ── the staged item ──────────────────────────────────────────────────────────

def test_a_single_file_is_staged_under_its_own_name(lib):
    _, root, cfg = lib
    data = af.m4b(root / "src.m4b", seconds=3).read_bytes()
    up = uploads.create(cfg, "My Book.m4b", len(data))
    with uploads.chunk(cfg, up["id"], 0, len(data) - 1, len(data)) as w:
        w.write(data)
    d, name = uploads.stage_item(cfg, up["id"])
    assert name == "My Book.m4b" and [p.name for p in d.iterdir()] == ["My Book.m4b"]
    assert not uploads.part_path(cfg, up["id"]).exists()


def test_stale_uploads_are_swept_and_fresh_ones_kept(lib):
    _, root, cfg = lib
    old, fresh = (uploads.create(cfg, "Book.m4b", 10)["id"] for _ in range(2))
    for i in (old, fresh):
        uploads.part_path(cfg, i).write_bytes(b"x")
        uploads.item_dir(cfg, i).mkdir()
    week = 8 * 86400
    for p in (root / uploads.STAGING).iterdir():
        if p.name.startswith(old):
            os.utime(p, (time.time() - week, time.time() - week))
    assert uploads.sweep(cfg) == 1
    left = sorted(p.name for p in (root / uploads.STAGING).iterdir())
    assert left == sorted([f"{fresh}.json", f"{fresh}.part", fresh])


def test_sweeping_with_no_folder_is_nothing(tmp_path):
    assert uploads.sweep(make_cfg(tmp_path, audiobook_dirs=[tmp_path / "nowhere"])) == 0
    assert uploads.sweep(make_cfg(tmp_path, audiobook_dirs=[])) == 0


# ── zip: what is kept, and what is never trusted ─────────────────────────────

def _zip(path, entries, compress=zipfile.ZIP_STORED):
    with zipfile.ZipFile(path, "w", compress) as zf:
        for name, data in entries:
            zf.writestr(name, data, compress_type=compress)
    return path


def _names(out):
    return [p.name for p in out]


def test_a_zip_gives_its_tracks_in_play_order_and_its_cover(tmp_path):
    z = _zip(tmp_path / "b.zip", [
        ("Book/02.mp3", b"2"), ("Book/01.mp3", b"1"), ("Book/cover.JPG", b"img"),
        ("Book/notes.txt", b"x"), ("Book/booklet.pdf", b"x"), ("__MACOSX/Book/._01.mp3", b"x"),
        ("Book/.DS_Store", b"x"), ("Book/", b"")])
    out = uploads.safe_extract(z, tmp_path / "out")
    assert _names(out) == ["01.mp3", "02.mp3"]  # already in order: names kept
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["01.mp3", "02.mp3", "cover.jpg"]


def test_tracks_whose_names_would_sort_wrong_are_numbered(tmp_path):
    z = _zip(tmp_path / "b.zip", [(f"{n}.mp3", str(n).encode()) for n in (10, 2, 1)])
    out = uploads.safe_extract(z, tmp_path / "out")
    assert _names(out) == ["001 1.mp3", "002 2.mp3", "003 10.mp3"]
    assert [p.read_bytes() for p in out] == [b"1", b"2", b"10"]  # and the bytes are the right ones


def test_discs_in_folders_become_one_numbered_folder(tmp_path):
    z = _zip(tmp_path / "b.zip", [("CD2/01.mp3", b"c"), ("CD1/02.mp3", b"b"), ("CD1/01.mp3", b"a")])
    out = uploads.safe_extract(z, tmp_path / "out")
    assert _names(out) == ["001 01.mp3", "002 02.mp3", "003 01.mp3"]
    assert [p.read_bytes() for p in out] == [b"a", b"b", b"c"]


@pytest.mark.parametrize("name", ["../evil.mp3", "/abs/evil.mp3", "a/../../evil.mp3", "..\\evil.mp3",
                                  "C:/evil.mp3", "ok/../../notes.txt"])
def test_a_zip_with_an_unsafe_path_is_refused_whole(tmp_path, name):
    z = _zip(tmp_path / "b.zip", [("01.mp3", b"a"), (name, b"x")])
    with pytest.raises(uploads.UploadError) as err:
        uploads.safe_extract(z, tmp_path / "out")
    assert err.value.code == "unsafe_zip"
    assert not (tmp_path / "out").exists() and not (tmp_path / "evil.mp3").exists()


def test_a_symlink_in_a_zip_is_refused(tmp_path):
    z = tmp_path / "b.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("01.mp3", b"a")
        link = zipfile.ZipInfo("02.mp3")
        link.external_attr = 0o120777 << 16
        zf.writestr(link, "/etc/passwd")
    with pytest.raises(uploads.UploadError) as err:
        uploads.safe_extract(z, tmp_path / "out")
    assert (err.value.code, "symbolic link" in str(err.value)) == ("unsafe_zip", True)


def test_a_password_protected_zip_is_refused(tmp_path):
    z = _zip(tmp_path / "b.zip", [("01.mp3", b"a")])
    raw = bytearray(z.read_bytes())
    raw[raw.find(b"PK\x01\x02") + 8] |= 1  # the encrypted flag, in the central directory
    z.write_bytes(raw)
    with pytest.raises(uploads.UploadError) as err:
        uploads.safe_extract(z, tmp_path / "out")
    assert "password" in str(err.value)


def test_a_zip_bomb_is_refused_before_anything_is_written(tmp_path):
    z = _zip(tmp_path / "b.zip", [("01.mp3", b"\0" * (40 << 20))], compress=zipfile.ZIP_DEFLATED)
    assert z.stat().st_size < 100_000
    with pytest.raises(uploads.UploadError) as err:
        uploads.safe_extract(z, tmp_path / "out")
    assert err.value.code == "zip_bomb" and not (tmp_path / "out").exists()


def test_a_zip_of_too_many_files_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(uploads, "ZIP_MAX_FILES", 2)
    z = _zip(tmp_path / "b.zip", [(f"{i}.mp3", b"x") for i in range(3)])
    with pytest.raises(uploads.UploadError) as err:
        uploads.safe_extract(z, tmp_path / "out")
    assert err.value.code == "zip_bomb"


def test_a_zip_with_no_audio_or_no_zip_at_all(tmp_path):
    z = _zip(tmp_path / "b.zip", [("notes.txt", b"x")])
    with pytest.raises(uploads.UploadError) as err:
        uploads.safe_extract(z, tmp_path / "out")
    assert err.value.code == "no_audio"
    (tmp_path / "c.zip").write_bytes(b"not a zip")
    with pytest.raises(uploads.UploadError) as err:
        uploads.safe_extract(tmp_path / "c.zip", tmp_path / "out2")
    assert err.value.code == "bad_zip"


def test_a_zip_needs_room_to_unpack(tmp_path, monkeypatch):
    z = _zip(tmp_path / "b.zip", [("01.mp3", b"x" * 1000)])
    monkeypatch.setattr(uploads, "_free", lambda _p: 500)
    with pytest.raises(uploads.UploadError) as err:
        uploads.safe_extract(z, tmp_path / "out")
    assert err.value.status == 507


# ── who may upload ───────────────────────────────────────────────────────────

def test_with_a_password_set_every_upload_route_needs_it(lib):
    client, _, _ = lib
    assert client.post("/api/auth/password", json={"password": "correct horse"}).status_code == 200
    stranger = TestClient(client.app)
    for call in (lambda: stranger.post(UP, json={"filename": "a.m4b", "size": 1}),
                 lambda: stranger.get(f"{UP}/{'0' * 24}"), lambda: stranger.delete(f"{UP}/{'0' * 24}"),
                 lambda: stranger.put(f"{UP}/{'0' * 24}", content=b"x"),
                 lambda: stranger.post(f"{UP}/{'0' * 24}/complete")):
        r = call()
        assert (r.status_code, r.json()["detail"]) == (401, "admin_required")
    assert client.post(UP, json={"filename": "a.m4b", "size": 1}).status_code == 201
