"""Hondana as a desktop app: a tray icon that keeps the server running.

`hondana desktop` (what double-clicking the packaged app runs) does four things:

* **One instance.** `<data_dir>/hondana.lock` is held while the app runs; a second
  launch just opens the browser at the running one (`hondana.url` says where).
* **First run.** With no config file and no saved settings it makes
  `~/Documents/Hondana/{Books,Audiobooks}`, saves them as the library folders,
  listens on 127.0.0.1 (the admin pages have no password until the user sets one)
  and opens `/settings?welcome=1`.
* **A supervised server.** `Supervisor` runs `hondana serve` as a child process and
  starts it again when it exits with 75 (what Settings' "Restart now" asks for) or
  crashes. A child process rather than a thread because that restart ends the
  process, and a crash must not take the tray down with it. The child watches this
  process and stops if it is gone.
* **The tray.** Open Hondana, Settings, Open data folder, Quit. pystray owns the
  main thread (macOS requires it); everything else runs in threads.

The tray is the only part that needs a display: `--no-tray` runs the same
supervisor headless, which is also how the tests drive it.
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from collections.abc import Callable
from pathlib import Path

from . import procs, settings, stdio
from .config import FROZEN, ROOT, Config

RESTART_CODE = 75        # `POST /api/admin/restart` exits with this
FIRST_PORT = 8000
PORT_SPAN = 50           # how many ports above the wanted one to try
LOCK_NAME = "hondana.lock"
URL_NAME = "hondana.url"
PARENT_ENV = "HONDANA_PARENT_PID"
READY_TIMEOUT = 90.0     # the first start imports spaCy and the tokenizers


# ── one instance ─────────────────────────────────────────────────────────────

class InstanceLock:
    """An exclusive lock on a file for as long as the process lives (the OS drops it
    if the process is killed, so there is no stale lock to clean up)."""

    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        fh.truncate(0)
        fh.write(str(os.getpid()).encode("ascii"))
        fh.flush()
        return True

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        fh.close()


# ── ports and addresses ──────────────────────────────────────────────────────

def port_free(host: str, port: int) -> bool:
    """Whether a server could listen on `host:port` now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if os.name != "nt":  # as uvicorn does; on Windows it would let a second listener in
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("" if host in ("0.0.0.0", "") else host, port))
        except OSError:
            return False
    return True


def choose_port(preferred: int, host: str) -> int:
    """`preferred`, or the next free port above it."""
    for port in range(preferred, min(preferred + PORT_SPAN, 65536)):
        if port_free(host, port):
            return port
    raise OSError(f"No free port from {preferred} to {preferred + PORT_SPAN - 1}")


def local_url(host: str, port: int) -> str:
    """Where this computer reaches the server: a wildcard address is reached as loopback."""
    return f"http://{'127.0.0.1' if host in ('0.0.0.0', '', '::') else host}:{port}"


def remembered_url(data_dir: Path) -> str | None:
    """The address the running instance wrote down, if it did."""
    try:
        return (data_dir / URL_NAME).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


# ── first run ────────────────────────────────────────────────────────────────

def first_run_setup(root: Path) -> bool:
    """On the very first start (no config file, no saved settings) make the library
    folders under `root` and save them as the settings. Returns whether it did."""
    if settings.find_config() is not None:
        return False
    data_dir = settings.default_data_dir()
    if settings.overlay_path(data_dir).exists():
        return False
    books, audio = root / "Books", root / "Audiobooks"
    for folder in (data_dir, books, audio):
        folder.mkdir(parents=True, exist_ok=True)
    settings.save_overlay(data_dir, {
        "epub_dir": str(books), "audiobook_dirs": [str(audio)],
        "host": "127.0.0.1", "port": choose_port(FIRST_PORT, "127.0.0.1"),
    })
    return True


def library_root(given: str | None) -> Path:
    return Path(given).expanduser() if given else Path.home() / "Documents" / "Hondana"


# ── the server child ─────────────────────────────────────────────────────────

def server_command() -> list[str]:
    """The command line that runs `hondana serve` from this installation: the app
    itself when packaged, else this interpreter running the package."""
    if FROZEN:
        return [sys.executable, "serve"]
    return [sys.executable, "-m", "epublib", "serve"]


