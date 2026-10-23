"""/api/books/{id}/edition and the pairing endpoints behind the book page's other-edition prompt."""
import json

import pytest
from fastapi import HTTPException

from epublib import schemas
from epublib.index_store import load_json, save_json


def _book(bid, title, lang, author, **extra):
    return {"book_id": bid, "title": title, "author": author, "language": lang,
            "path": f"{bid}.epub", "identifiers": {"isbn": bid}, **extra}


@pytest.fixture
def lib(server, monkeypatch):
    cfg = server.cfg
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    save_json(cfg.library_index, {
        "en1": _book("en1", "Night Bus", "en", "Yoru Sumino"),
        "en2": _book("en2", "Night Train", "en", "Yoru Sumino"),
        "en4": _book("en4", "Night Ferry", "en", "Yoru Sumino"),
        "jp1": _book("jp1", "夜のバス", "ja", "住野よる"),
        "fr1": _book("fr1", "Livre", "fr", "A"),
    })
    monkeypatch.setattr(server.build_mod, "build", lambda c: None)   # pairing is what's under test
    server.pairing_mod.pair(cfg)
    return server


def _body(resp):
    return json.loads(resp.body)


def _error(call) -> HTTPException:
    with pytest.raises(HTTPException) as e:
        call()
    return e.value


def test_get_edition_returns_state_and_404s_unknown(lib):
    s = _body(lib.get_edition("jp1"))
    assert s["mode"] == "unpaired" and {c["book_id"] for c in s["candidates"]} == {"en1", "en2", "en4"}
    assert _error(lambda: lib.get_edition("nope")).status_code == 404
    assert _body(lib.get_edition("fr1"))["eligible"] is False


def test_none_set_and_clear_round_trip_through_the_endpoints(lib):
    s = _body(lib.set_edition_none("jp1"))
    assert (s["mode"], s["candidates"]) == ("none", [])
    assert load_json(lib.cfg.pairing_overrides, {})["none"] == ["isbn:jp1"]
    s = _body(lib.clear_edition_none("jp1"))
    assert s["mode"] == "unpaired" and load_json(lib.cfg.pairing_overrides, {})["none"] == []


def test_none_is_refused_for_a_paired_book_and_for_other_languages(lib):
    lib.pairing_link(schemas.PairingKeysRequest(en_key="isbn:en1", jp_key="isbn:jp1"))
    e = _error(lambda: lib.set_edition_none("jp1"))
    assert e.status_code == 409 and "Unlink" in e.detail
    assert load_json(lib.cfg.pairing_overrides, {}).get("none", []) == []
    assert _error(lambda: lib.set_edition_none("fr1")).status_code == 400


def test_link_then_undo_leaves_no_block_but_not_this_one_does(lib):
    cfg = lib.cfg
    keys = dict(en_key="isbn:en1", jp_key="isbn:jp1")
    lib.pairing_link(schemas.PairingKeysRequest(**keys))
    assert _body(lib.get_edition("jp1"))["mode"] == "manual"

    lib.pairing_unlink(schemas.PairingUnlinkRequest(**keys, block=False))     # Undo
    assert _body(lib.get_edition("jp1"))["mode"] == "unpaired"
    assert load_json(cfg.pairing_overrides, {})["block"] == []

    r = lib.pairing_block(schemas.PairingKeysRequest(**keys))                  # Not this one
    assert r["status"] == "blocked"
    assert sorted(c["book_id"] for c in _body(lib.get_edition("jp1"))["candidates"]) == ["en2", "en4"]
    assert load_json(cfg.pairing_overrides, {})["block"] == [{"en": "isbn:en1", "jp": "isbn:jp1"}]


def test_unlink_defaults_to_blocking(lib):
    keys = dict(en_key="isbn:en1", jp_key="isbn:jp1")
    lib.pairing_link(schemas.PairingKeysRequest(**keys))
    lib.pairing_unlink(schemas.PairingUnlinkRequest(**keys))
    assert load_json(lib.cfg.pairing_overrides, {})["block"] == [{"en": "isbn:en1", "jp": "isbn:jp1"}]
