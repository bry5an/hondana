"""Regression tests for known.py's tuned reading-load constants and rules.

See CLAUDE.md's "Reading load (known stage)" section for the
constants under test: STRAIN_THRESHOLD, ACQUIRE_AFTER, EXPOSURE_MIN, and the
two-space orth/lemma matching design.
"""
from __future__ import annotations


from dataclasses import replace

from epublib import analyze, known
from epublib.index_store import load_json, save_json

from conftest import make_cfg


# ── walk() / profile(): flow / i+1 / hard percentages ───────────────────────

def test_profile_flow_i1_hard_percentages():
    # word 0 is known; 1/2/3 are not. STRAIN_THRESHOLD == 2.
    known_flags = [True, False, False, False]
    sents = [
        [0, 0],        # 0 unknown -> flow
        [0, 1],        # 1 unknown -> i+1
        [1, 2],        # 2 unknown -> hard
        [1, 2, 3],     # 3 unknown -> hard
    ]
    per_sentence, _, _ = known.walk(sents, known_flags)
    assert per_sentence == [0, 1, 2, 3]
    assert known.profile(sents, known_flags) == {
        "flow_pct": 25.0,
        "i1_pct": 25.0,
        "hard_pct": 50.0,
    }


def test_walk_acquire_after_stops_counting_lookups_past_the_5th():
    # A single unknown word type met 7 times in the same book. The first
    # ACQUIRE_AFTER (5) encounters are lookups; the 6th onward are free.
    known_flags = [False]
    sents = [[0]] * 7
    per_sentence, gaps, counts = known.walk(sents, known_flags)
    assert per_sentence == [1, 1, 1, 1, 1, 0, 0]
    assert gaps == [0, 0, 0, 0, 0]
    assert counts == [7]
    assert known.ACQUIRE_AFTER == 5


# ── known_mask(): the two-space orth/lemma match ────────────────────────────

def test_known_mask_two_space_lemma_match():
    # A deck imports "知れる" (orthBase=lemma="知れる"). A book spells the same
    # word "しれる" (orthBase="しれる", lemma="知れる") -- confirmed against the
    # real fugashi tagger. They share a lemma but not an orthBase; the match
    # must go through the lemma space, not the orth space.
    defs = [["しれる", "知れる", 0, "シレル", 50]]
    assert known.known_mask(defs, orth={"知れる"}, lemma={"知れる"}) == [True]
    assert known.known_mask(defs, orth=set(), lemma=set()) == [False]
    # orth-only match (no lemma needed) also works.
    assert known.known_mask(defs, orth={"しれる"}, lemma=set()) == [True]


def test_known_mask_free_words_always_known():
    # is_free=1 (proper nouns, numerals, bound affixes) short-circuits the
    # orth/lemma check entirely -- never a lookup regardless of known state.
    defs = [["太郎", "太郎", 1, "タロウ", -1]]
    assert known.known_mask(defs, orth=set(), lemma=set()) == [True]


# ── normalize(): the orth/lemma spaces an import is stored under ───────────

def test_normalize_single_token_stores_both_orth_and_lemma():
    orth, lemma = known.normalize(["知れる"], lang="ja")
    assert orth == ["知れる"]
    assert lemma == ["知れる"]


def test_normalize_multitoken_phrase_kept_verbatim_orth_only():
    # かも知れない has no single dictionary form (4 morphemes) -- normalize
    # skips the lemma step and keeps it verbatim in orth only.
    orth, lemma = known.normalize(["かも知れない"], lang="ja")
    assert orth == ["かも知れない"]
    assert lemma == []


# ── exposure_for(): a book's own contribution never counts for itself ──────

def test_exposure_for_excludes_books_own_contribution():
    # Only this book contributed (own used it >= EXPOSURE_MIN times, and it's
    # the sole book behind the tally) -> excluded from its own exposed set.
    assert known.exposure_for({"w": 1}, {"w": known.EXPOSURE_MIN}) == set()
    # A second book also contributed -> still exposed for this book.
    assert known.exposure_for({"w": 2}, {"w": known.EXPOSURE_MIN}) == {"w"}


def test_exposure_for_no_own_contribution_needs_only_one_book():
    assert known.exposure_for({"w": 1}, {"w": known.EXPOSURE_MIN - 1}) == {"w"}
    assert known.exposure_for({"w": 1}, {}) == {"w"}


def test_exposure_for_own_none_is_the_flat_set():
    assert known.exposure_for({"w": 1, "x": 3}, None) == {"w", "x"}


# ── save()/mark()/unmark(): replace vs merge, marked-word survival ─────────

