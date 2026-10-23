"""Search by romaji and kana readings."""
import sqlite3

import pytest

from epublib import romaji, search_index, textnorm
from epublib.index_store import save_json

pytestmark = pytest.mark.skipif(not romaji.available(), reason="pykakasi not installed")


@pytest.fixture
def indexed(cfg):
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    save_json(cfg.library_index, {
        "kaze": {"book_id": "kaze", "title": "風が強く吹いている", "author": "三浦 しをん"},
        "kafka": {"book_id": "kafka", "title": "海辺のカフカ", "author": "村上 春樹"},
        # a title pykakasi can't read the way the book is meant to be (the OPF knows)
        "oddly": {"book_id": "oddly", "title": "彼方", "author": "A", "title_reading": "ハルカ",
                  "author_reading": "ｱｲｳ"},
        "latin": {"book_id": "latin", "title": "Dune", "author": "Frank Herbert"},
        "pct": {"book_id": "pct", "title": "100%の力", "author": "B"},
    })
    save_json(cfg.audiobook_index, {"ab1": {"title": "ハリー・ポッターと賢者の石", "author": "J.K.ローリング",
                                            "series": "ハリー・ポッター"}})
    save_json(cfg.audiobook_data, {"B01": {"title": "ふしぎ駄菓子屋 銭天堂", "author": "廣嶋 玲子",
                                           "series": "銭天堂", "narrators": "田中 太郎",
                                           "description_full": "魔法の駄菓子"}})
    search_index.build(cfg)
    return cfg


def find(cfg, q, kind="epub"):
    return sorted(search_index.search(cfg, q, kind, 50))


@pytest.mark.parametrize("q, expected", [
    ("murakami", ["kafka"]),                  # author, romaji
    ("MURAKAMI", ["kafka"]),
    ("kafuka", ["kafka"]),                    # title, romaji
    ("murakamiharuki", ["kafka"]),            # run together
    ("かぜがつよく", ["kaze"]),                # hiragana reading
    ("カゼガツヨク", ["kaze"]),                # the same typed in katakana
    ("ｶﾌｶ", ["kafka"]),                      # half-width katakana
    ("うみべ", ["kafka"]),
    ("sumino", []),                           # not in this library
    ("村上春樹", ["kafka"]),                    # the raw text still works
    ("Herbert", ["latin"]),
    ("てすと", []),
])
def test_epub_search_by_romaji_and_kana(indexed, q, expected):
    assert find(indexed, q) == expected


def test_the_opf_reading_wins_where_the_tokenizer_would_guess_wrong(indexed):
    assert find(indexed, "はるか") == ["oddly"]       # title_reading ハルカ (pykakasi reads 彼方 as かなた)
    assert find(indexed, "あいう") == ["oddly"]       # author_reading ｱｲｳ, normalized
    assert find(indexed, "かなた") == ["oddly"]       # …and the guess is still searchable


def test_audiobooks_and_the_catalog_get_readings_too(indexed):
    assert find(indexed, "harii", "audiobooks") == ["ab1"]
    assert find(indexed, "ろーりんぐ", "audiobooks") == ["ab1"]
    assert find(indexed, "sentendou", "catalog") == ["B01"]
    assert find(indexed, "ひろしま", "catalog") == ["B01"]
    assert find(indexed, "たなか", "catalog") == ["B01"]            # narrators
    assert find(indexed, "魔法の駄菓子", "catalog") == ["B01"]       # descriptions still searchable


def test_short_queries_scan_the_readings_instead_of_returning_nothing(indexed):
    with pytest.raises(ValueError):
        search_index.search(indexed, "かぜ", "epub", 10)           # too short for trigrams, as before
    assert search_index.readings_like(indexed, "かぜ", "epub", 10) == ["kaze"]
    assert search_index.readings_like(indexed, "カゼ", "epub", 10) == ["kaze"]
    assert search_index.readings_like(indexed, "qq", "epub", 10) == []        # in no reading


def test_like_wildcards_in_a_query_are_literal(indexed):
    # a literal % matches only the title that has one — not everything, as an unescaped LIKE would
    assert search_index.readings_like(indexed, "%", "epub", 10) == ["pct"]
    assert search_index.readings_like(indexed, "_", "epub", 10) == []


def test_server_fallback_combines_substring_and_readings(server):
    cfg = server.cfg
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    save_json(cfg.library_index, {
        "kaze": {"book_id": "kaze", "title": "風が強く吹いている", "author": "三浦 しをん"},
        "plain": {"book_id": "plain", "title": "かぜ薬の本", "author": "X"},
    })
    search_index.build(cfg)
    assert sorted(server._search_local("かぜ", "epub", 10)) == ["kaze", "plain"]


def test_search_endpoint_end_to_end_and_when_the_index_breaks(server, monkeypatch):
    from fastapi.testclient import TestClient

    cfg = server.cfg
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    save_json(cfg.library_index, {
        "kafka": {"book_id": "kafka", "title": "海辺のカフカ", "author": "村上 春樹"}})
    search_index.build(cfg)
    c = TestClient(server.app)

    def get(q):
        return c.get("/api/search", params={"q": q, "kind": "epub"}).json()

    assert get("  ") == {"source": "empty", "ids": []}
    assert get("murakami") == {"source": "fts", "ids": ["kafka"]}

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("database disk image is malformed")

    monkeypatch.setattr(server.search_index_mod, "search", broken)
    # A broken index degrades to the substring pass (readings included) instead of a 500.
    assert get("カフカ") == {"source": "substring", "ids": ["kafka"]}
    assert get("むらかみ") == {"source": "substring", "ids": ["kafka"]}


def test_changing_how_readings_are_made_rebuilds_the_tables(indexed, monkeypatch):
    def signature():
        con = sqlite3.connect(indexed.search_db)
        try:
            return con.execute("select columns from meta where kind = 'catalog'").fetchone()[0]
        finally:
            con.close()
    before = signature()
    monkeypatch.setattr(search_index, "READINGS_VERSION", search_index.READINGS_VERSION + 1)
    search_index.build(indexed)
    assert signature() != before and "readings" in signature()


def test_reading_forms_and_kana_folding():
    assert romaji.reading_forms("Dune") == ()
    forms = romaji.reading_forms("ハリー・ポッター")
    assert forms == ("はりーぽったー", "harii pottaa", "hariipottaa")
    assert textnorm.to_hiragana("カゼ ｶﾌｶ ー・") == "かぜ かふか ー・"
