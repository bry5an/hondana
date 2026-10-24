"""The Hondana iOS reader's state routes: GET /api/reader/vocab (the
profile's effective known set, with an ETag) and GET /api/reader/progress."""
import pytest
from fastapi.testclient import TestClient

from epublib import analyze, known, profiles
from epublib import reader as reader_mod
from epublib.index_store import save_json

A = "aaaa000000000001"


@pytest.fixture
def client(server):
    """A library of one book that uses "rare" EXPOSURE_MIN+ times."""
    cfg = server.cfg
    assert known.EXPOSURE_MIN <= 6
    save_json(cfg.library_index, {A: {"book_id": A, "title": "A", "language": "ja"}})
    save_json(cfg.wordlists_dir / f"{A}.json", {
        "book_id": A, "schema": analyze.SCHEMA_VERSION, "total": 7,
        "words": {"known": 1, "rare": 6}})
    return TestClient(server.app)


def _vocab(client):
    r = client.get("/api/reader/vocab")
    assert r.status_code == 200
    return r.json(), r.headers["etag"]


def test_the_snapshot_folds_not_words_into_orth_and_carries_exposure(server, client):
    cfg = server.cfg
    assert client.post("/api/known-words/mark", json={"words": ["猫", "走る"]}).status_code == 200
    assert client.post("/api/known-words/skip", json={"words": ["ハンス"]}).status_code == 200
    assert client.post(f"/api/books/{A}/read", json={"read": True}).status_code == 200

    body, _ = _vocab(client)
    studied, lemma = known.load_sets(cfg)
    assert set(body["orth"]) == studied and {"猫", "走る", "ハンス"} <= studied
    assert set(body["lemma"]) == lemma and "走る" in lemma
    assert body["not_words"] == ["ハンス"]
    assert body["exposure"] == known.exposure_counts(cfg) == {"rare": 1}
    assert body["exposure_min"] == known.EXPOSURE_MIN
    assert body["learning"] == {}


def test_learning_words_come_through_and_are_not_known(server, client):
    cfg = server.cfg
    entry = {"lemma": "犬", "reading": "いぬ", "book_id": A,
             "sentence": "犬が走る。", "added_at": "2026-10-16T09:00:00+00:00"}
    save_json(cfg.reader_learning, {"schema": 1, "words": {"犬": entry}})
    body, _ = _vocab(client)
    assert body["learning"] == {"犬": entry}
    assert "犬" not in body["orth"]


def test_the_etag_changes_after_a_mark_a_read_toggle_and_a_learning_write(server, client):
    cfg = server.cfg
    seen = [_vocab(client)[1]]

    client.post("/api/known-words/mark", json={"words": ["猫"]})
    seen.append(_vocab(client)[1])
    client.post("/api/known-words/skip", json={"words": ["ハンス"]})
    seen.append(_vocab(client)[1])
    client.post(f"/api/books/{A}/read", json={"read": True})
    seen.append(_vocab(client)[1])
    client.post(f"/api/books/{A}/read", json={"read": False})
    seen.append(_vocab(client)[1])
    save_json(cfg.reader_learning, {"schema": 1, "words": {"犬": {"lemma": "犬"}}})
    seen.append(_vocab(client)[1])

    assert len(set(seen)) == len(seen)
    assert all(tag.startswith('"') and tag.endswith('"') for tag in seen)


def test_a_matching_etag_gets_a_304_without_building_the_snapshot(server, client, monkeypatch):
    client.post("/api/known-words/mark", json={"words": ["猫"]})
    _, tag = _vocab(client)

    def boom(_cfg):
        raise AssertionError("snapshot built for a 304")

    monkeypatch.setattr(reader_mod, "snapshot", boom)
    for header in (tag, f"W/{tag}", f'"nope", {tag}', "*"):
        r = client.get("/api/reader/vocab", headers={"If-None-Match": header})
        assert r.status_code == 304 and r.headers["etag"] == tag and r.content == b""


