"""Hosting your own Yomitan zips to the iOS reader: the manifest read
from each zip's index.json, and the whitelisted, rangeable download."""
import hashlib
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from conftest import toml_str
from epublib import dicts


def make_zip(path, index=None, banks=None):
    """A Yomitan zip with the given index.json and {bank file: rows}."""
    with zipfile.ZipFile(path, "w") as zf:
        if index is not None:
            zf.writestr("index.json", json.dumps(index))
        for name, rows in (banks or {}).items():
            zf.writestr(name, json.dumps(rows))


TERM = {"title": "Test Jitendex", "revision": "2026.10.01", "format": 3, "author": "someone",
        "attribution": "JMdict", "url": "https://example.test/jitendex"}


@pytest.fixture
def folder(server):
    root = server.cfg.dicts_dir
    root.mkdir(parents=True)
    make_zip(root / "jitendex.zip", TERM,
             {"term_bank_1.json": [["猫", "ねこ", "n", "", 1, ["cat"], 1, ""]]})
    make_zip(root / "freq.zip", {"title": "Freq", "revision": "1", "format": 3},
             {"term_meta_bank_1.json": [["猫", "freq", {"value": 120}]]})
    return root


@pytest.fixture
def client(server):
    return TestClient(server.app)


def by_file(client):
    r = client.get("/api/reader/dicts")
    assert r.status_code == 200
    return {d["file"]: d for d in r.json()["dicts"]}


def test_the_manifest_is_built_from_each_zips_index_and_banks(server, client, folder):
    got = by_file(client)
    assert list(got) == ["freq.zip", "jitendex.zip"]
    jit = got["jitendex.zip"]
    data = (folder / "jitendex.zip").read_bytes()
    assert jit == {
        "file": "jitendex.zip", "title": "Test Jitendex", "revision": "2026.10.01", "format": 3,
        "author": "someone", "attribution": "JMdict", "url": "https://example.test/jitendex",
        "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "kind": "term",
    }
    assert got["freq.zip"]["kind"] == "freq"
    # Fields the index lacks come back empty, not missing.
    assert got["freq.zip"]["author"] == "" and got["freq.zip"]["url"] == ""


def test_kind_follows_the_bank_files_and_the_meta_mode(server, client, folder):
    make_zip(folder / "pitch.zip", {"title": "Kanjium"},
             {"term_meta_bank_1.json": [["猫", "pitch", {"reading": "ねこ", "pitches": []}]]})
    make_zip(folder / "kanji.zip", {"title": "KANJIDIC"},
             {"kanji_bank_1.json": [["猫", "ビョウ", "ねこ", "", ["cat"], {}]]})
    make_zip(folder / "both.zip", {"title": "Mixed"},
             {"term_bank_1.json": [], "term_meta_bank_1.json": [["猫", "freq", 1]]})
    make_zip(folder / "oldfmt.zip", {"title": "Old", "version": 1}, {"term_bank_1.json": []})
    got = by_file(client)
    assert {f: got[f]["kind"] for f in ("pitch.zip", "kanji.zip", "both.zip", "oldfmt.zip")} == {
        "pitch.zip": "pitch", "kanji.zip": "kanji", "both.zip": "mixed", "oldfmt.zip": "term"}
    assert got["oldfmt.zip"]["format"] == 1   # `version` stands in for `format`


def test_only_readable_yomitan_zips_are_listed(server, client, folder):
    (folder / "notes.txt").write_text("hello")
    (folder / "broken.zip").write_bytes(b"not a zip")
    make_zip(folder / "no-index.zip", None, {"term_bank_1.json": []})
    (folder / "sub").mkdir()
    make_zip(folder / "sub" / "nested.zip", {"title": "Nested"}, {"term_bank_1.json": []})
    assert set(by_file(client)) == {"freq.zip", "jitendex.zip"}


def test_a_missing_or_empty_folder_is_an_empty_manifest(server, client):
    assert client.get("/api/reader/dicts").json() == {"dicts": []}
    server.cfg.dicts_dir.mkdir(parents=True)
    assert client.get("/api/reader/dicts").json() == {"dicts": []}


