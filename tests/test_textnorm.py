"""Author names: display cleanup, the comparison key, and what build does with them."""
import pytest

from epublib import build, textnorm
from epublib.index_store import load_json, save_json


@pytest.mark.parametrize("raw, shown", [
    ("James Nestor;", "James Nestor"),            # OPF list punctuation
    ("Johnson, Alios", "Alios Johnson"),          # written-out Last, First
    ("Bryson, Bill,", "Bill Bryson"),
    ("Bryson Bill", "Bryson Bill"),               # ambiguous without a comma: left alone
    ("廣嶋 玲子", "廣嶋 玲子"),                      # Japanese keeps its spacing as written
    ("岸見 一郎, 古賀 史健", "岸見 一郎, 古賀 史健"),   # a CJK list is not Last, First
    ("A, B, C", "A, B, C"),                       # more than one comma: not a name flip
    ("  ", None), ("", ""), (None, None),
])
def test_clean_author_only_removes_unambiguous_noise(raw, shown):
    assert textnorm.clean_author(raw) == shown


@pytest.mark.parametrize("group", [
    ["Bill Bryson", "Bryson Bill", "Bryson, Bill", "BILL BRYSON;", "Ｂｉｌｌ Ｂｒｙｓｏｎ"],
    ["J.K. Rowling", "J. K. Rowling", "Rowling, J.K.", "rowling j k"],
    ["廣嶋 玲子", "廣嶋玲子", "廣嶋　玲子", "廣嶋玲子;"],
    ["はやみね かおる", "はやみねかおる"],
])
def test_author_key_is_one_value_per_person(group):
    assert len({textnorm.author_key(a) for a in group}) == 1


def test_author_key_keeps_different_people_apart():
    keys = [textnorm.author_key(a) for a in
            ("Bill Bryson", "Bill Nye", "廣嶋玲子", "住野よる", "J.K. Rowling", "J.R. Rowling")]
    assert len(set(keys)) == len(keys)
    assert textnorm.author_key(None) is None and textnorm.author_key(" ; ") is None


def _library(cfg, **books):
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    save_json(cfg.library_index, {
        bid: {"book_id": bid, "title": bid, "raw_title": bid, "language": "en", "lang_source": "default",
              "path": f"{bid}.epub", "filesize": 1, "mtime": 1.0, "author": a}
        for bid, a in books.items()})


def test_build_cleans_the_display_author_and_adds_a_key(cfg):
    _library(cfg, a="Bryson Bill", b="Bill Bryson;", c="Johnson, Alios", d=None)
    build.build(cfg)
    lib = load_json(cfg.library_index, {})
    assert (lib["b"]["author"], lib["b"]["raw_author"]) == ("Bill Bryson", "Bill Bryson;")
    assert lib["c"]["author"] == "Alios Johnson"
    assert lib["a"]["author_key"] == lib["b"]["author_key"] == "bill bryson"
    assert lib["d"]["author_key"] is None


def test_an_override_is_shown_as_typed_and_keyed_like_any_other_author(cfg):
    _library(cfg, a="Bryson Bill", b="Someone Else")
    save_json(cfg.author_overrides, {"b": "Bill Bryson"})
    build.build(cfg)
    lib = load_json(cfg.library_index, {})
    assert lib["b"]["author"] == "Bill Bryson" and lib["b"]["author_key"] == lib["a"]["author_key"]
    save_json(cfg.author_overrides, {})                      # reset → back to the file's value
    build.build(cfg)
    assert load_json(cfg.library_index, {})["b"]["author"] == "Someone Else"


# ── titles ─────────────────────────────────────────────────────────────