def test_profiles_get_their_own_snapshots_and_tags(server, client):
    cfg = server.cfg
    ben = profiles.create(cfg, "Ben", "ja")["slug"]
    ben_client = TestClient(server.app, cookies={profiles.COOKIE_NAME: ben})

    client.post("/api/known-words/mark", json={"words": ["猫"]})
    ben_client.post("/api/known-words/mark", json={"words": ["犬"]})
    client.post(f"/api/books/{A}/read", json={"read": True})

    mine, my_tag = _vocab(client)
    theirs, their_tag = _vocab(ben_client)
    assert "猫" in mine["orth"] and "犬" not in mine["orth"]
    assert "犬" in theirs["orth"] and "猫" not in theirs["orth"]
    assert mine["exposure"] == {"rare": 1} and theirs["exposure"] == {}
    assert my_tag != their_tag
    # One profile's tag is not a hit for the other.
    r = ben_client.get("/api/reader/vocab", headers={"If-None-Match": my_tag})
    assert r.status_code == 200


def test_an_empty_profile_returns_empty_sets(server, client):
    ben = profiles.create(server.cfg, "Ben", "ja")["slug"]
    r = TestClient(server.app, cookies={profiles.COOKIE_NAME: ben}).get("/api/reader/vocab")
    assert r.status_code == 200
    assert r.json() == {
        "orth": [], "lemma": [], "not_words": [], "learning": {},
        "exposure": {}, "exposure_min": known.EXPOSURE_MIN,
    }
    assert r.headers["etag"]


def test_progress_returns_every_entry_for_the_profile_only(server, client):
    cfg = server.cfg
    assert client.get("/api/reader/progress").json() == {"progress": {}, "audio": {}}

    entries = {
        "asin:B000": {"book_id": A, "sha256": "ab" * 32, "cfi": "epubcfi(/6/4!/4/2)",
                      "fraction": 0.25, "updated_at": "2026-10-16T09:00:00+00:00"},
        "isbn:978": {"book_id": "bbbb000000000002", "sha256": "cd" * 32, "cfi": "epubcfi(/6/2)",
                     "fraction": 1.0, "updated_at": "2026-10-16T10:00:00+00:00"},
    }
    save_json(cfg.reader_progress, entries)
    r = client.get("/api/reader/progress")
    assert r.status_code == 200 and r.json() == {"progress": entries, "audio": {}}

    ben = profiles.create(cfg, "Ben", "ja")["slug"]
    ben_client = TestClient(server.app, cookies={profiles.COOKIE_NAME: ben})
    assert ben_client.get("/api/reader/progress").json() == {"progress": {}, "audio": {}}


# ── POST /api/reader/events ───────────────────────────────────────────

import json  # noqa: E402

from conftest import write_epub  # noqa: E402

from epublib import annotate  # noqa: E402
from epublib.index_store import load_json  # noqa: E402


def _status(n, orth, status, ts=1000.0, **kw):
    return {"id": f"s{n}", "ts": ts, "type": "status", "orth": orth, "lemma": orth,
            "reading": "", "status": status, "book_id": A, "sentence": f"{orth}を見る。", **kw}


def _post(client, events):
    r = client.post("/api/reader/events", json={"events": events})
    assert r.status_code == 200, r.text
    return r.json()


def _list(server):
    return load_json(server.cfg.known_words, {})


def test_ten_status_events_on_five_words_make_one_write_and_one_recompute(server, client, monkeypatch):
    writes, recomputes = [], []
    real_write, real_recompute = known._write, known.recompute_moved
    monkeypatch.setattr(known, "_write", lambda *a, **k: writes.append(1) or real_write(*a, **k))
    monkeypatch.setattr(known, "recompute_moved", lambda c: recomputes.append(1) or real_recompute(c))

    events = []
    for n, orth in enumerate(["猫", "犬", "鳥", "魚", "虫"]):
        events.append(_status(2 * n, orth, "learning", ts=100.0 + n))   # superseded below
        events.append(_status(2 * n + 1, orth, "known", ts=200.0 + n))
    res = _post(client, events)

    assert (len(writes), len(recomputes)) == (1, 1)
    assert res["status"] == "ok" and res["skipped"] == [] and res["rejected"] == []
    assert {"猫", "犬", "鳥", "魚", "虫"} <= set(_list(server)["marked"])
    # known beat learning: nothing is left in learning.json
    assert load_json(server.cfg.reader_learning, {}).get("words", {}) == {}