def test_a_zip_is_hashed_once_until_it_changes(server, client, folder, monkeypatch):
    calls = []
    real = dicts._entry
    monkeypatch.setattr(dicts, "_entry", lambda p, size: calls.append(p.name) or real(p, size))
    by_file(client)
    by_file(client)
    assert sorted(calls) == ["freq.zip", "jitendex.zip"]

    make_zip(folder / "freq.zip", {"title": "Freq v2", "revision": "2"},
             {"term_meta_bank_1.json": [["猫", "freq", {"value": 1}], ["犬", "freq", {"value": 2}]]})
    got = by_file(client)
    assert calls.count("freq.zip") == 2 and calls.count("jitendex.zip") == 1
    assert got["freq.zip"]["title"] == "Freq v2"

    (folder / "freq.zip").unlink()
    assert set(by_file(client)) == {"jitendex.zip"}


def test_a_listed_zip_downloads_whole(server, client, folder):
    r = client.get("/api/reader/dicts/jitendex.zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert r.content == (folder / "jitendex.zip").read_bytes()


def test_a_ranged_get_returns_206(server, client, folder):
    data = (folder / "jitendex.zip").read_bytes()
    r = client.get("/api/reader/dicts/jitendex.zip", headers={"Range": "bytes=10-49"})
    assert r.status_code == 206
    assert r.content == data[10:50]
    assert r.headers["content-range"] == f"bytes 10-49/{len(data)}"
    tail = client.get("/api/reader/dicts/jitendex.zip", headers={"Range": "bytes=-20"})
    assert tail.status_code == 206 and tail.content == data[-20:]


@pytest.mark.parametrize("name", [
    "..", "../index.json", "..%2Fsecret.zip", "%2e%2e%2Fsecret.zip", "%2Fetc%2Fpasswd",
    "/etc/passwd", "nope.zip", "notes.txt", "sub%2Fnested.zip", "jitendex.zip%00", "JITENDEX.ZIP",
])
def test_traversal_absolute_paths_and_unknown_names_are_404(server, client, folder, name):
    (server.cfg.data_dir / "secret.zip").write_bytes(b"secret")
    (folder / "notes.txt").write_text("hello")
    r = client.get(f"/api/reader/dicts/{name}")
    assert r.status_code == 404


def test_a_dicts_dir_in_the_config_is_honoured(tmp_path):
    from epublib import config

    elsewhere = tmp_path / "my dicts"
    toml = tmp_path / "c.toml"
    toml.write_text(f'epub_dir = {toml_str(tmp_path)}\ndata_dir = {toml_str(tmp_path / "d")}\n'
                    f'dicts_dir = {toml_str(elsewhere)}\n', encoding="utf-8")
    assert config.load(toml).dicts_dir == elsewhere
    toml.write_text(f'epub_dir = {toml_str(tmp_path)}\ndata_dir = {toml_str(tmp_path / "d")}\n', encoding="utf-8")
    assert config.load(toml).dicts_dir == tmp_path / "d" / "dicts"


def test_find_serves_only_names_in_the_manifest(server, folder):
    cfg = server.cfg
    (cfg.data_dir / "secret.zip").write_bytes(b"secret")
    assert dicts.find(cfg, "jitendex.zip") == folder / "jitendex.zip"
    for bad in ("../secret.zip", "/etc/passwd", "sub/../jitendex.zip", "", ".", "nope.zip"):
        assert dicts.find(cfg, bad) is None


# ── managing the folder from /admin ──

def upload(client, path_or_bytes, name="new.zip"):
    data = path_or_bytes.read_bytes() if hasattr(path_or_bytes, "read_bytes") else path_or_bytes
    return client.post("/api/admin/dicts", files={"file": (name, data, "application/zip")})


def test_an_uploaded_zip_is_listed_and_served(server, client, tmp_path):
    src = tmp_path / "src.zip"
    make_zip(src, {"title": "Uploaded", "revision": "7", "format": 3}, {"term_bank_1.json": []})
    r = upload(client, src, "My Dict.zip")
    assert r.status_code == 201
    row = r.json()
    assert row["file"] == "My Dict.zip" and row["title"] == "Uploaded" and row["kind"] == "term"
    assert row["sha256"] == hashlib.sha256(src.read_bytes()).hexdigest()
    assert by_file(client)["My Dict.zip"] == row
    assert client.get("/api/reader/dicts/My%20Dict.zip").content == src.read_bytes()
    # No temp file is left behind.
    assert [p.name for p in server.cfg.dicts_dir.iterdir()] == ["My Dict.zip"]


def test_uploading_a_name_again_replaces_it_and_the_phone_sees_the_change(server, client, folder, tmp_path):
    before = by_file(client)["freq.zip"]["sha256"]
    src = tmp_path / "v2.zip"
    make_zip(src, {"title": "Freq v2", "revision": "2"}, {"term_meta_bank_1.json": [["猫", "freq", 1]]})
    assert upload(client, src, "freq.zip").status_code == 201
    after = by_file(client)["freq.zip"]
    assert after["revision"] == "2" and after["sha256"] != before


@pytest.mark.parametrize("name, data", [
    ("notes.txt", b"hello"),
    ("broken.zip", b"not a zip"),
    (".hidden.zip", b"x"),
])
def test_a_bad_upload_is_refused_and_changes_nothing(server, client, folder, name, data):
    r = upload(client, data, name)
    assert r.status_code == 400 and r.json()["detail"]
    assert sorted(p.name for p in folder.iterdir()) == ["freq.zip", "jitendex.zip"]


def test_a_zip_without_index_json_is_refused_and_keeps_the_old_one(server, client, folder, tmp_path):
    src = tmp_path / "no-index.zip"
    make_zip(src, None, {"term_bank_1.json": []})
    before = (folder / "jitendex.zip").read_bytes()
    r = upload(client, src, "jitendex.zip")
    assert r.status_code == 400 and "index.json" in r.json()["detail"]
    assert (folder / "jitendex.zip").read_bytes() == before
    assert not [p for p in folder.iterdir() if p.name.startswith(".")]


def test_an_upload_cannot_name_a_path_outside_the_folder(server, client, tmp_path):
    src = tmp_path / "x.zip"
    make_zip(src, {"title": "X"}, {"term_bank_1.json": []})
    assert upload(client, src, "../../escape.zip").status_code == 201
    assert not (server.cfg.data_dir / "escape.zip").exists()
    assert (server.cfg.dicts_dir / "escape.zip").is_file()


def test_an_oversized_upload_is_refused(server, client, monkeypatch, tmp_path):
    monkeypatch.setattr(dicts, "MAX_BYTES", 100)
    src = tmp_path / "big.zip"
    make_zip(src, {"title": "Big"}, {"term_bank_1.json": [["a"] * 50]})
    assert src.stat().st_size > 100
    r = upload(client, src, "big.zip")
    assert r.status_code == 400 and "large" in r.json()["detail"].lower()
    assert not server.cfg.dicts_dir.exists() or not list(server.cfg.dicts_dir.iterdir())


def test_delete_removes_a_listed_zip(server, client, folder):
    r = client.delete("/api/admin/dicts/freq.zip")
    assert r.status_code == 200
    assert not (folder / "freq.zip").exists()
    assert set(by_file(client)) == {"jitendex.zip"}


@pytest.mark.parametrize("name", ["nope.zip", "..%2Fsecret.zip", "%2Fetc%2Fpasswd", "notes.txt"])
def test_delete_only_touches_manifest_zips(server, client, folder, name):
    (server.cfg.data_dir / "secret.zip").write_bytes(b"secret")
    (folder / "notes.txt").write_text("hello")
    assert client.delete(f"/api/admin/dicts/{name}").status_code == 404
    assert (server.cfg.data_dir / "secret.zip").exists() and (folder / "notes.txt").exists()