DISPLAY = [
    # leading label / imprint / edition marker, trailing edition note
    ("【大活字シリーズ】星の王子さま", "星の王子さま"),
    ("双葉社ジュニア文庫 ノベライズ この世界の片隅に", "ノベライズ この世界の片隅に"),
    ("新装版　魔女の宅急便　全６冊合本版", "魔女の宅急便"),
    ("完全版　ワンピース", "ワンピース"),
    # spaced-out kana, full-width digits and letters
    ("モ　モ", "モモ"),
    ("放課後ミステリクラブ １金魚の泳ぐプール事件", "放課後ミステリクラブ 1金魚の泳ぐプール事件"),
    ("名探偵コナン ＣＡＳＥ１", "名探偵コナン CASE1"),
    ("魔女の旅々２", "魔女の旅々2"),
    # what peel_title always did
    ("スーパーカブ【電子特別版】 (角川スニーカー文庫)", "スーパーカブ"),
    ("ハリー・ポッターと賢者の石 - Harry Potter and the Philosopher's Stone", "ハリー・ポッターと賢者の石"),
    # left alone: a bracketed volume number, an English subtitle, a title that IS a label,
    # a "文庫" with no imprint before it, kana words that merely contain a space
    ("都会のトム＆ソーヤ(1)", "都会のトム＆ソーヤ(1)"),
    ("Dune - A Novel", "Dune - A Novel"),
    ("【推しの子】", "【推しの子】"),
    ("【推しの子】 1", "【推しの子】 1"),
    ("文庫 の旅", "文庫 の旅"),
    ("ハリー　ポッター", "ハリー　ポッター"),
    ("新装版　", "新装版"),
]


@pytest.mark.parametrize("raw, shown", DISPLAY)
def test_display_title(raw, shown):
    assert textnorm.display_title(raw) == shown
    assert textnorm.display_title(shown) == shown            # idempotent


def test_peel_title_is_the_series_derivation_form_and_does_not_touch_leading_text():
    assert textnorm.peel_title("【大活字シリーズ】星の王子さま") == "【大活字シリーズ】星の王子さま"
    assert textnorm.peel_title("魔女の旅々２") == "魔女の旅々２"             # digits stay: the volume marker regexes read them
    assert textnorm.peel_title("四つ子ぐらし (角川つばさ文庫)") == "四つ子ぐらし"


@pytest.mark.parametrize("raw, query", [
    ("[3巻] 魔女の旅々", "魔女の旅々 3"),
    ("[3巻] 魔女の旅々3", "魔女の旅々3"),
    ("都会のトム＆ソーヤ(1)", "都会のトム＆ソーヤ"),                 # a bare volume number isn't searched for
    ("スーパーカブ【電子特別版】 (角川スニーカー文庫)", "スーパーカブ"),
    ("【大活字シリーズ】星の王子さま", "星の王子さま"),                # the label Natively already stripped
    ("ハリー・ポッターと賢者の石 - Harry Potter and the Philosopher's Stone", "ハリー・ポッターと賢者の石"),
])
def test_natively_query(raw, query):
    assert textnorm.natively_query(raw) == query


def test_the_match_keys_are_unchanged_by_the_move():
    assert textnorm.match_key("ふしぎ駄菓子屋　銭天堂　１３！") == "ふしぎ駄菓子屋銭天堂13"
    assert textnorm.match_key(None) == "" and textnorm.match_key("") == ""
    assert textnorm.compact_key("Ａ  b　C") == "abc"
    assert textnorm.strip_volume_prefix("[5巻] リアデイルの大地にて5") == "リアデイルの大地にて5"
    assert textnorm.strip_volume_prefix("【５巻】Ｘ") == "Ｘ"
    assert textnorm.file_title_key("[01] 銭天堂 [B0ABCDEFGH]") == "銭天堂"
    assert textnorm.file_title_key("銭天堂：（小学館文庫）") == "銭天堂"


def test_build_shows_the_display_title_and_a_hand_title_still_wins(cfg):
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    save_json(cfg.library_index, {
        bid: {"book_id": bid, "title": t, "raw_title": t, "language": "ja", "lang_source": "default",
              "path": f"{bid}.epub", "filesize": 1, "mtime": 1.0}
        for bid, t in (("a", "【大活字シリーズ】星の王子さま"), ("b", "モ　モ"))})
    save_json(cfg.title_overrides, {"b": "Momo (hand)"})
    build.build(cfg)
    lib = load_json(cfg.library_index, {})
    assert lib["a"]["title"] == "星の王子さま" and lib["a"]["raw_title"] == "【大活字シリーズ】星の王子さま"
    assert lib["b"]["title"] == "Momo (hand)"
    save_json(cfg.title_overrides, {})
    build.build(cfg)                                        # cleared → derived from raw_title again
    assert load_json(cfg.library_index, {})["b"]["title"] == "モモ"
