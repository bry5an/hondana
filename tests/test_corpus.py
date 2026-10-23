"""Regression tests for corpus.frequent_unknown()'s per-book dedupe and
breadth-first ranking -- see docs/dev/05-gotchas.md's "One book's word
table holds several entries per orthBase" gotcha, whose prototype bug
reported a word appearing in 136 books out of 73.
"""
from __future__ import annotations

from epublib import analyze, corpus


def _write_book(cfg, book_id: str, defs: list, sents: list) -> None:
    cfg.sentences_dir.mkdir(parents=True, exist_ok=True)
    analyze.save_sentences(
        cfg.sentences_dir / f"{book_id}.json.gz",
        book_id,
        {"words": defs, "sents": sents, "spans": [[0, 1]] * len(sents)},
    )


def test_frequent_unknown_dedupes_multiple_defs_per_book(cfg):
    # A single book's word table holds two distinct defs for the same
    # orthBase (different lemma/POS -- the real shape the sentence index
    # uses). A naive per-def count would report this word as appearing in 2
    # books despite there being only 1 book scanned.
    defs = [
        ["ただ", "ただ", 0, "タダ", 10],
        ["ただ", "ただ2", 0, "タダ", 10],
    ]
    _write_book(cfg, "bookA", defs, sents=[[0], [1]])

    result = corpus.frequent_unknown(cfg)
    assert result["books_scanned"] == 1
    row = next(r for r in result["words"] if r["word"] == "ただ")
    assert row["books"] == 1
    assert row["total"] == 2  # both occurrences still counted


def test_frequent_unknown_ranks_by_breadth_before_total_count(cfg):
    # 'ただ' appears in 2 books (1 occurrence each); 'すぐ' appears 100 times
    # but confined to a single book. Breadth (books) outranks raw frequency.
    _write_book(cfg, "bookA", [["ただ", "ただ", 0, "タダ", 10]], sents=[[0]])
    _write_book(cfg, "bookB", [["ただ", "ただ", 0, "タダ", 10]], sents=[[0]])
    _write_book(cfg, "bookC", [["すぐ", "すぐ", 0, "スグ", 5]], sents=[[0]] * 100)

    result = corpus.frequent_unknown(cfg)
    assert result["books_scanned"] == 3
    assert result["words"][0]["word"] == "ただ"
    assert result["words"][0]["books"] == 2
    assert result["words"][1]["word"] == "すぐ"
    assert result["words"][1]["books"] == 1
    assert result["words"][1]["total"] == 100

    # The invariant from the gotchas doc: no word can be "used in" more books
    # than were actually scanned.
    assert max(r["books"] for r in result["words"]) <= result["books_scanned"]


def test_frequent_unknown_excludes_known_words(cfg):
    from epublib import known

    _write_book(cfg, "bookA", [["ただ", "ただ", 0, "タダ", 10]], sents=[[0]])
    known.save(cfg, ["ただ"], source="test", mode="replace")

    result = corpus.frequent_unknown(cfg)
    assert all(r["word"] != "ただ" for r in result["words"])


def test_frequent_unknown_excludes_free_words(cfg):
    # is_free words (proper nouns, numerals, bound affixes) never surface as
    # study candidates regardless of known state.
    _write_book(cfg, "bookA", [["太郎", "太郎", 1, "タロウ", -1]], sents=[[0]])

    result = corpus.frequent_unknown(cfg)
    assert result["words"] == []
