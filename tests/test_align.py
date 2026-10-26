"""Audio↔text sync (align.py): SRT parsing, the cue→sentence matcher, storage,
and the routes. Synthetic EPUBs and SRTs; no tokenizer and no audio."""
from __future__ import annotations

import json
import random

import pytest
from fastapi.testclient import TestClient

from epublib import align, annotate, orphans, purge, rekey
from epublib.index_store import save_json
from test_annotate import write_book

_POOL = ("春夏秋冬朝昼夜雨雪風山川海空星月花鳥犬猫道町駅店家庭窓扉鍵箱本紙筆声歌音色光影水火土木金"
         "あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもやゆよらりるれろわ")
_RNG = random.Random(361)
# Forty distinct sentences (no two share a long run, as in a real book).
SENTENCES = ["".join(_RNG.choice(_POOL) for _ in range(_RNG.randint(14, 24))) + "。" for _ in range(40)]
# A kanji-and-ruby sentence: dom_text drops the reading, the cue never had it.
RUBY = "<p>漢<ruby>字<rt>じ</rt></ruby>を読む少女は微笑んだ。</p>"


def _cue(i: int, start: int, end: int, text: str) -> str:
    def ts(ms):
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
    return f"{i}\n{ts(start)} --> {ts(end)}\n{text}\n"


def srt_for(texts: list[str], step: int = 4000) -> str:
    """One 4 s cue per text, back to back."""
    return "\n".join(_cue(i + 1, i * step, (i + 1) * step, t) for i, t in enumerate(texts))


def _doc(body: str) -> str:
    return ('<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml">'
            f"<head><title>t</title></head><body>{body}</body></html>")


def _book(cfg, book_id="b1", *, docs=None, ab="B0AUDIO001") -> dict:
    docs = docs or [_doc("".join(f"<p>{s}</p>" for s in SENTENCES))]
    write_book(cfg.epub_dir / f"{book_id}.epub", docs)
    return {"book_id": book_id, "title": "T", "language": "ja", "path": f"{book_id}.epub",
            "sha256": "cd" * 32, "identifiers": {"asin": "B0TEST"},
            "audiobook": {"audible_isbn": ab, "audiobook_local": True}}


# ── SRT ──────────────────────────────────────────────────────────────────────

def test_parse_srt_is_lenient():
    raw = ("﻿1\r\n00:00:01,500 --> 00:00:03,000\r\n一行目\r\n二行目\r\n\r\n"
           "00:01:00.5 --> 00:01:02.25\r\n番号なし\r\n\r\n"
           "3\r\n00:02:00,000 --> 00:02:01,000\r\n\r\n")
    cues = align.parse_srt(raw)
    assert [(c.start, c.end, c.text) for c in cues] == [
        (1500, 3000, "一行目 二行目"), (60500, 62250, "番号なし")]


def test_norm_keeps_only_the_base_text():
    assert align.norm("「カタカナ」、ｶﾀｶﾅ！ 1 2") == "かたかなかたかな12"
    assert align.cue_norm("漢字（かんじ）を読む") == "漢字を読む"


# ── matching ─────────────────────────────────────────────────────────────────

def test_every_sentence_gets_its_own_cue_time():
    rows = align.match_cues(SENTENCES, align.parse_srt(srt_for(SENTENCES)))
    assert all(rows)
    assert rows[0] == [0, 4000] and rows[1] == [4000, 8000] and rows[39] == [156000, 160000]


def test_a_skipped_cue_leaves_a_null_and_the_rest_still_line_up():
    texts = SENTENCES[:10] + SENTENCES[11:]
    rows = align.match_cues(SENTENCES, align.parse_srt(srt_for(texts)))
    assert rows[10] is None
    assert sum(r is None for r in rows) == 1
    assert rows[9] == [36000, 40000] and rows[11] == [40000, 44000]


def test_a_cue_split_across_two_sentences_divides_its_time():
    a, b = SENTENCES[4], SENTENCES[5]
    texts = SENTENCES[:4] + [a + b] + SENTENCES[6:]
    rows = align.match_cues(SENTENCES, align.parse_srt(srt_for(texts)))
    r4, r5 = rows[4], rows[5]
    assert r4[0] == 16000 and r5[1] == 20000
    assert r4[1] == pytest.approx(r5[0], abs=50) and 16000 < r4[1] < 20000
    share = len(a) / (len(a) + len(b))  # time is linear in the text across a cue
    assert (r4[1] - r4[0]) / 4000 == pytest.approx(share, abs=0.03)


def test_a_sentence_cut_over_two_cues_runs_from_the_first_to_the_last():
    s = SENTENCES[7]
    half = len(s) // 2
    texts = SENTENCES[:7] + [s[:half], s[half:]] + SENTENCES[8:]
    cues = align.parse_srt(srt_for(texts))
    rows = align.match_cues(SENTENCES, cues)
    assert rows[7] == [28000, 36000]
    assert rows[8][0] == 36000


def test_recognition_noise_and_script_differences_still_match():
    noisy = [s.replace("本", "ほん").replace("。", "") for s in SENTENCES]
    noisy[3] = noisy[3][:6] + "ー" + noisy[3][8:]  # one wrong character
    noisy[20] = noisy[20].replace("は", "ハ")      # katakana for hiragana
    rows = align.match_cues(SENTENCES, align.parse_srt(srt_for(noisy)))
    assert all(rows)