def test_a_batch_that_changes_no_known_state_writes_nothing_and_recomputes_nothing(server, client, monkeypatch):
    _post(client, [_status(1, "猫", "known")])
    monkeypatch.setattr(known, "_write", lambda *a, **k: pytest.fail("rewrote the list"))
    monkeypatch.setattr(known, "recompute_moved", lambda c: pytest.fail("recomputed"))
    res = _post(client, [_status(2, "猫", "known", ts=2000.0), _status(3, "犬", "learning")])
    assert res["books_moved"] == 0


def test_replaying_a_batch_is_a_no_op(server, client):
    cfg = server.cfg
    batch = [
        _status(1, "猫", "known"), _status(2, "犬", "learning"), _status(3, "ハンス", "ignored"),
        {"id": "l1", "ts": 1000.0, "type": "lookup", "orth": "犬", "lemma": "犬",
         "book_id": A, "sentence": "犬が走る。"},
        {"id": "p1", "ts": 1000.0, "type": "progress", "stable_key": "asin:B0", "book_id": A,
         "sha256": "ab" * 32, "cfi": "epubcfi(/6/4)", "fraction": 0.5},
        {"id": "a1", "ts": 1000.0, "type": "audio_progress", "ab_id": "ab1", "track": 2, "seconds": 61.5},
    ]
    first = _post(client, batch)
    files = {p: p.read_bytes() for p in (cfg.known_words, cfg.reader_learning,
                                         cfg.reader_lookups, cfg.reader_progress,
                                         cfg.reader_audio_progress)}
    vocab_before = _vocab(client)
    second = _post(client, batch)
    assert {p: p.read_bytes() for p in files} == files
    assert second["etag"] == first["etag"] == vocab_before[1]
    assert len(cfg.reader_lookups.read_text(encoding="utf-8").splitlines()) == 1
    assert _vocab(client) == vocab_before


def test_unknown_on_an_imported_word_is_skipped_and_the_list_is_left_alone(server, client):
    cfg = server.cfg
    known.save(cfg, ["先輩", "弁当"], source="deck.csv", mode="replace")
    client.post("/api/known-words/mark", json={"words": ["猫"]})
    before = _list(server)

    res = _post(client, [_status(1, "先輩", "unknown"), _status(2, "猫", "unknown"),
                         _status(3, "雨", "unknown"), _status(4, "弁当", "learning")])
    after = _list(server)
    # 先輩 and 弁当 came from the import; 猫 was a click and 雨 was never known.
    assert res["skipped"] == ["先輩", "弁当"]
    assert "先輩" in after["words"] and "弁当" in after["words"]
    assert "猫" not in after["words"] and "猫" not in after["marked"]
    assert after["words"] == [w for w in before["words"] if w != "猫"]   # nothing else moved
    # A learning request that could not be honoured does not make it a learning word.
    assert load_json(cfg.reader_learning, {}).get("words", {}) == {}


def test_a_word_known_by_lemma_from_an_import_is_skipped_too(server, client):
    known.save(server.cfg, ["食べる"], source="deck.csv", mode="replace")
    res = _post(client, [_status(1, "食べ", "unknown", lemma="食べる")])
    assert res["skipped"] == ["食べ"]


def test_learning_on_a_hand_marked_word_removes_the_mark_and_adds_the_entry(server, client):
    cfg = server.cfg
    client.post("/api/known-words/mark", json={"words": ["猫"]})
    res = _post(client, [_status(1, "猫", "learning", reading="ねこ", ts=1760000000.0)])
    assert res["skipped"] == [] and res["books_moved"] >= 0
    data = _list(server)
    assert "猫" not in data["words"] and "猫" not in data["marked"]
    entry = load_json(cfg.reader_learning)["words"]["猫"]
    assert entry == {"lemma": "猫", "reading": "ねこ", "book_id": A, "sentence": "猫を見る。",
                     "added_at": "2025-10-09T08:53:20+00:00"}
    body, _ = _vocab(client)
    assert "猫" not in body["orth"] and body["learning"]["猫"]["reading"] == "ねこ"

    # Learning → known leaves learning.json; ignored does too.
    _post(client, [_status(2, "猫", "known", ts=1760000100.0)])
    assert "猫" not in load_json(cfg.reader_learning)["words"]
    assert "猫" in _list(server)["marked"]


