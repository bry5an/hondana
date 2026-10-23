"""Regression tests for analyze.py's tuned difficulty mapping, jpdb word-unit
merge rules, and the reading-alignment fixpoint.

Uses the real fugashi tagger on short literal sentences -- fast (unidic-lite)
and the only way to actually pin UniDic-dependent behavior instead of
asserting something made up. No EPUB needed.
"""
from __future__ import annotations

import json
from pathlib import Path

from epublib import analyze


# ── _difficulty(): the Z_EASY/Z_HARD -> 0-100 mapping ──────────────────────

def test_difficulty_boundaries_and_midpoint():
    assert analyze.Z_EASY == 6.0
    assert analyze.Z_HARD == 1.5
    assert analyze._difficulty(6.0) == 0
    assert analyze._difficulty(7.0) == 0  # anything >= Z_EASY floors at 0
    assert analyze._difficulty(1.5) == 100
    assert analyze._difficulty(0.0) == 100  # anything <= Z_HARD caps at 100
    # Exact midpoint between Z_HARD and Z_EASY -> exact midpoint difficulty.
    assert analyze._difficulty((6.0 + 1.5) / 2) == 50


# ── jpdb word-unit merge rules ───────────────────────────────────────────────

def test_predicate_bound_aux_is_merged_not_counted():
    # 食べた: た (助動詞) merges into 食べる's word unit -- doesn't add to
    # words_total. 太郎 (proper noun) and は (particle) both still count.
    stats, words, idx = analyze._analyze_text("太郎は食べた。")
    assert stats["words_total"] == 3  # 太郎, は, 食べる (not 4: た is merged)
    assert stats["content_tokens"] == 2  # 太郎, 食べる (は is a function word)
    assert dict(words) == {"太郎": 1, "食べる": 1}


def test_setai_josi_te_is_merged_not_counted():
    # 食べておいた: て (接続助詞) merges into 食べる, so it doesn't add to
    # words_total either. おいた's おく (UniDic tags it 動詞, not 助動詞) is
    # NOT merged -- a documented artifact (docs/dev/05-gotchas.md), not a
    # bug: it counts as its own content word.
    stats, words, idx = analyze._analyze_text("食べておいた。")
    assert stats["words_total"] == 2  # 食べる, おく (て and た both excluded)
    assert stats["content_tokens"] == 2
    assert dict(words) == {"食べる": 1, "おく": 1}


def test_nominalizing_no_is_merged_but_particle_no_is_not():
    # 食べるのが好きだ: の tagged 準体助詞 (nominalizing) merges -> excluded
    # from words_total. だ (助動詞) also merges.
    stats, _, _ = analyze._analyze_text("食べるのが好きだ。")
    assert stats["words_total"] == 3  # 食べる, が, 好き (の and だ excluded)
    assert stats["content_tokens"] == 2  # 食べる, 好き

    # 本の内容: の tagged plain 格助詞 here (possessive), NOT 準体助詞 -> it is
    # NOT merged, so it does add to words_total (same rule, different tag).
    stats2, _, _ = analyze._analyze_text("本の内容。")
    assert stats2["words_total"] == 3  # 本, の, 内容
    assert stats2["content_tokens"] == 2  # 本, 内容 (の is still a function word)


def test_proper_noun_is_free_and_excluded_from_difficulty():
    _, _, idx = analyze._analyze_text("太郎は食べた。")
    defs = idx["words"]
    by_base = {d[0]: d for d in defs}
    # [orthBase, lemma, is_free, kana, difficulty]
    assert by_base["太郎"][2] == 1  # is_free
    assert by_base["太郎"][4] == -1  # excluded from the difficulty distribution
    assert by_base["食べる"][2] == 0
    assert by_base["食べる"][4] != -1


# ── _count_readings(): fixpoint peeling of compounds ────────────────────────

def test_count_readings_single_kanji():
    assert analyze._count_readings({("本", "ホン")}) == 1


def test_count_readings_unresolvable_compound_without_anchors():
    # No single-kanji reading anywhere to anchor the peel -> unresolved, and
    # unresolved/ambiguous compounds are dropped rather than guessed at.
    assert analyze._count_readings({("薔薇", "バラ")}) == 0


def test_count_readings_gemination_variant_counts_as_its_own_reading():
    # 学(ガク) + 校(コウ) anchor 学校's ガッコウ reading; gemination shifts
    # 学's ガク -> ガッ, which counts as a distinct (kanji, reading) pair from
    # 学-ガク itself. Resolved: 学-ガク, 学-ガッ, 校-コウ = 3.
    pairs = {("学", "ガク"), ("校", "コウ"), ("学校", "ガッコウ")}
    assert analyze._count_readings(pairs) == 3


def test_count_readings_rendaku_variant_counts_as_its_own_reading():
    # 出(デ) + 口(クチ) anchor 出口's デグチ reading; rendaku voices 口's
    # クチ -> グチ, a distinct pair from 口-クチ. Resolved: 出-デ, 口-クチ,
    # 口-グチ = 3.
    pairs = {("出", "デ"), ("口", "クチ"), ("出口", "デグチ")}
    assert analyze._count_readings(pairs) == 3


# ── iter_tokens() / word_def(): the pieces the reader pack reuses ───────────

# The text and the index recorded from `_analyze_text` before it was split into
# iter_tokens/word_def, so the refactor can't move a word key.
_GOLDEN_TEXT = (
    "太郎は食べておいた。本の内容は面白い。\n"
    "ハリー・ポッターは賢者の石を持った。　猫が𠮟られる 3匹のりんごを買う。"
)


def test_analyze_text_matches_the_pre_refactor_index():
    golden = json.loads((Path(__file__).parent / "fixtures" / "analyze_golden.json").read_text("utf-8"))
    stats, words, idx = analyze._analyze_text(_GOLDEN_TEXT)
    assert json.loads(json.dumps({"stats": stats, "words": dict(words), "idx": idx})) == golden


def test_iter_tokens_offsets_round_trip():
    # Includes a full-width space (a token of its own), an ASCII space (MeCab
    # drops it) and a non-BMP character, which is one code point.
    seen = list(analyze.iter_tokens(_GOLDEN_TEXT))
    assert seen
    for start, end, tok in seen:
        assert _GOLDEN_TEXT[start:end] == tok.surface
    starts = [s for s, _, _ in seen]
    assert starts == sorted(starts)


def test_iter_tokens_offsets_survive_chunking(monkeypatch):
    monkeypatch.setattr(analyze, "MAX_CHUNK", 7)
    text = "太郎は食べておいた。本の内容は面白い。"
    seen = list(analyze.iter_tokens(text))
    assert seen
    for start, end, tok in seen:
        assert text[start:end] == tok.surface


def test_word_def_carries_the_sentence_index_fields():
    toks = {t.surface: t for _, _, t in analyze.iter_tokens("太郎は持った。")}
    taro = analyze.word_def(toks["太郎"])
    assert (taro.base, taro.free, taro.proper, taro.kind) == ("太郎", True, True, "content")
    # The dictionary-form reading, not the inflected one.
    motta = analyze.word_def(toks["持っ"])
    assert (motta.base, motta.reading, motta.kana) == ("持つ", "モツ", "モッ")
    assert motta.free is False


def test_word_def_marks_function_words_and_skips_punctuation():
    toks = {t.surface: t for _, _, t in analyze.iter_tokens("太郎は食べた。")}
    assert analyze.word_def(toks["は"]).kind == "function"
    assert analyze.word_def(toks["た"]).kind == "function"
    assert analyze.word_def(toks["。"]) is None