def test_a_jump_ahead_is_found_for_a_long_cue():
    """Front matter nobody narrates: the first cue is deep in the book."""
    rows = align.match_cues(SENTENCES, align.parse_srt(srt_for(SENTENCES[25:])))
    assert all(r is None for r in rows[:25]) and all(rows[25:])
    assert rows[25] == [0, 4000]


def test_a_common_line_far_ahead_does_not_drag_the_cursor_past_the_book():
    """A short line the book repeats later ("よろしくね") must not skip what lies
    between: a far hit is only believed when the next cue follows it."""
    refrain = "よろしくね。"
    book = SENTENCES[:25] + [refrain] + SENTENCES[25:]
    # The narrator says it once more, early, and that cue also fits the copy 21 sentences on.
    cues = SENTENCES[:4] + [refrain] + SENTENCES[4:25] + [refrain] + SENTENCES[25:]
    rows = align.match_cues(book, align.parse_srt(srt_for(cues)))
    assert all(rows[:25]) and all(rows[26:])


def test_cues_with_parts_name_the_part_and_do_not_span_the_join():
    one = align.parse_srt(srt_for(SENTENCES[:20]), "a.mp3")
    two = align.parse_srt(srt_for(SENTENCES[20:]), "b.mp3")
    rows = align.match_cues(SENTENCES, one + two)
    assert rows[0] == [0, 4000, "a.mp3"] and rows[20] == [0, 4000, "b.mp3"]
    assert rows[19][2] == "a.mp3"


# ── squeezed cues ───────────────────────────────────────────

def _paced(sizes: list[int], per_second: float = 7.0, pause: int = 600, part: str | None = None) -> list[list]:
    """Rows for sentences read at a steady pace, back to back with a short pause."""
    rows, at = [], 0
    for n in sizes:
        end = at + round(1000 * n / per_second)
        rows.append([at, end] + ([part] if part else []))
        at = end + pause
    return rows


def _squeeze(rows: list[list], first: int, last: int, keep: float = 0.25) -> list[list]:
    """Packs `rows[first:last+1]` into the last `keep` of the time they took, as a transcript that
    lost the audio writes them: a hole, then the run, ending where the next sentence begins."""
    run = rows[first:last + 1]
    start, end = run[0][0], run[-1][1]
    begin = end - round((end - start) * keep)
    squeezed = [[begin + round((end - begin) * (r[0] - start) / (end - start)),
                 begin + round((end - begin) * (r[1] - start) / (end - start))] + r[2:] for r in run]
    return rows[:first] + squeezed + rows[last + 1:]


def test_a_squeezed_run_after_a_hole_is_spread_over_the_time_it_took():
    sizes = [20] * 60
    good = _paced(sizes)
    rows = _squeeze(good, 20, 40)
    before = [list(r) for r in rows]
    out = align.retime(rows, sizes)
    run = out[20:41]
    assert all(a[1] <= b[0] + 1 for a, b in zip(run, run[1:]))           # in order, no overlap
    assert run[0][0] < before[20][0]                                       # it starts in the hole
    assert out[:19] == before[:19] and out[41:] == before[41:]            # nothing else moves
    assert abs(out[19][1] - good[19][1]) < 1500                            # the sentence before only trades its tail
    pace = sum(sizes[20:41]) / ((run[-1][1] - run[0][0]) / 1000)
    assert 5 < pace < 9                                                    # a human pace again
    assert run[-1][1] == before[40][1]
    for got, want in zip(run, good[20:41]):                                # and close to the truth
        assert abs(got[0] - want[0]) < 4000


def test_a_run_is_read_back_from_its_end_when_the_hole_is_longer_than_it_needs():
    sizes = [20] * 60
    good = _paced(sizes)
    good[20:] = [[a + 100_000, b + 100_000] for a, b in good[20:]]       # a minute and a half of music
    rows = _squeeze(good, 20, 40)
    before = [list(r) for r in rows]
    out = align.retime(rows, sizes)
    assert out[19] == before[19]                                           # the sentence before the hole stays
    assert out[20][0] > before[19][1] + 60_000                             # the music is left silent
    assert out[20][0] < before[20][0]
    assert abs(out[20][0] - good[20][0]) < 4000
    assert out[40][1] == before[40][1]


def test_ordinary_pace_and_real_silence_are_left_alone():
    sizes = [20] * 60
    rows = _paced(sizes)
    rows[30:] = [[a + 45_000, b + 45_000] for a, b in rows[30:]]           # a minute of music
    before = [list(r) for r in rows]
    assert align.retime(rows, sizes) == before


def test_a_few_short_fast_lines_are_not_a_squeezed_run():
    sizes = [20] * 60
    rows = _paced(sizes)
    for k in (10, 11, 30):                                                 # "そう。" read fast
        rows[k][1] = rows[k][0] + 700
    before = [list(r) for r in rows]
    assert align.retime(rows, sizes) == before