def test_a_status_is_exclusive_across_known_ignored_and_unknown(server, client):
    _post(client, [_status(1, "ハンス", "ignored")])
    assert _list(server)["not_words"] == ["ハンス"]
    _post(client, [_status(2, "ハンス", "known", ts=2000.0)])
    data = _list(server)
    assert data["not_words"] == [] and "ハンス" in data["marked"]
    _post(client, [_status(3, "ハンス", "ignored", ts=3000.0)])
    data = _list(server)
    assert data["not_words"] == ["ハンス"] and "ハンス" not in data["words"]
    _post(client, [_status(4, "ハンス", "unknown", ts=4000.0)])
    assert _list(server)["not_words"] == []


def test_the_newest_status_in_a_batch_wins_whatever_the_order(server, client):
    _post(client, [_status(1, "猫", "known", ts=500.0), _status(2, "猫", "unknown", ts=100.0)])
    assert "猫" in _list(server)["marked"]


def test_progress_keeps_the_newest_ts_when_events_arrive_out_of_order(server, client):
    cfg = server.cfg

    def progress(n, ts, cfi, key="asin:B0"):
        return {"id": f"p{n}", "ts": ts, "type": "progress", "stable_key": key,
                "book_id": A, "sha256": "ab" * 32, "cfi": cfi, "fraction": 0.1 * n}

    _post(client, [progress(3, "2026-10-16T12:00:00Z", "newest"), progress(1, 1760000000, "oldest")])
    assert load_json(cfg.reader_progress)["asin:B0"]["cfi"] == "newest"
    _post(client, [progress(2, "2026-10-16T11:00:00+00:00", "middle")])   # a later arrival, older ts
    stored = client.get("/api/reader/progress").json()["progress"]
    assert stored["asin:B0"]["cfi"] == "newest"
    assert stored["asin:B0"]["updated_at"] == "2026-10-16T12:00:00+00:00"
    _post(client, [progress(4, "2026-10-16T13:00:00Z", "later", key="isbn:1")])
    assert set(client.get("/api/reader/progress").json()["progress"]) == {"asin:B0", "isbn:1"}


def test_audio_progress_keeps_the_newest_ts_per_audiobook(server, client):
    def audio(n, ts, seconds, ab_id="ab1", track=0):
        return {"id": f"a{n}", "ts": ts, "type": "audio_progress", "ab_id": ab_id,
                "track": track, "seconds": seconds}

    _post(client, [audio(3, "2026-10-16T12:00:00Z", 300.0, track=1), audio(1, 1760000000, 10.0)])
    stored = client.get("/api/reader/progress").json()["audio"]
    assert stored == {"ab1": {"track": 1, "seconds": 300.0, "updated_at": "2026-10-16T12:00:00+00:00"}}
    _post(client, [audio(2, "2026-10-16T11:00:00+00:00", 5.0)])   # a later arrival, older ts
    assert client.get("/api/reader/progress").json()["audio"] == stored
    _post(client, [audio(4, "2026-10-16T13:00:00Z", 7.0, ab_id="ab2")])
    audio_now = client.get("/api/reader/progress").json()["audio"]
    assert set(audio_now) == {"ab1", "ab2"} and audio_now["ab1"] == stored["ab1"]
    # Reading positions are a separate store.
    assert client.get("/api/reader/progress").json()["progress"] == {}


def test_audio_progress_is_per_profile(server, client):
    _post(client, [{"id": "a1", "ts": 1000.0, "type": "audio_progress", "ab_id": "ab1", "seconds": 9.0}])
    ben = profiles.create(server.cfg, "Ben", "ja")["slug"]
    ben_client = TestClient(server.app, cookies={profiles.COOKIE_NAME: ben})
    assert ben_client.get("/api/reader/progress").json()["audio"] == {}
    assert client.get("/api/reader/progress").json()["audio"]["ab1"]["track"] == 0


def test_lookups_are_appended_once_per_id(server, client):
    lk = {"id": "l1", "ts": "2026-10-16T09:00:00Z", "type": "lookup", "orth": "犬", "lemma": "犬",
          "book_id": A, "sentence": "犬が走る。"}
    _post(client, [lk, {**lk, "id": "l2", "orth": "猫"}, lk])
    _post(client, [lk])
    rows = [json.loads(line) for line in server.cfg.reader_lookups.read_text(encoding="utf-8").splitlines()]
    assert [r["id"] for r in rows] == ["l1", "l2"]
    assert rows[0] == {"id": "l1", "ts": "2026-10-16T09:00:00+00:00", "orth": "犬", "lemma": "犬",
                       "book_id": A, "sentence": "犬が走る。"}