def healthy(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/healthz", timeout=2) as r:
            return r.status == 200
    except OSError:
        return False


class Supervisor:
    """Runs the server as a child process and keeps it running. Each start re-reads
    the settings, so a port or folder changed in Settings applies after the restart
    it asks for."""

    def __init__(self, *, log_path: Path, command: Callable[[], list[str]] = server_command,
                 load: Callable[[], Config] = settings.load,
                 ready: Callable[[str], bool] = healthy, max_crashes: int = 3,
                 crash_window: float = 60.0, crash_pause: float = 2.0, grace: float = 8.0):
        self.log_path = log_path
        self._command, self._load, self._ready = command, load, ready
        self._max_crashes, self._window, self._pause, self._grace = max_crashes, crash_window, crash_pause, grace
        self.url = ""
        self.data_dir: Path | None = None
        self.proc: subprocess.Popen | None = None
        self.starts = 0
        self.gave_up = threading.Event()   # crashed too often to keep trying
        self.running = threading.Event()   # set whenever a child has been started and not yet exited
        self._quit = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="supervisor", daemon=True)
        self._thread.start()

    def _spawn(self, logf) -> subprocess.Popen:
        cfg = self._load()
        port = choose_port(cfg.port, cfg.host)
        env = {**os.environ, "HONDANA_SUPERVISED": "1", PARENT_ENV: str(os.getpid())}
        if port != cfg.port:  # the wanted one is taken: this run only
            env["HONDANA_PORT"] = str(port)
        self.data_dir = cfg.data_dir
        self.url = local_url(cfg.host, port)
        try:
            cfg.data_dir.mkdir(parents=True, exist_ok=True)
            (cfg.data_dir / URL_NAME).write_text(self.url, encoding="utf-8")
        except OSError:
            pass
        return subprocess.Popen(self._command(), env=env, stdin=subprocess.DEVNULL, stdout=logf,
                                stderr=subprocess.STDOUT, **procs.popen_flags(hide_window=True))

    def _loop(self) -> None:
        crashes: list[float] = []
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "ab") as logf:
            while not self._quit.is_set():
                try:
                    proc = self._spawn(logf)
                except (OSError, SystemExit) as exc:
                    logf.write(f"could not start the server: {exc}\n".encode("utf-8", "replace"))
                    code = 1
                else:
                    self.proc = proc
                    self.starts += 1
                    self.running.set()
                    code = proc.wait()
                    self.running.clear()
                if self._quit.is_set() or code == RESTART_CODE:
                    continue
                now = time.monotonic()
                crashes = [t for t in crashes if now - t < self._window] + [now]
                logf.write(f"the server exited with {code}\n".encode("utf-8"))
                if len(crashes) > self._max_crashes:
                    self.gave_up.set()
                    return
                self._quit.wait(self._pause)

    def wait_ready(self, timeout: float = READY_TIMEOUT) -> bool:
        """Block until the server answers, or it gave up, or `timeout` passes."""
        end = time.monotonic() + timeout
        while time.monotonic() < end and not self.gave_up.is_set():
            if self.running.is_set() and self.url and self._ready(self.url):
                return True
            time.sleep(0.2)
        return False

    def stop(self) -> None:
        self._quit.set()
        proc = self.proc
        if proc is not None:
            procs.shutdown_process(proc, self._grace)
        if self._thread is not None:
            self._thread.join(timeout=self._grace + 5)


# ── the tray ─────────────────────────────────────────────────────────────────

def open_folder(path: Path) -> None:
    """Show a folder in the file manager."""
    if sys.platform == "win32":
        os.startfile(path)
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])


def open_page(sup: Supervisor, path: str = "") -> None:
    webbrowser.open(sup.url + path)


def run_tray(sup: Supervisor, *, first_run: bool, open_browser: bool) -> None:
    """Show the tray icon and block until Quit. Needs the `desktop` extra (pystray)."""
    import pystray
    from PIL import Image

    def quit_app(icon, _item=None) -> None:
        threading.Thread(target=lambda: (sup.stop(), icon.stop()), name="quit", daemon=True).start()

    def folder(_icon, _item) -> None:
        if sup.data_dir:
            open_folder(sup.data_dir)

    menu = pystray.Menu(
        pystray.MenuItem("Open Hondana", lambda *_: open_page(sup), default=True),
        pystray.MenuItem("Settings", lambda *_: open_page(sup, "/settings")),
        pystray.MenuItem("Open data folder", folder),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit Hondana", quit_app),
    )
    icon = pystray.Icon("hondana", Image.open(ROOT / "static" / "icon.png"), "Hondana", menu)

    def setup(icon) -> None:
        icon.visible = True
        sup.start()
        if sup.wait_ready():
            if open_browser:
                open_page(sup, "/settings?welcome=1" if first_run else "")
        elif sup.gave_up.is_set():
            icon.notify("Hondana's server keeps stopping. The log is in the data folder's logs folder.",
                        "Hondana")
        else:
            icon.notify("Hondana's server is taking a long time to start.", "Hondana")

    icon.run(setup)


def run_headless(sup: Supervisor, *, first_run: bool, open_browser: bool) -> None:
    """The same supervisor without a tray: runs until Ctrl-C or SIGTERM."""
    done = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", signal.SIGINT)):
        signal.signal(sig, lambda *_: done.set())
    sup.start()
    if sup.wait_ready() and open_browser:
        open_page(sup, "/settings?welcome=1" if first_run else "")
    while not done.is_set() and not sup.gave_up.is_set():
        done.wait(0.5)


def run(*, library: str | None = None, open_browser: bool = True, tray: bool = True) -> int:
    first_run = first_run_setup(library_root(library))
    cfg = settings.load()
    stdio.redirect_to_log(cfg.data_dir / "logs" / "desktop.log")
    lock = InstanceLock(cfg.data_dir / LOCK_NAME)
    if not lock.acquire():
        url = remembered_url(cfg.data_dir) or local_url(cfg.host, cfg.port)
        print(f"Hondana is already running at {url}")
        if open_browser:
            webbrowser.open(url)
        return 0
    sup = Supervisor(log_path=cfg.data_dir / "logs" / "desktop.log")
    try:
        (run_tray if tray else run_headless)(sup, first_run=first_run, open_browser=open_browser)
    finally:
        sup.stop()
        lock.release()
    return 1 if sup.gave_up.is_set() else 0
