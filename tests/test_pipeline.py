"""The stage registry and the plumbing generated from it: CLI subcommands,
/admin stages, the per-book chain; plus the Manage-mode override endpoints, the page
route table and the render cache."""
import asyncio
import json
import os
import time

import pytest

from epublib import cli, inbox, pipeline, schemas
from epublib.index_store import load_json
from test_book_details import _seed


def _record(monkeypatch, calls):
    """Replace every module function the stages call with a recorder."""
    def rec(name):
        return lambda *a, **k: calls.append((name, k.get("refresh")))
    monkeypatch.setattr(inbox, "run_once", lambda *a, **k: calls.append(("inbox", None)) or inbox.Report())
    monkeypatch.setattr(pipeline.scanner_mod, "scan", rec("scan"))
    monkeypatch.setattr(pipeline.analyze_mod, "analyze", rec("analyze"))
    monkeypatch.setattr(pipeline.enrich_mod, "enrich", rec("enrich"))
    monkeypatch.setattr(pipeline.pairing_mod, "pair", rec("pair"))
    monkeypatch.setattr(pipeline.audiobook_scanner_mod, "scan", rec("audio-scan"))
    monkeypatch.setattr(pipeline.audiobook_scanner_mod, "build_index", rec("audio-index"))
    monkeypatch.setattr(pipeline.audiobook_catalog_mod, "fetch_missing", rec("catalog"))
    monkeypatch.setattr(pipeline.xref_mod, "xref", rec("xref"))
    monkeypatch.setattr(pipeline.build_mod, "build", rec("build"))
    monkeypatch.setattr(pipeline.epub_catalog_mod, "fetch_missing", rec("epub-catalog"))


# ── registry ─────────────────────────────────────────────────────────────────

def test_stage_order_is_the_documented_chain():
    assert pipeline.CHAIN == ("inbox → scan → analyze → enrich → pair → audio-scan → catalog → xref → build"
                              " → epub-catalog")
    assert list(pipeline.STAGE_BY_ID) == [s.id for s in pipeline.STAGES]
    assert pipeline.BOOK_CHAIN == ("analyze", "enrich", "pair", "link", "build", "epub-catalog")
    assert pipeline.CHAIN in pipeline.ALL.description


def test_run_all_runs_every_stage_in_order_and_refresh_reaches_only_enrich(monkeypatch):
    calls = []
    _record(monkeypatch, calls)
    pipeline.run_all(None)
    assert [c[0] for c in calls] == ["inbox", "scan", "build", "analyze", "enrich", "pair",
                                     "audio-scan", "audio-index", "catalog", "xref", "build", "epub-catalog"]
    calls.clear()
    pipeline.run_all(None, refresh=True)
    assert dict(calls)["enrich"] is True and dict(calls)["analyze"] is False


def test_scan_stage_rebuilds_the_index_afterwards(monkeypatch):
    calls = []
    _record(monkeypatch, calls)
    pipeline.STAGE_BY_ID["scan"].run(None)
    assert [c[0] for c in calls] == ["scan", "build"]


# ── generated consumers ──────────────────────────────────────────────────────

def test_admin_stages_are_the_registry_plus_all(server):
    assert list(server.ADMIN_STAGES) == [*pipeline.STAGE_BY_ID, "all"]
    label, desc, fn = server.ADMIN_STAGES["enrich"]
    assert label == "Enrich (Natively)" and "learnnatively" in desc
    assert server.ADMIN_STAGES["all"][2] is pipeline.run_all
    assert [s["id"] for s in json.loads(server.admin_stages().body)] == list(server.ADMIN_STAGES)


def test_admin_stages_carry_group_chips_and_chain_membership(server):
    rows = {s["id"]: s for s in json.loads(server.admin_stages().body)}
    groups = {"index", "audiobooks", "finish", "maintenance", "all"}
    assert {r["group"] for r in rows.values()} <= groups
    # the four sections /admin draws are each non-empty, and every stage is in one
    assert {r["group"] for r in rows.values() if r["id"] != "all"} == groups - {"all"}
    # the stages that sit outside "all" are exactly the in_chain=False ones
    assert {i for i, r in rows.items() if not r["in_chain"]} == {
        s.id for s in pipeline.STAGES if not s.in_chain
    }
    assert "Natively" in rows["enrich-audiobooks"]["chips"]
    assert rows["scan"]["chips"] == []


def test_admin_stages_report_when_each_last_ran_from_the_run_logs(server):
    import os

    rows = {s["id"]: s for s in json.loads(server.admin_stages().body)}
    assert all(r["last_run"] is None for r in rows.values())  # no logs yet

    server.LOG_DIR.mkdir(parents=True, exist_ok=True)
    for name, mtime in [
        ("20261001-100000-pair.log", 1_000),
        ("20261002-100000-pair.log", 2_000),       # the newer run wins
        ("20261002-110000-audio-scan.log", 3_000),  # hyphenated stage ids
        ("notes.log", 9_000),                        # not a run log
    ]:
        path = server.LOG_DIR / name
        path.write_text("")
        os.utime(path, (mtime, mtime))
    rows = {s["id"]: s for s in json.loads(server.admin_stages().body)}
    assert rows["pair"]["last_run"] == 2_000
    assert rows["audio-scan"]["last_run"] == 3_000
    assert rows["scan"]["last_run"] is None