def test_save_replace_keeps_marked_words(cfg):
    known.save(cfg, ["a", "b"], source="import1", mode="replace")
    known.mark(cfg, ["c"])
    out = known.save(cfg, ["x", "y"], source="import2", mode="replace")
    # "c" was hand-marked, not part of the import being replaced -> survives.
    assert set(out["words"]) == {"x", "y", "c"}
    assert out["marked"] == ["c"]


def test_save_merge_unions_words():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        cfg = make_cfg(Path(d))
        known.save(cfg, ["a", "b"], source="import1", mode="replace")
        out = known.save(cfg, ["b", "c"], source="import2", mode="merge")
        assert set(out["words"]) == {"a", "b", "c"}


def test_skip_is_free_not_known_and_survives_reimport(cfg):
    known.save(cfg, ["a"], source="import1", mode="replace")
    assert known.skip(cfg, ["ばあ", "ばあ", " "])["added"] == ["ばあ"]
    assert known.skip(cfg, ["ばあ"])["added"] == []
    orth, lemma = known.load_sets(cfg)
    assert "ばあ" in orth and "ばあ" not in lemma
    # Set aside is not "known": the list, its count and `marked` are untouched.
    data = load_json(cfg.known_words, {})
    assert data["words"] == ["a"] and data["count"] == 1 and data["marked"] == []
    assert known.status(cfg)["not_words"] == ["ばあ"]

    known.save(cfg, ["x"], source="import2", mode="replace")
    known.mark(cfg, ["y"])
    assert load_json(cfg.known_words, {})["not_words"] == ["ばあ"]

    assert known.unskip(cfg, ["ばあ", "nope"])["removed"] == ["ばあ"]
    assert "ばあ" not in known.load_sets(cfg)[0]


def test_skip_works_before_any_import_and_round_trips_a_snapshot(cfg):
    known.skip(cfg, ["ばあ"])
    assert known.load_sets(cfg) == ({"ばあ"}, set())
    known.save(cfg, ["a"], source="deck.csv", mode="replace")
    known.save(cfg, ["b"], source="deck2.csv", mode="replace")  # snapshots the first
    (snap,) = known.list_snapshots(cfg)
    known.unskip(cfg, ["ばあ"])
    known.restore(cfg, snap["id"])
    assert load_json(cfg.known_words, {})["not_words"] == ["ばあ"]


def test_unmark_only_removes_hand_marked_words(cfg):
    known.save(cfg, ["imported"], source="import1", mode="replace")
    known.mark(cfg, ["clicked"])
    result = known.unmark(cfg, ["imported", "clicked"])
    assert result["removed"] == ["clicked"]
    assert result["skipped"] == ["imported"]
    data = load_json(cfg.known_words, {})
    assert "imported" in data["words"]
    assert "clicked" not in data["words"]


# ── recompute(): end-to-end on one tiny synthetic book ─────────────────────

def _write_synthetic_book(cfg, book_id: str):
    """A 2-word-type, 4-sentence synthetic book: 'known' (id0, known) and
    'unknown' (id1, not known), each content words. Hand-computed:
    - wordlist: known x2, unknown x2 -> total=4
    - sentence walk: 2 sentences all-known (flow), 2 sentences with the
      single unknown (i+1 each, since STRAIN_THRESHOLD=2 and only 1 unknown
      word per sentence) -> flow_pct=50.0, i1_pct=50.0, hard_pct=0.0
    """
    save_json(cfg.wordlists_dir / f"{book_id}.json", {
        "book_id": book_id,
        "schema": analyze.SCHEMA_VERSION,
        "total": 4,
        "words": {"known": 2, "unknown": 2},
    })
    defs = [
        ["known", "known", 0, "", 10],
        ["unknown", "unknown", 0, "", 80],
    ]
    sents = [[0], [0], [1], [1]]
    spans = [[0, 1], [1, 2], [2, 3], [3, 4]]
    cfg.sentences_dir.mkdir(parents=True, exist_ok=True)
    analyze.save_sentences(
        cfg.sentences_dir / f"{book_id}.json.gz",
        book_id,
        {"words": defs, "sents": sents, "spans": spans},
    )


def test_recompute_end_to_end_synthetic_book(cfg):
    book_id = "deadbeef00000001"
    _write_synthetic_book(cfg, book_id)
    known.save(cfg, ["known"], source="test", mode="replace")

    stats = known.recompute(cfg)
    entry = stats[book_id]

    assert entry["known_pct"] == 50.0
    assert entry["known_unique"] == 1
    assert entry["flow_pct"] == 50.0
    assert entry["i1_pct"] == 50.0
    assert entry["hard_pct"] == 0.0
    # words_to_98: need = 0.98*4 - 2 known_tokens = 1.92, one more unknown
    # occurrence (count 2) covers it.
    assert entry["words_to_98"] == 1

    on_disk = load_json(cfg.known_stats, {})
    assert on_disk[book_id]["known_pct"] == 50.0


