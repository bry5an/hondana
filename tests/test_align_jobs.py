"""Running SubPlz on the server (align_jobs.py). The command is a stand-in
script that writes one 4 s cue per line of {text}; the audio files are stubs whose
content tells it how to behave (FAIL, NOSRT, SLEEP)."""
from __future__ import annotations

import json
import sys
import time

import pytest
from fastapi.testclient import TestClient

from conftest import make_cfg
from epublib import align, align_jobs
from epublib.index_store import save_json
from test_align import SENTENCES, _book, srt_for

FAKE = '''\
import pathlib, sys, time
audio, text, out = map(pathlib.Path, sys.argv[1:4])
mode = audio.read_text()
pathlib.Path("ran-here").write_text("x")
with open(audio.parent / "calls.log", "a") as f:
    f.write(audio.name + "\\n")
if mode == "FAIL":
    print("model exploded", file=sys.stderr)
    sys.exit(2)
if mode == "SLEEP":
    time.sleep(60)
if mode == "NOSRT":
    sys.exit(0)
lines = text.read_text(encoding="utf-8").splitlines()
def ts(ms):
    return "%02d:%02d:%02d,%03d" % (ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000)
cues = ["%d\\n%s --> %s\\n%s\\n" % (i + 1, ts(i * 4000), ts((i + 1) * 4000), s) for i, s in enumerate(lines)]
(out / (audio.stem + ".srt")).write_text("\\n".join(cues), encoding="utf-8")
'''


@pytest.fixture
def rig(tmp_path):
    """cfg with the stand-in command, a book, and a folder of stub audio."""
    script = tmp_path / "fake_subplz.py"
    script.write_text(FAKE, encoding="utf-8")
    cfg = make_cfg(tmp_path, subplz_cmd=[sys.executable, str(script), "{audio}", "{text}", "{out}"])
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    return cfg, _book(cfg), audio_dir, align_jobs.AlignJobs(cfg)


def _audio(audio_dir, *names, mode="OK"):
    paths = []
    for n in names:
        (audio_dir / n).write_text(mode)
        paths.append((n if len(names) > 1 else None, audio_dir / n))
    return paths


def _wait(jobs, book_id, *states, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        st = jobs.status(book_id)
        if st and st["state"] in states:
            return st
        time.sleep(0.02)
    raise AssertionError(f"still {jobs.status(book_id)} after {timeout}s")


def _calls(audio_dir):
    log = audio_dir / "calls.log"
    return log.read_text().split() if log.exists() else []


def _started(audio_dir, timeout=20):
    """Wait until the stand-in command has begun (state flips to running before the
    interpreter has even started, so cancelling at once would race its first write)."""
    end = time.time() + timeout
    while not _calls(audio_dir):
        assert time.time() < end, "the command never started"
        time.sleep(0.02)


# ── the command ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cmd, why", [
    ("", "not set"),
    ("subplz sync {audio}", "{audio} and {text}"),
    ("subplz sync {text}", "{audio} and {text}"),
    ("subplz 'unterminated {audio} {text}", "valid command"),
])
def test_command_problem(tmp_path, cmd, why):
    assert why in align_jobs.command_problem(make_cfg(tmp_path, subplz_cmd=cmd))


def test_a_good_command_has_no_problem(rig):
    assert align_jobs.command_problem(rig[0]) is None


def test_placeholders_are_replaced_without_touching_other_braces(tmp_path):
    argv = align_jobs._argv("run --opt '{\"a\": 1}' {audio} --t={text} {out}",
                            tmp_path / "a b.m4b", tmp_path / "t.txt", tmp_path / "o")
    assert argv == ["run", "--opt", '{"a": 1}', str(tmp_path / "a b.m4b"),
                    f"--t={tmp_path / 't.txt'}", str(tmp_path / "o")]


# ── a run ────────────────────────────────────────────────────────────────────

def test_a_run_stores_the_same_files_as_an_upload(rig):
    cfg, book, audio_dir, jobs = rig
    jobs.enqueue(book, "B0AUDIO001", None, _audio(audio_dir, "book.m4b"))
    assert _wait(jobs, "b1", "done", "error")["state"] == "done"
    assert align.state(cfg, book, "B0AUDIO001") == {"state": "ready", "coverage": 1.0, "ok": True,
                                                     "source": "synced", "estimated": 0, "anchors": 0,
                                                     "speech_gaps": None, "speech_gap_s": None}
    m = json.loads(align.map_path(cfg, "b1").read_text("utf-8"))
    assert m["docs"][0]["sents"][0] == [0, 4000] and m["matched"] == len(SENTENCES)
    assert align._srt_path(cfg, "b1").exists()
    assert not align.work_dir(cfg, "b1").exists()  # a finished run cleans up after itself


