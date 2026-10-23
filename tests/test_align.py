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
    assert align.state(cfg, book, "B0AUDIO001") == {"state": "ready", "coverage": 1.0, "ok": True}
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
    assert r.json() == {"status": "ok", "audiobook": "B0AUDIO001", "coverage": 1.0, "ok": True,
                        "matched": 40, "total": 40}

    r = client.get("/api/reader/books/b1/alignment")
    assert r.status_code == 200
    body = r.json()
    assert body["schema"] == align.ALIGN_SCHEMA and body["book_id"] == "b1" and body["audiobook"] == "B0AUDIO001"
    assert body["coverage"] == 1.0 and body["docs"][0]["sents"][0] == [0, 4000]
    tag = r.headers["etag"]
    assert client.get("/api/reader/books/b1/alignment", headers={"If-None-Match": tag}).status_code == 304

    st = client.get("/api/books/b1/status").json()["alignment"]
    assert st == {"state": "ready", "coverage": 1.0, "ok": True, "step": None, "error": None,
                  "eligible": True, "can_generate": False}

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


def test_the_alignment_routes_do_not_wait_for_the_index_lock(client, server):
    assert _post(client, "b1", srt_for(SENTENCES)).status_code == 200
    with server._index_lock:
        assert client.get("/api/reader/books/b1/alignment").status_code == 200
        assert _post(client, "b1", srt_for(SENTENCES)).status_code == 200
