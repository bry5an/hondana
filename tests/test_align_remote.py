"""Aligning a server's book from another machine (align_remote.py): the text
route, the multi-part upload with its sha check, and the client against a real
server on a local port. The "Mac" is the tmp dir; its `subplz_cmd` is a stand-in
that writes one 4 s cue per line of {text} and misbehaves on the audio's say-so
(FAIL, SLEEP, NOSRT)."""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient

from epublib import cli
from epublib import align, align_jobs, align_remote
from epublib.align_remote import RemoteError, Server
from epublib.index_store import save_json
from test_align import SENTENCES, _book, srt_for

FAKE = '''\
import os, pathlib, sys, time
audio, text, out = map(pathlib.Path, sys.argv[1:4])
mode = audio.read_bytes()[:8]
with open(audio.parent / "calls.log", "a") as f:
    f.write(audio.name + "\\n")
if mode.startswith(b"FAIL"):
    print("model exploded", file=sys.stderr)
    sys.exit(2)
if mode.startswith(b"SLEEP"):
    pathlib.Path(os.environ["PIDFILE"]).write_text(str(os.getpid())) if "PIDFILE" in os.environ else None
    time.sleep(60)
if mode.startswith(b"NOSRT"):
    sys.exit(0)
lines = text.read_text(encoding="utf-8").splitlines()
def ts(ms):
    return "%02d:%02d:%02d,%03d" % (ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000)
cues = ["%d\\n%s --> %s\\n%s\\n" % (i + 1, ts(i * 4000), ts((i + 1) * 4000), s) for i, s in enumerate(lines)]
(out / (audio.stem + ".srt")).write_text("\\n".join(cues), encoding="utf-8")
'''
BIG = os.urandom(3 * 1024 * 1024 + 123)


@pytest.fixture
def mac(tmp_path, monkeypatch):
    """The other machine: a stand-in subplz, and a cache dir of its own."""
    script = tmp_path / "fake_subplz.py"
    script.write_text(FAKE, encoding="utf-8")
    cache = tmp_path / "mac-cache"
    monkeypatch.setattr(align_remote, "CACHE_ROOT", cache)
    cmd = f"{sys.executable} {script} {{audio}} {{text}} {{out}}"
    return cmd, cache


@pytest.fixture
def lib(server, tmp_path):
    """The server's library: b1 (one m4b), b2 (two mp3s), an English book and one
    with no local audiobook; `bigfile` is only for the download tests."""
    scfg = server.cfg
    root = tmp_path / "audiobooks"
    (root / "folder").mkdir(parents=True)
    (root / "book.m4b").write_bytes(b"OK" * 50)
    (root / "folder" / "a.mp3").write_bytes(b"OK-a")
    (root / "folder" / "b.mp3").write_bytes(b"OK-b")
    (root / "big.m4b").write_bytes(BIG)
    scfg.audiobook_dirs = [root]
    save_json(scfg.library_index, {
        "b1": {**_book(scfg, "b1"), "title": "Single Volume"},
        "b2": {**_book(scfg, "b2", ab="B0MP3FOLDR"), "title": "Folder Book"},
        "nolink": {**_book(scfg, "nolink"), "title": "Elsewhere",
                   "audiobook": {"audible_isbn": "B0GONE0003", "audiobook_local": False}},
        "en": {**_book(scfg, "en"), "title": "English Edition", "language": "en"},
    })
    ent = {"local": True, "root": str(root)}
    save_json(scfg.audiobook_index, {
        "B0AUDIO001": {**ent, "ab_id": "B0AUDIO001", "format": "m4b", "path": "book.m4b",
                       "filesize": 100, "runtime_min": 90},
        "B0MP3FOLDR": {**ent, "ab_id": "B0MP3FOLDR", "format": "mp3-folder", "path": "folder",
                       "files": ["a.mp3", "b.mp3"], "filesize": 8, "runtime_min": 30},
        "B0BIGFILE1": {**ent, "ab_id": "B0BIGFILE1", "format": "m4b", "path": "big.m4b",
                       "filesize": len(BIG), "runtime_min": 5},
    })
    return root


