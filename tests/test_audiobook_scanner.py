"""The audiobook scan: what counts as one book, which key it gets, and ids that
stay put across moves, renames and re-tags (and move everything with them when
a file gains a key)."""
import json

import pytest

import audio_fixtures as af
from conftest import make_cfg
from epublib import audiobook_catalog, audiobook_scanner as scanner, audiobook_xref, reader
from epublib.index_store import load_json, save_json


@pytest.fixture
def lib(tmp_path):
    root = tmp_path / "audiobooks"
    root.mkdir()
    return make_cfg(tmp_path, audiobook_dirs=[root]), root


def _by_path(index):
    return {e["path"]: e for e in index.values()}


# ── what is one book ─────────────────────────────────────────────────────────

def test_formats_group_into_books(lib):
    cfg, root = lib
    af.m4b(root / "A" / "One [B0AAAAAAAA].m4b")
    for i in (1, 2):
        af.mp3(root / "Rip" / f"{i:02}.mp3", album="Rip")
    af.m4a(root / "Solo" / "solo.m4a", title="Solo")
    for i in (1, 2):
        af.flac(root / "Two" / f"a{i}.flac", album="Album A")
        af.opus(root / "Two" / f"b{i}.opus", album="Album B")
    af.m4a(root / "Two" / "c.m4a", album="Album C")
    # A folder with an m4b is that book; the loose tracks beside it are extras.
    af.m4b(root / "Mixed" / "book.m4b")
    af.mp3(root / "Mixed" / "sample.mp3")
    af.m4a(root / "Mixed" / "bonus.m4a")

    index = scanner.scan(cfg)
    got = {(e["path"], e["format"], e["group"], tuple(e["files"] or ())) for e in index.values()}
    assert got == {
        ("A/One [B0AAAAAAAA].m4b", "m4b", None, ()),
        ("Mixed/book.m4b", "m4b", None, ()),
        ("Rip", "mp3-folder", None, ("01.mp3", "02.mp3")),
        ("Solo/solo.m4a", "m4a", None, ()),
        ("Two", "tracks", "Album A", ("a1.flac", "a2.flac")),
        ("Two", "tracks", "Album B", ("b1.opus", "b2.opus")),
        ("Two/c.m4a", "m4a", None, ()),
    }
    two = {e["group"]: e for e in index.values() if e["path"] == "Two"}
    assert two["Album A"]["filename"] == "Album A"
    assert two["Album A"]["ab_id"] != two["Album B"]["ab_id"]


@pytest.mark.parametrize("junk", [".hidden/x.m4b", "@eaDir/x.m4b", "#recycle/x.m4b",
                                  "Book.m4b.download/Book.m4b", "._x.m4b", "x.m4b.part",
                                  ".staging/y.m4b"])
def test_junk_folders_and_partial_downloads_are_skipped(lib, junk):
    cfg, root = lib
    af.m4b(root / junk)
    af.m4b(root / "real.m4b")
    assert [e["path"] for e in scanner.scan(cfg).values()] == ["real.m4b"]


def test_an_m4b_beats_its_mp3_copy_of_the_same_asin(lib, capsys):
    cfg, root = lib
    af.mp3(root / "Copy [B0AAAAAAAA]" / "01.mp3")
    af.m4b(root / "Book [B0AAAAAAAA].m4b")
    index = scanner.scan(cfg)
    assert index["B0AAAAAAAA"]["format"] == "m4b"
    assert "duplicate B0AAAAAAAA" in capsys.readouterr().out


# ── keys ─────────────────────────────────────────────────────────────────────

def test_the_key_is_the_name_then_a_hand_match_then_the_tag(lib):
    cfg, root = lib
    af.m4b(root / "named [B0NAMENAME].m4b", asin="B0TAGTAGTA")
    af.m4b(root / "tagged.m4b", asin="B0TAGTAGTA")
    af.m4b(root / "matched.m4b", seconds=61, asin="B0TAGTAG00")
    af.m4b(root / "plain.m4b", seconds=62)
    first = _by_path(scanner.scan(cfg))
    save_json(cfg.audiobook_assignments, {first["matched.m4b"]["fid"]: "B0HANDHAND"})

    e = _by_path(scanner.scan(cfg))
    assert (e["named [B0NAMENAME].m4b"]["ab_id"], e["named [B0NAMENAME].m4b"]["key_source"]) == ("B0NAMENAME", "name")
    assert (e["tagged.m4b"]["ab_id"], e["tagged.m4b"]["key_source"]) == ("B0TAGTAGTA", "tag")
    assert (e["matched.m4b"]["ab_id"], e["matched.m4b"]["key_source"]) == ("B0HANDHAND", "assigned")
    plain = e["plain.m4b"]
    assert plain["ab_id"] == plain["fid"] == scanner.ab_id_for("plain.m4b", plain["filesize"])
    assert plain["catalog_key"] is None


