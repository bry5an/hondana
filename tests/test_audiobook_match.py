"""Matching a local audiobook to an Audible book by hand: search, the match and its
corrections, what moves with the file, the refusals, and the file rename.
Audible is faked (`audible.search` / `audible.fetch_product`); nothing here
touches the network."""
import time

import pytest
import requests
from fastapi.testclient import TestClient

import audio_fixtures as af
from epublib import audible, audiobook_match, audiobook_scanner
from epublib.index_store import load_json, save_json
from test_align import _book

ASIN = "B0MATCHED1"
OTHER = "B0OTHERBK2"


def product(asin=ASIN, title="夜のピクニック", author="恩田陸", **kw):
    return {"asin": asin, "title": title, "authors": [{"name": author}], "narrators": [{"name": "声優"}],
            "series": [{"title": "ピクニック", "sequence": "1"}], "runtime_length_min": 490,
            "product_images": {"500": f"https://img/{asin}.jpg"}, "release_date": "2004-07-01", **kw}


@pytest.fixture
def lib(server, tmp_path):
    root = tmp_path / "audio"
    root.mkdir()
    server.cfg.audiobook_dirs = [root]
    return server, root, TestClient(server.app)


@pytest.fixture
def aud(monkeypatch):
    """A fake Audible: `aud.products` {asin: product | None | Exception}, `aud.results`
    for a search; `aud.fetched` / `aud.searched` record what was asked."""
    class Aud:
        products: dict = {ASIN: product(), OTHER: product(OTHER, "別の本", "誰か")}
        results: list = []
        fetched: list = []
        searched: list = []

    def fetch(asin, marketplace=None):
        Aud.fetched.append((asin, marketplace))
        out = Aud.products.get(asin)
        if isinstance(out, Exception):
            raise out
        return out

    def search(title="", author="", keywords="", *, marketplace=None, limit=10):
        if not (title.strip() or author.strip() or keywords.strip()):
            raise ValueError("Give a title, an author or some keywords to search for.")
        Aud.searched.append((title, author, keywords, marketplace))
        return Aud.results

    Aud.results, Aud.fetched, Aud.searched = [], [], []
    monkeypatch.setattr(audible, "fetch_product", fetch)
    monkeypatch.setattr(audible, "search", search)
    return Aud


def _scan(cfg) -> dict:
    """Scan + index; the local books' ab_ids by path."""
    files = audiobook_scanner.scan(cfg)
    audiobook_scanner.build_index(cfg)
    return {e["path"]: k for k, e in files.items()}


def _row(client, ab_id) -> dict:
    return next(e for e in client.get("/api/audiobooks").json() if e["ab_id"] == ab_id)


def _progress(cfg, ab_id, seconds=12.0):
    path = cfg.users_dir / cfg.active_user / "audio_progress.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    save_json(path, {**load_json(path, {}), ab_id: {"track": 0, "seconds": seconds,
                                                    "updated_at": "2026-10-01T00:00:00+00:00"}})
    return path


# ── search ───────────────────────────────────────────────────────────────────