@pytest.fixture
def live(server, lib):
    """The real app on a local port, so the client's HTTP (Range included) is exercised."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    srv = uvicorn.Server(uvicorn.Config(server.app, log_level="warning", lifespan="off"))
    t = threading.Thread(target=srv.run, kwargs={"sockets": [sock]}, daemon=True)
    t.start()
    end = time.time() + 10
    while not srv.started:
        assert time.time() < end, "the server never started"
        time.sleep(0.01)
    yield Server(f"http://127.0.0.1:{sock.getsockname()[1]}")
    srv.should_exit = True
    t.join(5)


def _plan(srv, book_id, **kw):
    entries = {e["ab_id"]: e for e in srv.json("/api/audiobooks")}
    book = next(b for b in srv.books() if b["book_id"] == book_id)
    return align_remote.make_plan(srv, book, entries, **kw)


def _run(srv, mac, book_id, **kw):
    return align_remote.align_book(srv, _plan(srv, book_id), mac[0], log=lambda _: None, **kw)


def _calls(path):
    log = path / "calls.log"
    return log.read_text().split() if log.exists() else []


# ── the text route ───────────────────────────────────────────────────────────

def test_the_text_route_and_the_server_job_write_the_same_text(server, lib, tmp_path):
    scfg = server.cfg
    scfg.subplz_cmd = f"{sys.executable} -c 'import sys; sys.exit(2)' {{audio}} {{text}}"
    (lib / "book.m4b").write_bytes(b"FAIL")
    c = TestClient(server.app)
    r = c.get("/api/books/b1/alignment/text")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert r.headers["X-Book-Sha256"] == "cd" * 32
    assert r.text.splitlines() == SENTENCES
    c.post("/api/books/b1/alignment/generate")
    end = time.time() + 20
    while (server._align_jobs.status("b1") or {}).get("state") != "error":
        assert time.time() < end
        time.sleep(0.02)
    assert (align.work_dir(scfg, "b1") / "book.txt").read_text("utf-8") == r.text


def test_the_text_route_says_when_there_is_nothing_to_align(server, lib):
    c = TestClient(server.app)
    assert c.get("/api/books/nope/alignment/text").status_code == 404
    assert c.get("/api/books/nolink/alignment/text").json()["reason"] == "no_audiobook"
    r = c.get("/api/books/en/alignment/text")
    assert r.status_code == 409 and r.json()["reason"] == "not_ja"


# ── the upload ───────────────────────────────────────────────────────────────

def _files(*parts_and_text):
    return [("file", (f"{p or 'x'}.srt", t.encode(), "application/x-subrip")) for p, t in parts_and_text]


def test_a_stale_sha_is_refused_and_stores_nothing(server, lib):
    c = TestClient(server.app)
    r = c.post("/api/books/b1/alignment", files=_files((None, srt_for(SENTENCES))),
               headers={"X-Book-Sha256": "00" * 32})
    assert r.status_code == 409 and r.json()["reason"] == "book_changed"
    assert not align._srt_path(server.cfg, "b1").exists() and not align.map_path(server.cfg, "b1").exists()
    ok = c.post("/api/books/b1/alignment", files=_files((None, srt_for(SENTENCES))),
                headers={"X-Book-Sha256": "cd" * 32})
    assert ok.status_code == 200 and ok.json()["ok"] is True


def test_a_multi_part_upload_stores_every_part_in_one_request(server, lib):
    c = TestClient(server.app)
    r = c.post("/api/books/b2/alignment", files=_files(("a.mp3", srt_for(SENTENCES[:20])),
                                                       ("b.mp3", srt_for(SENTENCES[20:]))),
               data={"part": ["a.mp3", "b.mp3"]})
    assert r.status_code == 200 and r.json()["coverage"] == 1.0
    assert align._srt_path(server.cfg, "b2", 0).exists() and align._srt_path(server.cfg, "b2", 1).exists()
    rows = c.get("/api/reader/books/b2/alignment").json()["docs"][0]["sents"]
    assert rows[0][2] == "a.mp3" and rows[-1][2] == "b.mp3"


def test_one_rejected_part_stores_none_and_keeps_the_old_alignment(server, lib):
    c = TestClient(server.app)
    cfg = server.cfg
    c.post("/api/books/b2/alignment", files=_files(("a.mp3", srt_for(SENTENCES))), data={"part": ["a.mp3"]})
    before = align._srt_path(cfg, "b2", 0).read_bytes()
    r = c.post("/api/books/b2/alignment", files=_files(("a.mp3", srt_for(SENTENCES, step=1)),
                                                       ("b.mp3", "no cues in here")),
               data={"part": ["a.mp3", "b.mp3"]})
    assert r.status_code == 400 and r.json()["reason"] == "bad_srt"
    assert align._srt_path(cfg, "b2", 0).read_bytes() == before
    assert not align._srt_path(cfg, "b2", 1).exists()


def test_upload_shapes_that_do_not_add_up_are_400(server, lib):
    c = TestClient(server.app)
    two = _files(("a.mp3", srt_for(SENTENCES)), ("b.mp3", srt_for(SENTENCES)))
    assert c.post("/api/books/b2/alignment", files=two).status_code == 400  # no parts
    assert c.post("/api/books/b2/alignment", files=two, data={"part": ["a.mp3"]}).status_code == 400
    assert c.post("/api/books/b2/alignment", files=two, data={"part": ["a.mp3", "a.mp3"]}).status_code == 400
    assert c.post("/api/books/b2/alignment", files=two[:1], data={"part": ["c.mp3"]}).json()["reason"] == "bad_part"
    assert not align._srt_path(server.cfg, "b2", 0).exists()


# ── a whole run ──────────────────────────────────────────────────────────────

def test_a_book_goes_from_none_to_ready(live, mac):
    assert live.status("b1")["alignment"]["state"] == "none"
    r = _run(live, mac, "b1")
    assert r.ok and r.response["coverage"] == 1.0
    assert live.status("b1")["alignment"] == {"state": "ready", "coverage": 1.0, "ok": True,
                                              "step": None, "error": None,
                                              "eligible": True, "can_generate": False}
    served = live.json("/api/reader/books/b1/alignment")
    assert served["docs"][0]["sents"][0] == [0, 4000] and served["matched"] == len(SENTENCES)
    assert not (mac[1] / live.host / "b1").exists()  # a finished run clears its cache


def test_an_mp3_folder_is_stored_one_srt_per_part(live, mac, server):
    r = _run(live, mac, "b2")
    assert r.ok
    assert align._srt_path(server.cfg, "b2", 0).exists() and align._srt_path(server.cfg, "b2", 1).exists()
    assert not align._srt_path(server.cfg, "b2").exists()


def test_a_ready_book_is_skipped_unless_forced(live, mac):
    _run(live, mac, "b1")
    again = _run(live, mac, "b1")
    assert again.skipped and "--force" in again.skipped
    assert _run(live, mac, "b1", force=True).response["ok"]


def test_a_failing_command_reports_and_leaves_the_servers_alignment(live, mac, lib, server):
    c = TestClient(server.app)
    c.post("/api/books/b1/alignment", files=_files((None, srt_for(SENTENCES, step=1000))))
    before = align._srt_path(server.cfg, "b1").read_bytes()
    (lib / "book.m4b").write_bytes(b"FAIL" * 20)
    with pytest.raises(RemoteError, match="exited with 2.*model exploded"):
        _run(live, mac, "b1", force=True)
    assert align._srt_path(server.cfg, "b1").read_bytes() == before


def test_a_missing_command_is_reported_before_anything_is_fetched(live, mac):
    plan = _plan(live, "b1")
    with pytest.raises(RemoteError, match="subplz_cmd is not set"):
        align_remote.align_book(live, plan, "", log=lambda _: None)
    with pytest.raises(RemoteError, match="could not run"):
        align_remote.align_book(live, plan, "/no/such/subplz {audio} {text} {out}", log=lambda _: None)
    assert live.status("b1")["alignment"]["state"] == "none"


def test_a_retry_skips_the_files_already_transcribed(live, mac, lib):
    (lib / "folder" / "b.mp3").write_bytes(b"FAIL-longer")
    with pytest.raises(RemoteError):
        _run(live, mac, "b2")
    work = mac[1] / live.host / "b2"
    assert (work / "out-000").is_dir() and (work / "audio" / "a.mp3").exists()
    assert live.status("b2")["alignment"]["state"] == "none"  # nothing was uploaded
    (lib / "folder" / "b.mp3").write_bytes(b"OK-b")
    calls_before = _calls(work / "audio")
    assert _run(live, mac, "b2").ok
    assert calls_before == ["a.mp3", "b.mp3"] and not work.exists()


def test_a_changed_epub_discards_the_old_transcripts(live, mac, lib, server, monkeypatch):
    (lib / "folder" / "b.mp3").write_bytes(b"FAIL-longer")
    with pytest.raises(RemoteError):
        _run(live, mac, "b2")
    work = mac[1] / live.host / "b2"
    assert (work / "out-000").is_dir()
    lib_ = server._load_library()
    save_json(server.cfg.library_index, {**lib_, "b2": {**lib_["b2"], "sha256": "ee" * 32}})
    (lib / "folder" / "b.mp3").write_bytes(b"OK-b")
    ran, real = [], align_jobs.transcribe
    monkeypatch.setattr(align_jobs, "transcribe", lambda t, audio, *a, **kw: (ran.append(audio.name), real(t, audio, *a, **kw))[1])
    assert _run(live, mac, "b2").ok
    assert ran == ["a.mp3", "b.mp3"]  # a.mp3 was transcribed again from the new text


def test_an_upload_the_server_refuses_leaves_the_work_for_a_retry(live, mac, server):
    plan = _plan(live, "b1")
    real = live.upload

    def stale(book_id, srts, sha):
        lib_ = server._load_library()
        save_json(server.cfg.library_index, {**lib_, "b1": {**lib_["b1"], "sha256": "ee" * 32}})
        return real(book_id, srts, sha)

    live.upload = stale
    with pytest.raises(RemoteError, match="book_changed"):
        align_remote.align_book(live, plan, mac[0], log=lambda _: None)
    assert (mac[1] / live.host / "b1" / "out-000").is_dir()
    assert not align._srt_path(server.cfg, "b1").exists()


def test_a_copy_already_here_is_used_instead_of_a_download(live, mac, tmp_path):
    here = tmp_path / "here"
    here.mkdir()
    (here / "book.m4b").write_bytes(b"OK" * 50)
    plan = _plan(live, "b1", audio_dir=here)
    assert plan.to_download() == 0
    assert align_remote.align_book(live, plan, mac[0], log=lambda _: None).ok
    assert _calls(here) == ["book.m4b"]  # the command read the Mac's own file


def test_a_local_m4b_of_another_size_is_refused(live, tmp_path):
    here = tmp_path / "here"
    here.mkdir()
    (here / "book.m4b").write_bytes(b"OK")
    with pytest.raises(RemoteError, match="would not line up"):
        _plan(live, "b1", audio=[here / "book.m4b"])


# ── choosing a book ──────────────────────────────────────────────────────────

BOOKS = [{"book_id": "x1", "title": "コンビニ人間"}, {"book_id": "x2", "title": "コンビニ人間 2"},
         {"book_id": "x3", "title": "Another Story"}]


def test_find_book_by_id_exact_title_or_substring():
    assert align_remote.find_book(BOOKS, "x3")["book_id"] == "x3"
    assert align_remote.find_book(BOOKS, "story")["book_id"] == "x3"
    assert align_remote.find_book(BOOKS, "コンビニ人間")["book_id"] == "x1"  # the exact title wins


def test_an_ambiguous_or_unknown_title_stops_with_the_candidates():
    with pytest.raises(RemoteError, match="(?s)2 books match.*x1.*x2"):
        align_remote.find_book(BOOKS, "コンビニ")
    with pytest.raises(RemoteError, match="no book"):
        align_remote.find_book(BOOKS, "zzz")


def test_ineligible_books_say_why(live):
    books = {b["book_id"]: b for b in live.books()}
    assert align_remote.ineligible(books["b1"]) is None
    assert "not_ja" in align_remote.ineligible(books["en"])
    assert "no_audiobook" in align_remote.ineligible(books["nolink"])
    with pytest.raises(RemoteError, match="no_audiobook"):
        _plan(live, "nolink")


# ── downloads ────────────────────────────────────────────────────────────────

def test_a_download_resumes_from_the_part_file(live, tmp_path):
    dest = tmp_path / "dl" / "big.m4b"
    dest.parent.mkdir()
    dest.with_name("big.m4b.part").write_bytes(BIG[:1_000_000])
    seen = []
    live.download("/download/audiobook/B0BIGFILE1", dest, lambda got, total: seen.append((got, total)))
    assert dest.read_bytes() == BIG and not dest.with_name("big.m4b.part").exists()
    assert seen[0][0] > 1_000_000 and seen[-1] == (len(BIG), len(BIG))  # picked up where it stopped


def test_a_finished_download_is_confirmed_not_fetched_again(live, tmp_path):
    dest = tmp_path / "big.m4b"
    dest.write_bytes(BIG)
    calls = []
    live.download("/download/audiobook/B0BIGFILE1", dest, lambda *a: calls.append(a))
    assert dest.read_bytes() == BIG and calls == []


def test_a_file_that_changed_on_the_server_is_fetched_afresh(live, tmp_path):
    dest = tmp_path / "big.m4b"
    dest.write_bytes(BIG + b"extra")  # longer than what the server has now
    live.download("/download/audiobook/B0BIGFILE1", dest)
    assert dest.read_bytes() == BIG


def test_download_errors_name_the_problem(live, tmp_path):
    with pytest.raises(RemoteError, match="Audiobook not found"):
        live.download("/download/audiobook/NOPE", tmp_path / "x")
    with pytest.raises(RemoteError, match="cannot reach"):
        Server("http://127.0.0.1:9").json("/api/books")


# ── --missing, --dry-run ─────────────────────────────────────────────────────

def test_missing_lists_the_right_books(live, mac, capsys):
    assert {b["book_id"] for b in align_remote.candidates(live)} == {"b1", "b2"}
    _run(live, mac, "b1")
    assert [b["book_id"] for b in align_remote.candidates(live)] == ["b2"]


def _cli(live, tmp_path, *args):
    cfg = tmp_path / "mac.toml"
    cfg.write_text(f'epub_dir = "{tmp_path / "x"}"\ndata_dir = "{tmp_path / "xd"}"\n'
                   f'[alignment]\nsubplz_cmd = "false {{audio}} {{text}} {{out}}"\n')
    return cli.main(["--config", str(cfg), "align-remote", "--server", live.base, *args])


def test_dry_run_prints_the_plan_and_starts_nothing(live, mac, tmp_path, capsys):
    assert _cli(live, tmp_path, "--missing", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "2 book(s)" in out and "Single Volume" in out and "Folder Book" in out
    assert "1.5 h" in out and "2.0 h of audio" in out  # 90 + 30 minutes
    assert not mac[1].exists()  # no work dir, no download, no command
    assert _cli(live, tmp_path, "Single Volume", "--dry-run") == 0
    assert not mac[1].exists()


def test_missing_without_run_only_lists(live, mac, tmp_path, capsys):
    assert _cli(live, tmp_path, "--missing") == 0
    assert "add --run" in capsys.readouterr().out and not mac[1].exists()


def test_without_config_the_machines_align_toml_names_the_server_and_command(
        live, mac, tmp_path, monkeypatch, capsys):
    mine = tmp_path / "align.toml"
    mine.write_text(f'server = "{live.base}"\n'
                    f'[alignment]\nsubplz_cmd = "false {{audio}} {{text}} {{out}}"\n')
    monkeypatch.setattr(align_remote, "USER_CONFIG", mine)
    assert cli.main(["align-remote", "--missing", "--dry-run"]) == 0
    assert f"on {live.base} with" in capsys.readouterr().out
    # --server still wins over the file.
    assert cli.main(["align-remote", "--server", "http://127.0.0.1:9", "--missing"]) == 1
    assert "cannot reach http://127.0.0.1:9" in capsys.readouterr().err
    mine.write_text("server = [")
    assert cli.main(["align-remote", "--missing"]) == 2
    assert "isn't valid TOML" in capsys.readouterr().err


def test_with_no_server_named_the_one_found_on_the_network_is_used():
    one = [{"name": "nas", "url": "http://192.0.2.10:8000"}]
    assert align_remote.find_server(None, browse=lambda: one) == "http://192.0.2.10:8000"
    assert align_remote.find_server("http://given", browse=lambda: 1 / 0) == "http://given"
    with pytest.raises(RemoteError, match="no Hondana server found"):
        align_remote.find_server(None, browse=lambda: [])
    with pytest.raises(RemoteError, match="2 Hondana servers found"):
        align_remote.find_server(None, browse=lambda: one * 2)


def test_cli_argument_errors_and_exit_codes(live, mac, tmp_path, capsys):
    assert _cli(live, tmp_path) == 2
    assert _cli(live, tmp_path, "b1", "--run") == 2
    assert _cli(live, tmp_path, "Nothing Like This") == 1
    assert "no book" in capsys.readouterr().err
    assert _cli(live, tmp_path, "b1") == 1  # `false` as the command: exits non-zero, reported
    assert "exited with 1" in capsys.readouterr().err


def test_missing_run_continues_past_a_failure(live, mac, lib, tmp_path, capsys):
    (lib / "book.m4b").write_bytes(b"FAIL" * 25)
    cfg = tmp_path / "mac.toml"
    script = tmp_path / "fake_subplz.py"
    script.write_text(FAKE, encoding="utf-8")
    cfg.write_text(f'epub_dir = "{tmp_path / "x"}"\ndata_dir = "{tmp_path / "xd"}"\n'
                   f'[alignment]\nsubplz_cmd = "{sys.executable} {script} {{audio}} {{text}} {{out}}"\n')
    code = cli.main(["--config", str(cfg), "align-remote", "--server", live.base, "--missing", "--run"])
    assert code == 1  # one failed...
    assert live.status("b2")["alignment"]["state"] == "ready"  # ...the next one still ran
    assert live.status("b1")["alignment"]["state"] == "none"


# ── Ctrl-C ───────────────────────────────────────────────────────────────────

def test_ctrl_c_leaves_no_subplz_process(tmp_path, mac):
    """Interrupt a `transcribe` in a child Python and check the command died with it."""
    audio = tmp_path / "a" / "book.m4b"
    audio.parent.mkdir()
    audio.write_bytes(b"SLEEP")
    text = tmp_path / "book.txt"
    text.write_text("x\n")
    work = tmp_path / "work"
    work.mkdir()
    pidfile = tmp_path / "pid"
    code = ("import sys; from pathlib import Path; from epublib import align_jobs; "
            "align_jobs.transcribe(sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]), 0)")
    parent = subprocess.Popen([sys.executable, "-c", code, mac[0], str(audio), str(text), str(work)],
                              env={**os.environ, "PIDFILE": str(pidfile)},
                              cwd=os.path.dirname(os.path.dirname(__file__)), stderr=subprocess.PIPE)
    end = time.time() + 20
    while not pidfile.exists() or not pidfile.read_text():
        assert time.time() < end, "the command never started"
        time.sleep(0.02)
    child = int(pidfile.read_text())
    parent.send_signal(signal.SIGINT)
    parent.wait(10)
    end = time.time() + 10
    while time.time() < end:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(child, signal.SIGKILL)
        pytest.fail("the command outlived Ctrl-C")
    assert not align_jobs.out_dir(work, 0).exists()  # nothing counted as done


def test_ctrl_c_on_the_command_exits_130_and_keeps_the_work(live, lib, tmp_path):
    """The real command line, interrupted mid-transcription."""
    (lib / "book.m4b").write_bytes(b"SLEEP" * 20)
    script = tmp_path / "fake_subplz.py"
    script.write_text(FAKE, encoding="utf-8")
    pidfile = tmp_path / "pid"
    cfg = tmp_path / "mac.toml"
    cfg.write_text(f'epub_dir = "{tmp_path / "x"}"\ndata_dir = "{tmp_path / "xd"}"\n'
                   f'[alignment]\nsubplz_cmd = "{sys.executable} {script} {{audio}} {{text}} {{out}}"\n')
    home = tmp_path / "home"
    proc = subprocess.Popen([sys.executable, "cli.py", "--config", str(cfg), "align-remote", "b1",
                             "--server", live.base],
                            env={**os.environ, "HOME": str(home), "PIDFILE": str(pidfile)},
                            cwd=os.path.dirname(os.path.dirname(__file__)),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    end = time.time() + 30
    while not pidfile.exists() or not pidfile.read_text():
        assert time.time() < end, "the command never started"
        time.sleep(0.05)
    child = int(pidfile.read_text())
    proc.send_signal(signal.SIGINT)
    _, err = proc.communicate(timeout=15)
    assert proc.returncode == 130 and "run the same command to resume" in err
    with pytest.raises(ProcessLookupError):
        for _ in range(100):
            os.kill(child, 0)
            time.sleep(0.05)
        os.kill(child, signal.SIGKILL)
    assert (home / ".cache/hondana/align" / live.host / "b1" / "audio" / "book.m4b").exists()
    assert live.status("b1")["alignment"]["state"] == "none"
