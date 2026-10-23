"""Curated state follows a book across a rename / move / replace."""
import os
import shutil
import zipfile

from conftest import write_epub
from epublib import rekey, scanner
from epublib.index_store import load_json, save_json


def _curate(cfg, bid):
    save_json(cfg.title_overrides, {bid: "My Title"})
    save_json(cfg.genre_overrides, {bid: "児童書"})
    save_json(cfg.natively_index, {bid: {"natively_level": 30}})
    save_json(cfg.read_books, {"books": {bid: {"read": True, "read_at": "2026-01-01"}}})


def _assert_followed(cfg, new):
    assert load_json(cfg.title_overrides, {}) == {new: "My Title"}
    assert load_json(cfg.genre_overrides, {}) == {new: "児童書"}
    assert set(load_json(cfg.natively_index, {})) == {new}
    assert set(load_json(cfg.read_books, {})["books"]) == {new}


def _scan_one(cfg, rel, paragraphs=("こんにちは",)):
    write_epub(cfg.epub_dir / rel, list(paragraphs))
    return scanner.scan(cfg)


def test_moving_a_file_between_folders_keeps_its_state(cfg):
    cfg.epub_dir.mkdir(parents=True)
    (bid,) = _scan_one(cfg, "a.epub")
    _curate(cfg, bid)

    (cfg.epub_dir / "Japanese").mkdir()
    shutil.move(cfg.epub_dir / "a.epub", cfg.epub_dir / "Japanese" / "a.epub")
    (new,) = scanner.scan(cfg)
    assert new != bid
    _assert_followed(cfg, new)


def test_replacing_a_file_in_place_keeps_its_state(cfg):
    cfg.epub_dir.mkdir(parents=True)
    (bid,) = _scan_one(cfg, "a.epub")
    _curate(cfg, bid)

    (new,) = _scan_one(cfg, "a.epub", ["こんにちは", "世界、corrected copy with a different size"])
    assert new != bid
    _assert_followed(cfg, new)


def test_renaming_a_file_keeps_its_state_via_opf_identifier(cfg):
    cfg.epub_dir.mkdir(parents=True)
    (bid,) = _scan_one(cfg, "a.epub")
    _curate(cfg, bid)
    os.rename(cfg.epub_dir / "a.epub", cfg.epub_dir / "renamed.epub")
    (new,) = scanner.scan(cfg)
    assert new != bid
    _assert_followed(cfg, new)


def test_unrelated_new_file_does_not_inherit_state(cfg):
    cfg.epub_dir.mkdir(parents=True)
    (bid,) = _scan_one(cfg, "a.epub")
    _curate(cfg, bid)
    os.remove(cfg.epub_dir / "a.epub")
    other = cfg.epub_dir / "b.epub"
    write_epub(other, ["全く別の本"])
    with zipfile.ZipFile(other) as zf:
        files = {n: zf.read(n) for n in zf.namelist()}
    # Give the new book a different identity so no signal matches the old one.
    files["OEBPS/content.opf"] = (
        files["OEBPS/content.opf"].replace(b"urn:uuid:test", b"urn:uuid:other")
        .replace(b"Test Book", b"Another Book")
    )
    with zipfile.ZipFile(other, "w") as zf:
        for n, d in files.items():
            zf.writestr(n, d)
    (new,) = scanner.scan(cfg)
    assert new != bid
    assert set(load_json(cfg.read_books, {})["books"]) == {bid}


def test_match_moves_tiers_and_ambiguity():
    v = {
        "o1": {"path": "x.epub", "title": "T", "author": "A", "language": "ja",
               "identifiers": {"isbn": "111"}},
        "o2": {"path": "y.epub", "title": "Same", "author": "Au", "language": "ja"},
        "o3": {"path": "z.epub", "title": "Same", "author": "Au", "language": "ja"},
    }
    f = {
        "n1": {"path": "sub/renamed.epub", "title": "Other", "author": "B",
               "language": "ja", "identifiers": {"isbn": "111"}},   # isbn → o1
        "n2": {"path": "sub/y2.epub", "title": "Same", "author": "Au", "language": "ja"},
    }
    # n2 matches both o2 and o3 on title+author: ambiguous → no migration.
    assert rekey.match_moves(v, f) == {"o1": "n1"}


def test_rekey_never_overwrites_state_already_on_the_new_id(cfg):
    save_json(cfg.title_overrides, {"old": "from old", "new": "already here"})
    rekey.rekey_book(cfg, "old", "new")
    assert load_json(cfg.title_overrides, {}) == {"new": "already here"}


def test_recover_orphans_after_a_folder_move(cfg):
    cfg.epub_dir.mkdir(parents=True)
    (cfg.epub_dir / "Japanese").mkdir()
    write_epub(cfg.epub_dir / "Japanese" / "a.epub", ["こんにちは"])
    library = scanner.scan(cfg)
    (new,) = library
    size = library[new]["filesize"]
    old = scanner.book_id_for("a.epub", size)  # it used to live in the root
    _curate(cfg, old)

    assert rekey.recover_orphans(cfg, library) == {old: new}
    _assert_followed(cfg, new)
    assert rekey.recover_orphans(cfg, library) == {}  # idempotent
