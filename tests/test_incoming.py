"""Filing uploads into the library: language folder + duplicate detection."""
import json
import zipfile

import pytest
from conftest import write_epub
from epublib import incoming

JA = "ほんもんのはなし。これはにほんごのほんです。"


def _epub(path, *, title="Test Book", lang="ja", author=None, isbn=None, text=JA):
    """write_epub's fixed OPF has no author/isbn, so rewrite the OPF here."""
    write_epub(path, [text])
    extra = ""
    if author:
        extra += f"<dc:creator>{author}</dc:creator>"
    if isbn:
        extra += f'<dc:identifier opf:scheme="ISBN" xmlns:opf="http://www.idpf.org/2007/opf">{isbn}</dc:identifier>'
    opf = (
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
        'unique-identifier="bookid"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'<dc:title>{title}</dc:title><dc:language>{lang}</dc:language>'
        f'<dc:identifier id="bookid">urn:uuid:{title}</dc:identifier>{extra}</metadata>'
        '<manifest><item id="doc1" href="doc1.xhtml" media-type="application/xhtml+xml"/></manifest>'
        '<spine><itemref idref="doc1"/></spine></package>'
    )
    src = path.read_bytes()
    path.unlink()
    import io
    with zipfile.ZipFile(io.BytesIO(src)) as zin, zipfile.ZipFile(path, "w") as zout:
        for item in zin.infolist():
            zout.writestr(item.filename, opf if item.filename.endswith(".opf") else zin.read(item))
    return path.read_bytes()


def _add(cfg, tmp_path, name, language="auto", force=False, **kw):
    content = _epub(tmp_path / "src.epub", **kw)
    library = json.loads(cfg.library_index.read_text()) if cfg.library_index.exists() else {}
    return incoming.store_incoming_epub(cfg, library, content, name, language, force)


def test_auto_detect_files_into_the_language_folder(cfg, tmp_path):
    ja = _add(cfg, tmp_path, "吾輩は猫である.epub", title="吾輩は猫である")
    en = _add(cfg, tmp_path, "Some Book.epub", title="Some Book", lang="en")
    assert ja.parent == (cfg.epub_dir / "Japanese").resolve()
    assert en.parent == (cfg.epub_dir / "English").resolve()


def test_explicit_language_overrides_detection(cfg, tmp_path):
    p = _add(cfg, tmp_path, "吾輩は猫である.epub", language="en")
    assert p.parent == (cfg.epub_dir / "English").resolve()


def test_unsafe_name_is_sanitized_and_collisions_get_a_counter(cfg, tmp_path):
    a = _add(cfg, tmp_path, "../../evil/x.epub", language="ja")
    b = _add(cfg, tmp_path, "../../evil/x.epub", language="ja", force=True)
    assert a.parent == b.parent == (cfg.epub_dir / "Japanese").resolve()
    assert {a.name, b.name} == {"x.epub", "x_1.epub"}


def test_oversize_rejected(cfg, monkeypatch):
    monkeypatch.setattr(incoming, "MAX_BYTES", 10)
    with pytest.raises(ValueError, match="100 MB"):
        incoming.store_incoming_epub(cfg, {}, b"x" * 11, "a.epub")


def _library_with(cfg, tmp_path, name, **kw):
    """Add a book and index it the way the scanner would."""
    from epublib import scanner
    _add(cfg, tmp_path, name, **kw)
    return scanner.scan(cfg)


def test_identical_bytes_is_a_duplicate_then_force_adds(cfg, tmp_path):
    lib = _library_with(cfg, tmp_path, "a.epub", language="ja")
    bid = next(iter(lib))
    content = (cfg.epub_dir / lib[bid]["path"]).read_bytes()
    with pytest.raises(incoming.DuplicateEpub) as e:
        incoming.store_incoming_epub(cfg, lib, content, "renamed.epub", "ja")
    assert e.value.book_id == bid and e.value.reason == "identical file"
    assert incoming.store_incoming_epub(cfg, lib, content, "renamed.epub", "ja", force=True).exists()


def test_entry_without_hash_is_still_matched_by_content(cfg, tmp_path):
    lib = _library_with(cfg, tmp_path, "a.epub", language="ja")
    for entry in lib.values():
        entry.pop("sha256")  # an index scanned before the column existed
    content = (cfg.epub_dir / next(iter(lib.values()))["path"]).read_bytes()
    with pytest.raises(incoming.DuplicateEpub):
        incoming.store_incoming_epub(cfg, lib, content, "b.epub", "ja")