def test_each_part_is_judged_alone_and_keeps_its_name():
    sizes = [20] * 80
    a, b = _paced(sizes[:40], part="a.mp3"), _paced(sizes[40:], part="b.mp3")
    b = _squeeze(b, 10, 25)
    rows = a + b
    before = [list(r) for r in rows]
    out = align.retime(rows, sizes)
    assert out[:40] == before[:40]
    assert out[50][0] < before[50][0]
    assert all(r[2] == "b.mp3" for r in out[40:])


def test_a_book_with_few_cues_is_not_judged():
    sizes = [20] * 12
    rows = _squeeze(_paced(sizes), 4, 9)
    before = [list(r) for r in rows]
    assert align.retime(rows, sizes) == before


def test_unmatched_sentences_keep_their_place_through_retime():
    sizes = [20] * 60
    rows: list = _squeeze(_paced(sizes), 20, 40)
    rows[25] = None
    out = align.retime(rows, sizes)
    assert out[25] is None and len(out) == 60


def test_the_rows_retime_moved_are_listed_as_estimated():
    squeezed = _squeeze(_paced([len(align.norm(s)) for s in SENTENCES]), 15, 30)
    cues = [align.Cue(r[0], r[1], s) for r, s in zip(squeezed, SENTENCES)]
    rows, est = align.match_synced(SENTENCES, cues)
    assert est and est <= set(range(10, 31))
    assert all(rows[i] == squeezed[i] for i in range(40) if i not in est)


# ── plain transcripts ─────────────────────────────────────────────────

def _heard(texts: list[str]) -> list[str]:
    """What Whisper writes for these lines: no 。, and two of them to a cue."""
    plain = [t.replace("。", "") for t in texts]
    return [plain[i] + (plain[i + 1] if i + 1 < len(plain) else "") for i in range(0, len(plain), 2)]


def test_a_transcript_grouped_its_own_way_times_every_sentence():
    rows, est = align.match_transcript(SENTENCES, align.parse_srt(srt_for(_heard(SENTENCES), step=8000)))
    assert all(rows) and not est
    assert rows[0][0] == 0 and rows[1][1] == 8000 and rows[39][1] == 160000
    assert all(a[1] <= b[0] + 1 for a, b in zip(rows, rows[1:]))


def test_one_line_placed_wrongly_does_not_drag_the_lines_after_it():
    """SubPlz sync's failure: a cursor that follows one wrong place
    loses every line after it. The book says sentence 5 again at 30; a cue that fits
    both copies must not take the far one and pull its neighbours along."""
    book = SENTENCES[:30] + [SENTENCES[5]] + SENTENCES[30:]
    rows, _ = align.match_transcript(book, align.parse_srt(srt_for(book)))
    assert all(rows)
    assert rows[5] == [20000, 24000] and rows[30] == [120000, 124000] and rows[31] == [124000, 128000]


def test_a_stretch_whisper_dropped_is_estimated_between_the_lines_either_side():
    cues = [c for i, c in enumerate(align.parse_srt(srt_for(SENTENCES))) if not 10 <= i < 14]
    rows, est = align.match_transcript(SENTENCES, cues)
    assert est == {10, 11, 12, 13}
    assert rows[10][0] == 40000 and rows[13][1] == 56000              # the time the voice took
    assert all(a[1] <= b[0] + 1 for a, b in zip(rows[9:15], rows[10:15]))


def test_a_gap_too_long_for_the_sentence_is_left_unheard():
    """A minute of music between two lines is not the one short line between them."""
    texts = SENTENCES[:20] + SENTENCES[21:]
    cues = align.parse_srt(srt_for(texts))
    cues = cues[:20] + [align.Cue(c.start + 60000, c.end + 60000, c.text) for c in cues[20:]]
    rows, est = align.match_transcript(SENTENCES, cues)
    assert rows[20] is None and 20 not in est and rows[21][0] == 140000


def test_unread_front_matter_and_a_publisher_intro_stay_unheard():
    rows, est = align.match_transcript(SENTENCES, align.parse_srt(srt_for(["オーディブルがお届けします"] + SENTENCES[5:])))
    assert rows[:5] == [None] * 5 and not est
    assert rows[5] == [4000, 8000]


def test_short_lines_are_placed_between_the_lines_around_them():
    book = SENTENCES[:10] + ["はい。"] + SENTENCES[10:20] + ["はい。"] + SENTENCES[20:]
    rows, est = align.match_transcript(book, align.parse_srt(srt_for(book)))
    assert all(rows) and not est
    assert rows[10] == [40000, 44000] and rows[21] == [84000, 88000]


def _named_book() -> tuple[list[str], list[str]]:
    """The book writes a name in kanji and Whisper in kana (仁科 → にしな)."""
    book, heard = list(SENTENCES), [s.replace("。", "") for s in SENTENCES]
    for i in (3, 9, 15, 21, 27, 33):  # the name inside an ordinary line: the cue still matches, and shows the spelling
        book[i] = SENTENCES[i][:5] + "仁科" + SENTENCES[i][5:]
        heard[i] = heard[i][:5] + "にしな" + heard[i][5:]
    for i in (6, 12, 18, 24, 30, 36):  # a line that is mostly the name: without the spelling too little of the cue fits
        book[i] = f"仁科さん{SENTENCES[i][:2]}。"
        heard[i] = f"にしなさん{SENTENCES[i][:2]}"
    return book, heard


