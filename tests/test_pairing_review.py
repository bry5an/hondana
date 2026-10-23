"""The /pair review page's block list: unlink writes a block, unblock lifts it."""
from epublib import pairing
from epublib.index_store import load_json, save_json


def _book(bid, title, lang, author):
    return {"book_id": bid, "title": title, "author": author, "language": lang,
            "path": f"{bid}.epub", "identifiers": {"isbn": bid}}


def _seed(cfg):
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    save_json(cfg.library_index, {
        "en1": _book("en1", "Night Bus", "en", "Yoru Sumino"),
        "jp1": _book("jp1", "夜のバス", "ja", "住野よる"),
    })


def test_unlink_is_listed_as_blocked_and_unblock_clears_it(cfg):
    _seed(cfg)

    assert pairing.compute_suggestions(cfg)["blocked"] == []

    pairing.unlink(cfg, "isbn:en1", "isbn:jp1")
    blocked = pairing.compute_suggestions(cfg)["blocked"]
    assert [(b["en"]["title"], b["jp"]["title"]) for b in blocked] == [("Night Bus", "夜のバス")]
    # unlink never lists the pair as a candidate again, which is why it needs a way back
    assert all(c["book_id"] != "en1" for s in pairing.compute_suggestions(cfg)["suggestions"]
               for c in s["candidates"])

    pairing.unblock(cfg, "isbn:en1", "isbn:jp1")
    assert pairing.compute_suggestions(cfg)["blocked"] == []
    ov = load_json(cfg.pairing_overrides, {})
    assert ov["block"] == [] and ov["force"] == []  # lifted, not forced


def test_blocked_skips_entries_for_books_no_longer_in_the_library(cfg):
    _seed(cfg)
    save_json(cfg.pairing_overrides, {"force": [], "block": [{"en": "isbn:gone", "jp": "isbn:jp1"}]})
    assert pairing.compute_suggestions(cfg)["blocked"] == []


def test_candidate_signal_says_author_match_only_at_the_auto_pair_bar(cfg):
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    save_json(cfg.library_index, {
        # two English books by the same author: ambiguous, so not auto-paired, but a real author match
        "en1": _book("en1", "Night Bus", "en", "Yoru Sumino"),
        "en2": _book("en2", "Night Train", "en", "Yoru Sumino"),
        "en3": _book("en3", "Sapiens", "en", "Yuval Noah Harari"),
        "jp1": _book("jp1", "夜のバス", "ja", "住野よる"),
        "jp2": _book("jp2", "さいはての彼女", "ja", "原田 マハ"),
    })
    by_jp = {s["jp"]["book_id"]: {c["book_id"]: c for c in s["candidates"]}
             for s in pairing.compute_suggestions(cfg)["suggestions"]}
    assert by_jp["jp1"]["en1"]["signal"] == "author" and by_jp["jp1"]["en2"]["signal"] == "author"
    weak = [c for c in by_jp["jp2"].values()]
    assert weak and all(c["signal"] == "weak" for c in weak)


# ── a book's own other-edition state (the book page) ─────────────────────────

def _seed_two_authors(cfg):
    """jp1's author has two English books, so the match is real but ambiguous (no auto-pair)."""
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    save_json(cfg.library_index, {
        "en1": _book("en1", "Night Bus", "en", "Yoru Sumino"),
        "en2": _book("en2", "Night Train", "en", "Yoru Sumino"),
        "en3": _book("en3", "Sapiens", "en", "Yuval Noah Harari"),
        "en4": _book("en4", "Night Ferry", "en", "Yoru Sumino"),
        "jp1": _book("jp1", "夜のバス", "ja", "住野よる"),
        "xx1": {**_book("xx1", "Livre", "fr", "A"), "language": "fr"},
    })
    pairing.pair(cfg)


def test_edition_state_ranks_candidates_for_one_book_from_either_side(cfg):
    _seed_two_authors(cfg)
    jp = pairing.edition_state(cfg, "jp1")
    assert (jp["eligible"], jp["mode"], jp["partner"]) == (True, "unpaired", None)
    assert (jp["book"]["book_id"], jp["book"]["stable_key"], jp["book"]["language"]) == ("jp1", "isbn:jp1", "ja")
    assert {c["book_id"] for c in jp["candidates"]} >= {"en1", "en2", "en4"}
    assert all(c["signal"] == "author" for c in jp["candidates"] if c["book_id"] in ("en1", "en2"))
    assert {b["book_id"] for b in jp["others"]} == {"en1", "en2", "en3", "en4"}

    en = pairing.edition_state(cfg, "en1")   # from the English side the candidates are Japanese
    assert [c["book_id"] for c in en["candidates"]] == ["jp1"] and en["candidates"][0]["signal"] == "author"
    assert [b["book_id"] for b in en["others"]] == ["jp1"]


