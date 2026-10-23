"""Regression tests for study.py's example-sentence selection (fewest
competing unknowns, then most context, then earliest -- the span-based
lookup that once shipped without a test) and the `stale` guard.

Uses a real minimal EPUB (via conftest.write_epub) so the example text is a
genuine slice of epub_text.extract_text() output at the analyzer's own
sentence spans, rather than hand-faked offsets -- the exact class of bug an earlier
version had: a mismatch there.
"""
from __future__ import annotations

from epublib import analyze, epub_text, known, study
from epublib.index_store import save_json

from conftest import write_epub

BOOK_ID = "book0000000001"

# defs: [orthBase, lemma, is_free, kana, difficulty]
_DEFS = [
    ["みかん", "みかん", 0, "ミカン", 70],  # 0: target word -- most frequent
    ["りんご", "りんご", 0, "リンゴ", 60],  # 1: secondary unknown word
    ["の", "の", 0, "ノ", 0],               # 2: known filler (marked known below)
    ["ねこ", "ねこ", 0, "ネコ", 50],        # 3: only ever appears alongside 4 unknowns
    ["いぬ", "いぬ", 0, "イヌ", 50],        # 4: same
]

_PARAGRAPHS = [
    "これはとても長い文章のテストです。",     # S_A: {0}, 5 tokens
    "これも長い文章のテスト用の例文です。",     # S_B: {0,1}, 5 tokens
    "ここにも十分に長い文章が入っています。",   # S_C: {0}, 10 tokens (best for word 0)
    "ここにも同じくらい長い文章があります。",   # S_D: {0}, 10 tokens (tie with C, later)
    "短い。",                                 # S_E: {0}, 3 tokens (< MIN_EXAMPLE_TOKENS)
    "これは四つの未知語を含む長い文章です。",   # S_F: {0,1,3,4}, 4 unknowns (> MAX)
]

_SENTS = [
    [0] + [2] * 4,
    [0, 1] + [2] * 3,
    [0] + [2] * 9,
    [0] + [2] * 9,
    [0] + [2] * 2,
    [0, 1, 3, 4] + [2],
]


def _setup(cfg, *, stale: bool = False):
    epub_path = cfg.epub_dir / "book.epub"
    write_epub(epub_path, _PARAGRAPHS)
    text = epub_text.extract_text(epub_path)
    spans = [list(s) for s in epub_text.split_sentences_with_spans(text)]
    assert len(spans) == len(_SENTS)  # one span per paragraph, as designed

    cfg.sentences_dir.mkdir(parents=True, exist_ok=True)
    analyze.save_sentences(
        cfg.sentences_dir / f"{BOOK_ID}.json.gz",
        BOOK_ID,
        {"words": _DEFS, "sents": _SENTS, "spans": spans},
    )
    save_json(cfg.wordlists_dir / f"{BOOK_ID}.json", {
        "book_id": BOOK_ID, "schema": analyze.SCHEMA_VERSION,
        "total": 20, "words": {"の": 10},
    })
    known.save(cfg, ["の"], source="test", mode="replace")

    save_json(cfg.library_index, {
        BOOK_ID: {"book_id": BOOK_ID, "path": "book.epub", "language": "ja",
                   "mtime": 1000, "filesize": 2000},
    })
    save_json(cfg.vocab_index, {
        BOOK_ID: {"mtime": 999 if stale else 1000, "filesize": 2000},
    })


def test_study_path_ranks_by_frequency(cfg):
    _setup(cfg)
    result = study.study_path(cfg, BOOK_ID, limit=100)
    assert result["status"] == "ok"
    assert result["stale"] is False
    assert [w["word"] for w in result["words"]] == ["みかん", "りんご", "ねこ", "いぬ"]
    assert [w["count"] for w in result["words"]] == [6, 2, 1, 1]


def test_study_path_example_prefers_fewest_unknowns_then_most_context_then_earliest(cfg):
    _setup(cfg)
    result = study.study_path(cfg, BOOK_ID, limit=100)
    by_word = {w["word"]: w for w in result["words"]}

    # S_C wins over S_A (same unknown count, more context), over S_D (same
    # unknown count and context, but S_C comes first), and over S_B (fewer
    # unknowns than S_B's 2).
    assert by_word["みかん"]["example"] == "ここにも十分に長い文章が入っています。"
    # りんご's only eligible sentence is S_B (S_F has too many unknowns).
    assert by_word["りんご"]["example"] == "これも長い文章のテスト用の例文です。"


def test_study_path_max_example_unknowns_gate_excludes_candidate(cfg):
    # ねこ/いぬ only ever co-occur in S_F, which has 4 unknown words --
    # over MAX_EXAMPLE_UNKNOWNS (3) -- so neither gets an example at all.
    _setup(cfg)
    result = study.study_path(cfg, BOOK_ID, limit=100)
    by_word = {w["word"]: w for w in result["words"]}
    assert by_word["ねこ"]["example"] is None
    assert by_word["いぬ"]["example"] is None


def test_study_path_curve_stops_at_ranked_length():
    assert study.CURVE_POINTS[0] == 0
    assert study.CURVE_POINTS[1] == 25


def test_study_path_curve_n0_matches_full_book_profile(cfg):
    _setup(cfg)
    result = study.study_path(cfg, BOOK_ID, limit=100)
    # Only 4 unknown word types exist, all below CURVE_POINTS[1] (25), so the
    # curve stops after the n=0 point.
    assert [pt["n"] for pt in result["curve"]] == [0]
    assert result["curve"][0]["hard_pct"] == 33.3  # S_B and S_F are hard (>=2 unknown)


def test_study_path_stale_suppresses_example_text(cfg):
    _setup(cfg, stale=True)
    result = study.study_path(cfg, BOOK_ID, limit=100)
    assert result["stale"] is True
    assert all(w["example"] is None for w in result["words"])


def test_study_path_no_known_words_short_circuits(cfg):
    _setup(cfg)
    known.clear(cfg)
    result = study.study_path(cfg, BOOK_ID, limit=100)
    assert result["status"] == "no_known_words"
    assert result["words"] == []


def test_study_path_leaves_out_words_set_aside_as_not_a_word(cfg):
    _setup(cfg)
    before = study.study_path(cfg, BOOK_ID, limit=100)["curve"][0]["hard_pct"]
    known.skip(cfg, ["みかん"])
    after = study.study_path(cfg, BOOK_ID, limit=100)
    assert [w["word"] for w in after["words"]] == ["りんご", "ねこ", "いぬ"]
    # みかん sits in 5 of 6 sentences; free, it can no longer make one hard.
    assert after["curve"][0]["hard_pct"] < before