# ── sticky ids ───────────────────────────────────────────────────────────────

def test_a_keyless_book_keeps_its_id_across_a_move_a_rename_and_a_retag(lib):
    cfg, root = lib
    big = 3 << 20  # so the middle-of-file hash sees audio, not tags
    src = af.m4b(root / "Inbox" / "book.m4b", seconds=600, title="題名", pad=big)
    af.m4b(root / "other.m4b", seconds=30, title="別の本")
    first = _by_path(scanner.scan(cfg))
    ab_id = first["Inbox/book.m4b"]["ab_id"]
    other_id = first["other.m4b"]["ab_id"]

    moved = root / "Author" / "book.m4b"          # same name and size
    moved.parent.mkdir()
    src.rename(moved)
    assert _by_path(scanner.scan(cfg))["Author/book.m4b"]["ab_id"] == ab_id

    renamed = moved.with_name("題名.m4b")          # same tag title and duration
    moved.rename(renamed)
    assert _by_path(scanner.scan(cfg))["Author/題名.m4b"]["ab_id"] == ab_id

    af.m4b(renamed, seconds=600, title="題名", narrator="読み手", pad=big)  # re-tagged in place
    assert _by_path(scanner.scan(cfg))["Author/題名.m4b"]["ab_id"] == ab_id

    # Renamed *and* re-titled (to a title of the same length, so the same size):
    # only the middle-of-file hash still knows it.
    size = renamed.stat().st_size
    final = renamed.with_name("x.m4b")
    renamed.rename(final)
    af.m4b(final, seconds=600, title="名題", narrator="読み手", pad=big)
    assert final.stat().st_size == size
    index = _by_path(scanner.scan(cfg))
    assert index["Author/x.m4b"]["ab_id"] == ab_id
    assert index["other.m4b"]["ab_id"] == other_id


def test_a_hand_match_follows_the_file_when_it_moves(lib):
    cfg, root = lib
    p = af.m4b(root / "a" / "rip.m4b", seconds=60)
    fid = next(iter(scanner.scan(cfg).values()))["fid"]
    save_json(cfg.audiobook_assignments, {fid: "B0HANDHAND"})
    assert list(scanner.scan(cfg)) == ["B0HANDHAND"]
    (root / "b").mkdir()
    p.rename(root / "b" / "rip.m4b")
    index = scanner.scan(cfg)
    assert list(index) == ["B0HANDHAND"] and index["B0HANDHAND"]["fid"] == fid


def test_an_ambiguous_signal_carries_nothing(lib):
    cfg, root = lib
    a = af.m4b(root / "a" / "same.m4b", seconds=60, title="同じ")
    b = af.m4b(root / "b" / "same.m4b", seconds=60, title="同じ")
    old = {e["ab_id"] for e in scanner.scan(cfg).values()}
    for p in (a, b):
        dest = root / "moved" / p.parent.name / p.name
        dest.parent.mkdir(parents=True)
        p.rename(dest)
    new = {e["ab_id"] for e in scanner.scan(cfg).values()}
    assert not old & new  # two equal candidates: neither is guessed


def test_ids_from_before_fids_are_kept(lib):
    cfg, root = lib
    af.m4b(root / "keyed [B0AAAAAAAA].m4b")
    af.m4b(root / "plain.m4b", seconds=5)
    size = (root / "plain.m4b").stat().st_size
    legacy = scanner.ab_id_for("ANOTHER/PATH.m4b", size)  # whatever it was given then
    save_json(cfg.audiobook_files_index, {
        "B0AAAAAAAA": {"ab_id": "B0AAAAAAAA", "catalog_key": "B0AAAAAAAA", "root": str(root),
                       "path": "keyed [B0AAAAAAAA].m4b", "filename": "keyed [B0AAAAAAAA].m4b",
                       "filesize": 1, "format": "m4b", "files": None},
        legacy: {"ab_id": legacy, "catalog_key": None, "root": str(root), "path": "plain.m4b",
                 "filename": "plain.m4b", "filesize": size, "format": "m4b", "files": None},
    })
    assert set(scanner.scan(cfg)) == {"B0AAAAAAAA", legacy}


