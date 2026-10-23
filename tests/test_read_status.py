"""Marking a book read: the endpoint writes the requesting profile's
read_books.json, and a read book's vocabulary becomes "exposed" — known when
scoring every *other* book, never the book itself."""
import pytest
from fastapi.testclient import TestClient

from epublib import analyze, known, profiles
from epublib.index_store import load_json, save_json

# Book A uses "rare" EXPOSURE_MIN+ times; book B uses it twice, always in a
# sentence with another unknown word ("other"), so it's B's strain.
A, B = "aaaa000000000001", "bbbb000000000002"


def _book(cfg, bid, words: dict[str, int], sents: list[list[int]]):
    save_json(cfg.wordlists_dir / f"{bid}.json", {
        "book_id": bid, "schema": analyze.SCHEMA_VERSION,
        "total": sum(words.values()), "words": words})
    defs = [[w, w, 0, "", 50] for w in words]
    cfg.sentences_dir.mkdir(parents=True, exist_ok=True)
    analyze.save_sentences(cfg.sentences_dir / f"{bid}.json.gz", bid, {
        "words": defs, "sents": sents, "spans": [[i, i + 1] for i in range(len(sents))]})


@pytest.fixture
def library(cfg):
    assert known.EXPOSURE_MIN <= 6
    save_json(cfg.library_index, {A: {"language": "ja"}, B: {"language": "ja"}})
    _book(cfg, A, {"known": 1, "rare": 6}, [[0], [1], [1], [1], [1], [1], [1]])
    _book(cfg, B, {"known": 2, "rare": 2, "other": 2}, [[0], [0], [1, 2], [1, 2]])
    known.save(cfg, ["known"], source="test", mode="replace")
    return cfg


def test_a_read_book_lowers_strain_in_other_books_but_not_its_own(library):
    cfg = library
    before = known.recompute(cfg)
    assert before[B]["hard_pct"] == 50.0 and before[B]["known_pct"] == pytest.approx(33.3)

    known.mark_read(cfg, A)
    after = known.recompute(cfg)
    # In B, "rare" is now known: each of its sentences has one unknown, not two.
    assert after[B]["hard_pct"] == 0.0 and after[B]["i1_pct"] == 50.0
    assert after[B]["known_pct"] == pytest.approx(66.7)
    assert after[B]["exposed_unique"] == 1
    # A is scored without its own contribution, so finishing it changes nothing.
    assert after[A]["known_pct"] == before[A]["known_pct"]
    assert after[A]["exposed_unique"] == 0 and after[A]["read"] is True
    assert known.summary(cfg)["exposed"] == 1
    assert known.summary(cfg)["exposure_min"] == known.EXPOSURE_MIN

    known.mark_read(cfg, A, read=False)
    assert known.recompute(cfg)[B]["hard_pct"] == 50.0


def test_a_word_below_the_exposure_threshold_is_not_exposed(library):
    cfg = library
    known.mark_read(cfg, B)  # B uses "rare" only twice
    assert known.exposure_counts(cfg) == {}
    assert known.recompute(cfg)[A]["exposed_unique"] == 0


# ── the endpoint ─────────────────────────────────────────────────────────────

@pytest.fixture
def client(server):
    cfg = server.cfg
    save_json(cfg.library_index, {A: {"book_id": A, "title": "A", "language": "ja"},
                                  B: {"book_id": B, "title": "B", "language": "ja"}})
    _book(cfg, A, {"known": 1, "rare": 6}, [[0], [1], [1], [1], [1], [1], [1]])
    _book(cfg, B, {"known": 2, "rare": 2, "other": 2}, [[0], [0], [1, 2], [1, 2]])
    return TestClient(server.app)


def _read_ids(cfg, slug):
    return set(load_json(cfg.users_dir / slug / "read_books.json", {}).get("books", {}))


def test_read_endpoint_marks_and_unmarks_for_the_requesting_profile(server, client):
    cfg = server.cfg
    ben = profiles.create(cfg, "Ben", "ja")["slug"]
    default = profiles.resolve_slug(cfg, None)
    assert default != ben

    r = client.post(f"/api/books/{A}/read", json={"read": True})
    assert r.status_code == 200
    assert r.json()["read"] is True and r.json()["exposed"] == 1
    assert _read_ids(cfg, default) == {A}
    assert _read_ids(cfg, ben) == set()  # another profile's list is untouched
    stats = load_json(cfg.users_dir / default / "known_stats.json", {})
    assert stats[A]["read"] is True

    ben_client = TestClient(server.app, cookies={profiles.COOKIE_NAME: ben})
    assert ben_client.post(f"/api/books/{B}/read", json={"read": True}).status_code == 200
    assert _read_ids(cfg, ben) == {B} and _read_ids(cfg, default) == {A}

    r = client.post(f"/api/books/{A}/read", json={"read": False})
    assert r.status_code == 200 and r.json()["exposed"] == 0
    assert _read_ids(cfg, default) == set()