def test_recompute_moved_counts_only_books_whose_numbers_changed(cfg):
    # A mark on /known promises "moved on N books": N is books that changed, not
    # every book scored.
    _write_synthetic_book(cfg, "deadbeef00000001")
    _write_synthetic_book(cfg, "deadbeef00000002")
    known.save(cfg, ["known"], source="test", mode="replace")
    known.recompute(cfg)

    known.mark(cfg, ["unrelated"])           # a word neither book uses
    stats, moved = known.recompute_moved(cfg)
    assert len(stats) == 2 and moved == 0

    known.mark(cfg, ["unknown"])             # both books use it
    stats, moved = known.recompute_moved(cfg)
    assert moved == 2 and stats["deadbeef00000001"]["known_pct"] == 100.0


# ── recompute_all(): every profile, not just the active one ──────────

def test_recompute_all_covers_every_profile_and_filters_by_language(cfg):
    from epublib import profiles

    book_id = "deadbeef00000002"
    _write_synthetic_book(cfg, book_id)
    save_json(cfg.library_index, {book_id: {"language": "ja"}})
    for name, lang in [("Aki", "ja"), ("Ben", "ja"), ("Cat", "en")]:
        slug = profiles.create(cfg, name, lang)["slug"]
        # recompute() skips a profile with no known words at all.
        known.save(replace(cfg, active_user=slug), ["known"], source="t", mode="replace")
    cfg.active_user = "aki"

    out = known.recompute_all(cfg)
    assert set(out) == {"aki", "ben", "cat"}
    for slug in ("aki", "ben"):
        on_disk = load_json(cfg.users_dir / slug / "known_stats.json", {})
        assert book_id in on_disk  # a profile that wasn't active still got stats
    # The English profile only scores English books; this one is Japanese.
    assert load_json(cfg.users_dir / "cat" / "known_stats.json", {}) == {}

    only_en = known.recompute_all(cfg, {"en"})
    assert set(only_en) == {"cat"}


# ── snapshots ─────────────────────────────────────────────────────────

def test_replace_import_snapshots_the_old_list_and_restore_undoes_it(cfg):
    known.save(cfg, ["猫", "犬"], source="deck.csv")
    assert known.list_snapshots(cfg) == []  # nothing to lose yet
    known.mark(cfg, ["鳥"])
    known.save(cfg, ["本"], source="wrong.csv", mode="replace")
    [snap] = known.list_snapshots(cfg)
    assert (snap["reason"], snap["source"], snap["count"], snap["marked"]) == (
        "replace", "deck.csv", 3, 1)

    known.restore(cfg, snap["id"])
    data = load_json(cfg.known_words, {})
    assert set(data["words"]) == {"猫", "犬", "鳥"}
    assert data["marked"] == ["鳥"] and data["source"] == "deck.csv"
    assert data["orth"]  # derived keys rebuilt
    # the list the restore replaced was itself kept
    assert [s["reason"] for s in known.list_snapshots(cfg)] == ["restore", "replace"]


def test_clear_snapshots_including_marked_words(cfg):
    known.save(cfg, ["猫"], source="deck.csv")
    known.mark(cfg, ["鳥"])
    known.clear(cfg)
    assert load_json(cfg.known_words, {}) == {}
    [snap] = known.list_snapshots(cfg)
    assert snap["reason"] == "clear" and snap["marked"] == 1
    known.restore(cfg, snap["id"])
    assert load_json(cfg.known_words, {})["marked"] == ["鳥"]
    known.clear(cfg)
    known.clear(cfg)  # clearing an empty list adds no snapshot
    assert len(known.list_snapshots(cfg)) == 2  # the two real clears


def test_merge_and_seed_steps_do_not_snapshot(cfg):
    known.save(cfg, ["猫"], source="deck.csv")
    known.save(cfg, ["犬"], source="more.csv", mode="merge")
    known.save(cfg, ["a"], source="seed:top-1k-en", seeded_top_n=1000)
    assert known.list_snapshots(cfg) == []


def test_history_keeps_only_the_newest_snapshots(cfg):
    for i in range(known.SNAPSHOT_KEEP + 3):
        known.save(cfg, [f"w{i}"], source=f"s{i}")
    snaps = known.list_snapshots(cfg)
    assert len(snaps) == known.SNAPSHOT_KEEP
    assert snaps[0]["source"] == f"s{known.SNAPSHOT_KEEP + 1}"  # newest first