def test_a_transcribe_cmd_is_preferred_and_its_srt_is_stored_as_a_transcript(rig, tmp_path):
    """With `transcribe_cmd` set, a run here makes a plain transcript (no {text})."""
    cfg, book, audio_dir, jobs = rig
    script = tmp_path / "fake_gen.py"
    script.write_text(
        "import pathlib, sys\n"
        "audio, out, heard = map(pathlib.Path, sys.argv[1:4])\n"
        "lines = heard.read_text(encoding='utf-8').splitlines()\n"
        "ts = lambda ms: '%02d:%02d:%02d,%03d' % (ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000)\n"
        "cues = ['%d\\n%s --> %s\\n%s\\n' % (i + 1, ts(i * 8000), ts((i + 1) * 8000), s) for i, s in enumerate(lines)]\n"
        "(out / (audio.stem + '.srt')).write_text('\\n'.join(cues), encoding='utf-8')\n", encoding="utf-8")
    plain = [s.replace("。", "") for s in SENTENCES]
    heard = tmp_path / "heard.txt"
    heard.write_text("\n".join(plain[i] + plain[i + 1] for i in range(0, len(plain), 2)), encoding="utf-8")
    cfg.transcribe_cmd = f"{sys.executable} {script} {{audio}} {{out}} {heard}"
    assert align_jobs.job_command(cfg)[1] == "transcript" and align_jobs.command_problem(cfg) is None
    jobs.enqueue(book, "B0AUDIO001", None, _audio(audio_dir, "book.m4b"))
    assert _wait(jobs, "b1", "done", "error")["state"] == "done"
    assert align.state(cfg, book, "B0AUDIO001")["source"] == "transcript"
    assert align._srt_path(cfg, "b1", kind="transcript").exists() and not align._srt_path(cfg, "b1").exists()


def test_the_text_given_to_the_command_is_one_sentence_per_line(rig):
    cfg, book, audio_dir, jobs = rig
    jobs.enqueue(book, "B0AUDIO001", None, _audio(audio_dir, "book.m4b", mode="FAIL"))
    _wait(jobs, "b1", "error")
    assert (align.work_dir(cfg, "b1") / "book.txt").read_text("utf-8").splitlines() == SENTENCES


def test_the_command_runs_in_the_work_dir_not_the_servers(rig):
    """SubPlz leaves a ./config/ cache in its cwd; that must not be the repo root."""
    cfg, book, audio_dir, jobs = rig
    jobs.enqueue(book, "B0AUDIO001", None, _audio(audio_dir, "book.m4b", mode="FAIL"))
    _wait(jobs, "b1", "error")
    assert (align.work_dir(cfg, "b1") / "ran-here").exists()


def test_an_mp3_folder_is_run_per_file_and_stored_per_part(rig):
    cfg, book, audio_dir, jobs = rig
    jobs.enqueue(book, "B0MP3FOLDR", ["a.mp3", "b.mp3"], _audio(audio_dir, "a.mp3", "b.mp3"))
    assert _wait(jobs, "b1", "done", "error")["state"] == "done"
    assert _calls(audio_dir) == ["a.mp3", "b.mp3"]
    assert align._srt_path(cfg, "b1", 0).exists() and align._srt_path(cfg, "b1", 1).exists()
    assert not align._srt_path(cfg, "b1").exists()
    m = json.loads(align.map_path(cfg, "b1").read_text("utf-8"))
    assert m["docs"][0]["sents"][0][2] in ("a.mp3", "b.mp3")


def test_enqueue_is_idempotent_while_a_run_is_in_flight(rig):
    cfg, book, audio_dir, jobs = rig
    audio = _audio(audio_dir, "book.m4b", mode="SLEEP")
    first = jobs.enqueue(book, "B0AUDIO001", None, audio)
    assert jobs.enqueue(book, "B0AUDIO001", None, audio)["queued_at"] == first["queued_at"]
    _wait(jobs, "b1", "running")
    _started(audio_dir)
    assert jobs.cancel("b1")
    _wait(jobs, "b1", "cancelled")
    assert _calls(audio_dir) == ["book.m4b"]


# ── failures leave the stored alignment alone ────────────────────────────────

def test_a_failing_command_reports_why_and_keeps_the_existing_alignment(rig):
    cfg, book, audio_dir, jobs = rig
    align.ingest(cfg, book, "B0AUDIO001", None, srt_for(SENTENCES, step=1000))
    before = align._srt_path(cfg, "b1").read_bytes()
    jobs.enqueue(book, "B0AUDIO001", None, _audio(audio_dir, "book.m4b", mode="FAIL"))
    st = _wait(jobs, "b1", "error")
    assert "exited with 2" in st["error"] and "model exploded" in st["error"]
    shown = align.state(cfg, book, "B0AUDIO001", st)
    assert shown["state"] == "ready" and shown["error"] == st["error"]
    assert align._srt_path(cfg, "b1").read_bytes() == before