def test_a_name_the_transcript_writes_in_kana_is_found_from_the_cues_that_did_match():
    book, heard = _named_book()
    cues = align.parse_srt(srt_for(heard))
    rows, est = align.match_transcript(book, cues)
    assert all(rows) and not est
    assert [rows[i] for i in (6, 12, 36)] == [[24000, 28000], [48000, 52000], [144000, 148000]]
    assert align._respellings(*_places(book, cues)) == {"にしな": "仁科"}


def _places(book: list[str], cues: list[align.Cue]):
    """`_respellings`' arguments for cues that each sit in the sentence of the same number."""
    norms, b, bounds = align._book(book)
    texts = [align.cue_norm(c.text) for c in cues]
    return b, texts, {k: bounds[k] for k in range(len(cues))}


def test_a_respelling_needs_several_cues_and_is_not_a_word_the_book_writes_in_kana():
    book = [f"{SENTENCES[i][:4]}事{SENTENCES[i][4:]}" for i in range(6)]
    heard = [f"{SENTENCES[i][:4]}こと{SENTENCES[i][4:]}".replace("。", "") for i in range(6)]
    assert align._respellings(*_places(book, align.parse_srt(srt_for(heard)))) == {"こと": "事"}
    assert align._respellings(*_places(book[:2], align.parse_srt(srt_for(heard[:2])))) == {}   # two cues is not enough
    kana_book = book + [f"こと{SENTENCES[i]}こと" for i in range(6, 12)]                        # the book writes こと too
    assert align._respellings(*_places(kana_book, align.parse_srt(srt_for(heard + SENTENCES[6:12])))) == {}


def test_kanji_digits_match_digits():
    assert align.norm("第二話『夫婦』") == align.norm("第2話 夫婦") == "第2話夫婦"
    assert align.norm("三八〇円") == "380円" and len(align.norm("二十一歳")) == 4  # one character each, so lengths agree


def test_a_line_the_cue_spells_differently_is_found_by_the_hole_it_leaves():
    """「なに？」 against 何?: no text in common, but both neighbours are placed and the one cue
    between them has nowhere else to go."""
    book = SENTENCES[:10] + ["「なに？」"] + SENTENCES[10:]
    heard = [s.replace("。", "") for s in SENTENCES[:10]] + ["何?"] + [s.replace("。", "") for s in SENTENCES[10:]]
    rows, est = align.match_transcript(book, align.parse_srt(srt_for(heard)))
    assert all(rows) and not est
    assert rows[10] == [40000, 44000] and rows[9] == [36000, 40000] and rows[11] == [44000, 48000]


def test_one_cue_for_two_lines_in_a_hole_is_shared_by_their_length():
    book = SENTENCES[:10] + ["「なに？」", "「べつに」"] + SENTENCES[10:]
    heard = [s.replace("。", "") for s in SENTENCES[:10]] + ["何 別に"] + [s.replace("。", "") for s in SENTENCES[10:]]
    rows, est = align.match_transcript(book, align.parse_srt(srt_for(heard)))
    assert not est
    assert rows[10] == [40000, 41600] and rows[11] == [41600, 44000]  # 2 and 3 characters of the 4 s


def test_a_hole_with_more_cues_than_lines_is_left_alone():
    """Two unmatched cues for one line: which is it? Neither is guessed."""
    book = SENTENCES[:10] + ["「なに？」"] + SENTENCES[10:]
    plain = [s.replace("。", "") for s in SENTENCES]
    heard = plain[:10] + ["何?", "ふむ"] + plain[10:]
    rows, est = align.match_transcript(book, align.parse_srt(srt_for(heard)))
    assert rows[10] is None and 10 not in est and rows[9] and rows[11]


def test_a_hole_is_only_filled_from_cues_in_the_same_part():
    book = SENTENCES[:20] + ["「なに？」"] + SENTENCES[20:]
    plain = [s.replace("。", "") for s in SENTENCES]
    one = align.parse_srt(srt_for(plain[:20]), "a.mp3")
    two = align.parse_srt(srt_for(["何?"] + plain[20:]), "b.mp3")  # the stray cue opens the next file
    rows, _ = align.match_transcript(book, one + two)
    assert rows[20] is None  # its neighbours are in different files: the cue could be in either hole


def test_transcript_parts_name_their_part_and_do_not_span_the_join():
    one = align.parse_srt(srt_for(_heard(SENTENCES[:20]), step=8000), "a.mp3")
    two = align.parse_srt(srt_for(_heard(SENTENCES[20:]), step=8000), "b.mp3")
    rows, _ = align.match_transcript(SENTENCES, one + two)
    assert rows[0][2] == "a.mp3" and rows[19][2] == "a.mp3" and rows[20] == [0, rows[20][1], "b.mp3"]


def test_sync_output_is_told_from_a_transcript():
    assert align.looks_synced(SENTENCES, align.parse_srt(srt_for(SENTENCES)))
    assert not align.looks_synced(SENTENCES, align.parse_srt(srt_for(_heard(SENTENCES))))


