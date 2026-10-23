"""cli.py gc: report and remove data keyed by books no longer in the library."""
import pytest

from epublib import orphans, thumbs
from epublib.index_store import load_json, save_json
from epublib.scanner import book_id_for

LIVE = "1111111111111111"
GONE = "9999999999999999"


def _seed(cfg, bid):
    """Something under every per-book store, for `bid`."""
    d = cfg.data_dir
    save_json(cfg.wordlists_dir / f"{bid}.json", {"book_id": bid})
    cfg.sentences_dir.mkdir(parents=True, exist_ok=True)
    (cfg.sentences_dir / f"{bid}.json.gz").write_bytes(b"x")
    cfg.annotations_dir.mkdir(parents=True, exist_ok=True)
    (cfg.annotations_dir / f"{bid}.json.gz").write_bytes(b"x")
    cfg.alignments_dir.mkdir(parents=True, exist_ok=True)
    (cfg.alignments_dir / f"{bid}.srt").write_bytes(b"x")
    (cfg.alignments_dir / f"{bid}.json").write_bytes(b"{}")
    cfg.custom_covers_dir.mkdir(parents=True, exist_ok=True)
    (cfg.custom_covers_dir / f"{bid}.png").write_bytes(b"x")
    (d / "covers" / f"{bid}.jpg").write_bytes(b"x")
    t = thumbs.thumb_path(d, bid)
    t.parent.mkdir(parents=True, exist_ok=True)
    t.write_bytes(b"x")
    for name in ("natively_epub", "vocab_epub", "audiobook_links", "title_overrides",
                 "series_overrides", "genre_overrides", "author_overrides",
                 "language_overrides", "audiobook_link_overrides", "cover_overrides"):
        path = d / f"{name}.json"
        data = load_json(path, {})
        data[bid] = {"v": 1}
        save_json(path, data)
    for slug in ("aki", "ben"):
        u = cfg.users_dir / slug
        stats = load_json(u / "known_stats.json", {})
        stats[bid] = {}
        save_json(u / "known_stats.json", stats)
        read = load_json(u / "read_books.json", {"books": {}})
        read["books"][bid] = {"read": True}
        save_json(u / "read_books.json", read)


@pytest.fixture
def lib(cfg):
    (cfg.data_dir / "covers").mkdir(parents=True, exist_ok=True)
    _seed(cfg, LIVE)
    _seed(cfg, GONE)
    save_json(cfg.pairing_overrides, {GONE: "stable-key-keyed"})
    return {LIVE: {"path": "live.epub", "filesize": 1}}


def test_find_reports_every_store_and_only_the_orphans(cfg, lib):
    found = orphans.find(cfg, lib)
    assert all(ids == [GONE] for ids in found.values())
    assert {"wordlists/", "sentences/", "annotations/", "alignments/", "covers/", "covers/thumbs/", "covers/custom/",
            "natively_epub.json", "title_overrides.json", "cover_overrides.json",
            "users/aki/read_books.json", "users/ben/known_stats.json"} <= set(found)


def test_find_is_empty_when_everything_is_live(cfg, lib):
    assert orphans.find(cfg, {LIVE: lib[LIVE], GONE: {}}) == {}


def test_titles_come_from_override_or_natively_match(cfg, lib):
    save_json(cfg.natively_index, {GONE: {"matched_title": "Natively名"}})
    assert orphans.titles(cfg, {GONE}) == {GONE: "Natively名"}
    save_json(cfg.title_overrides, {GONE: "上書き"})
    assert orphans.titles(cfg, {GONE}) == {GONE: "上書き"}


def test_dry_run_changes_nothing(cfg, lib):
    out = []
    n = orphans.run(cfg, lib, do_apply=False, out=out.append)
    assert n == 1
    assert GONE in "\n".join(out) and "Dry run" in "\n".join(out)
    assert orphans.find(cfg, lib)  # still there


def test_apply_removes_orphans_everywhere_and_keeps_the_rest(cfg, lib):
    orphans.run(cfg, lib, do_apply=True, out=lambda *_: None)
    assert orphans.find(cfg, lib) == {}
    stores = orphans.collect(cfg)
    assert [s for s, ids in stores.items() if LIVE not in ids] == []  # live book untouched
    assert load_json(cfg.pairing_overrides, {}) == {GONE: "stable-key-keyed"}
    assert not list(cfg.alignments_dir.glob(f"{GONE}.*"))
    assert (cfg.alignments_dir / f"{LIVE}.srt").exists()


def test_apply_refuses_an_empty_library(cfg, lib):
    with pytest.raises(SystemExit):
        orphans.run(cfg, {}, do_apply=True, out=lambda *_: None)
    assert load_json(cfg.natively_index, {}).keys() == {LIVE, GONE}


def test_a_moved_book_is_recovered_not_removed(cfg, lib):
    old = book_id_for("a.epub", 100)
    save_json(cfg.read_books, {"books": {old: {"read": True}}})
    save_json(cfg.title_overrides, {old: "手直し"})
    new = book_id_for("Japanese/a.epub", 100)
    (cfg.epub_dir / "Japanese").mkdir(parents=True, exist_ok=True)
    library = {**lib, new: {"path": "Japanese/a.epub", "filesize": 100}}

    orphans.run(cfg, library, do_apply=True, out=lambda *_: None)

    assert load_json(cfg.title_overrides, {})[new] == "手直し"
    assert new in load_json(cfg.read_books, {})["books"]
    assert old not in load_json(cfg.title_overrides, {})