def test_same_isbn_is_a_duplicate(cfg, tmp_path):
    lib = _library_with(cfg, tmp_path, "a.epub", language="ja", title="Alpha", isbn="9784101010014")
    other = _epub(tmp_path / "o.epub", title="Different", isbn="978-4-10-101001-4", text="べつのほん。")
    with pytest.raises(incoming.DuplicateEpub) as e:
        incoming.store_incoming_epub(cfg, lib, other, "b.epub", "ja")
    assert e.value.reason == "same ISBN"


def test_same_title_and_author_is_a_duplicate_but_title_alone_is_not(cfg, tmp_path):
    lib = _library_with(cfg, tmp_path, "a.epub", language="ja", title="Alpha", author="Ｓａｋｕｒａ")
    same = _epub(tmp_path / "o.epub", title="Alpha (文庫)", author="Sakura", text="べつ。")
    with pytest.raises(incoming.DuplicateEpub) as e:
        incoming.store_incoming_epub(cfg, lib, same, "b.epub", "ja")
    assert e.value.reason == "same title and author"
    other_author = _epub(tmp_path / "p.epub", title="Alpha", author="Someone Else", text="べつ。")
    assert incoming.store_incoming_epub(cfg, lib, other_author, "c.epub", "ja").exists()


def test_the_same_author_spelled_differently_is_still_a_duplicate(cfg, tmp_path):
    lib = _library_with(cfg, tmp_path, "a.epub", language="ja", title="Alpha", author="Bryson Bill")
    flipped = _epub(tmp_path / "o.epub", title="Alpha", author="Bryson, Bill;", text="べつ。")
    with pytest.raises(incoming.DuplicateEpub) as e:
        incoming.store_incoming_epub(cfg, lib, flipped, "b.epub", "ja")
    assert e.value.reason == "same title and author"


def test_distinct_book_is_not_a_duplicate(cfg, tmp_path):
    lib = _library_with(cfg, tmp_path, "a.epub", language="ja", title="Alpha")
    other = _epub(tmp_path / "o.epub", title="Beta", text="べつのほん。")
    assert incoming.store_incoming_epub(cfg, lib, other, "b.epub", "ja").exists()


def test_upload_endpoint_files_by_language_then_409s_on_reupload_until_forced(server, monkeypatch):
    import asyncio
    import io

    from fastapi import UploadFile
    cfg = server.cfg
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.epub_dir.mkdir(parents=True, exist_ok=True)
    queued = []
    monkeypatch.setattr(server._book_jobs, "enqueue", lambda bid: queued.append(bid))
    monkeypatch.setattr(server.build_mod, "build", lambda c: None)
    src = cfg.epub_dir.parent / "src.epub"
    content = _epub(src, title="吾輩は猫である")

    def upload(force=False):
        f = UploadFile(io.BytesIO(content), filename="吾輩は猫である.epub")
        return asyncio.run(server.upload_book(f, "auto", force, False))

    r = upload()
    body = json.loads(r.body)
    assert r.status_code == 200 and queued == [body["book_id"]]
    assert (cfg.epub_dir / "Japanese" / "吾輩は猫である.epub").exists()
    assert not (cfg.epub_dir / "吾輩は猫である.epub").exists()

    r = upload()
    dup = json.loads(r.body)
    assert r.status_code == 409 and dup["status"] == "duplicate"
    assert dup["existing"]["book_id"] == body["book_id"]
    assert len(queued) == 1  # nothing was filed or queued

    assert upload(force=True).status_code == 200
    assert (cfg.epub_dir / "Japanese" / "吾輩は猫である_1.epub").exists()