def test_a_transcript_is_stored_apart_and_the_map_is_made_from_it(cfg):
    book = _book(cfg)
    align.ingest(cfg, book, "B0AUDIO001", None, srt_for(SENTENCES[:30]))
    m = align.ingest(cfg, book, "B0AUDIO001", None, srt_for(_heard(SENTENCES), step=8000), kind="transcript")
    assert (cfg.alignments_dir / "b1.srt").exists() and (cfg.alignments_dir / "b1.transcript.srt").exists()
    assert m["schema"] == align.ALIGN_SCHEMA and m["source"] == "transcript" and m["matched"] == 40 and m["estimated"] == 0
    assert m["docs"][0]["len"] == [len(align.norm(s)) for s in SENTENCES] and m["docs"][0]["est"] == []
    assert align.state(cfg, book, "B0AUDIO001")["source"] == "transcript"
    assert align.srt_bytes(cfg, "b1", None, None).decode().count("-->") == 20   # what the map is made from
    assert align.srt_bytes(cfg, "b1", None, None, "synced").decode().count("-->") == 30


def test_a_transcript_that_is_really_sync_output_is_refused(cfg):
    book = _book(cfg)
    with pytest.raises(align.AlignError) as e:
        align.ingest(cfg, book, "B0AUDIO001", None, srt_for(SENTENCES), kind="transcript")
    assert e.value.reason == "bad_srt" and "sync" in str(e.value)
    assert not (cfg.alignments_dir / "b1.transcript.srt").exists()
    with pytest.raises(align.AlignError) as e:
        align.ingest(cfg, book, "B0AUDIO001", None, srt_for(SENTENCES), kind="guess")
    assert e.value.reason == "bad_kind"


def test_removing_the_transcript_goes_back_to_the_synced_srt(cfg):
    book = _book(cfg)
    align.ingest(cfg, book, "B0AUDIO001", None, srt_for(SENTENCES[:30]))
    align.ingest(cfg, book, "B0AUDIO001", None, srt_for(_heard(SENTENCES), step=8000), kind="transcript")
    assert align.delete(cfg, "b1", "transcript")
    assert (cfg.alignments_dir / "b1.srt").exists() and not (cfg.alignments_dir / "b1.transcript.srt").exists()
    m = align.ensure(cfg, book, "B0AUDIO001", None)
    assert m["source"] == "synced" and m["matched"] == 30
    assert not align.delete(cfg, "b1", "transcript")


def test_numbered_text_has_every_pack_sentence(cfg):
    book = _book(cfg)
    lines = align.numbered_text(cfg, book).splitlines()
    index = annotate.sentence_texts(cfg, book)[0]["index"]
    assert lines == [f"{index}:{i}\t{s}" for i, s in enumerate(SENTENCES)]


# ── storage and the pack's anchors ───────────────────────────────────────────

def test_sentence_texts_line_up_with_the_packs_sents(cfg):
    book = _book(cfg, docs=[_doc("".join(f"<p>{s}</p>" for s in SENTENCES) + RUBY)])
    docs = annotate.sentence_texts(cfg, book)
    pack = annotate.build_pack(cfg, book)
    assert [d["index"] for d in docs] == [d["index"] for d in pack["docs"]]
    assert [d["fnv"] for d in docs] == [d["fnv"] for d in pack["docs"]]
    assert [len(d["texts"]) for d in docs] == [len(d["sents"]) for d in pack["docs"]]
    assert docs[0]["texts"][-1] == "漢字を読む少女は微笑んだ。"


def test_ruby_text_in_the_book_matches_a_plain_cue(cfg):
    book = _book(cfg, docs=[_doc("".join(f"<p>{s}</p>" for s in SENTENCES) + RUBY)])
    m = align.ingest(cfg, book, "B0AUDIO001", None,
                     srt_for(SENTENCES + ["漢字を読む少女は微笑んだ。"]))
    assert m["matched"] == m["total"] == 41 and m["ok"]
    assert m["docs"][0]["sents"][40] == [160000, 164000]


def test_ingest_stores_the_srt_and_the_map_and_reports_coverage(cfg):
    book = _book(cfg)
    m = align.ingest(cfg, book, "B0AUDIO001", None, srt_for(SENTENCES[:30]))
    assert m["total"] == 40 and m["matched"] == 30 and m["coverage"] == 0.75 and not m["ok"]
    assert m["audiobook"] == "B0AUDIO001" and m["sha256"] == "cd" * 32
    assert (cfg.alignments_dir / "b1.srt").read_text("utf-8").startswith("1\n00:00:00,000")
    doc = m["docs"][0]
    assert doc["fnv"] == annotate.sentence_texts(cfg, book)[0]["fnv"]
    assert doc["sents"][29] and doc["sents"][30] is None


def test_ingest_refuses_without_a_local_audiobook_a_bad_srt_or_a_bad_part(cfg):
    book = _book(cfg)
    with pytest.raises(align.AlignError) as e:
        align.ingest(cfg, book, None, None, srt_for(SENTENCES))
    assert e.value.reason == "no_audiobook"
    with pytest.raises(align.AlignError) as e:
        align.ingest(cfg, book, "B0AUDIO001", None, "not an srt")
    assert e.value.reason == "bad_srt"
    with pytest.raises(align.AlignError) as e:
        align.ingest(cfg, book, "B0AUDIO001", ["a.mp3", "b.mp3"], srt_for(SENTENCES), "c.mp3")
    assert e.value.reason == "bad_part"
    assert not list(cfg.alignments_dir.glob("*")) if cfg.alignments_dir.exists() else True


