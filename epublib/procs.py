"""Starting and stopping child processes the same way on every OS.

POSIX puts a child in its own session and ends it with `killpg`; Windows has no
process groups to signal, so the child gets its own group (which makes it the
target of CTRL_BREAK_EVENT) and `taskkill /T` walks its tree. Used by the SubPlz
runs (`align_jobs`) and by the desktop app's server supervisor (`desktop`), and
kept free of heavy imports so the tray process can load it.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable

_WINDOWS = os.name == "nt"
_CREATE_NEW_PROCESS_GROUP = 0x200
_CREATE_NO_WINDOW = 0x08000000


def popen_flags(*, hide_window: bool = False) -> dict:
    """Popen arguments that put a command in a group `stop_process` can end: its
    own session on POSIX, its own process group on Windows (`hide_window` also
    keeps a console window from flashing up when the app itself has none)."""
    if _WINDOWS:
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", _CREATE_NEW_PROCESS_GROUP)
        if hide_window:
            flags |= getattr(subprocess, "CREATE_NO_WINDOW", _CREATE_NO_WINDOW)
        return {"creationflags": flags}
    return {"start_new_session": True}


def stop_process(proc: subprocess.Popen, *, force: bool = False) -> None:
    """End the command and anything it spawned (it runs in its own group, see
    `popen_flags`). POSIX sends SIGTERM, or SIGKILL with `force`; Windows has no
    gentle form here and always ends the whole tree with `taskkill /T /F`."""
    if _WINDOWS:
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           stdin=subprocess.DEVNULL, capture_output=True, check=False)
        except OSError:
            proc.terminate()
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL if force else signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


def shutdown_process(proc: subprocess.Popen, grace: float = 8.0) -> int | None:
    """Ask the command to exit (SIGTERM, or Ctrl-Break on Windows, which uvicorn
    treats as a request to finish), give it `grace` seconds, then end it for good.
    Returns its exit code."""
    if proc.poll() is not None:
        return proc.returncode
    try:
        if _WINDOWS:
            proc.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM))
        else:
            os.killpg(proc.pid, signal.SIGTERM)
    except (OSError, ValueError):
        pass
    try:
        return proc.wait(grace)
    except subprocess.TimeoutExpired:
        stop_process(proc, force=True)
        return proc.wait()


def pid_alive(pid: int) -> bool:
    """Whether a process with this id is running."""
    if pid <= 0:
        return False
    if _WINDOWS:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def watch_parent(pid: int, on_gone: Callable[[], None], *, every: float = 2.0) -> threading.Thread:
    """Call `on_gone` once the process `pid` has exited: a supervised server must not
    outlive the app that started it (a force-quit tray leaves no one to stop it)."""
    def run() -> None:
        while pid_alive(pid):
            time.sleep(every)
        on_gone()

    thread = threading.Thread(target=run, name="parent-watch", daemon=True)
    thread.start()
    return thread


def interrupt_self() -> None:
    """Ask this process to shut down the way the supervisor does."""
    if _WINDOWS:
        os.kill(os.getpid(), getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM))
    else:
        os.kill(os.getpid(), signal.SIGTERM)
