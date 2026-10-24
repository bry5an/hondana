"""Embedded tags, per container: the same book tagged in an m4b, an mp3, a FLAC
and an Ogg Opus file reads the same."""
import pytest

import audio_fixtures as af
from epublib import audio_tags

TAGS = dict(title="夜のピクニック", album="夜のピクニック", album_artist="恩田 陸",
            narrator="朗読者", series="ピクニック", part="2", asin="B0ABCDEFGH",
            description="あらすじ", year="2004-07-30", genre="文芸作品", cover=af.PNG)


@pytest.mark.parametrize("make, name", [(af.m4b, "a.m4b"), (af.m4a, "a.m4a"), (af.mp3, "a.mp3"),
                                        (af.flac, "a.flac"), (af.opus, "a.opus")])
def test_every_container_reads_the_same_fields(tmp_path, make, name):
    t = audio_tags.read(make(tmp_path / name, seconds=3, **TAGS))
    assert {k: t[k] for k in ("title", "album", "author", "narrator", "series", "part", "asin",
                              "description", "year", "genre")} == {
        "title": "夜のピクニック", "album": "夜のピクニック", "author": "恩田 陸",
        "narrator": "朗読者", "series": "ピクニック", "part": "2", "asin": "B0ABCDEFGH",
        "description": "あらすじ", "year": "2004", "genre": "文芸作品"}
    assert t["duration"] == pytest.approx(3, abs=0.1)
    assert t["cover"] == af.PNG and t["cover_mime"] == "image/png"


def test_audibles_own_atoms_narrator_from_composer_and_artist_as_author(tmp_path):
    t = audio_tags.read(af.m4b(tmp_path / "a.m4b", title="T", artist="著者", composer="読み手",
                               cdek="B0CDEKCDEK", track=4))
    assert (t["author"], t["narrator"], t["asin"], t["track"]) == ("著者", "読み手", "B0CDEKCDEK", 4)


def test_a_malformed_asin_is_dropped(tmp_path):
    assert audio_tags.read(af.flac(tmp_path / "a.flac", title="T", asin="not an asin"))["asin"] is None


def test_an_unreadable_file_is_none_and_an_untagged_one_has_only_a_duration(tmp_path):
    junk = tmp_path / "junk.m4b"
    junk.write_bytes(b"not audio at all")
    assert audio_tags.read(junk) is None
    bare = audio_tags.read(af.m4b(tmp_path / "bare.m4b", seconds=5))
    assert bare["title"] is None and bare["duration"] == 5


def test_tracks_merge_into_one_book_titled_by_the_album(tmp_path):
    paths = [af.mp3(tmp_path / f"{i:02}.mp3", seconds=1, album="本の題", title=f"第{i}章",
                    artist="著者", track=i, cover=af.PNG if i == 2 else None) for i in (1, 2, 3)]
    stray = af.mp3(tmp_path / "04.mp3", seconds=1, album="本の題", artist="別人")
    book = audio_tags.read_tracks([*paths, stray])
    assert (book["title"], book["author"], book["tracks"]) == ("本の題", "著者", 4)
    assert book["duration"] == pytest.approx(4, abs=0.2)
    assert book["cover"] == af.PNG


def test_summary_is_json_safe(tmp_path):
    s = audio_tags.summary(audio_tags.read(af.m4b(tmp_path / "a.m4b", **TAGS)))
    assert "cover" not in s and s["has_cover"] is True
    assert audio_tags.summary(audio_tags.merge_tracks([s, s]))["has_cover"] is True