def test_a_book_that_cannot_have_sentences_keeps_no_srt(cfg):
    book = {**_book(cfg), "language": "en"}
    with pytest.raises(annotate.NoPack):
        align.ingest(cfg, book, "B0AUDIO001", None, srt_for(SENTENCES))
    assert not (cfg.alignments_dir / "b1.srt").exists()


def test_multi_file_audiobooks_take_one_srt_per_part(cfg):
    book = _book(cfg)
    files = ["a.mp3", "b.mp3"]
    m = align.ingest(cfg, book, "B0AUDIO001", files, srt_for(SENTENCES[:20]), "a.mp3")
    assert m["matched"] == 20
    m = align.ingest(cfg, book, "B0AUDIO001", files, srt_for(SENTENCES[20:]), "b.mp3")
    assert m["matched"] == 40 and m["docs"][0]["sents"][20] == [0, 4000, "b.mp3"]
    assert align.srt_bytes(cfg, "b1", files, "b.mp3").decode().startswith("1\n")
    with pytest.raises(align.AlignError):
        align.srt_bytes(cfg, "b1", files, None)


def test_ensure_rematches_when_the_book_changes_and_refuses_another_audiobook(cfg):
    book = _book(cfg)
    first = align.ingest(cfg, book, "B0AUDIO001", None, srt_for(SENTENCES))
    assert align.ensure(cfg, book, "B0AUDIO001", None) == first          # served as stored
    changed = {**book, "sha256": "ef" * 32}
    again = align.ensure(cfg, changed, "B0AUDIO001", None)               # rebuilt from the SRT
    assert again["sha256"] == "ef" * 32 and again["matched"] == 40
    with pytest.raises(align.AlignError) as e:
        align.ensure(cfg, changed, "B0OTHER0002", None)
    assert e.value.reason == "audiobook_changed"
    with pytest.raises(align.AlignError) as e:
        align.ensure(cfg, changed, None, None)
    assert e.value.reason == "no_audiobook"
    assert align.ensure(cfg, _book(cfg, "zz"), "B0AUDIO001", None) is None


def test_state_reports_none_ready_and_stale(cfg):
    book = _book(cfg)
    assert align.state(cfg, book, "B0AUDIO001")["state"] == "none"
    align.ingest(cfg, book, "B0AUDIO001", None, srt_for(SENTENCES))
    assert align.state(cfg, book, "B0AUDIO001") == {"state": "ready", "coverage": 1.0, "ok": True, "source": "synced", "estimated": 0, "anchors": 0,
                                                     "speech_gaps": None, "speech_gap_s": None}
    assert align.state(cfg, book, "B0OTHER0002")["state"] == "stale"
    assert align.state(cfg, {**book, "sha256": "00" * 32}, "B0AUDIO001")["state"] == "stale"


# ── curated-state hygiene ────────────────────────────────────────────────────

def test_purge_keeps_the_srt_unless_curated_and_rekey_moves_it(cfg):
    book = _book(cfg)
    align.ingest(cfg, book, "B0AUDIO001", None, srt_for(SENTENCES))
    purge.purge_book(cfg, "b1")
    assert (cfg.alignments_dir / "b1.srt").exists() and not align.map_path(cfg, "b1").exists()

    rekey.rekey_book(cfg, "b1", "b2")
    assert (cfg.alignments_dir / "b2.srt").exists() and not (cfg.alignments_dir / "b1.srt").exists()

    purge.purge_book(cfg, "b2", curated=True)
    assert not list(cfg.alignments_dir.glob("b2.*"))


def test_gc_finds_orphaned_alignment_files(cfg):
    book = _book(cfg)
    align.ingest(cfg, book, "B0AUDIO001", None, srt_for(SENTENCES))
    assert orphans.find(cfg, {"other": {}})["alignments/"] == ["b1"]
    assert "alignments/" not in orphans.find(cfg, {"b1": book})


# ── routes ───────────────────────────────────────────────────────────────────

@pytest.fixture
def client(server):
    scfg = server.cfg
    save_json(scfg.library_index, {
        "b1": _book(scfg, "b1"),
        "nolink": {**_book(scfg, "nolink"), "audiobook": {"audible_isbn": "B0GONE0003",
                                                           "audiobook_local": False}},
        "en": {**_book(scfg, "en"), "language": "en"},
    })
    save_json(scfg.audiobook_index, {
        "B0AUDIO001": {"ab_id": "B0AUDIO001", "local": True, "format": "m4b"},
        "B0MP3FOLDR": {"ab_id": "B0MP3FOLDR", "local": True, "format": "mp3-folder",
                       "files": ["a.mp3", "b.mp3"]},
    })
    return TestClient(server.app)


def _post(client, book_id, srt, **data):
    return client.post(f"/api/books/{book_id}/alignment",
                       files={"file": ("a.srt", srt.encode("utf-8"), "application/x-subrip")}, data=data)