def test_a_command_that_writes_no_srt_is_an_error(rig):
    cfg, book, audio_dir, jobs = rig
    jobs.enqueue(book, "B0AUDIO001", None, _audio(audio_dir, "book.m4b", mode="NOSRT"))
    assert "wrote no .srt" in _wait(jobs, "b1", "error")["error"]
    assert align.state(cfg, book, "B0AUDIO001")["state"] == "none"


def test_a_command_that_cannot_start_is_an_error(rig):
    cfg, book, audio_dir, _ = rig
    cfg.subplz_cmd = "/no/such/subplz {audio} {text} {out}"
    jobs = align_jobs.AlignJobs(cfg)
    jobs.enqueue(book, "B0AUDIO001", None, _audio(audio_dir, "book.m4b"))
    assert "could not run" in _wait(jobs, "b1", "error")["error"]


def test_a_rejected_match_restores_every_part(rig):
    cfg, book, audio_dir, jobs = rig
    align.ingest(cfg, book, "B0MP3FOLDR", ["a.mp3", "b.mp3"], srt_for(SENTENCES), "a.mp3")
    before = align._srt_path(cfg, "b1", 0).read_bytes()
    with pytest.raises(align.AlignError):
        align.ingest_parts(cfg, book, "B0MP3FOLDR", ["a.mp3", "b.mp3"],
                           {"a.mp3": srt_for(SENTENCES, step=1), "b.mp3": "no cues here"})
    assert align._srt_path(cfg, "b1", 0).read_bytes() == before
    assert not align._srt_path(cfg, "b1", 1).exists()


# ── resume and cancel ────────────────────────────────────────────────────────

def test_a_retry_skips_the_files_already_done(rig):
    cfg, book, audio_dir, jobs = rig
    audio = _audio(audio_dir, "a.mp3", "b.mp3")
    (audio_dir / "b.mp3").write_text("FAIL")
    jobs.enqueue(book, "B0MP3FOLDR", ["a.mp3", "b.mp3"], audio)
    _wait(jobs, "b1", "error")
    assert (align.work_dir(cfg, "b1") / "out-000").is_dir()  # kept for the retry
    assert align.state(cfg, book, "B0MP3FOLDR")["state"] == "none"  # nothing stored yet
    (audio_dir / "b.mp3").write_text("OK")
    jobs.enqueue(book, "B0MP3FOLDR", ["a.mp3", "b.mp3"], audio)
    assert _wait(jobs, "b1", "done", "error")["state"] == "done"
    assert _calls(audio_dir) == ["a.mp3", "b.mp3", "b.mp3"]  # a.mp3 ran once


def test_cancelling_a_running_job_stops_the_process_and_keeps_the_work_dir(rig):
    cfg, book, audio_dir, jobs = rig
    jobs.enqueue(book, "B0AUDIO001", None, _audio(audio_dir, "book.m4b", mode="SLEEP"))
    st = _wait(jobs, "b1", "running")
    _started(audio_dir)
    t0 = time.time()
    assert jobs.cancel("b1")
    st = _wait(jobs, "b1", "cancelled")
    assert time.time() - t0 < 10 and st["error"] is None
    assert align.work_dir(cfg, "b1").is_dir()
    assert align.state(cfg, book, "B0AUDIO001", st)["state"] == "none"
    assert not jobs.cancel("b1")  # nothing left to cancel


def test_a_cancelled_queued_job_never_runs(rig):
    cfg, book, audio_dir, jobs = rig
    other = {**book, "book_id": "b2"}
    jobs.enqueue(book, "B0AUDIO001", None, _audio(audio_dir, "slow.m4b", mode="SLEEP"))
    _wait(jobs, "b1", "running")
    jobs.enqueue(other, "B0AUDIO001", None, _audio(audio_dir, "never.m4b"))
    assert jobs.status("b2")["state"] == "queued"
    assert jobs.cancel("b2") and jobs.status("b2")["state"] == "cancelled"
    jobs.cancel("b1")
    _wait(jobs, "b1", "cancelled")
    time.sleep(0.3)  # the worker pops b2 next and must skip it
    assert "never.m4b" not in _calls(audio_dir) and jobs.status("b2")["state"] == "cancelled"


# ── the routes ───────────────────────────────────────────────────────────────