def test_deferred_uploads_are_held_then_released_as_one_batch(server, monkeypatch):
    import asyncio
    import io

    from fastapi import UploadFile
    from epublib import schemas
    cfg = server.cfg
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.epub_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(server.build_mod, "build", lambda c: None)
    held, queued = [], {}
    monkeypatch.setattr(server._book_jobs, "defer", lambda bid: held.append(bid))
    monkeypatch.setattr(server._book_jobs, "enqueue", lambda bid: queued.setdefault("single", bid))
    monkeypatch.setattr(server._book_jobs, "release",
                        lambda extra=(): queued.setdefault("batch", list(extra)) and {b: {} for b in extra})

    ids = []
    for i, title in enumerate(("巻の一", "巻の二")):
        content = _epub(cfg.epub_dir.parent / f"s{i}.epub", title=title, text=f"べつのほん{i}。")
        f = UploadFile(io.BytesIO(content), filename=f"{title}.epub")
        r = json.loads(asyncio.run(server.upload_book(f, "auto", False, True)).body)
        ids.append(r["book_id"])
    assert held == ids and "single" not in queued          # nothing ran per file

    out = json.loads(asyncio.run(server.analyze_batch(
        schemas.AnalyzeBatchRequest(book_ids=ids + ["nope"]))).body)
    assert queued["batch"] == ids and set(out["jobs"]) == set(ids)   # unknown id skipped


# ── POST /api/acquire: download by URL ─────────────────────────────────

class _Download:
    """A streamed `requests` response: what _fetch_epub reads."""

    def __init__(self, body: bytes, headers: dict):
        self.body, self.headers = body, headers

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, size):
        for i in range(0, len(self.body), size):
            yield self.body[i:i + size]


def _acquire_client(server, monkeypatch, body: bytes, headers: dict, requested: list):
    from fastapi.testclient import TestClient

    cfg = server.cfg
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.epub_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(server.build_mod, "build", lambda c: None)
    monkeypatch.setattr(server._book_jobs, "enqueue", lambda bid: None)

    def fake_get(url, **kwargs):
        requested.append(url)
        return _Download(body, headers)

    monkeypatch.setattr(server.req_lib, "get", fake_get)
    return TestClient(server.app)


def test_acquire_files_the_download_then_409s_until_forced(server, monkeypatch):
    content = _epub(server.cfg.epub_dir.parent / "src.epub", title="吾輩は猫である")
    headers = {"content-type": "application/epub+zip",
               "content-disposition": "attachment; filename*=UTF-8''%E7%8C%AB.epub"}
    c = _acquire_client(server, monkeypatch, content, headers, requested := [])
    body = {"url": "https://example.com/dl?id=1"}

    r = c.post("/api/acquire", json=body)
    assert r.status_code == 200 and r.json()["status"] == "ok"
    added = r.json()["book_id"]
    assert requested == ["https://example.com/dl?id=1"]
    # Named from Content-Disposition, filed by the book's own language.
    assert (server.cfg.epub_dir / "Japanese" / "猫.epub").read_bytes() == content

    r = c.post("/api/acquire", json=body)
    assert r.status_code == 409 and r.json()["status"] == "duplicate"
    assert r.json()["existing"]["book_id"] == added

    assert c.post("/api/acquire", json={**body, "force": True}).status_code == 200
    assert (server.cfg.epub_dir / "Japanese" / "猫_1.epub").exists()


@pytest.mark.parametrize("url, headers, body, detail", [
    ("ftp://example.com/a.epub", {}, b"x", "http or https"),
    ("https://example.com/page.html", {"content-type": "text/html"}, b"x", "does not appear to be an EPUB"),
    ("https://example.com/a.epub", {"content-length": "200"}, b"x", "100 MB"),   # declared too big
    ("https://example.com/a.epub", {}, b"x" * 200, "100 MB"),                    # streamed too big
])
def test_acquire_rejects_bad_urls_and_oversize_downloads(server, monkeypatch, url, headers, body, detail):
    monkeypatch.setattr(incoming, "MAX_BYTES", 100)
    c = _acquire_client(server, monkeypatch, body, headers, [])
    r = c.post("/api/acquire", json={"url": url})
    assert r.status_code == 400 and detail in r.json()["detail"]
    assert not any(server.cfg.epub_dir.rglob("*.epub"))


def test_acquire_reports_a_failed_download(server, monkeypatch):
    import requests

    c = _acquire_client(server, monkeypatch, b"", {}, [])

    def unreachable(url, **kwargs):
        raise requests.ConnectionError("Name or service not known")

    monkeypatch.setattr(server.req_lib, "get", unreachable)
    r = c.post("/api/acquire", json={"url": "https://nowhere.invalid/a.epub"})
    assert r.status_code == 400 and r.json()["detail"].startswith("Download failed")
