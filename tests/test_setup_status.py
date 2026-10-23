"""The first-run checklist behind `/`'s "Getting started" card."""
import json

import pytest
from fastapi.testclient import TestClient

from epublib import setup_status


@pytest.fixture
def client(server):
    return TestClient(server.app)


def by_id(cfg, library):
    return {s["id"]: s for s in setup_status.steps(cfg, library)}


def test_a_fresh_install_has_every_required_step_open(cfg):
    got = by_id(cfg, {})
    assert [s for s, v in got.items() if v["required"]] == ["epub_dir", "library", "known"]
    assert not any(v["done"] for v in got.values())
    assert setup_status.summary(cfg, {})["complete"] is False


def test_library_step_waits_for_japanese_books_to_be_analyzed(cfg):
    cfg.data_dir.mkdir(parents=True)
    library = {"a": {"language": "ja"}, "b": {"language": "ja"}, "c": {"language": "en"}}
    cfg.vocab_index.write_text(json.dumps({"a": {"status": "ok"}}))
    step = by_id(cfg, library)["library"]
    assert not step["done"] and "1 of 2 Japanese" in step["detail"]

    cfg.vocab_index.write_text(json.dumps({"a": {}, "b": {"status": "no_text"}}))
    assert by_id(cfg, library)["library"]["done"]
    # English-only libraries have nothing to analyze.
    assert by_id(cfg, {"c": {"language": "en"}})["library"]["done"]


def test_complete_once_the_required_steps_are_done(cfg):
    cfg.epub_dir.mkdir(parents=True)
    (cfg.epub_dir / "Japanese").mkdir()
    (cfg.epub_dir / "Japanese" / "a.epub").write_bytes(b"x")
    cfg.known_words.parent.mkdir(parents=True)
    cfg.known_words.write_text(json.dumps({"count": 5000}))
    cfg.vocab_index.write_text(json.dumps({"a": {}}))
    summary = setup_status.summary(cfg, {"a": {"language": "ja"}})
    assert summary["complete"] is True
    # Optional steps never gate the card.
    assert not by_id(cfg, {})["audiobooks"]["done"]


def test_optional_steps_read_what_exists(cfg):
    cfg.data_dir.mkdir(parents=True)
    audio = cfg.data_dir / "audio"
    audio.mkdir()
    cfg.audiobook_dirs = [audio]
    assert "nothing is scanned" in by_id(cfg, {})["audiobooks"]["detail"]
    cfg.audiobook_files_index.write_text(json.dumps({"ab1": {}}))
    assert by_id(cfg, {})["audiobooks"]["done"]

    cfg.reader_progress.parent.mkdir(parents=True)
    cfg.reader_progress.write_text(json.dumps({"asin:1": {"fraction": 0.1}}))
    assert by_id(cfg, {})["phone"]["done"]


def test_endpoint(client):
    r = client.get("/api/setup")
    assert r.status_code == 200
    body = r.json()
    assert body["complete"] is False
    assert [s["id"] for s in body["steps"]] == ["epub_dir", "library", "known", "audiobooks", "phone", "dicts"]
    assert all({"label", "detail", "href", "link", "done", "required"} <= set(s) for s in body["steps"])
