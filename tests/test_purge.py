"""purge_book removes everything keyed by a book_id."""
import os

from conftest import write_epub
from epublib import purge, scanner, thumbs
from epublib.index_store import load_json, save_json


def _seed(cfg, bid, other="keep0000000000000"):
    """Every store that holds something keyed by book_id, for `bid` and `other`."""
    d = cfg.data_dir
    for b in (bid, other):
        save_json(cfg.wordlists_dir / f"{b}.json", {"book_id": b})
        cfg.sentences_dir.mkdir(parents=True, exist_ok=True)
        (cfg.sentences_dir / f"{b}.json.gz").write_bytes(b"x")
        cfg.annotations_dir.mkdir(parents=True, exist_ok=True)
        (cfg.annotations_dir / f"{b}.json.gz").write_bytes(b"x")
        t = thumbs.thumb_path(d, b)
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_bytes(b"x")
        (d / "covers" / f"{b}.jpg").write_bytes(b"x")
    for name in ("natively_epub", "vocab_epub", "audiobook_links",
                 "title_overrides", "series_overrides", "genre_overrides"):
        save_json(d / f"{name}.json", {bid: {"v": 1}, other: {"v": 2}})
    for slug in ("aki", "ben"):
        u = cfg.users_dir / slug
        save_json(u / "known_stats.json", {bid: {}, other: {}})
        save_json(u / "read_books.json",
                  {"books": {bid: {"read": True}, other: {"read": True}}})
    save_json(cfg.pairing_overrides, {bid: "stable-key-keyed"})


def _ids(path, nested=None):
    data = load_json(path, {})
    return set(data[nested] if nested else data)


def test_purge_derived_only_keeps_curated_state(cfg):
    bid = "gone0000000000000"
    _seed(cfg, bid)
    purge.purge_book(cfg, bid, cover=f"covers/{bid}.jpg")

    d = cfg.data_dir
    assert not (cfg.wordlists_dir / f"{bid}.json").exists()
    assert not (cfg.sentences_dir / f"{bid}.json.gz").exists()
    assert not (cfg.annotations_dir / f"{bid}.json.gz").exists()
    assert (cfg.annotations_dir / "keep0000000000000.json.gz").exists()
    assert not thumbs.thumb_path(d, bid).exists()
    assert not (d / "covers" / f"{bid}.jpg").exists()
    for name in ("natively_epub", "vocab_epub", "audiobook_links"):
        assert _ids(d / f"{name}.json") == {"keep0000000000000"}
    for slug in ("aki", "ben"):
        assert _ids(cfg.users_dir / slug / "known_stats.json") == {"keep0000000000000"}
        # curated: untouched, so `rekey` can migrate it if the file was just renamed
        assert bid in _ids(cfg.users_dir / slug / "read_books.json", "books")
    for name in ("title_overrides", "series_overrides", "genre_overrides"):
        assert bid in _ids(d / f"{name}.json")
    # the other book is untouched everywhere
    assert (cfg.wordlists_dir / "keep0000000000000.json").exists()


def test_purge_curated_drops_read_status_and_overrides_in_every_profile(cfg):
    bid = "gone0000000000000"
    _seed(cfg, bid)
    purge.purge_book(cfg, bid, curated=True)

    for slug in ("aki", "ben"):
        assert _ids(cfg.users_dir / slug / "read_books.json", "books") == {"keep0000000000000"}
    for name in ("title_overrides", "series_overrides", "genre_overrides"):
        assert _ids(cfg.data_dir / f"{name}.json") == {"keep0000000000000"}
    # keyed by stable key, not book_id: never touched
    assert _ids(cfg.pairing_overrides) == {bid}


def test_purge_on_empty_data_dir_is_a_noop(cfg):
    assert purge.purge_book(cfg, "nothing00000000000", curated=True) == []
    assert not cfg.data_dir.exists() or not any(cfg.data_dir.rglob("*.tmp"))


def test_scanner_vanished_file_purges_derived_data(cfg):
    cfg.epub_dir.mkdir(parents=True)
    epub = cfg.epub_dir / "a.epub"
    write_epub(epub, ["こんにちは"])
    index = scanner.scan(cfg)
    (bid,) = index
    save_json(cfg.wordlists_dir / f"{bid}.json", {"book_id": bid})
    t = thumbs.thumb_path(cfg.data_dir, bid)
    t.parent.mkdir(parents=True, exist_ok=True)
    t.write_bytes(b"x")
    save_json(cfg.natively_index, {bid: {}})
    save_json(cfg.read_books, {"books": {bid: {"read": True}}})

    os.remove(epub)
    assert scanner.scan(cfg) == {}
    assert not (cfg.wordlists_dir / f"{bid}.json").exists()
    assert not t.exists()
    assert _ids(cfg.natively_index) == set()
    assert bid in _ids(cfg.read_books, "books")  # left for `rekey`
