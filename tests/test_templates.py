"""Page shell partials: head/shell/header includes, attribute handling,
nested includes, and that every page actually uses them."""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PAGES = sorted(p.name for p in (ROOT / "templates").glob("*.html"))


def test_every_page_uses_the_shared_shell(server):
    assert len(PAGES) == 11
    for name in PAGES:
        src = (ROOT / "templates" / name).read_text(encoding="utf-8")
        for partial in ("head", "shell", "header"):
            assert f"<!-- include: {partial} " in src, f"{name} should include {partial}"
        # nothing of the shell is copied back in
        assert "epub-theme" not in src and 'id="nav-toggle"' not in src and "<style>" not in src, name


@pytest.mark.parametrize("name", PAGES)
def test_pages_render_fully_expanded(server, name):
    html = server._render(name)
    assert "<!-- include:" not in html and "{title}" not in html and "{active" not in html
    assert html.count('<nav id="sidebar"') == 1 and html.count('id="overlay"') == 1
    assert html.count('id="nav-toggle"') == 1 and html.count("<h1") == 1
    assert "fonts.googleapis.com/css2" in html and 'rel="preconnect"' in html
    assert '<link rel="shortcut icon"' in html            # pair.html never had one
    assert html.count('class="main-col"') == 1
    assert re.search(r'<script src="/static/viewstate\.js\?v=\d+">', html)   # nav.js's "back to the last view" needs it everywhere


def test_titles_and_the_active_sidebar_entry(server):
    nxt = server._render("next.html")
    assert "<title>Read Next — Hondana</title>" in nxt
    assert '<h1 id="page-title">Read Next</h1>' in nxt
    assert re.search(r'<a href="/next" class="nav-link active">', nxt)
    assert nxt.count("nav-link active") == 1

    pair = server._render("pair.html")
    assert "<h1>Editions</h1>" in pair and 'id="page-title"' not in pair
    assert re.search(r'<a href="/pair" class="nav-link active">', pair)

    assert "nav-link active" not in server._render("profile.html")  # active=none


def test_include_attributes_are_html_escaped(server, tmp_path, monkeypatch):
    page = tmp_path / "templates"
    (page / "partials").mkdir(parents=True)
    (page / "partials" / "demo.html").write_text('<i{h1id}>{title} / {missing}</i>')
    (page / "t.html").write_text('<!-- include: demo title="A & <b>" h1id=x-y -->')
    monkeypatch.setattr(server, "TEMPLATES_DIR", page)
    assert server._render("t.html") == '<i id="x-y">A &amp; &lt;b&gt; / </i>'


def test_css_is_static_and_fonts_are_not_an_import(server):
    app = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
    assert "@import" not in app
    for name in ("add", "admin", "book", "known", "next", "profile", "series", "hero"):
        assert (ROOT / "static" / f"{name}.css").exists()
    # hero.css is shared by exactly the pages that use the night hero
    users = [n for n in PAGES if "/static/hero.css" in (ROOT / "templates" / n).read_text(encoding="utf-8")]
    assert users == ["add.html", "book.html", "next.html", "series.html"]


def test_static_refs_are_versioned_by_mtime(server, tmp_path, monkeypatch):
    html = server._render("index.html")
    st = (ROOT / "static" / "app.js").stat().st_mtime_ns
    assert f'src="/static/app.js?v={st}"' in html
    assert re.search(r'href="/static/app\.css\?v=\d+"', html)
    assert re.search(r'href="/static/favicon\.ico\?v=\d+"', html)
    assert re.findall(r'(?:src|href)="/static/[^"?]+"', html) == []   # none left unversioned

    # a ref to a file that isn't there is left alone rather than 500ing the page
    page = tmp_path / "templates"
    (page / "partials").mkdir(parents=True)
    (page / "t.html").write_text('<script src="/static/nope.js"></script>')
    monkeypatch.setattr(server, "TEMPLATES_DIR", page)
    assert server._render("t.html") == '<script src="/static/nope.js"></script>'


def test_editing_an_asset_bumps_the_page_version(server, tmp_path, monkeypatch):
    static, page = tmp_path / "static", tmp_path / "templates"
    static.mkdir()
    page.mkdir()
    (page / "t.html").write_text('<script src="/static/x.js"></script>')
    (static / "x.js").write_text("1")
    monkeypatch.setattr(server, "TEMPLATES_DIR", page)
    monkeypatch.setattr(server, "STATIC_DIR", static)
    before = server._render("t.html")
    (static / "x.js").write_text("22")   # new size too, so a coarse mtime can't hide it
    assert server._render("t.html") != before


def test_static_cache_headers(server):
    from fastapi.testclient import TestClient

    c = TestClient(server.app)
    v = c.get("/static/app.css?v=123")
    assert v.status_code == 200
    assert v.headers["cache-control"] == "public, max-age=31536000, immutable"
    plain = c.get("/static/app.css")
    assert plain.headers["cache-control"] == "no-cache"
    # a revalidation that hits still carries the policy
    assert c.get("/static/app.css?v=123", headers={"If-None-Match": v.headers["etag"]}).headers[
        "cache-control"
    ] == "public, max-age=31536000, immutable"
    assert c.get("/static/nope.js?v=1").status_code == 404