@pytest.mark.parametrize("argv, expected", [
    (["scan"], [("scan", None), ("build", None)]),
    (["analyze", "--refresh"], [("analyze", True)]),
    (["enrich"], [("enrich", False)]),
    (["audio-scan"], [("audio-scan", None), ("audio-index", None)]),
])
def test_cli_subcommands_run_their_registry_stage(monkeypatch, argv, expected):
    calls = []
    _record(monkeypatch, calls)
    monkeypatch.setattr(cli, "_cfg", lambda args: None)
    assert cli.main(argv) == 0
    assert calls == expected


def test_cli_all_passes_refresh_to_enrich_only(monkeypatch):
    calls = []
    _record(monkeypatch, calls)
    monkeypatch.setattr(cli, "_cfg", lambda args: None)
    cli.main(["all", "--refresh"])
    assert dict(calls)["enrich"] is True and dict(calls)["analyze"] is False


def test_every_stage_has_a_cli_subcommand(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    for stage in (*pipeline.STAGES, pipeline.ALL):
        assert stage.id in out and stage.help in out


# ── override endpoints ───────────────────────────────────────────────────────

def _builds(server, monkeypatch):
    builds = []
    monkeypatch.setattr(server.build_mod, "build", lambda c: builds.append(1))
    return builds


def _call(coro):
    return json.loads(asyncio.run(coro).body)


def test_genre_and_title_overrides_set_clear_and_skip_noop_rebuilds(server, monkeypatch):
    cfg = server.cfg
    _seed(cfg)
    builds = _builds(server, monkeypatch)

    r = _call(server.set_book_genre("b1", schemas.GenreRequest(genre=" ノンフィクション ")))
    assert r["genre"] == "ノンフィクション" and len(builds) == 1
    assert load_json(cfg.genre_overrides, {}) == {"b1": "ノンフィクション"}
    _call(server.set_book_genre("b1", schemas.GenreRequest(genre="ノンフィクション")))
    assert len(builds) == 1                                  # unchanged → no rebuild
    r = _call(server.set_book_genre("b1", schemas.GenreRequest(genre="")))
    assert r["genre"] is None and load_json(cfg.genre_overrides, {}) == {}

    _call(server.set_book_title("b1", schemas.TitleRequest(title="New")))
    assert load_json(cfg.title_overrides, {}) == {"b1": "New"}
    _call(server.set_book_title("b1", schemas.TitleRequest(title="")))
    assert load_json(cfg.title_overrides, {}) == {}


def test_series_override_pins_standalone_with_an_empty_name(server, monkeypatch):
    cfg = server.cfg
    _seed(cfg)
    _builds(server, monkeypatch)
    r = _call(server.set_book_series("b1", schemas.SeriesRequest(series_name="S", series_order="2")))
    assert (r["series_name"], r["series_order"]) == ("S", "2")
    assert load_json(cfg.series_overrides, {})["b1"] == {"series_name": "S", "series_order": "2"}
    _call(server.set_book_series("b1", schemas.SeriesRequest(series_name="", series_order="2")))
    assert load_json(cfg.series_overrides, {})["b1"] == {"series_name": None, "series_order": None}


def test_override_endpoints_reject_bad_input_and_unknown_books(server, monkeypatch):
    from fastapi import HTTPException
    _seed(server.cfg)
    _builds(server, monkeypatch)
    with pytest.raises(HTTPException) as e:
        asyncio.run(server.set_book_genre("b1", schemas.GenreRequest(genre="nope")))
    assert e.value.status_code == 400
    for call in (server.set_book_title("zz", schemas.TitleRequest(title="x")),
                 server.set_book_genre("zz", schemas.GenreRequest(genre="")),
                 server.set_book_series("zz", schemas.SeriesRequest(series_name="", series_order=""))):
        with pytest.raises(HTTPException) as e:
            asyncio.run(call)
        assert e.value.status_code == 404


# ── page routes + render cache ───────────────────────────────────────────────

def test_every_page_in_the_table_is_routed_to_its_template(server):
    routes = {r.path: r for r in server.app.routes if getattr(r, "methods", None) == {"GET"}}
    for path, template in server.PAGES.items():
        assert path in routes, path
        assert routes[path].endpoint() == server._render(template)
    assert len({n.name for n in (routes[p] for p in server.PAGES)}) == len(server.PAGES)


def test_render_is_cached_until_a_template_or_partial_changes(server, tmp_path, monkeypatch):
    root = tmp_path / "templates"
    (root / "partials").mkdir(parents=True)
    page, part = root / "t.html", root / "partials" / "demo.html"
    page.write_text("<!-- include: demo -->|body")
    part.write_text("partial-v1")
    monkeypatch.setattr(server, "TEMPLATES_DIR", root)

    reads = []
    real = type(page).read_text
    monkeypatch.setattr(type(page), "read_text",
                        lambda self, *a, **k: (reads.append(self.name), real(self, *a, **k))[1])

    assert server._render("t.html") == "partial-v1|body"
    n = len(reads)
    assert server._render("t.html") == "partial-v1|body" and len(reads) == n   # served from cache

    part.write_text("partial-v2!")                                              # size changes
    assert server._render("t.html") == "partial-v2!|body"
    page.write_text("<!-- include: demo -->|edited")
    later = time.time() + 5
    os.utime(page, (later, later))
    assert server._render("t.html") == "partial-v2!|edited"