def test_restore_rejects_unknown_and_path_like_ids(cfg):
    known.save(cfg, ["猫"], source="deck.csv")
    for bad in ("nope", "../known_words", "20260101T000000000000Z-replace"):
        try:
            known.restore(cfg, bad)
        except KeyError:
            continue
        raise AssertionError(f"{bad!r} should be rejected")


# ── the /api/known-words endpoints ───────────────────────────────

def _known_client(server):
    from fastapi.testclient import TestClient

    return TestClient(server.app)


def test_import_snapshot_restore_round_trip_over_http(server):
    c = _known_client(server)
    r = c.post("/api/known-words", data={"text": "猫\n犬", "format": "text", "mode": "replace"})
    assert r.status_code == 200 and r.json()["total"] == 2
    assert c.get("/api/known-words/snapshots").json() == []  # nothing to lose yet

    assert c.post("/api/known-words", data={"text": "鳥", "format": "text"}).json()["total"] == 1
    (snap,) = c.get("/api/known-words/snapshots").json()
    assert snap["reason"] == "replace" and snap["count"] == 2

    r = c.post("/api/known-words/restore", json={"id": snap["id"]})
    assert r.status_code == 200 and r.json()["total"] == 2
    assert c.get("/api/known-words").json()["count"] == 2
    # The restore saved the list it replaced, so it can be undone too.
    assert [s["reason"] for s in c.get("/api/known-words/snapshots").json()] == ["restore", "replace"]


def test_import_result_names_the_snapshot_that_undoes_it(server):
    # /known's import banner offers Undo straight from the result.
    c = _known_client(server)
    first = c.post("/api/known-words", data={"text": "猫\n犬", "format": "text", "mode": "replace"}).json()
    assert first["snapshot"] is None  # replacing an empty list loses nothing

    merged = c.post("/api/known-words", data={"text": "鳥", "format": "text", "mode": "merge"}).json()
    assert merged["snapshot"] is None and merged["total"] == 3
    c.post("/api/known-words/mark", json={"words": ["本"]})

    replaced = c.post("/api/known-words", data={"text": "魚", "format": "text", "mode": "replace"}).json()
    assert replaced["snapshot"] and replaced["marked"] == 1
    assert replaced["total"] == 2  # 魚 plus the hand-marked 本

    r = c.post("/api/known-words/restore", json={"id": replaced["snapshot"]})
    assert r.status_code == 200 and c.get("/api/known-words").json()["count"] == 4


def test_restore_and_import_reject_bad_input(server):
    c = _known_client(server)
    assert c.post("/api/known-words/restore", json={"id": "nope"}).status_code == 404
    assert c.post("/api/known-words/restore", json={"id": "../known_words"}).status_code == 404
    assert c.post("/api/known-words", data={"text": "  "}).status_code == 400
    assert c.post("/api/known-words/mark", json={"words": []}).status_code == 400


def test_mark_and_unmark_over_http_only_remove_hand_marked_words(server):
    c = _known_client(server)
    c.post("/api/known-words", data={"text": "猫", "format": "text"})

    r = c.post("/api/known-words/mark", json={"words": ["鳥"]}).json()
    assert r["added"] == 1 and r["total"] == 2 and r["marked"] == 1
    assert c.post("/api/known-words/mark", json={"words": ["鳥"]}).json()["added"] == 0

    r = c.post("/api/known-words/unmark", json={"words": ["猫", "鳥"]}).json()
    assert r["unmarked"] == 1 and r["skipped"] == ["猫"] and r["total"] == 1


def test_skip_and_unskip_over_http(server):
    c = _known_client(server)
    c.post("/api/known-words", data={"text": "猫", "format": "text"})
    r = c.post("/api/known-words/skip", json={"words": ["ばあ"]}).json()
    assert r["added"] == 1 and r["not_words"] == 1
    assert c.post("/api/known-words/skip", json={"words": ["ばあ"]}).json()["added"] == 0
    assert c.get("/api/known-words").json()["not_words"] == ["ばあ"]
    r = c.post("/api/known-words/unskip", json={"words": ["ばあ"]}).json()
    assert r["removed"] == 1 and r["not_words"] == 0
    assert c.post("/api/known-words/skip", json={"words": []}).status_code == 400


def test_clear_over_http_snapshots_first(server):
    c = _known_client(server)
    c.post("/api/known-words", data={"text": "猫\n犬", "format": "text"})
    assert c.delete("/api/known-words").json() == {"status": "cleared"}
    assert c.get("/api/known-words").json()["count"] == 0
    (snap,) = c.get("/api/known-words/snapshots").json()
    assert snap["reason"] == "clear" and snap["count"] == 2
