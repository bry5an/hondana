"""What has to hold on Windows too (P6.1): atomic writes with no fchmod, a command
that can be written as an argv array, process control without killpg, and a
windowed build whose stdout/stderr are missing. The Windows-only branches are
driven by the module-level `_WINDOWS` flags, so they run on every OS."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from epublib import align_jobs, align_remote, index_store, procs, settings, stdio
from epublib.index_store import replace_file, save_json, write_bytes_atomic


# ── atomic writes ────────────────────────────────────────────────────────────

def test_write_bytes_atomic_replaces_the_file_and_leaves_no_temp(tmp_path):
    path = tmp_path / "sub" / "pack.json.gz"
    write_bytes_atomic(path, b"one")
    write_bytes_atomic(path, b"two")
    assert path.read_bytes() == b"two"
    assert [p.name for p in path.parent.iterdir()] == ["pack.json.gz"]


@pytest.mark.skipif(os.name == "nt", reason="Windows keeps only the read-only bit")
def test_write_bytes_atomic_applies_the_mode(tmp_path):
    path = tmp_path / "pack.json.gz"
    write_bytes_atomic(path, b"x")
    assert path.stat().st_mode & 0o777 == 0o644


def test_write_bytes_atomic_cleans_up_when_the_replace_fails(tmp_path, monkeypatch):
    path = tmp_path / "pack.json.gz"

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(index_store, "replace_file", boom)
    with pytest.raises(OSError):
        write_bytes_atomic(path, b"x")
    assert list(tmp_path.iterdir()) == []


def _flaky_replace(monkeypatch, failures: int) -> list[int]:
    calls: list[int] = []
    real = os.replace

    def replace(src, dst):
        calls.append(1)
        if len(calls) <= failures:
            raise PermissionError("in use")
        real(src, dst)

    monkeypatch.setattr(index_store.os, "replace", replace)
    monkeypatch.setattr(index_store.time, "sleep", lambda s: None)
    return calls


def test_a_windows_replace_is_retried_while_the_target_is_in_use(tmp_path, monkeypatch):
    monkeypatch.setattr(index_store, "_WINDOWS", True)
    calls = _flaky_replace(monkeypatch, failures=3)
    save_json(tmp_path / "a.json", {"a": 1})
    assert (tmp_path / "a.json").read_text("utf-8").count('"a"') == 1
    assert len(calls) == 4


def test_a_windows_replace_gives_up_eventually(tmp_path, monkeypatch):
    monkeypatch.setattr(index_store, "_WINDOWS", True)
    calls = _flaky_replace(monkeypatch, failures=99)
    src = tmp_path / "src"
    src.write_text("x", encoding="utf-8")
    with pytest.raises(PermissionError):
        replace_file(src, tmp_path / "dst")
    assert len(calls) == index_store._REPLACE_TRIES


def test_a_posix_permission_error_is_not_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(index_store, "_WINDOWS", False)
    calls = _flaky_replace(monkeypatch, failures=99)
    with pytest.raises(PermissionError):
        replace_file(tmp_path / "src", tmp_path / "dst")
    assert len(calls) == 1


# ── subplz_cmd as an argv array ──────────────────────────────────────────────

WIN_EXE = r"C:\Program Files\SubPlz\subplz.exe"


def test_an_argv_array_keeps_windows_paths_as_written(tmp_path):
    argv = align_jobs._argv([WIN_EXE, "sync", "--audio", "{audio}", "--text={text}", "{out}"],
                            tmp_path / "a b.m4b", tmp_path / "t.txt", tmp_path / "o")
    assert argv == [WIN_EXE, "sync", "--audio", str(tmp_path / "a b.m4b"),
                    f"--text={tmp_path / 't.txt'}", str(tmp_path / "o")]


def test_a_string_is_still_split_like_a_shell(tmp_path):
    argv = align_jobs._argv("run 'a b' {audio} {text} {out}", tmp_path / "x", tmp_path / "t", tmp_path / "o")
    assert argv[:2] == ["run", "a b"]


@pytest.mark.parametrize("cmd, why", [
    ([], "not set"),
    (["subplz", "{audio}"], "{audio} and {text}"),
    (["subplz", "{text}"], "{audio} and {text}"),
])
def test_an_argv_array_is_checked_like_a_string(cmd, why):
    assert why in align_jobs.template_problem(cmd)


def test_a_good_argv_array_has_no_problem():
    assert align_jobs.template_problem(["subplz", "--in={audio}", "{text}", "{out}"]) is None


def test_an_argv_array_runs(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    script = tmp_path / "echo_args.py"
    script.write_text("import sys, pathlib\npathlib.Path(sys.argv[3], 'x.srt').write_text(sys.argv[1] + '|' + sys.argv[2])\n",
                      encoding="utf-8")
    (tmp_path / "book.m4b").write_text("x", encoding="utf-8")
    (tmp_path / "book.txt").write_text("t", encoding="utf-8")
    done = align_jobs.transcribe([sys.executable, str(script), "{audio}", "{text}", "{out}"],
                                 tmp_path / "book.m4b", tmp_path / "book.txt", tmp_path, 0)
    assert (done / "x.srt").read_text("utf-8") == f"{tmp_path / 'book.m4b'}|{tmp_path / 'book.txt'}"


def test_the_setting_takes_a_string_or_an_array():
    key = settings.BY_NAME["alignment.subplz_cmd"]
    assert settings.coerce(key, "  subplz {audio}  ", None) == "subplz {audio}"
    assert settings.coerce(key, ["a", "b c"], None) == ["a", "b c"]
    assert settings.coerce(key, '["C:\\\\x\\\\s.exe", "{audio}"]', None) == ["C:\\x\\s.exe", "{audio}"]


def test_a_toml_array_loads_as_a_list(tmp_path):
    (tmp_path / "config.toml").write_text(
        'epub_dir = "books"\ndata_dir = "data"\n[alignment]\n'
        'subplz_cmd = ["C:\\\\Tools\\\\subplz.exe", "sync", "{audio}", "{text}", "{out}"]\n',
        encoding="utf-8")
    cfg = settings.load(tmp_path / "config.toml")
    assert cfg.subplz_cmd == ["C:\\Tools\\subplz.exe", "sync", "{audio}", "{text}", "{out}"]
    assert align_jobs.command_problem(cfg) is None


def test_the_laptops_own_config_may_use_an_array_too(tmp_path):
    path = tmp_path / "align.toml"
    path.write_text('server = "http://x"\n[alignment]\nsubplz_cmd = ["subplz", "{audio}", "{text}"]\n',
                    encoding="utf-8")
    assert align_remote.user_settings(path) == {
        "server": "http://x", "subplz_cmd": ["subplz", "{audio}", "{text}"]}
    path.write_text('[alignment]\nsubplz_cmd = "subplz {audio} {text}"\n', encoding="utf-8")
    assert align_remote.user_settings(path) == {"subplz_cmd": "subplz {audio} {text}"}


# ── process control ──────────────────────────────────────────────────────────

def test_posix_commands_get_their_own_session(monkeypatch):
    monkeypatch.setattr(procs, "_WINDOWS", False)
    assert align_jobs.popen_flags() == {"start_new_session": True}


def test_windows_commands_get_their_own_process_group(monkeypatch):
    monkeypatch.setattr(procs, "_WINDOWS", True)
    assert align_jobs.popen_flags() == {"creationflags": 0x200}


def test_windows_stops_the_whole_process_tree_with_taskkill(monkeypatch):
    seen = []
    monkeypatch.setattr(procs, "_WINDOWS", True)
    monkeypatch.setattr(align_jobs.subprocess, "run", lambda argv, **kw: seen.append(argv))

    class Proc:
        pid = 4242

    align_jobs.stop_process(Proc())
    assert seen == [["taskkill", "/T", "/F", "/PID", "4242"]]


def test_windows_falls_back_to_terminate_without_taskkill(monkeypatch):
    def missing(argv, **kw):
        raise FileNotFoundError("taskkill")

    class Proc:
        pid = 1
        stopped = False

        def terminate(self):
            self.stopped = True

    monkeypatch.setattr(procs, "_WINDOWS", True)
    monkeypatch.setattr(align_jobs.subprocess, "run", missing)
    proc = Proc()
    align_jobs.stop_process(proc)
    assert proc.stopped


@pytest.mark.skipif(os.name == "nt", reason="the POSIX path")
def test_stop_process_ends_a_command_and_its_children(tmp_path):
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "time.sleep(60)\n", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(parent)], **align_jobs.popen_flags())
    align_jobs.stop_process(proc)
    assert proc.wait(timeout=10) != 0


# ── a build with no console ──────────────────────────────────────────────────

def test_missing_streams_are_pointed_at_a_log_file(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    log = tmp_path / "logs" / "server.log"
    assert stdio.redirect_to_log(log) is True
    try:
        print("hello from a windowed build")
        sys.stderr.isatty()  # what uvicorn's logging setup calls
    finally:
        sys.stdout.close()
    assert "hello from a windowed build" in log.read_text("utf-8")


def test_a_terminal_run_keeps_its_streams(tmp_path, monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    before = (sys.stdout, sys.stderr)
    assert stdio.redirect_to_log(tmp_path / "server.log") is False
    assert (sys.stdout, sys.stderr) == before
    assert not (tmp_path / "server.log").exists()


def test_a_frozen_app_without_a_terminal_logs_to_the_file(tmp_path, monkeypatch):
    class Quiet:
        def isatty(self):
            return False

        def write(self, text):
            return len(text)

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "stdout", Quiet())
    monkeypatch.setattr(sys, "stderr", Quiet())
    assert stdio.needs_log_file()
    assert stdio.redirect_to_log(tmp_path / "server.log") is True
    sys.stdout.close()
