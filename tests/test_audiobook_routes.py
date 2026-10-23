"""The audiobook routes over the new formats: track folders of any kind, a
book's own cover, old ids redirected after a re-key, and a tag-linked book
eligible for audio sync."""
import pytest
from fastapi.testclient import TestClient

import audio_fixtures as af
from epublib import audiobook_scanner, audiobook_xref, build
from epublib.index_store import load_json, save_json


@pytest.fixture
def lib(server, tmp_path):
    root = tmp_path / "audio"
    root.mkdir()
    server.cfg.audiobook_dirs = [root]
    return server, root, TestClient(server.app)


def _index(cfg):
    """The served rows of the local books, by path (+ album, for a shared folder)."""
    files = audiobook_scanner.scan(cfg)
    built = audiobook_scanner.build_index(cfg)
    return {e["path"] + (e["group"] or ""): built[k] for k, e in files.items()}


def test_a_folder_of_flac_tracks_is_served_track_by_track(lib):
    server, root, client = lib
    for i in (1, 2):
        af.flac(root / "Book" / f"{i:02}.flac", album="本")
    ab_id = _index(server.cfg)["Book"]["ab_id"]

    assert client.get(f"/download/audiobook/{ab_id}").json() == {"ab_id": ab_id, "files": ["01.flac", "02.flac"]}
    r = client.get(f"/download/audiobook/{ab_id}", params={"part": "02.flac"})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/flac"
    assert client.get(f"/download/audiobook/{ab_id}", params={"part": "../02.flac"}).status_code == 404


def test_a_single_file_of_any_format_downloads_under_its_own_name(lib):
    server, root, client = lib
    af.opus(root / "Solo" / "solo.opus", title="T")
    ab_id = _index(server.cfg)["Solo/solo.opus"]["ab_id"]
    r = client.get(f"/download/audiobook/{ab_id}")
    assert r.status_code == 200 and r.headers["content-type"] == "application/octet-stream"
    assert 'filename="solo.opus"' in r.headers["content-disposition"]


def test_deleting_one_album_leaves_the_other_in_its_folder(lib):
    server, root, client = lib
    for i in (1, 2):
        af.m4a(root / "Two" / f"a{i}.m4a", album="A")
        af.m4a(root / "Two" / f"b{i}.m4a", album="B")
    index = _index(server.cfg)
    r = client.delete(f"/api/audiobooks/{index['TwoA']['ab_id']}")
    assert r.status_code == 200 and sorted(r.json()["deleted_files"]) == ["a1.m4a", "a2.m4a"]
    assert sorted(p.name for p in (root / "Two").iterdir()) == ["b1.m4a", "b2.m4a"]
    assert index["TwoB"]["ab_id"] in load_json(server.cfg.audiobook_files_index, {})


def test_a_books_own_cover_is_served_as_a_thumbnail_or_in_full(lib):
    server, root, client = lib
    af.m4b(root / "x.m4b", title="T", cover=af.PNG)
    row = _index(server.cfg)["x.m4b"]
    assert row["cover_url"].startswith(f"/covers/audiobook/{row['ab_id']}?v=")
    thumb = client.get(row["cover_url"])
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/webp"
    assert "immutable" in thumb.headers["cache-control"]
    full = client.get(f"/covers/audiobook/{row['ab_id']}", params={"full": 1})
    assert full.content == af.PNG
    assert client.get("/covers/audiobook/nope").status_code == 404


def test_an_old_id_redirects_to_where_the_book_went(lib):
    server, root, client = lib
    af.m4b(root / "x.m4b", title="T", cover=af.PNG)
    old = next(iter(_index(server.cfg).values()))["ab_id"]
    (root / "x.m4b").rename(root / "x [B0NEWNEWNE].m4b")
    _index(server.cfg)
    r = client.get(f"/download/audiobook/{old}", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/download/audiobook/B0NEWNEWNE"
    r = client.get(f"/covers/audiobook/{old}", params={"full": 1}, follow_redirects=False)
    assert r.headers["location"] == "/covers/audiobook/B0NEWNEWNE?full=1"
    assert client.get(f"/download/audiobook/{old}").status_code == 200


def test_a_tag_linked_book_can_be_audio_synced(lib):
    server, root, client = lib
    cfg = server.cfg
    af.m4b(root / "x.m4b", title="夜のピクニック", album_artist="恩田陸")
    ab_id = next(iter(_index(cfg).values()))["ab_id"]
    save_json(cfg.library_index, {"bk": {"book_id": "bk", "language": "ja", "path": "bk.epub",
                                         "title": "夜のピクニック", "author": "恩田陸"}})
    audiobook_xref.xref(cfg)
    build.build(cfg)
    st = client.get("/api/books/bk/status").json()["alignment"]
    assert st["eligible"] is True
    assert load_json(cfg.audiobook_links, {})["bk"]["audible_isbn"] == ab_id