def test_dry_run_reports_and_writes_nothing(lib, capsys):
    cfg, root = lib
    p = af.m4b(root / "a.m4b", seconds=60, title="T")
    scanner.scan(cfg)
    before = cfg.audiobook_files_index.read_bytes()
    p.rename(root / "b.m4b")
    af.m4b(root / "c.m4b", seconds=61, asin="B0TAGTAGTA")
    scanner.scan(cfg, dry_run=True)
    out = capsys.readouterr().out
    assert "moved: a.m4b → b.m4b" in out and "1 moved" in out
    assert cfg.audiobook_files_index.read_bytes() == before


def test_an_unmounted_root_keeps_its_entries(tmp_path):
    root, gone = tmp_path / "ab", tmp_path / "unplugged"
    af.m4b(root / "a.m4b")
    af.m4b(gone / "b [B0BBBBBBBB].m4b")
    cfg = make_cfg(tmp_path, audiobook_dirs=[root, gone])
    scanner.scan(cfg)
    (gone / "b [B0BBBBBBBB].m4b").unlink()
    gone.rmdir()
    assert "B0BBBBBBBB" in scanner.scan(cfg)


# ── gaining a key moves its state ────────────────────────────────────────────

def test_gaining_a_key_moves_everything_kept_under_the_old_id(lib):
    cfg, root = lib
    p = af.m4b(root / "rip.m4b", seconds=60, title="題名")
    old = next(iter(scanner.scan(cfg)))
    user = cfg.users_dir / "reader2"
    user.mkdir(parents=True)
    save_json(user / "audio_progress.json", {old: {"track": 0, "seconds": 12.0, "updated_at": "2026-10-01T00:00:00+00:00"}})
    save_json(cfg.audiobook_hidden, {"ab_ids": [old, "B0OTHEROTH"]})
    save_json(cfg.audiobook_link_overrides, {"book1": old, "book2": "B0OTHEROTH"})
    save_json(cfg.audiobook_metadata_overrides, {old: {"title": "手で直した題"}})
    save_json(cfg.audiobook_natively, {old: {"natively_level": 20}})
    cfg.alignments_dir.mkdir(parents=True)
    save_json(cfg.alignments_dir / "book1.json", {"schema": 2, "audiobook": old})

    p.rename(root / "rip [B0NEWNEWNE].m4b")
    index = scanner.scan(cfg)
    assert list(index) == ["B0NEWNEWNE"]
    new = "B0NEWNEWNE"
    assert set(load_json(user / "audio_progress.json", {})) == {new}
    assert load_json(cfg.audiobook_hidden, {})["ab_ids"] == ["B0OTHEROTH", new]
    assert load_json(cfg.audiobook_link_overrides, {}) == {"book1": new, "book2": "B0OTHEROTH"}
    assert set(load_json(cfg.audiobook_metadata_overrides, {})) == {new}
    assert set(load_json(cfg.audiobook_natively, {})) == {new}
    assert load_json(cfg.alignments_dir / "book1.json", {})["audiobook"] == new
    assert load_json(cfg.audiobook_aliases, {}) == {old: new}

    # A phone that still knows the old id lands its position on the new one.
    reader.merge_audio_progress(cfg.__class__(**{**cfg.__dict__, "active_user": "reader2"}),
                                [{"ab_id": old, "track": 0, "seconds": 99.0, "ts": 1_900_000_000}])
    assert load_json(user / "audio_progress.json", {})[new]["seconds"] == 99.0


def test_a_keyed_id_is_never_moved_onto_another(lib):
    cfg, root = lib
    p = af.m4b(root / "a [B0AAAAAAAA].m4b", seconds=60)
    scanner.scan(cfg)
    save_json(cfg.audiobook_hidden, {"ab_ids": ["B0AAAAAAAA"]})
    p.rename(root / "a [B0BBBBBBBB].m4b")
    scanner.scan(cfg)
    assert load_json(cfg.audiobook_hidden, {})["ab_ids"] == ["B0AAAAAAAA"]
    assert not cfg.audiobook_aliases.exists()


# ── covers ───────────────────────────────────────────────────────────────────

def test_covers_come_from_tags_or_the_folder_and_are_dropped_with_the_book(lib):
    cfg, root = lib
    af.m4b(root / "tagged.m4b", cover=af.PNG)
    af.mp3(root / "Rip" / "01.mp3")
    (root / "Rip" / "Cover.JPG").write_bytes(b"\xff\xd8 a jpeg")
    af.m4b(root / "Shared" / "a.m4b")
    af.m4b(root / "Shared" / "b.m4b", seconds=5)
    (root / "Shared" / "cover.jpg").write_bytes(b"\xff\xd8 whose?")
    e = _by_path(scanner.scan(cfg))
    tagged = cfg.data_dir / e["tagged.m4b"]["cover"]
    assert tagged.read_bytes() == af.PNG and tagged.suffix == ".png"
    assert (cfg.data_dir / e["Rip"]["cover"]).read_bytes() == b"\xff\xd8 a jpeg"
    assert e["Shared/a.m4b"]["cover"] is None  # two books share the folder's picture: neither takes it

    (root / "tagged.m4b").unlink()
    scanner.scan(cfg)
    assert not tagged.exists()


