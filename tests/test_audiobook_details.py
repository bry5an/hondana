"""Editing an audiobook's details and cover by hand, and what a book with no
ASIN can then do: link to its EPUB (automatically and by the picker) and be
audio-synced."""
import sys
import time

import pytest
from fastapi.testclient import TestClient

import audio_fixtures as af
from epublib import audiobook_scanner
from epublib.index_store import load_json, save_json
from test_align import _book


@pytest.fixture
def lib(server, tmp_path):
    root = tmp_path / "audio"
    root.mkdir()
    server.cfg.audiobook_dirs = [root]
    return server, root, TestClient(server.app)


def _scan(cfg) -> dict:
    """Scan + index; the local books by path."""
    files = audiobook_scanner.scan(cfg)
    audiobook_scanner.build_index(cfg)
    return {e["path"]: k for k, e in files.items()}


def _row(client, ab_id) -> dict:
    return next(e for e in client.get("/api/audiobooks").json() if e["ab_id"] == ab_id)


# ── details ──────────────────────────────────────────────────────────────────

def test_a_guessed_book_is_edited_into_a_real_one_and_stays_edited(lib):
    server, root, client = lib
    af.m4b(root / "rip01.m4b", seconds=5)
    ab_id = _scan(server.cfg)["rip01.m4b"]
    st = client.get(f"/api/audiobooks/{ab_id}/details").json()
    assert (st["source"], st["values"]["title"], st["overridden"], st["local"]) == ("filename", "rip01", [], True)

    r = client.post(f"/api/audiobooks/{ab_id}/details", json={
        "title": "夜のピクニック", "author": "恩田 陸", "series": "ピクニック", "order": "2",
        "genre": "ホラー", "description": "  あらすじ\n二行目  ", "release_date": "2004-07"})
    assert r.status_code == 200, r.text
    st = r.json()
    assert st["source"] == "manual"
    assert st["values"] == {"title": "夜のピクニック", "author": "恩田 陸", "series": "ピクニック",
                            "order": "2", "narrators": None, "genre": "ホラー",
                            "description": "あらすじ\n二行目", "release_date": "2004-07"}
    assert st["auto"]["title"] == "rip01"
    row = _row(client, ab_id)
    assert (row["title"], row["series"], row["source"]) == ("夜のピクニック", "ピクニック", "manual")

    _scan(server.cfg)  # a rescan keeps the edits
    assert client.get(f"/api/audiobooks/{ab_id}/details").json()["values"]["title"] == "夜のピクニック"

    st = client.post(f"/api/audiobooks/{ab_id}/details", json={"title": None, "genre": ""}).json()
    assert st["values"]["title"] == "rip01" and st["values"]["genre"] is None
    assert sorted(st["overridden"]) == ["author", "description", "order", "release_date", "series"]


def test_an_audible_row_can_be_corrected_too(lib):
    server, root, client = lib
    save_json(server.cfg.audiobook_data, {"B0AAAAAAAA": {"title": "T", "series": "S", "order": "03"}})
    audiobook_scanner.build_index(server.cfg)
    st = client.post("/api/audiobooks/B0AAAAAAAA/details", json={"order": "04"}).json()
    assert (st["values"]["order"], st["auto"]["order"], st["source"], st["local"]) == ("04", "03", "catalog", False)


def test_nothing_changed_writes_nothing(lib):
    server, root, client = lib
    af.m4b(root / "a.m4b")
    ab_id = _scan(server.cfg)["a.m4b"]
    assert client.post(f"/api/audiobooks/{ab_id}/details", json={"title": ""}).status_code == 200
    assert not server.cfg.audiobook_metadata_overrides.exists()


@pytest.mark.parametrize("body, detail", [
    ({"genre": "not a genre"}, "Unknown genre"),
    ({"release_date": "July 2004"}, "Release date"),
    ({"order": "x" * 13}, "too long"),
])
def test_bad_values_are_refused_before_anything_is_written(lib, body, detail):
    server, root, client = lib
    af.m4b(root / "a.m4b")
    ab_id = _scan(server.cfg)["a.m4b"]
    r = client.post(f"/api/audiobooks/{ab_id}/details", json={"title": "ok", **body})
    assert r.status_code == 400 and detail in r.json()["detail"]
    assert not server.cfg.audiobook_metadata_overrides.exists()


def test_an_unknown_audiobook_is_404(lib):
    server, root, client = lib
    assert client.get("/api/audiobooks/nope/details").status_code == 404
    assert client.post("/api/audiobooks/nope/details", json={"title": "x"}).status_code == 404
    assert client.delete("/api/audiobooks/nope/cover").status_code == 404


# ── cover ────────────────────────────────────────────────────────────────────

