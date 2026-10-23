"""Hand-set book covers: the upload/reset endpoints, build() laying the cover
over the EPUB's own, and the override surviving rescans, renames and deletes."""
import asyncio
import io
import json

import pytest
from conftest import write_epub
from fastapi import HTTPException, UploadFile
from PIL import Image

from epublib import build, custom_cover, purge, rekey, scanner
from epublib.index_store import load_json, save_json


def _img(fmt="PNG", size=(40, 60), color="red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, fmt)
    return buf.getvalue()


def _upload(raw: bytes, name="cover.png") -> UploadFile:
    return UploadFile(file=io.BytesIO(raw), filename=name)


def _call(coro):
    return json.loads(asyncio.run(coro).body)


def _error(coro) -> HTTPException:
    with pytest.raises(HTTPException) as e:
        asyncio.run(coro)
    return e.value


def _scan_one(cfg, rel="a.epub"):
    cfg.epub_dir.mkdir(parents=True, exist_ok=True)
    write_epub(cfg.epub_dir / rel, ["こんにちは"])
    (bid,) = scanner.scan(cfg)
    # write_epub has no cover; give the entry one the way the scanner would.
    index = load_json(cfg.library_index, {})
    (cfg.covers_dir).mkdir(parents=True, exist_ok=True)
    (cfg.covers_dir / f"{bid}.jpg").write_bytes(_img("JPEG"))
    index[bid]["cover"] = f"covers/{bid}.jpg"
    save_json(cfg.library_index, index)
    return bid


def _book(cfg, bid):
    return load_json(cfg.library_index, {})[bid]


# ── custom_cover: validation + storage ──────────────────────────────────────

def test_ext_comes_from_the_bytes_not_the_filename():
    assert custom_cover.detect_ext(_img("PNG")) == ".png"
    assert custom_cover.detect_ext(_img("JPEG")) == ".jpg"
    assert custom_cover.detect_ext(_img("WEBP")) == ".webp"


@pytest.mark.parametrize("raw", [
    b"not an image",
    b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"/>',
    b"",
])
def test_non_images_are_rejected(raw):
    with pytest.raises(custom_cover.InvalidCover):
        custom_cover.detect_ext(raw)


def test_oversize_is_rejected(monkeypatch):
    monkeypatch.setattr(custom_cover, "MAX_BYTES", 10)
    with pytest.raises(custom_cover.InvalidCover, match="limit"):
        custom_cover.detect_ext(_img())


def test_replacing_with_another_format_leaves_one_file(cfg):
    custom_cover.store(cfg, "b1", _img("PNG"))
    custom_cover.store(cfg, "b1", _img("JPEG"))
    assert [p.name for p in cfg.custom_covers_dir.iterdir()] == ["b1.jpg"]
    assert load_json(cfg.cover_overrides, {}) == {"b1": {"ext": ".jpg"}}


def test_a_rejected_upload_changes_nothing(cfg):
    custom_cover.store(cfg, "b1", _img("PNG"))
    with pytest.raises(custom_cover.InvalidCover):
        custom_cover.store(cfg, "b1", b"junk")
    assert [p.name for p in cfg.custom_covers_dir.iterdir()] == ["b1.png"]


# ── build(): override applies, resets, and survives a rescan ────────────────

def test_build_applies_and_resets_the_override(cfg):
    bid = _scan_one(cfg)
    build.build(cfg)
    own = f"covers/{bid}.jpg"
    assert _book(cfg, bid)["cover"] == own and "cover_v" not in _book(cfg, bid)

    custom_cover.store(cfg, bid, _img("PNG"))
    build.build(cfg)
    b = _book(cfg, bid)
    assert b["cover"] == f"covers/custom/{bid}.png" and b["raw_cover"] == own and b["cover_v"]

    custom_cover.clear(cfg, bid)
    build.build(cfg)
    b = _book(cfg, bid)
    assert b["cover"] == own and "cover_v" not in b
    assert (cfg.covers_dir / f"{bid}.jpg").exists()  # the EPUB's own cover was never touched


def test_override_without_its_image_falls_back_to_the_epubs_cover(cfg):
    bid = _scan_one(cfg)
    save_json(cfg.cover_overrides, {bid: {"ext": ".png"}})
    build.build(cfg)
    assert _book(cfg, bid)["cover"] == f"covers/{bid}.jpg"


def test_override_survives_a_rescan_and_a_schema_bump(cfg, monkeypatch):
    bid = _scan_one(cfg)
    custom_cover.store(cfg, bid, _img("PNG"))
    build.build(cfg)

    scanner.scan(cfg)                                  # unchanged file: skipped
    build.build(cfg)
    assert _book(cfg, bid)["cover"] == f"covers/custom/{bid}.png"

    monkeypatch.setattr(scanner, "SCHEMA_VERSION", scanner.SCHEMA_VERSION + 1)
    scanner.scan(cfg)                                  # entry rewritten from scratch
    assert _book(cfg, bid)["schema"] == scanner.SCHEMA_VERSION
    build.build(cfg)
    assert _book(cfg, bid)["cover"] == f"covers/custom/{bid}.png"
    assert (cfg.custom_covers_dir / f"{bid}.png").exists()


# ── rekey / purge ───────────────────────────────────────────────────────────

def test_rekey_moves_the_flag_and_the_image(cfg):
    custom_cover.store(cfg, "old", _img("PNG"))
    rekey.rekey_book(cfg, "old", "new")
    assert load_json(cfg.cover_overrides, {}) == {"new": {"ext": ".png"}}
    assert [p.name for p in cfg.custom_covers_dir.iterdir()] == ["new.png"]


def test_rekey_never_overwrites_the_new_ids_own_cover(cfg):
    custom_cover.store(cfg, "old", _img("PNG", color="red"))
    custom_cover.store(cfg, "new", _img("PNG", color="blue"))
    before = (cfg.custom_covers_dir / "new.png").read_bytes()
    rekey.rekey_book(cfg, "old", "new")
    assert (cfg.custom_covers_dir / "new.png").read_bytes() == before


def test_a_file_move_keeps_the_hand_set_cover(cfg):
    bid = _scan_one(cfg)
    custom_cover.store(cfg, bid, _img("PNG"))
    (cfg.epub_dir / "Japanese").mkdir()
    (cfg.epub_dir / "a.epub").rename(cfg.epub_dir / "Japanese" / "a.epub")
    (new,) = scanner.scan(cfg)
    assert new != bid
    build.build(cfg)
    assert _book(cfg, new)["cover"] == f"covers/custom/{new}.png"
    assert not (cfg.custom_covers_dir / f"{bid}.png").exists()


def test_purge_drops_the_custom_cover_only_when_curated(cfg):
    custom_cover.store(cfg, "b1", _img("PNG"))
    purge.purge_book(cfg, "b1")
    assert (cfg.custom_covers_dir / "b1.png").exists() and "b1" in load_json(cfg.cover_overrides, {})
    purge.purge_book(cfg, "b1", curated=True)
    assert not (cfg.custom_covers_dir / "b1.png").exists()
    assert load_json(cfg.cover_overrides, {}) == {}


# ── endpoints ───────────────────────────────────────────────────────────────

def _seed_server(server, cover=True):
    cfg = server.cfg
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    (cfg.covers_dir).mkdir(parents=True, exist_ok=True)
    (cfg.covers_dir / "b1.jpg").write_bytes(_img("JPEG"))
    save_json(cfg.library_index, {"b1": {
        "book_id": "b1", "title": "T", "raw_title": "T", "author": "A", "language": "ja",
        "lang_source": "default", "path": "b1.epub", "filesize": 1, "mtime": 1.0,
        "cover": "covers/b1.jpg" if cover else None}})
    return cfg


def test_cover_endpoints_round_trip(server):
    cfg = _seed_server(server)
    r = _call(server.set_book_cover("b1", _upload(_img("PNG"))))
    assert r["cover"]["overridden"] and r["cover"]["v"]
    assert _book(cfg, "b1")["cover"] == "covers/custom/b1.png"

    r = _call(server.reset_book_cover("b1"))
    assert r["cover"] == {"has": True, "overridden": False, "v": None}
    assert _book(cfg, "b1")["cover"] == "covers/b1.jpg"


def test_a_book_with_no_cover_can_be_given_one(server):
    cfg = _seed_server(server, cover=False)
    assert server.get_book_details("b1")["cover"] == {"has": False, "overridden": False, "v": None}
    r = _call(server.set_book_cover("b1", _upload(_img("PNG"))))
    assert r["cover"]["has"] and _book(cfg, "b1")["cover"] == "covers/custom/b1.png"


def test_cover_endpoint_rejects_bad_input_without_writing(server):
    cfg = _seed_server(server)
    assert _error(server.set_book_cover("b1", _upload(b"junk"))).status_code == 400
    assert _error(server.set_book_cover("nope", _upload(_img()))).status_code == 404
    assert _error(server.reset_book_cover("nope")).status_code == 404
    assert not cfg.cover_overrides.exists()


def test_thumbnail_is_regenerated_after_a_replace(server):
    cfg = _seed_server(server)
    server.thumbs_mod.ensure_thumb(cfg.data_dir, "b1", cfg.covers_dir / "b1.jpg")
    assert server.thumbs_mod.thumb_path(cfg.data_dir, "b1").exists()
    _call(server.set_book_cover("b1", _upload(_img("PNG"))))
    assert not server.thumbs_mod.thumb_path(cfg.data_dir, "b1").exists()


# ── serving: GET /covers/{id} (custom covers + the immutable cache) ──────────

def _pixel(raw: bytes) -> tuple:
    with Image.open(io.BytesIO(raw)) as im:
        return im.convert("RGB").getpixel((5, 5))


def _is(rgb: tuple, color: str) -> bool:
    want = {"red": (255, 0, 0), "blue": (0, 0, 255)}[color]
    return all(abs(a - b) < 40 for a, b in zip(rgb, want))


def test_covers_route_serves_the_hand_set_cover_then_the_epubs_own(server):
    from fastapi.testclient import TestClient

    _seed_server(server)  # the EPUB's own cover is red
    c = TestClient(server.app)
    r = c.get("/covers/b1")
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"  # thumbnail
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert _is(_pixel(r.content), "red")

    blue = _img("PNG", color="blue")
    _call(server.set_book_cover("b1", _upload(blue)))
    assert c.get("/covers/b1", params={"full": 1}).content == blue
    assert _is(_pixel(c.get("/covers/b1").content), "blue")  # not the stale red thumbnail

    _call(server.reset_book_cover("b1"))
    assert _is(_pixel(c.get("/covers/b1", params={"full": 1}).content), "red")
    assert _is(_pixel(c.get("/covers/b1").content), "red")


def test_covers_route_404s_without_a_cover(server):
    from fastapi.testclient import TestClient

    cfg = _seed_server(server, cover=False)
    c = TestClient(server.app)
    assert c.get("/covers/b1").status_code == 404
    assert c.get("/covers/nope").status_code == 404
    _seed_server(server)
    (cfg.covers_dir / "b1.jpg").unlink()  # indexed but gone from disk
    assert c.get("/covers/b1").status_code == 404