def test_post_then_get_serves_the_map_with_an_etag_and_a_304(client):
    assert client.get("/api/reader/books/b1/alignment").status_code == 404
    assert client.get("/api/books/b1/status").json()["alignment"]["state"] == "none"

    r = _post(client, "b1", srt_for(SENTENCES))
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "audiobook": "B0AUDIO001", "source": "synced", "coverage": 1.0,
                        "ok": True, "matched": 40, "estimated": 0, "total": 40}

    r = client.get("/api/reader/books/b1/alignment")
    assert r.status_code == 200
    body = r.json()
    assert body["schema"] == align.ALIGN_SCHEMA and body["book_id"] == "b1" and body["audiobook"] == "B0AUDIO001"
    assert body["coverage"] == 1.0 and body["docs"][0]["sents"][0] == [0, 4000]
    tag = r.headers["etag"]
    assert client.get("/api/reader/books/b1/alignment", headers={"If-None-Match": tag}).status_code == 304

    st = client.get("/api/books/b1/status").json()["alignment"]
    assert st == {"state": "ready", "coverage": 1.0, "ok": True, "step": None, "error": None,
                  "eligible": True, "can_generate": False, "source": "synced", "estimated": 0, "anchors": 0,
                  "speech_gaps": None, "speech_gap_s": None, "accepts": ["synced", "transcript"]}

    raw = client.get("/api/reader/books/b1/alignment.srt")
    assert raw.status_code == 200 and raw.text.startswith("1\n00:00:00,000 --> 00:00:04,000")

    assert client.delete("/api/books/b1/alignment").status_code == 200
    assert client.get("/api/reader/books/b1/alignment").status_code == 404
    assert client.delete("/api/books/b1/alignment").status_code == 404


def test_a_low_coverage_map_is_served_but_marked_not_ok(client):
    assert _post(client, "b1", srt_for(SENTENCES[:20])).json()["ok"] is False
    body = client.get("/api/reader/books/b1/alignment").json()
    assert body["coverage"] == 0.5 and body["ok"] is False


def test_post_errors(client):
    assert _post(client, "nope", srt_for(SENTENCES)).status_code == 404
    r = _post(client, "nolink", srt_for(SENTENCES))
    assert r.status_code == 409 and r.json()["reason"] == "no_audiobook"
    assert _post(client, "b1", "garbage").json()["reason"] == "bad_srt"
    r = _post(client, "en", srt_for(SENTENCES))
    assert r.status_code == 409 and r.json() == {"reason": "not_ja"}
    big = client.post("/api/books/b1/alignment",
                      files={"file": ("a.srt", b"x" * (5 * 1024 * 1024 + 1))})
    assert big.status_code == 413


def test_a_relinked_audiobook_makes_the_map_stale_not_wrong(client, server):
    assert _post(client, "b1", srt_for(SENTENCES)).status_code == 200
    library = json.loads(server.cfg.library_index.read_text("utf-8"))
    library["b1"]["audiobook"]["audible_isbn"] = "B0MP3FOLDR"
    save_json(server.cfg.library_index, library)
    r = client.get("/api/reader/books/b1/alignment")
    assert r.status_code == 409 and r.json()["reason"] == "audiobook_changed"
    assert client.get("/api/books/b1/status").json()["alignment"]["state"] == "stale"


def test_mp3_folder_routes_take_a_part(client, server):
    library = json.loads(server.cfg.library_index.read_text("utf-8"))
    library["b1"]["audiobook"]["audible_isbn"] = "B0MP3FOLDR"
    save_json(server.cfg.library_index, library)
    assert _post(client, "b1", srt_for(SENTENCES[:20])).json()["reason"] == "bad_part"
    assert _post(client, "b1", srt_for(SENTENCES[:20]), part="a.mp3").status_code == 200
    assert _post(client, "b1", srt_for(SENTENCES[20:]), part="b.mp3").json()["matched"] == 40
    sents = client.get("/api/reader/books/b1/alignment").json()["docs"][0]["sents"]
    assert sents[0] == [0, 4000, "a.mp3"] and sents[20] == [0, 4000, "b.mp3"]
    assert client.get("/api/reader/books/b1/alignment.srt").status_code == 400
    assert client.get("/api/reader/books/b1/alignment.srt?part=b.mp3").status_code == 200


def test_routes_take_a_transcript_and_say_what_the_map_is_made_from(client):
    assert _post(client, "b1", srt_for(SENTENCES[:30])).json()["source"] == "synced"
    r = _post(client, "b1", srt_for(_heard(SENTENCES), step=8000), kind="transcript")
    assert r.status_code == 200 and r.json()["source"] == "transcript" and r.json()["matched"] == 40
    body = client.get("/api/reader/books/b1/alignment").json()
    assert body["source"] == "transcript" and body["docs"][0]["len"][0] == len(align.norm(SENTENCES[0]))
    st = client.get("/api/books/b1/status").json()["alignment"]
    assert st["source"] == "transcript" and st["accepts"] == ["synced", "transcript"]
    assert client.get("/api/reader/books/b1/alignment.srt?kind=synced").text.count("-->") == 30
    assert client.get("/api/reader/books/b1/alignment.srt?kind=nope").status_code == 400
    assert _post(client, "b1", srt_for(SENTENCES), kind="transcript").json()["reason"] == "bad_srt"
    assert _post(client, "b1", srt_for(SENTENCES), kind="nope").json()["reason"] == "bad_kind"
    assert client.delete("/api/books/b1/alignment?kind=transcript").status_code == 200
    assert client.get("/api/reader/books/b1/alignment").json()["source"] == "synced"
    numbered = client.get("/api/books/b1/alignment/text?numbered=1").text.splitlines()
    assert numbered[0].endswith("\t" + SENTENCES[0]) and ":" in numbered[0].split("\t")[0]


