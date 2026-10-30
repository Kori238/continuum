"""Launch Continuum Control as a native desktop window.

Runs the FastAPI server on a background thread, then opens it in a real OS
window via pywebview (WebView2 on Windows). If pywebview isn't available, it
falls back to the default browser so the app still works.

Single-instance: launching replaces any server already on the port, so you can
never end up with two instances — one stale, one current — serving different
code. That split-brain is exactly what produced a wrong answer once: a leftover
process running pre-fix code answered while the fresh one sat idle.
"""
from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("continuum.app.window")

ICON = Path(__file__).parent / "static" / "continuum.ico"

# Windows groups taskbar buttons by Application User Model ID, which it
# otherwise infers from the running executable — so our window lands under
# "pythonw.exe", sharing a button and a generic Python icon with any other
# Python GUI. Claiming our own ID separates us, and makes a pinned button stick
# to Continuum rather than to the interpreter that happens to host it.
APP_ID = "Continuum.Desktop"


def _claim_taskbar_identity() -> None:
    """Announce ourselves to the shell as our own app. Must run *before* any
    window exists — the ID is read when the window is first shown, so calling
    this late is silently useless."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception as exc:  # cosmetic only — never lose the window over it
        log.debug("could not set AppUserModelID: %s", exc)


def _icon_arg() -> dict[str, str]:
    """`icon=` for `webview.start()`, or nothing if the file is missing.

    pywebview's own docstring claims this is GTK/QT-only. That is stale: the
    WinForms backend honours it, and when it is absent falls back to extracting
    an icon from `sys.executable` — which is where the Python logo came from.
    """
    return {"icon": str(ICON)} if ICON.is_file() else {}


def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((host, port)) == 0


def _wait_for_port(host: str, port: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_open(host, port):
            return True
        time.sleep(0.15)
    return False


def _wait_port_free(host: str, port: int, timeout: float = 6.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _port_open(host, port):
            return True
        time.sleep(0.15)
    return False


def _stop_running_posix(port: int) -> bool:
    """The same job on POSIX, via psutil rather than PowerShell.

    This used to be a bare `return False`, which was fine when Continuum only
    ever ran on Windows and became a quiet dead end when the hub moved to Linux:
    `python -m continuum stop` printed "nothing running" at a hub that was very
    much running, and `launch()` found the port busy, called this, got False, and
    raised an error telling Jake to run the command that had just done nothing.

    SIGTERM, not SIGKILL — uvicorn has a shutdown handler and the stores want
    their connections closed. The Windows branch uses `Stop-Process -Force`
    because WM_CLOSE doesn't reach a console-less Python process; POSIX has no
    such excuse.
    """
    try:
        import psutil
    except Exception:
        log.debug("psutil unavailable; cannot stop by port on this platform")
        return False
    me = os.getpid()
    pids: set[int] = set()
    try:
        for c in psutil.net_connections(kind="inet"):
            if (c.status == psutil.CONN_LISTEN and c.laddr
                    and c.laddr.port == port and c.pid and c.pid != me):
                pids.add(c.pid)
    except (psutil.AccessDenied, PermissionError):
        # Unprivileged `net_connections` can't see other users' sockets on some
        # kernels. Say so rather than reporting an empty result as "nothing here".
        log.warning("cannot inspect listeners on port %d without more privilege", port)
        return False
    except Exception as exc:
        log.debug("net_connections failed: %s", exc)
        return False
    stopped = False
    for pid in pids:
        try:
            psutil.Process(pid).terminate()
            stopped = True
        except Exception as exc:
            log.debug("could not terminate %d: %s", pid, exc)
    return stopped


def stop_running(port: int = 8848) -> bool:
    """Terminate whatever process is serving on `port`. Returns True if
    something was found and asked to stop."""
    if sys.platform != "win32":
        return _stop_running_posix(port)
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        f"$p = Get-NetTCPConnection -LocalPort {port} -State Listen "
        "| Select-Object -Expand OwningProcess -Unique;"
        "if ($p) { $p | ForEach-Object { Stop-Process -Id $_ -Force }; 'stopped' } else { 'none' }"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=12,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return "stopped" in (out.stdout or "")
    except Exception as exc:
        log.debug("stop_running failed: %s", exc)
        return False


def launch(host: str = "127.0.0.1", port: int = 8848) -> None:
    import uvicorn

    from .server import create_app

    # Replace any existing instance so this launch's code is the one that serves.
    if _port_open(host, port):
        log.info("Continuum already on %s:%d — replacing it with this instance", host, port)
        stop_running(port)
        if not _wait_port_free(host, port):
            raise RuntimeError(f"port {port} still busy; run `python -m continuum stop`")

    config = uvicorn.Config(create_app(), host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="continuum-uvicorn", daemon=True)
    thread.start()

    if not _wait_for_port(host, port):
        raise RuntimeError(f"Continuum server did not come up on {host}:{port}")

    url = f"http://{host}:{port}/"

    try:
        import webview  # pywebview
    except Exception:
        log.warning("pywebview not available — opening in browser: %s", url)
        import webbrowser

        webbrowser.open(url)
        print(f"\n  Continuum Control is running at {url}  (Ctrl+C to stop)\n")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return
        return

    _claim_taskbar_identity()
    webview.create_window(
        "Continuum",
        url,
        width=1200,
        height=800,
        min_size=(940, 640),
        background_color="#0a1519",
    )
    # blocks until the window is closed; then the process exits
    webview.start(**_icon_arg())


def open_remote(url: str) -> None:
    """A window onto a hub running somewhere else.

    The mirror image of `launch()`: no server, no stores, no agent. This
    process owns nothing but the window — the hub holds all of that, reached
    over the tailnet.

    Deliberately NOT coupled to the `continuum desktop` client. It would be
    tidier to start both from one icon, but then closing the window would stop
    the machine reporting and the hub would silently stop counting activity —
    a budget that quietly stops measuring is worse than one that is obviously
    off. The client is a background service; this is just a viewer for it.
    """
    try:
        import webview  # pywebview
    except Exception:
        log.warning("pywebview not available — opening in browser: %s", url)
        import webbrowser

        webbrowser.open(url)
        return

    _claim_taskbar_identity()
    webview.create_window(
        "Continuum",
        url,
        width=1200,
        height=800,
        min_size=(940, 640),
        background_color="#0a1519",
    )
    webview.start(**_icon_arg())