@pytest.fixture
def client(server, tmp_path):
    scfg = server.cfg
    script = tmp_path / "fake_subplz.py"
    script.write_text(FAKE, encoding="utf-8")
    root = tmp_path / "audio"
    root.mkdir()
    (root / "book.m4b").write_text("OK")
    scfg.audiobook_dirs = [root]
    scfg.subplz_cmd = [sys.executable, str(script), "{audio}", "{text}", "{out}"]
    save_json(scfg.library_index, {
        "b1": _book(scfg, "b1"),
        "nolink": {**_book(scfg, "nolink"), "audiobook": {"audible_isbn": "B0GONE0003",
                                                           "audiobook_local": False}},
        "en": {**_book(scfg, "en"), "language": "en"},
    })
    save_json(scfg.audiobook_index, {
        "B0AUDIO001": {"ab_id": "B0AUDIO001", "local": True, "format": "m4b",
                       "root": str(root), "path": "book.m4b"},
    })
    return TestClient(server.app)


def test_generate_runs_while_the_index_lock_is_held(client, server):
    with server._index_lock:
        r = client.post("/api/books/b1/alignment/generate")
        assert r.status_code == 200 and r.json()["status"] == "queued"
        assert r.json()["alignment"]["state"] in ("queued", "running")
        _wait(server._align_jobs, "b1", "done", "error")
        st = client.get("/api/books/b1/status").json()["alignment"]
    assert st["state"] == "ready" and st["coverage"] == 1.0 and st["error"] is None
    assert client.get("/api/reader/books/b1/alignment").json()["docs"][0]["sents"][0] == [0, 4000]


def test_generate_says_when_it_cannot_run(client, server):
    server.cfg.subplz_cmd = ""
    r = client.post("/api/books/b1/alignment/generate")
    assert r.status_code == 409 and r.json()["reason"] == "subplz_not_configured"
    server.cfg.subplz_cmd = "x {audio} {text} {out}"
    assert client.post("/api/books/nolink/alignment/generate").json()["reason"] == "no_audiobook"
    assert client.post("/api/books/en/alignment/generate").json()["reason"] == "not_ja"
    assert client.post("/api/books/nope/alignment/generate").status_code == 404


def test_cancel_route_and_a_failed_run_show_in_status(client, server):
    assert client.delete("/api/books/b1/alignment/generate").status_code == 404
    (server.cfg.audiobook_dirs[0] / "book.m4b").write_text("SLEEP")
    assert client.post("/api/books/b1/alignment/generate").status_code == 200
    _wait(server._align_jobs, "b1", "running")
    r = client.delete("/api/books/b1/alignment/generate")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    _wait(server._align_jobs, "b1", "cancelled")
    (server.cfg.audiobook_dirs[0] / "book.m4b").write_text("FAIL")
    client.post("/api/books/b1/alignment/generate")
    _wait(server._align_jobs, "b1", "error")
    st = client.get("/api/books/b1/status").json()["alignment"]
    assert st["state"] == "none" and "exited with 2" in st["error"]


def test_deleting_the_alignment_also_drops_a_half_done_run(client, server):
    (server.cfg.audiobook_dirs[0] / "book.m4b").write_text("FAIL")
    client.post("/api/books/b1/alignment/generate")
    _wait(server._align_jobs, "b1", "error")
    assert align.work_dir(server.cfg, "b1").is_dir()
    assert client.post("/api/books/b1/alignment", files={
        "file": ("a.srt", srt_for(SENTENCES).encode(), "application/x-subrip")}).status_code == 200
    assert client.delete("/api/books/b1/alignment").status_code == 200
    assert not align.work_dir(server.cfg, "b1").exists()


# ── what the book page and /admin read ────────────────────────────────

def test_status_says_whether_the_book_can_be_synced_and_generated_here(client, server):
    st = client.get("/api/books/b1/status").json()["alignment"]
    assert st["eligible"] and st["can_generate"]
    assert not client.get("/api/books/nolink/status").json()["alignment"]["eligible"]
    assert not client.get("/api/books/en/status").json()["alignment"]["eligible"]
    server.cfg.subplz_cmd = ""
    assert not client.get("/api/books/b1/status").json()["alignment"]["can_generate"]


def test_the_admin_queue_lists_unsynced_books_with_a_local_audiobook(client, server):
    r = client.get("/api/admin/alignment-queue")
    assert r.status_code == 200
    body = r.json()
    assert [b["book_id"] for b in body["books"]] == ["b1"]  # not the unlinked or English one
    assert body["can_generate"] is True
    # Once synced it leaves the queue.
    (server.cfg.audiobook_dirs[0] / "book.m4b").write_text("OK")
    client.post("/api/books/b1/alignment/generate")
    _wait(server._align_jobs, "b1", "done", "error")
    assert client.get("/api/admin/alignment-queue").json()["books"] == []


def test_the_admin_queue_counts_the_audio_length(client, server):
    audiobooks = json.loads(server.cfg.audiobook_index.read_text())
    audiobooks["B0AUDIO001"]["runtime_min"] = 90
    save_json(server.cfg.audiobook_index, audiobooks)
    body = client.get("/api/admin/alignment-queue").json()
    assert body["books"][0]["runtime_min"] == 90 and body["total_min"] == 90