def test_the_alignment_routes_do_not_wait_for_the_index_lock(client, server):
    assert _post(client, "b1", srt_for(SENTENCES)).status_code == 200
    with server._index_lock:
        assert client.get("/api/reader/books/b1/alignment").status_code == 200
        assert _post(client, "b1", srt_for(SENTENCES)).status_code == 200


# ── listeners' fixes ──────────────────────────────────────────────────

def _lagging() -> list[list]:
    """300 sentences of 20 characters at 5 a second, where the map runs 240 s behind from 50 and races
    back through 150..229: the shape SubPlz sync gave one real novel."""
    rows = [[4000 * i, 4000 * (i + 1)] for i in range(300)]
    for i in range(50, 150):
        rows[i] = [4000 * i + 240_000, 4000 * (i + 1) + 240_000]
    for i in range(150, 230):
        rows[i] = [840_000 + 1000 * (i - 150), 840_000 + 1000 * (i - 149)]
    return rows


def test_a_fix_puts_a_lagging_stretch_right_until_the_map_agrees_again():
    rows = _lagging()
    moved = align.apply_anchors(rows, [20] * 300, {60: (None, 240_000)}, set())
    assert all(abs(rows[i][0] - 4000 * i) <= 2000 for i in range(50, 300))
    assert rows[230] == [920_000, 924_000] and rows[49] == [196_000, 200_000]
    assert 61 in moved and 229 in moved and 60 not in moved and 230 not in moved
    starts = [r[0] for r in rows if r]
    assert starts == sorted(starts)


def test_a_fix_where_the_map_is_right_changes_nothing():
    rows = [[4000 * i, 4000 * (i + 1)] for i in range(300)]
    before = [list(r) for r in rows]
    align.apply_anchors(rows, [20] * 300, {100: (None, 400_000)}, set())
    assert rows == before


def _fix(client, *, ms=240_000, op="set", ab="B0AUDIO001", sentence=12, eid="f1", ts=1000):
    return client.post("/api/reader/events", json={"events": [{
        "id": eid, "ts": ts, "type": "sync_anchor", "book_id": "b1", "ab_id": ab,
        "doc": 0, "sent": sentence, "part": None, "ms": ms, "op": op}]}).json()


def test_a_fix_from_the_phone_reaches_the_map_and_its_etag(client):
    assert _post(client, "b1", srt_for(SENTENCES)).status_code == 200
    first = client.get("/api/reader/books/b1/alignment")
    body = first.json()
    assert body["anchors"] == []                                         # this server takes fixes
    doc = body["docs"][0]["index"]
    r = client.post("/api/reader/events", json={"events": [{
        "id": "f1", "ts": 1000, "type": "sync_anchor", "book_id": "b1", "ab_id": "B0AUDIO001",
        "doc": doc, "sent": 12, "part": None, "ms": 50_000}]}).json()
    assert r["rejected"] == []
    again = client.get("/api/reader/books/b1/alignment", headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 200 and again.headers["etag"] != first.headers["etag"]
    fixed = again.json()
    assert fixed["anchors"] == [[doc, 12, None, 50_000]] and fixed["docs"][0]["sents"][12][0] == 50_000
    assert client.get("/api/books/b1/status").json()["alignment"]["anchors"] == 1
    client.post("/api/reader/events", json={"events": [{
        "id": "f2", "ts": 1001, "type": "sync_anchor", "book_id": "b1", "ab_id": "B0AUDIO001",
        "doc": doc, "sent": 12, "ms": 0, "op": "clear"}]})
    assert client.get("/api/reader/books/b1/alignment").json()["docs"][0]["sents"][12] == [48000, 52000]


def test_a_fix_for_another_audiobook_or_book_is_refused(client):
    assert _post(client, "b1", srt_for(SENTENCES)).status_code == 200
    assert _fix(client, ab="B0OTHER0002")["rejected"] == [{"id": "f1", "reason": "audiobook_changed"}]
    r = client.post("/api/reader/events", json={"events": [{
        "id": "f3", "ts": 1, "type": "sync_anchor", "book_id": "nope", "ab_id": "B0AUDIO001", "doc": 0,
        "sent": 0, "ms": 0}]}).json()
    assert r["rejected"] == [{"id": "f3", "reason": "no_book"}]


def test_fixes_are_kept_like_the_srt(cfg):
    book = _book(cfg)
    align.ingest(cfg, book, "B0AUDIO001", None, srt_for(SENTENCES))
    align.add_anchors(cfg, "b1", "B0AUDIO001", [{"ts": 1, "doc": 0, "sent": 3, "part": None, "ms": 10}])
    purge.purge_book(cfg, "b1")
    assert align.anchors_path(cfg, "b1").exists()
    rekey.rekey_book(cfg, "b1", "b2")
    assert align.anchors_path(cfg, "b2").exists() and not align.anchors_path(cfg, "b1").exists()
    assert align.load_anchors(cfg, "b2", "B0AUDIO001") == {(0, 3): (None, 10)}
    assert align.load_anchors(cfg, "b2", "B0OTHER0002") == {}
