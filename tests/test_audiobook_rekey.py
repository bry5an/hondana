"""audiobook_rekey: what follows an audiobook when its id changes (a keyless file gains a
key, or a hand match is corrected). Synthetic files only."""
import json

from epublib import align, audiobook_rekey
from epublib.index_store import load_json, save_json

OLD, NEW, OTHER = "f0123456789abcd", "B0AUDIO001", "B0OTHER0002"


def _fix(ab_id, sentence, ts):
    return {"ts": ts, "ab_id": ab_id, "doc": 3, "sentence": sentence, "part": None, "ms": 5000,
            "op": "set", "by": "reader"}


def _seed(cfg):
    cfg.alignments_dir.mkdir(parents=True)
    lines = [json.dumps(_fix(OLD, 4, 1.0)), "not json", json.dumps(_fix(OTHER, 9, 2.0)),
             json.dumps(_fix(OLD, 5, 3.0))]
    (cfg.alignments_dir / "b1.anchors.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (cfg.alignments_dir / "b2.anchors.jsonl").write_text(json.dumps(_fix(OTHER, 1, 1.0)) + "\n",
                                                         encoding="utf-8")
    for slug in ("ann", "ben"):
        (cfg.users_dir / slug).mkdir(parents=True)
    save_json(cfg.users_dir / "ann" / "bookmarks.json", {
        "bm1": {"ab_id": OLD, "seconds": 12.0, "updated_at": "2026-10-01T00:00:00Z"},
        "bm2": {"deleted": True, "updated_at": "2026-10-02T00:00:00Z"},
        "bm3": {"ab_id": OTHER, "seconds": 3.0, "updated_at": "2026-10-03T00:00:00Z"},
        "bm4": {"ab_id": "", "cfi": "epubcfi(/6/2)", "updated_at": "2026-10-04T00:00:00Z"}})
    save_json(cfg.users_dir / "ben" / "bookmarks.json", {
        "bm5": {"ab_id": OLD, "seconds": 1.0, "updated_at": "2026-10-05T00:00:00Z"}})


def _bookmarks(cfg, slug):
    return load_json(cfg.users_dir / slug / "bookmarks.json", {})


def test_a_rekey_points_the_fixes_and_every_profiles_bookmarks_at_the_new_id(cfg):
    _seed(cfg)

    touched = audiobook_rekey.rekey_audiobook(cfg, OLD, NEW)

    assert {"alignments/b1.anchors.jsonl", "users/ann/bookmarks.json",
            "users/ben/bookmarks.json"} <= set(touched)
    assert "alignments/b2.anchors.jsonl" not in touched  # nothing there was made against the old id
    assert align.load_anchors(cfg, "b1", NEW) == {(3, 4): (None, 5000), (3, 5): (None, 5000)}
    assert align.load_anchors(cfg, "b1", OLD) == {}
    assert align.load_anchors(cfg, "b1", OTHER) == {(3, 9): (None, 5000)}  # another audiobook's fix stays
    assert "not json" in (cfg.alignments_dir / "b1.anchors.jsonl").read_text("utf-8").splitlines()
    ann = _bookmarks(cfg, "ann")
    assert ann["bm1"]["ab_id"] == NEW and ann["bm1"]["seconds"] == 12.0
    assert ann["bm3"]["ab_id"] == OTHER and ann["bm4"]["ab_id"] == ""
    assert ann["bm2"] == {"deleted": True, "updated_at": "2026-10-02T00:00:00Z"}
    assert _bookmarks(cfg, "ben")["bm5"]["ab_id"] == NEW


def test_a_corrected_match_moves_the_fixes_and_bookmarks_with_the_file(cfg):
    """`whole_book=False`: the fixes are about the audio, and so is a bookmark's place in it."""
    _seed(cfg)

    audiobook_rekey.rekey_audiobook(cfg, OLD, NEW, whole_book=False)

    assert align.load_anchors(cfg, "b1", NEW) == {(3, 4): (None, 5000), (3, 5): (None, 5000)}
    assert _bookmarks(cfg, "ann")["bm1"]["ab_id"] == NEW


def test_a_rekey_with_nothing_to_move_touches_only_the_alias(cfg):
    touched = audiobook_rekey.rekey_audiobook(cfg, OLD, NEW)
    assert touched == [cfg.audiobook_aliases.name]