def test_a_catalog_cover_wins_so_nothing_is_extracted(lib):
    cfg, root = lib
    save_json(cfg.audiobook_data, {"B0AAAAAAAA": {"title": "T", "cover": "https://img/a.jpg"}})
    af.m4b(root / "a [B0AAAAAAAA].m4b", cover=af.PNG)
    assert scanner.scan(cfg)["B0AAAAAAAA"]["cover"] is None


# ── the catalog's local rows, the index and xref ─────────────────────────────

def test_local_files_get_catalog_rows_from_tags_or_names(lib):
    cfg, root = lib
    af.m4b(root / "t.m4b", seconds=3600, title="夜のピクニック", album_artist="恩田 陸",
           series="ピクニック", part="3", narrator="読み手", genre="Audiobook", cover=af.PNG)
    af.m4b(root / "[作者] 名前のシリーズ" / "[02] 名前の本.m4b", seconds=5)
    index = scanner.scan(cfg)
    rows = audiobook_catalog.load(cfg)
    tagged = rows[_by_path(index)["t.m4b"]["ab_id"]]
    assert {k: tagged[k] for k in ("title", "author", "series", "order", "narrators",
                                   "runtime_length_min", "genre", "source")} == {
        "title": "夜のピクニック", "author": "恩田 陸", "series": "ピクニック", "order": "03",
        "narrators": "読み手", "runtime_length_min": "60", "genre": None, "source": "tags"}
    assert tagged["cover"].startswith("/covers/audiobook/")
    named = rows[_by_path(index)["[作者] 名前のシリーズ/[02] 名前の本.m4b"]["ab_id"]]
    assert {k: named[k] for k in ("title", "author", "series", "order", "source")} == {
        "title": "名前の本", "author": "作者", "series": "名前のシリーズ", "order": "02",
        "source": "filename"}
    assert audiobook_catalog.load(cfg, local=False) == {}

    built = scanner.build_index(cfg)
    row = built[_by_path(index)["t.m4b"]["ab_id"]]
    assert (row["source"], row["local"], row["catalog_key"], row["runtime_min"]) == ("tags", True, None, 60)
    assert row["cover_url"] == tagged["cover"]


def test_hand_edits_lay_over_any_row_and_make_a_guess_manual(lib):
    cfg, root = lib
    af.m4b(root / "guess.m4b")
    ab_id = next(iter(scanner.scan(cfg)))
    save_json(cfg.audiobook_metadata_overrides, {ab_id: {"title": "本当の題", "path": "ignored"}})
    row = audiobook_catalog.load(cfg)[ab_id]
    assert (row["title"], row["source"]) == ("本当の題", "manual")
    assert "path" not in row


def _epub(cfg, **book):
    save_json(cfg.library_index, {"bk": {"book_id": "bk", "language": "ja", **book}})


def test_a_tagged_local_book_links_to_its_epub_and_counts_as_local(lib):
    cfg, root = lib
    af.m4b(root / "x.m4b", title="夜のピクニック", album_artist="恩田陸")
    ab_id = next(iter(scanner.scan(cfg)))
    _epub(cfg, title="夜のピクニック", author="恩田陸")
    link = audiobook_xref.xref(cfg)["bk"]
    assert (link["audible_isbn"], link["audiobook_local"], link["audible_url"]) == (ab_id, True, None)


def test_a_book_known_only_by_its_file_name_is_never_auto_linked(lib):
    cfg, root = lib
    af.m4b(root / "夜のピクニック.m4b")
    ab_id = next(iter(scanner.scan(cfg)))
    _epub(cfg, title="夜のピクニック", author="恩田陸")
    assert audiobook_xref.xref(cfg) == {}
    save_json(cfg.audiobook_link_overrides, {"bk": ab_id})  # by hand it can be
    assert audiobook_xref.xref(cfg)["bk"]["audiobook_local"] is True


def test_the_search_index_follows_the_local_rows(lib):
    from epublib import search_index

    cfg, root = lib
    af.m4b(root / "x.m4b", title="とても珍しい題名")
    ab_id = next(iter(scanner.scan(cfg)))
    search_index.build(cfg)
    assert search_index.search(cfg, "珍しい題", "catalog", 10) == [ab_id]
    json.dumps(load_json(cfg.audiobook_tags_cache, {}))  # the cache is plain JSON