def test_read_endpoint_rejects_an_unknown_book(server, client):
    assert client.post("/api/books/nope/read", json={"read": True}).status_code == 404
    assert _read_ids(server.cfg, profiles.resolve_slug(server.cfg, None)) == set()


# ── books read counts only books still in the library ─────────────────

GONE = "cccc000000000003"


def test_books_read_counts_only_books_in_the_library(library):
    cfg = library
    for bid in (A, GONE):
        known.mark_read(cfg, bid)
    live, gone = known.read_split(cfg)
    assert [e["book_id"] for e in live] == [A] and [e["book_id"] for e in gone] == [GONE]
    s = known.summary(cfg)
    assert s["books_read"] == 1 and s["orphan_reads"] == 1


def test_an_empty_library_index_is_not_read_as_every_book_gone(library):
    cfg = library
    known.mark_read(cfg, GONE)
    live, gone = known.read_split(cfg, {})
    assert len(live) == 1 and gone == []


def test_clear_orphan_reads_drops_only_the_marks_for_missing_books(library):
    cfg = library
    known.mark_read(cfg, A)
    known.mark_read(cfg, GONE)
    assert known.clear_orphan_reads(cfg) == [GONE]
    assert set(load_json(cfg.read_books, {})["books"]) == {A}
    assert known.clear_orphan_reads(cfg) == []


def test_profile_endpoints_agree_and_clearing_leaves_the_list_alone(server, client):
    cfg = server.cfg
    client.post(f"/api/books/{A}/read", json={"read": True})
    known.mark_read(cfg, GONE)

    r = client.get("/api/profile/read-books").json()
    assert [b["book_id"] for b in r["books"]] == [A]
    assert [o["book_id"] for o in r["orphans"]] == [GONE]
    status = client.get("/api/known-words").json()
    assert status["books_read"] == len(r["books"]) == 1 and status["orphan_reads"] == 1

    cleared = client.delete("/api/profile/orphan-reads").json()
    assert cleared["cleared"] == 1 and cleared["books_read"] == 1 and cleared["orphan_reads"] == 0
    assert client.get("/api/profile/read-books").json()["orphans"] == []
    assert client.delete("/api/profile/orphan-reads").json()["cleared"] == 0


# ── changing the day a book was finished ──────────────────────────────

def test_set_read_date_stores_noon_utc_and_reorders_the_list(library):
    from datetime import date
    cfg = library
    known.mark_read(cfg, A)
    known.mark_read(cfg, B)
    stored = known.set_read_date(cfg, A, date(2020, 3, 1))
    assert stored == "2020-03-01T12:00:00+00:00"
    entry = load_json(cfg.read_books, {})["books"][A]
    assert entry == {"read": True, "read_at": stored}
    assert [e["book_id"] for e in known.read_entries(cfg)] == [B, A]  # 2020 sorts after "now"


def test_set_read_date_never_creates_a_mark(library):
    from datetime import date
    cfg = library
    with pytest.raises(KeyError):
        known.set_read_date(cfg, A, date(2020, 3, 1))
    assert load_json(cfg.read_books, {}).get("books", {}) == {}


def test_read_date_endpoint(server, client):
    cfg = server.cfg
    slug = profiles.resolve_slug(cfg, None)
    url = f"/api/books/{A}/read-date"
    assert client.patch(url, json={"read_at": "2020-03-01"}).status_code == 409  # not read yet
    client.post(f"/api/books/{A}/read", json={"read": True})

    r = client.patch(url, json={"read_at": "2020-03-01"})
    assert r.status_code == 200 and r.json()["read_at"] == "2020-03-01T12:00:00+00:00"
    books = load_json(cfg.users_dir / slug / "read_books.json", {})["books"]
    assert books[A]["read_at"] == "2020-03-01T12:00:00+00:00" and books[A]["read"] is True

    assert client.patch("/api/books/nope/read-date", json={"read_at": "2020-03-01"}).status_code == 404
    assert client.patch(url, json={"read_at": "2999-01-01"}).status_code == 400
    assert client.patch(url, json={"read_at": "yesterday"}).status_code == 422
    assert load_json(cfg.users_dir / slug / "read_books.json", {})["books"][A]["read_at"].startswith("2020-03-01")