def test_bad_events_are_rejected_one_by_one_and_the_rest_apply(server, client):
    res = _post(client, [
        _status(1, "猫", "known"),
        {"id": "x1", "ts": 1.0, "type": "status", "orth": "犬", "status": "loved"},
        {"id": "x2", "ts": "not a time", "type": "lookup", "orth": "犬"},
        {"id": "x3", "ts": 1.0, "type": "teleport"},
        {"id": "x4", "ts": 1.0, "type": "progress", "stable_key": "k", "cfi": "c", "fraction": 7},
        {"ts": 1.0, "type": "status", "orth": "犬", "status": "known"},
        {"id": "x5", "ts": 1.0, "type": "audio_progress", "ab_id": "ab", "seconds": -1},
        {"id": "x6", "ts": 1.0, "type": "audio_progress", "ab_id": "", "seconds": 1},
    ])
    assert [r["id"] for r in res["rejected"]] == ["x1", "x2", "x3", "x4", None, "x5", "x6"]
    assert all(r["reason"] for r in res["rejected"])
    assert "猫" in _list(server)["marked"] and "犬" not in _list(server).get("words", [])


def test_the_batch_cap_and_the_empty_batch(server, client):
    assert _post(client, [])["skipped"] == []
    r = client.post("/api/reader/events", json={"events": [_status(i, "猫", "known") for i in range(1001)]})
    assert r.status_code == 400
    assert "猫" not in _list(server).get("words", [])
    r = client.post("/api/reader/events", json={"events": [_status(i, "猫", "known") for i in range(1000)]})
    assert r.status_code == 200


def test_events_land_in_the_requesting_profile_only(server, client):
    ben = profiles.create(server.cfg, "Ben", "ja")["slug"]
    ben_client = TestClient(server.app, cookies={profiles.COOKIE_NAME: ben})
    _post(ben_client, [_status(1, "犬", "learning")])
    assert "犬" in _vocab(ben_client)[0]["learning"]
    assert _vocab(client)[0]["learning"] == {}


def test_the_response_etag_is_the_vocab_etag_after_the_batch(server, client):
    res = _post(client, [_status(1, "猫", "known"), _status(2, "犬", "learning")])
    assert res["etag"] == _vocab(client)[1]
    again = client.get("/api/reader/vocab", headers={"If-None-Match": res["etag"]})
    assert again.status_code == 304


def test_a_mark_sent_from_a_packs_orth_makes_that_word_known_by_known_mask(server, client):
    """The real tagger, end to end: what the phone sends is the pack word's orth
    key, and the library's known_mask must then agree the word is known."""
    cfg = server.cfg
    write_epub(cfg.epub_dir / "b.epub", ["猫が居る。水が有る。雨かもしれない。パンを食べる。"] * 30)
    book = {"book_id": "b1", "language": "ja", "path": "b.epub", "sha256": "ab" * 32, "identifiers": {}}
    pack = annotate.build_pack(cfg, book)
    # A row is [orth, lemma, kana, kanaBase, ruby, free, kind, pos1, goshu].
    content = [w for w in pack["words"] if w[6] == "content"]
    orths = {w[0] for w in content}
    assert {"居る", "有る", "しれる", "食べる"} <= orths   # かもしれない is cut up by the tagger

    defs = [[w[0], w[1], w[5]] for w in content]
    studied, lemma = known.load_sets(cfg)
    assert not any(known.known_mask(defs, studied, lemma))

    _post(client, [_status(i, w[0], "known", lemma=w[1]) for i, w in enumerate(content)])
    studied, lemma = known.load_sets(cfg)
    assert all(known.known_mask(defs, studied, lemma))
    assert all(w[0] in _vocab(client)[0]["orth"] for w in content)

    # And back: unknown on a hand-marked word un-knows it again.
    _post(client, [_status(100 + i, w[0], "unknown", ts=2000.0, lemma=w[1]) for i, w in enumerate(content)])
    studied, lemma = known.load_sets(cfg)
    assert not any(known.known_mask(defs, studied, lemma))