def test_a_hand_set_cover_wins_and_a_reset_goes_back_to_the_files_own(lib):
    server, root, client = lib
    af.m4b(root / "a.m4b", title="T", cover=af.PNG)
    ab_id = _scan(server.cfg)["a.m4b"]
    own = client.get(f"/api/audiobooks/{ab_id}/details").json()["cover"]["url"]

    upload = af.PNG[:-12] + af.PNG[-12:]  # same image, its own bytes
    r = client.post(f"/api/audiobooks/{ab_id}/cover", files={"file": ("c.png", upload, "image/png")})
    assert r.status_code == 200, r.text
    cover = r.json()["cover"]
    assert cover["overridden"] and cover["url"] != own and cover["auto_url"] == own
    assert _row(client, ab_id)["cover_url"] == cover["url"]
    assert client.get(cover["url"]).headers["content-type"] == "image/webp"
    custom = server.cfg.audiobook_custom_covers_dir / f"{ab_id}.png"
    assert custom.read_bytes() == upload

    _scan(server.cfg)  # neither the scan nor its cover sweep touches it
    assert custom.exists()

    cover = client.delete(f"/api/audiobooks/{ab_id}/cover").json()["cover"]
    assert (cover["overridden"], cover["url"]) == (False, own) and not custom.exists()


def test_a_cover_that_isnt_an_image_is_refused(lib):
    server, root, client = lib
    af.m4b(root / "a.m4b")
    ab_id = _scan(server.cfg)["a.m4b"]
    r = client.post(f"/api/audiobooks/{ab_id}/cover", files={"file": ("c.png", b"<svg/>", "image/png")})
    assert r.status_code == 400


def test_edits_and_a_cover_follow_the_book_when_it_gains_a_key(lib):
    server, root, client = lib
    af.m4b(root / "a.m4b", seconds=60, title="題名")
    old = _scan(server.cfg)["a.m4b"]
    client.post(f"/api/audiobooks/{old}/details", json={"narrators": "読み手"})
    client.post(f"/api/audiobooks/{old}/cover", files={"file": ("c.png", af.PNG, "image/png")})
    (root / "a.m4b").rename(root / "a [B0NEWNEWNE].m4b")
    _scan(server.cfg)
    st = client.get("/api/audiobooks/B0NEWNEWNE/details").json()
    assert st["values"]["narrators"] == "読み手" and st["cover"]["overridden"]
    assert (server.cfg.audiobook_custom_covers_dir / "B0NEWNEWNE.png").exists()


# ── what a book with no ASIN can now do ──────────────────────────────────────

def _epub(cfg, title="夜のピクニック", author="恩田陸"):
    book = {**_book(cfg, "b1"), "title": title, "author": author}
    del book["audiobook"]
    save_json(cfg.library_index, {"b1": book})


def test_a_tagged_book_links_by_itself_and_through_the_picker(lib):
    from epublib import audiobook_xref, build

    server, root, client = lib
    cfg = server.cfg
    af.m4b(root / "tagged.m4b", title="夜のピクニック", album_artist="恩田陸")
    af.m4b(root / "guess.m4b", seconds=5)
    ids = _scan(cfg)
    _epub(cfg)
    audiobook_xref.xref(cfg)
    build.build(cfg)
    assert load_json(cfg.audiobook_links, {})["b1"]["audible_isbn"] == ids["tagged.m4b"]

    # The picker finds the guessed one by its file name, and pins it.
    found = client.get("/api/audiobook-catalog/search", params={"q": "guess"}).json()["results"]
    assert [(e["isbn"], e["local"]) for e in found] == [(ids["guess.m4b"], True)]
    r = client.post("/api/books/b1/audiobook-link", json={"isbn": ids["guess.m4b"]})
    assert r.status_code == 200 and r.json()["link"]["audiobook_local"] is True


def test_a_book_whose_details_were_typed_in_links_and_can_be_synced(lib, tmp_path):
    server, root, client = lib
    cfg = server.cfg
    af.m4b(root / "rip.m4b", seconds=5)
    ab_id = _scan(cfg)["rip.m4b"]
    _epub(cfg)
    client.post(f"/api/audiobooks/{ab_id}/details", json={"title": "夜のピクニック", "author": "恩田陸"})
    assert load_json(cfg.audiobook_links, {})["b1"]["audible_isbn"] == ab_id

    # A stand-in for SubPlz that times each sentence 4 s apart.
    script = tmp_path / "fake.py"
    script.write_text(
        "import pathlib, sys\n"
        "audio, text, out = map(pathlib.Path, sys.argv[1:4])\n"
        "ts = lambda ms: '%02d:%02d:%02d,%03d' % (ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000)\n"
        "lines = text.read_text(encoding='utf-8').splitlines()\n"
        "cues = ['%d\\n%s --> %s\\n%s\\n' % (i + 1, ts(i * 4000), ts(i * 4000 + 4000), s) for i, s in enumerate(lines)]\n"
        "(out / (audio.stem + '.srt')).write_text('\\n'.join(cues), encoding='utf-8')\n")
    cfg.subplz_cmd = [sys.executable, str(script), "{audio}", "{text}", "{out}"]
    assert client.get("/api/books/b1/status").json()["alignment"]["eligible"] is True
    r = client.post("/api/books/b1/alignment/generate")
    assert r.status_code == 200, r.text
    end = time.time() + 20
    while (st := client.get("/api/books/b1/status").json()["alignment"])["state"] != "ready":
        assert st["error"] is None and time.time() < end, st
        time.sleep(0.05)
    assert st["coverage"] == 1.0
