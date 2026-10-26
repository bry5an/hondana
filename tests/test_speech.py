"""Checking a map against its audio (speech.py): ffmpeg's silences, the gaps the narrator
talks through, the cache, and `align.audit` with a stand-in ffmpeg."""
from __future__ import annotations

import os
import stat
import sys

from epublib import align, speech
from epublib.index_store import save_json
from test_align import SENTENCES, _book, _cue

STDERR = """Input #0, mov,mp4,m4a, from 'book.m4b':
  Duration: 00:01:40.00, start: 0.000000, bitrate: 63 kb/s
[Parsed_silencedetect_0 @ 0x7c] silence_start: 3.5
[Parsed_silencedetect_0 @ 0x7c] silence_end: 4.25 | silence_duration: 0.75
[Parsed_silencedetect_0 @ 0x7c] silence_start: 40
[Parsed_silencedetect_0 @ 0x7c] silence_end: 60.000 | silence_duration: 20
[Parsed_silencedetect_0 @ 0x7c] silence_start: 99.2
"""


def test_silences_are_read_from_ffmpeg_and_an_open_tail_is_dropped():
    assert speech.parse_silences(STDERR) == [[3500, 4250], [40000, 60000]]


def _docs(*rows_per_doc):
    return [{"index": i, "sents": rows} for i, rows in enumerate(rows_per_doc)]


def test_a_gap_the_narrator_talks_through_is_found_and_a_quiet_one_is_not():
    silences = {None: [[24000, 60000]]}
    docs = _docs([[0, 4000], [20000, 24000], [60000, 61000], [61000, 64000]])
    assert speech.speech_gaps(docs, silences) == [[0, 0, 1, 4000, 20000, 1.0]]
    assert speech.speech_gaps(_docs([[0, 4000]], [[30000, 34000]]), silences) == []  # a chapter break


def test_short_gaps_and_unknown_parts_are_not_checked():
    assert speech.speech_gaps(_docs([[0, 4000], [8000, 9000]]), {None: []}) == []
    assert speech.speech_gaps(_docs([[0, 4000, "a.mp3"], [20000, 24000, "a.mp3"]]), {"b.mp3": []}) == []


def test_quality_says_whether_the_audio_was_checked():
    assert speech.quality(_docs([[0, 1]]), None) == {"checked": False}
    q = speech.quality(_docs([[0, 4000], [20000, 24000]]), {None: []})
    assert q["checked"] and q["speech_gaps"] == 1 and q["speech_gap_s"] == 16.0


def test_silences_are_cached_until_the_file_changes(cfg, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(speech, "detect_silences", lambda path, ffmpeg="ffmpeg": calls.append(path) or [[0, 10]])
    audio = tmp_path / "book.m4b"
    audio.write_bytes(b"x" * 10)
    assert speech.check(cfg, "b1", [(None, audio)]) and len(calls) == 1
    assert not speech.check(cfg, "b1", [(None, audio)]) and len(calls) == 1
    audio.write_bytes(b"y" * 11)
    assert speech.check(cfg, "b1", [(None, audio)]) and len(calls) == 2
    assert speech.load(cfg, "b1") == {None: [[0, 10]]}


FAKE_FFMPEG = """#!{python}
import sys
sys.stderr.write("[silencedetect] silence_start: 0.0\\n[silencedetect] silence_end: 0.1\\n")
"""


def test_an_audit_puts_the_gaps_in_the_map_and_the_status(cfg, tmp_path):
    root = tmp_path / "audiobooks"
    root.mkdir()
    (root / "book.m4b").write_bytes(b"OK")
    cfg.audiobook_dirs = [root]
    save_json(cfg.audiobook_index, {"B0AUDIO001": {"ab_id": "B0AUDIO001", "local": True, "format": "m4b",
                                                    "root": str(root), "path": "book.m4b"}})
    book = _book(cfg)
    # One 30 s hole mid-book, where (says the stand-in ffmpeg) nobody is quiet.
    srt = "\n".join(_cue(i + 1, i * 4000 + (30_000 if i >= 20 else 0), (i + 1) * 4000 + (30_000 if i >= 20 else 0), s)
                    for i, s in enumerate(SENTENCES))
    align.ingest(cfg, book, "B0AUDIO001", None, srt)
    assert align.ensure(cfg, book, "B0AUDIO001", None)["quality"] == {"checked": False}
    fake = tmp_path / "ffmpeg"
    fake.write_text(FAKE_FFMPEG.format(python=sys.executable), encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    m = align.audit(cfg, book, ffmpeg=str(fake))
    assert m["quality"]["speech_gaps"] == 1 and m["quality"]["gaps"][0][1:3] == [19, 20]
    st = align.state(cfg, book, "B0AUDIO001")
    assert st["speech_gaps"] == 1 and st["speech_gap_s"] == 30.0
    assert align.audit(cfg, {**book, "audiobook": {}}, ffmpeg=str(fake)) is None
    assert os.path.exists(speech.silence_path(cfg, "b1"))