def test_edition_state_not_eligible_outside_ja_en_or_unknown(cfg):
    _seed_two_authors(cfg)
    for bid in ("xx1", "nope"):
        s = pairing.edition_state(cfg, bid)
        assert s["eligible"] is False and s["mode"] is None and s["candidates"] == []


def test_edition_state_reports_auto_and_manual_partners(cfg):
    _seed(cfg)                      # one en, one ja, same author: the scanner pairs them
    pairing.pair(cfg)
    s = pairing.edition_state(cfg, "jp1")
    assert (s["mode"], s["partner"]["book_id"], s["candidates"], s["others"]) == ("auto", "en1", [], [])
    assert s["book"]["stable_key"] == "isbn:jp1"

    pairing.unlink(cfg, "isbn:en1", "isbn:jp1")
    pairing.link(cfg, "isbn:en1", "isbn:jp1")
    pairing.pair(cfg)
    assert pairing.edition_state(cfg, "en1")["mode"] == "manual"


def test_not_this_one_removes_only_that_candidate(cfg):
    _seed_two_authors(cfg)
    pairing.unlink(cfg, "isbn:en1", "isbn:jp1")             # "Not this one" for Night Bus
    pairing.pair(cfg)
    ids = [c["book_id"] for c in pairing.edition_state(cfg, "jp1")["candidates"]]
    assert "en1" not in ids and {"en2", "en4"} <= set(ids)


def test_rejecting_one_of_two_candidates_lets_the_scanner_pair_the_other(cfg):
    """Pinned so the book page's "Not this one" is built knowing it: the scanner auto-pairs
    only an unambiguous match, and rejecting one of two leaves the other unambiguous."""
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    save_json(cfg.library_index, {
        "en1": _book("en1", "Night Bus", "en", "Yoru Sumino"),
        "en2": _book("en2", "Night Train", "en", "Yoru Sumino"),
        "jp1": _book("jp1", "夜のバス", "ja", "住野よる"),
    })
    pairing.pair(cfg)
    assert pairing.edition_state(cfg, "jp1")["mode"] == "unpaired"
    pairing.unlink(cfg, "isbn:en1", "isbn:jp1")
    pairing.pair(cfg)
    s = pairing.edition_state(cfg, "jp1")
    assert (s["mode"], s["partner"]["book_id"]) == ("auto", "en2")


def test_none_stops_pairing_and_suggestions_and_clear_restores_them(cfg):
    _seed(cfg)
    pairing.set_none(cfg, "isbn:jp1")
    pairing.pair(cfg)                                       # would have auto-paired
    assert load_json(cfg.pairings_index, {}) == {}
    s = pairing.edition_state(cfg, "jp1")
    assert (s["mode"], s["candidates"]) == ("none", []) and [b["book_id"] for b in s["others"]] == ["en1"]
    sugg = pairing.compute_suggestions(cfg)
    assert sugg["suggestions"] == [] and [b["book_id"] for b in sugg["no_edition"]] == ["jp1"]
    # the other side is not offered a book that has no other edition
    assert pairing.edition_state(cfg, "en1")["candidates"] == []

    pairing.clear_none(cfg, "isbn:jp1")
    pairing.pair(cfg)
    assert pairing.edition_state(cfg, "jp1")["mode"] == "auto"
    assert pairing.compute_suggestions(cfg)["no_edition"] == []


def test_link_supersedes_none_and_unlink_without_block_leaves_no_block(cfg):
    _seed(cfg)
    pairing.set_none(cfg, "isbn:jp1")
    pairing.set_none(cfg, "isbn:en1")
    pairing.link(cfg, "isbn:en1", "isbn:jp1")
    ov = load_json(cfg.pairing_overrides, {})
    assert ov["none"] == [] and ov["force"] == [{"en": "isbn:en1", "jp": "isbn:jp1"}]

    pairing.unlink(cfg, "isbn:en1", "isbn:jp1", block=False)    # undo a link just made
    ov = load_json(cfg.pairing_overrides, {})
    assert ov["force"] == [] and ov["block"] == []


def test_none_entries_for_books_no_longer_in_the_library_are_ignored(cfg):
    _seed(cfg)
    pairing.set_none(cfg, "isbn:gone")
    pairing.pair(cfg)
    assert pairing.compute_suggestions(cfg)["no_edition"] == []
    assert pairing.edition_state(cfg, "jp1")["mode"] == "auto"
