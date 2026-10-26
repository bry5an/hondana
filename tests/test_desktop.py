"""The desktop launcher (desktop.py) without a display: the single-instance lock,
port choice, the first-run settings, and the supervisor that keeps `serve` running.
The supervisor is driven with stand-in commands (tiny Python scripts), and one test
starts the real thing headless."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time


from conftest import make_cfg
from epublib import desktop, procs, selftest, settings


def _wait(predicate, timeout=20.0, what="the condition"):
    end = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < end, f"timed out waiting for {what}"
        time.sleep(0.02)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── one instance ─────────────────────────────────────────────────────────────

def test_a_second_lock_is_refused_until_the_first_lets_go(tmp_path):
    path = tmp_path / "data" / desktop.LOCK_NAME
    first, second = desktop.InstanceLock(path), desktop.InstanceLock(path)
    assert first.acquire() is True
    assert second.acquire() is False
    first.release()
    assert path.read_text("ascii") == str(os.getpid())  # not readable while locked, on Windows
    assert second.acquire() is True
    second.release()
    second.release()  # releasing twice is fine


# ── ports ────────────────────────────────────────────────────────────────────

def test_a_taken_port_is_skipped():
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        taken = busy.getsockname()[1]
        assert desktop.port_free("127.0.0.1", taken) is False
        assert desktop.choose_port(taken, "127.0.0.1") > taken
    assert desktop.choose_port(taken, "127.0.0.1") == taken  # free again


def test_a_wildcard_listen_address_is_reached_as_loopback():
    assert desktop.local_url("0.0.0.0", 8001) == "http://127.0.0.1:8001"
    assert desktop.local_url("127.0.0.1", 8000) == "http://127.0.0.1:8000"
    assert desktop.local_url("books.local", 80) == "http://books.local:80"


# ── first run ────────────────────────────────────────────────────────────────

def test_the_first_run_makes_the_folders_and_saves_them(tmp_path):
    root = tmp_path / "Documents" / "Hondana"
    assert desktop.first_run_setup(root) is True
    assert (root / "Books").is_dir() and (root / "Audiobooks").is_dir()
    cfg = settings.load()
    assert cfg.epub_dir == (root / "Books") and cfg.audiobook_dirs == [root / "Audiobooks"]
    assert cfg.host == "127.0.0.1" and cfg.sources["epub_dir"] == "settings"
    assert desktop.first_run_setup(root) is False  # saved settings: not the first run any more


def test_a_config_file_means_it_is_not_a_first_run(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("HONDANA_CONFIG", str(cfg_file))
    assert desktop.first_run_setup(tmp_path / "Hondana") is False
    assert not (tmp_path / "Hondana").exists()


def test_the_default_data_dir_follows_the_environment(tmp_path, monkeypatch):
    assert settings.default_data_dir() == (tmp_path / "home" / "data").resolve()
    monkeypatch.setenv("HONDANA_DATA_DIR", str(tmp_path / "elsewhere"))
    assert settings.default_data_dir() == (tmp_path / "elsewhere").resolve()


# ── the supervisor ───────────────────────────────────────────────────────────

def _script(tmp_path, body: str) -> list[str]:
    path = tmp_path / "fake_server.py"
    path.write_text(body, encoding="utf-8")
    return [sys.executable, str(path)]


def _sup(tmp_path, command, **kw):
    cfg = make_cfg(tmp_path, port=_free_port())
    return desktop.Supervisor(log_path=tmp_path / "logs" / "desktop.log", command=lambda: command,
                              load=lambda: cfg, ready=lambda url: True, crash_pause=0.01, grace=5, **kw), cfg


SLEEPER = "import time\ntime.sleep(60)\n"


def test_the_server_is_started_and_stopped_with_the_supervisor(tmp_path):
    sup, cfg = _sup(tmp_path, _script(tmp_path, SLEEPER))
    sup.start()
    assert sup.wait_ready(10)
    proc = sup.proc
    assert sup.url == f"http://127.0.0.1:{cfg.port}"
    assert desktop.remembered_url(cfg.data_dir) == sup.url
    sup.stop()
    assert proc.poll() is not None and not sup.gave_up.is_set()


def test_exit_75_restarts_the_server_and_rereads_the_settings(tmp_path):
    marker = tmp_path / "ran-once"
    code = (f"import pathlib, sys, time\nm = pathlib.Path({str(marker)!r})\n"
            "if not m.exists():\n    m.write_text('x')\n    sys.exit(75)\ntime.sleep(60)\n")
    before, after = _free_port(), _free_port()
    ports = iter([before, after])  # what Settings saved before it asked for the restart
    sup = desktop.Supervisor(
        log_path=tmp_path / "logs" / "desktop.log", command=lambda: _script(tmp_path, code),
        load=lambda: make_cfg(tmp_path, port=next(ports)), ready=lambda url: True, grace=5)
    sup.start()
    _wait(lambda: sup.starts == 2 and sup.running.is_set(), what="the second start")
    assert sup.url == f"http://127.0.0.1:{after}" and not sup.gave_up.is_set()
    sup.stop()


def test_a_server_that_keeps_crashing_is_given_up_on(tmp_path):
    sup, _ = _sup(tmp_path, _script(tmp_path, "import sys\nsys.exit(3)\n"), max_crashes=2)
    sup.start()
    _wait(sup.gave_up.is_set, what="giving up")
    assert sup.starts == 3  # the first run and two retries, then the third crash is one too many
    assert "exited with 3" in (tmp_path / "logs" / "desktop.log").read_text("utf-8")
    assert not sup.wait_ready(1)
    sup.stop()


def test_a_taken_port_moves_this_run_only(tmp_path):
    out = tmp_path / "env.txt"
    code = (f"import os, pathlib, time\npathlib.Path({str(out)!r}).write_text("
            "os.environ.get('HONDANA_PORT', '-') + ' ' + os.environ['HONDANA_SUPERVISED'] + ' ' + os.environ['HONDANA_PARENT_PID'])\n"
            "time.sleep(60)\n")
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        sup, cfg = _sup(tmp_path, _script(tmp_path, code))
        cfg.port = busy.getsockname()[1]
        sup.start()
        _wait(out.exists, what="the child's environment")
        _wait(lambda: out.read_text(encoding="utf-8").count(" ") == 2, what="the full line")
        port, supervised, parent = out.read_text(encoding="utf-8").split()
        assert int(port) > cfg.port and supervised == "1" and parent == str(os.getpid())
        assert sup.url.endswith(f":{port}")
        sup.stop()


# ── a server that outlives its app must not ──────────────────────────────────

def test_pid_alive():
    assert procs.pid_alive(os.getpid())
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait()
    assert not procs.pid_alive(done.pid)
    assert not procs.pid_alive(0)


def test_the_parent_watch_fires_when_the_parent_is_gone():
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    fired = []
    procs.watch_parent(gone.pid, lambda: fired.append(1), every=0.01)
    _wait(lambda: fired, 5, "the callback")


# ── the real thing, headless ─────────────────────────────────────────────────

def test_the_headless_app_runs_the_server_and_a_second_launch_finds_it(tmp_path):
    env = {**os.environ, "HONDANA_HOME": str(tmp_path / "home"), "HONDANA_DISCOVERY_ENABLED": "0"}
    for name in list(env):
        if name.startswith(("HONDANA_CONFIG", "EPUB_LIBRARY")):
            del env[name]
    cmd = [sys.executable, "-m", "epublib", "desktop", "--no-tray", "--no-browser",
           "--library", str(tmp_path / "Library")]
    first = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", **procs.popen_flags())
    data = tmp_path / "home" / "data"
    try:
        _wait(lambda: (data / desktop.URL_NAME).exists(), 60, "the server's address")
        url = desktop.remembered_url(data)
        _wait(lambda: desktop.healthy(url), 90, "the server")
        assert (tmp_path / "Library" / "Books").is_dir()
        second = subprocess.run(cmd, env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)
        assert second.returncode == 0 and f"already running at {url}" in second.stdout
    finally:
        procs.shutdown_process(first, 15)
    _wait(lambda: not desktop.healthy(url), 10, "the server to stop with the app")


# ── --self-test ──────────────────────────────────────────────────────────────

def test_the_self_test_passes_here(capsys):
    assert selftest.run() == 0
    out = capsys.readouterr().out
    for check in ("bundled files", "server", "analysis", "tokenizers"):
        assert f"ok   {check}" in out
    assert "self-test passed" in out