def test_a_search_marks_the_books_already_here(lib, aud):
    server, root, client = lib
    af.m4b(root / "have [B0HELD0001].m4b", seconds=5)
    _scan(server.cfg)
    save_json(server.cfg.audiobook_data, {"B0INCATLOG": {"title": "カタログ"}})
    aud.results = [product("B0HELD0001"), product("B0INCATLOG", "カタログ"), product("B0FRESH000", "新刊")]
    r = client.get("/api/audible/search", params={"title": "夜", "author": "恩田"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["marketplace"] == {"code": "jp", "label": "Japan"}
    assert [(c["asin"], c["local"], c["in_catalog"]) for c in body["results"]] == [
        ("B0HELD0001", True, False), ("B0INCATLOG", False, True), ("B0FRESH000", False, False)]
    first = body["results"][2]
    assert (first["title"], first["author"], first["narrators"], first["series"], first["order"],
            first["runtime_min"], first["release_date"]) == ("新刊", "恩田陸", "声優", "ピクニック", "01", 490, "2004-07-01")
    assert first["url"] == "https://www.audible.co.jp/pd/B0FRESH000" and first["cover"].endswith("B0FRESH000.jpg")
    assert aud.searched == [("夜", "恩田", "", "jp")]


def test_the_search_goes_to_the_configured_store(lib, aud):
    server, root, client = lib
    server.cfg.audible_marketplace = "uk"
    body = client.get("/api/audible/search", params={"keywords": "x"}).json()
    assert body["marketplace"] == {"code": "uk", "label": "United Kingdom"}
    assert aud.searched[-1][3] == "uk"


def test_an_empty_search_is_a_400(lib, aud):
    server, root, client = lib
    r = client.get("/api/audible/search", params={"title": " "})
    assert r.status_code == 400 and "Give a title" in r.json()["detail"]


def test_audible_being_down_is_a_502_with_a_readable_message(lib, monkeypatch):
    server, root, client = lib

    def timeout(*a, **k):
        raise requests.Timeout()

    monkeypatch.setattr(audible, "_cache", {})
    monkeypatch.setattr(audible.requests, "get", timeout)
    r = client.get("/api/audible/search", params={"title": "題名"})
    assert r.status_code == 502 and "didn't answer in time" in r.json()["detail"]


# ── matching ─────────────────────────────────────────────────────────────────

def _epub(cfg, title="夜のピクニック", author="恩田陸"):
    book = {**_book(cfg, "b1"), "title": title, "author": author}
    del book["audiobook"]
    save_json(cfg.library_index, {"b1": book})


def test_a_guessed_file_becomes_the_audible_book_and_everything_follows_it(lib, aud):
    from epublib import audiobook_xref

    server, root, client = lib
    cfg = server.cfg
    af.m4b(root / "rip01.m4b", seconds=5)
    old = _scan(cfg)["rip01.m4b"]
    fid = load_json(cfg.audiobook_files_index, {})[old]["fid"]
    assert old == fid and _row(client, old)["source"] == "filename"
    # State kept under the surrogate id.
    progress = _progress(cfg, old)
    save_json(cfg.audiobook_metadata_overrides, {old: {"narrators": "読み手"}})
    cfg.alignments_dir.mkdir(parents=True, exist_ok=True)
    save_json(cfg.alignments_dir / "b1.json", {"schema": 2, "audiobook": old})
    _epub(cfg)
    audiobook_xref.xref(cfg)
    assert "b1" not in load_json(cfg.audiobook_links, {})        # a guess is never auto-linked

    r = client.post(f"/api/audiobooks/{old}/match",
                    json={"asin": f"https://www.audible.co.jp/pd/x/{ASIN}?ref=1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["ab_id"], body["old_ab_id"], body["renamed"], body["rename_error"]) == (ASIN, old, False, None)
    assert (body["source"], body["local"], body["values"]["title"]) == ("catalog", True, "夜のピクニック")
    assert aud.fetched == [(ASIN, "jp")]
    assert load_json(cfg.audiobook_assignments, {}) == {fid: ASIN}
    assert load_json(cfg.audiobook_catalog_extra, {})[ASIN]["title"] == "夜のピクニック"

    row = _row(client, ASIN)
    assert (row["local"], row["key_source"], row["catalog_key"], row["path"]) == (True, "assigned", ASIN, "rip01.m4b")
    assert row["audible_url"] == f"https://www.audible.co.jp/pd/{ASIN}"
    assert old not in {e["ab_id"] for e in client.get("/api/audiobooks").json()}
    # What was kept under the old id is under the new one.
    assert list(load_json(progress, {})) == [ASIN]
    assert load_json(cfg.alignments_dir / "b1.json", {})["audiobook"] == ASIN
    assert load_json(cfg.audiobook_metadata_overrides, {})[ASIN]["narrators"] == "読み手"
    assert load_json(cfg.audiobook_aliases, {}) == {old: ASIN}

    # It is a real book now: the EPUB links to it, and it can be audio-synced.
    link = load_json(cfg.audiobook_links, {})["b1"]
    assert (link["audible_isbn"], link["audiobook_local"]) == (ASIN, True)
    assert client.get("/api/books/b1/status").json()["alignment"]["eligible"] is True

    # A rescan keeps the match.
    assert _scan(cfg) == {"rip01.m4b": ASIN}
    st = client.get(f"/api/audiobooks/{ASIN}/match").json()
    assert (st["mode"], st["asin"], st["matchable"]) == ("assigned", ASIN, True)


def test_a_book_a_catalog_already_has_is_not_fetched_again(lib, aud):
    server, root, client = lib
    cfg = server.cfg
    save_json(cfg.audiobook_data, {"B0INCATLOG": {"title": "カタログ", "author": "誰か", "series": "S", "order": "02"}})
    af.m4b(root / "rip.m4b", seconds=5)
    old = _scan(cfg)["rip.m4b"]
    r = client.post(f"/api/audiobooks/{old}/match", json={"asin": "B0INCATLOG"})
    assert r.status_code == 200 and r.json()["values"]["title"] == "カタログ"
    assert aud.fetched == [] and not cfg.audiobook_catalog_extra.exists()
    assert _row(client, "B0INCATLOG")["local"] is True


def test_a_wrong_match_is_corrected_and_the_files_position_goes_with_it(lib, aud):
    server, root, client = lib
    cfg = server.cfg
    af.m4b(root / "rip.m4b", seconds=5)
    old = _scan(cfg)["rip.m4b"]
    assert client.post(f"/api/audiobooks/{old}/match", json={"asin": OTHER}).status_code == 200

    # Listening under the wrong book, and a note about *that* book.
    progress = _progress(cfg, OTHER, seconds=300.0)
    cfg.alignments_dir.mkdir(parents=True, exist_ok=True)
    save_json(cfg.alignments_dir / "b1.json", {"schema": 2, "audiobook": OTHER})
    save_json(cfg.audiobook_metadata_overrides, {OTHER: {"narrators": "別の人"}})
    save_json(cfg.audiobook_hidden, {"ab_ids": [OTHER]})

    r = client.post(f"/api/audiobooks/{OTHER}/match", json={"asin": ASIN})
    assert r.status_code == 200, r.text
    assert (r.json()["ab_id"], r.json()["old_ab_id"]) == (ASIN, OTHER)
    # The position and the alignment follow the file; what describes the other book stays with it.
    assert list(load_json(progress, {})) == [ASIN] and load_json(progress, {})[ASIN]["seconds"] == 300.0
    assert load_json(cfg.alignments_dir / "b1.json", {})["audiobook"] == ASIN
    assert set(load_json(cfg.audiobook_metadata_overrides, {})) == {OTHER}
    assert load_json(cfg.audiobook_hidden, {})["ab_ids"] == [OTHER]
    assert _row(client, ASIN)["local"] is True
    assert OTHER not in {e["ab_id"] for e in client.get("/api/audiobooks").json()}   # hidden, and not local
    assert load_json(cfg.audiobook_aliases, {})[OTHER] == ASIN


def test_taking_a_match_back_returns_the_file_to_what_it_was(lib, aud):
    server, root, client = lib
    cfg = server.cfg
    af.m4b(root / "rip.m4b", seconds=5)
    old = _scan(cfg)["rip.m4b"]
    client.post(f"/api/audiobooks/{old}/match", json={"asin": ASIN})
    progress = _progress(cfg, ASIN, seconds=77.0)

    r = client.delete(f"/api/audiobooks/{ASIN}/match")
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["ab_id"], body["old_ab_id"], body["source"]) == (old, ASIN, "filename")
    assert not load_json(cfg.audiobook_assignments, {})
    assert list(load_json(progress, {})) == [old] and load_json(progress, {})[old]["seconds"] == 77.0
    row = _row(client, ASIN)                     # the Audible book is still in the catalog, without a file
    assert row["local"] is False
    assert _row(client, old)["key_source"] is None
    assert client.delete(f"/api/audiobooks/{old}/match").status_code == 409


def test_a_file_with_an_asin_tag_goes_back_to_it_when_the_match_is_taken_back(lib, aud):
    server, root, client = lib
    cfg = server.cfg
    af.m4b(root / "tagged.m4b", seconds=5, title="タグの本", asin="B0TAGGED01")
    save_json(cfg.audiobook_data, {"B0TAGGED01": {"title": "タグの本"}})
    old = _scan(cfg)["tagged.m4b"]
    assert old == "B0TAGGED01"
    assert client.get(f"/api/audiobooks/{old}/match").json()["mode"] == "tag"
    assert client.post(f"/api/audiobooks/{old}/match", json={"asin": ASIN}).json()["ab_id"] == ASIN   # a hand match beats the tag
    assert client.delete(f"/api/audiobooks/{ASIN}/match").json()["ab_id"] == "B0TAGGED01"


# ── refusals ─────────────────────────────────────────────────────────────────

def _refused(client, ab_id, asin, status, reason):
    r = client.post(f"/api/audiobooks/{ab_id}/match", json={"asin": asin})
    assert (r.status_code, r.json().get("reason")) == (status, reason), r.text
    return r.json()["detail"]


def test_a_file_whose_name_has_the_key_must_be_renamed_instead(lib, aud):
    server, root, client = lib
    af.m4b(root / "named [B0NAMED001].m4b", seconds=5)
    ab_id = _scan(server.cfg)["named [B0NAMED001].m4b"]
    st = client.get(f"/api/audiobooks/{ab_id}/match").json()
    assert (st["mode"], st["matchable"], st["can_rename"]) == ("name", False, False)
    assert "[B0NAMED001]" in st["reason"]
    assert "Rename the file" in _refused(client, ab_id, ASIN, 409, "key_in_name")
    assert aud.fetched == [] and not server.cfg.audiobook_assignments.exists()


def test_the_same_book_twice_and_a_book_another_file_has_are_refused(lib, aud):
    server, root, client = lib
    cfg = server.cfg
    af.m4b(root / "a.m4b", seconds=5, title="A")
    af.m4b(root / "b.m4b", seconds=6, title="B")
    ids = _scan(cfg)
    assert client.post(f"/api/audiobooks/{ids['a.m4b']}/match", json={"asin": ASIN}).status_code == 200
    _refused(client, ASIN, ASIN, 409, "same")
    assert "a.m4b" in _refused(client, ids["b.m4b"], ASIN, 409, "taken")
    assert load_json(cfg.audiobook_assignments, {}).keys() == {load_json(cfg.audiobook_files_index, {})[ASIN]["fid"]}


def test_a_file_that_is_not_here_cannot_be_matched(lib, aud):
    server, root, client = lib
    save_json(server.cfg.audiobook_data, {"B0INCATLOG": {"title": "カタログ"}})
    audiobook_scanner.build_index(server.cfg)
    assert client.get("/api/audiobooks/B0INCATLOG/match").json()["matchable"] is False
    _refused(client, "B0INCATLOG", ASIN, 409, "not_local")
    assert client.get("/api/audiobooks/nope/match").status_code == 404
    _refused(client, "nope", ASIN, 404, "unknown_audiobook")
    assert client.delete("/api/audiobooks/nope/match").json()["reason"] == "unknown_audiobook"


@pytest.mark.parametrize("text", ["夜のピクニック", "", "B0SHORT"])
def test_something_that_is_not_an_asin_is_a_400(lib, aud, text):
    server, root, client = lib
    af.m4b(root / "a.m4b", seconds=5)
    ab_id = _scan(server.cfg)["a.m4b"]
    r = client.post(f"/api/audiobooks/{ab_id}/match", json={"asin": text})
    assert r.status_code == 400 and "ASIN" in r.json()["detail"]
    assert aud.fetched == []


def test_an_asin_audible_does_not_have_stores_nothing(lib, aud):
    server, root, client = lib
    cfg = server.cfg
    af.m4b(root / "a.m4b", seconds=5)
    ab_id = _scan(cfg)["a.m4b"]
    assert "no book with the ASIN B0NOSUCH01" in _refused(client, ab_id, "B0NOSUCH01", 404, "not_found")
    aud.products["B0SERIES01"] = product("B0SERIES01", runtime_length_min=None)
    assert "series page" in _refused(client, ab_id, "B0SERIES01", 409, "not_a_book")
    assert not cfg.audiobook_assignments.exists() and not cfg.audiobook_catalog_extra.exists()


def test_audible_failing_mid_match_changes_nothing(lib, aud):
    server, root, client = lib
    cfg = server.cfg
    af.m4b(root / "a.m4b", seconds=5)
    ab_id = _scan(cfg)["a.m4b"]
    aud.products[ASIN] = audible.AudibleError("Couldn't reach Audible. Check this computer's internet connection.")
    r = client.post(f"/api/audiobooks/{ab_id}/match", json={"asin": ASIN})
    assert r.status_code == 502 and "Couldn't reach Audible" in r.json()["detail"]
    assert not cfg.audiobook_assignments.exists() and not cfg.audiobook_catalog_extra.exists()
    assert _row(client, ab_id)["source"] == "filename"


# ── adding the key to the file name ──────────────────────────────────────────

def test_a_match_can_also_put_the_key_in_the_file_name(lib, aud):
    server, root, client = lib
    cfg = server.cfg
    af.m4b(root / "rip.m4b", seconds=5, title="タイトル")
    old = _scan(cfg)["rip.m4b"]
    assert client.get(f"/api/audiobooks/{old}/match").json()["can_rename"] is True
    progress = _progress(cfg, old)
    r = client.post(f"/api/audiobooks/{old}/match", json={"asin": ASIN, "rename": True})
    assert r.status_code == 200, r.text
    assert (r.json()["renamed"], r.json()["rename_error"], r.json()["ab_id"]) == (True, None, ASIN)
    assert not (root / "rip.m4b").exists() and (root / f"rip [{ASIN}].m4b").exists()
    row = _row(client, ASIN)
    assert (row["path"], row["key_source"], row["local"]) == (f"rip [{ASIN}].m4b", "name", True)
    assert list(load_json(progress, {})) == [ASIN]
    assert client.get(f"/api/audiobooks/{ASIN}/match").json()["mode"] == "name"
    assert (root / f"rip [{ASIN}].m4b").stat().st_size == load_json(cfg.audiobook_files_index, {})[ASIN]["filesize"]


def test_a_folder_of_mp3s_is_renamed_as_a_whole(lib, aud):
    server, root, client = lib
    cfg = server.cfg
    for i in (1, 2):
        af.mp3(root / "Book" / f"{i:02}.mp3", seconds=2.0 + i)
    old = _scan(cfg)["Book"]
    r = client.post(f"/api/audiobooks/{old}/match", json={"asin": ASIN, "rename": True})
    assert r.status_code == 200, r.text
    assert r.json()["renamed"] is True
    assert sorted(p.name for p in (root / f"Book [{ASIN}]").iterdir()) == ["01.mp3", "02.mp3"]
    row = _row(client, ASIN)
    assert (row["path"], row["files"], row["local"]) == (f"Book [{ASIN}]", ["01.mp3", "02.mp3"], True)


def test_a_name_already_taken_leaves_the_file_alone_but_keeps_the_match(lib, aud):
    server, root, client = lib
    af.m4b(root / "rip.m4b", seconds=5)
    old = _scan(server.cfg)["rip.m4b"]
    (root / f"rip [{ASIN}].m4b").mkdir()                       # in the way
    r = client.post(f"/api/audiobooks/{old}/match", json={"asin": ASIN, "rename": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["renamed"] is False and f"rip [{ASIN}].m4b" in body["rename_error"]
    assert (root / "rip.m4b").exists()
    assert _row(client, ASIN)["path"] == "rip.m4b"


def test_a_folder_holding_two_albums_cannot_be_renamed_as_one_book():
    entry = {"format": "tracks", "path": "Shared", "group": "Album A", "root": "/x"}
    assert "more than one album" in audiobook_match._rename_blocked(entry)
    assert "audiobook folder itself" in audiobook_match._rename_blocked(
        {"format": "mp3-folder", "path": ".", "group": None, "root": "/x"})
    assert audiobook_match._rename_blocked({"format": "m4b", "path": "a.m4b", "group": None, "root": "/x"}) is None


# ── the store setting ────────────────────────────────────────────────────────

def test_the_store_is_a_choice_and_changing_it_rewrites_the_audible_links(server):
    client = TestClient(server.app)
    cfg = server.cfg
    save_json(cfg.audiobook_data, {"B0AAAAAAAA": {"title": "本"}})
    audiobook_scanner.build_index(cfg)
    assert _row(client, "B0AAAAAAAA")["audible_url"] == "https://www.audible.co.jp/pd/B0AAAAAAAA"

    doc = client.get("/api/settings").json()
    field = next(f for f in doc["fields"] if f["key"] == "audible.marketplace")
    assert (field["kind"], field["value"], field["group"]) == ("choice", "jp", "metadata")
    assert field["choices"][0] == ["jp", "Japan"] and ["us", "United States"] in field["choices"]

    r = client.put("/api/settings", json={"values": {"audible.marketplace": "moon"}})
    assert r.status_code == 400 and "must be one of" in r.json()["detail"]
    r = client.put("/api/settings", json={"values": {"audible.marketplace": "us"}})
    assert r.status_code == 200 and r.json()["applied"] == ["audible.marketplace"]
    assert cfg.audible_marketplace == "us"
    end = time.time() + 10
    while _row(client, "B0AAAAAAAA")["audible_url"] != "https://www.audible.com/pd/B0AAAAAAAA":
        assert time.time() < end, "the links were not rewritten"
        time.sleep(0.05)
