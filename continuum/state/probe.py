"""Low-level OS probes for live state. Windows-first (ctypes + psutil).

Everything here is a pure sample of "right now": the visible windows and the
process behind each, the foreground window, per-process detail, and system
metrics. The tracker layer turns these snapshots into durations and history.
On non-Windows platforms the window probes degrade to empty lists so the rest
of Continuum still runs.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any, Optional

import psutil

log = logging.getLogger("continuum.state.probe")

_WIN = sys.platform == "win32"

# Shell/among windows we never want to present as "apps you have open".
_IGNORE_TITLES = {"Program Manager", "Windows Input Experience", "Settings"}
_IGNORE_PROCS = {"ApplicationFrameHost.exe", "TextInputHost.exe", "SystemSettings.exe"}


def friendly_name(proc_name: str) -> str:
    """"Code.exe" -> "Code", "chrome.exe" -> "Chrome"."""
    base = proc_name[:-4] if proc_name.lower().endswith(".exe") else proc_name
    return base[:1].upper() + base[1:] if base else proc_name


def list_windows() -> list[dict[str, Any]]:
    """Visible, titled top-level windows: [{hwnd, title, pid}]."""
    if not _WIN:
        return []
    import ctypes
    import ctypes.wintypes as wt

    user32 = ctypes.windll.user32
    out: list[dict[str, Any]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buff = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buff, length + 1)
        title = buff.value.strip()
        if not title or title in _IGNORE_TITLES:
            return True
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        out.append({"hwnd": int(hwnd), "title": title, "pid": int(pid.value)})
        return True

    user32.EnumWindows(_cb, 0)
    return out


def foreground_pid() -> Optional[int]:
    if not _WIN:
        return None
    import ctypes
    import ctypes.wintypes as wt

    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def process_brief(pid: int) -> Optional[dict[str, Any]]:
    """Cheap per-process facts used to build the basic snapshot."""
    try:
        p = psutil.Process(pid)
        name = p.name()
        if name in _IGNORE_PROCS:
            return None
        return {"pid": pid, "proc": name, "name": friendly_name(name),
                "create_time": p.create_time()}
    except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
        return None


def process_detail(pid: int) -> dict[str, Any]:
    """Everything reasonable about one process — the 'request more' payload."""
    p = psutil.Process(pid)
    with p.oneshot():
        info: dict[str, Any] = {
            "pid": pid,
            "proc": p.name(),
            "name": friendly_name(p.name()),
        }
        for key, fn in [
            ("exe", p.exe), ("status", p.status), ("username", p.username),
            ("create_time", p.create_time), ("num_threads", p.num_threads),
        ]:
            try:
                info[key] = fn()
            except Exception:
                info[key] = None
        try:
            info["cmdline"] = " ".join(p.cmdline())
        except Exception:
            info["cmdline"] = None
        try:
            info["memory_mb"] = round(p.memory_info().rss / (1024 * 1024), 1)
        except Exception:
            info["memory_mb"] = None
        try:
            info["cpu_pct"] = p.cpu_percent(interval=0.15)
        except Exception:
            info["cpu_pct"] = None
        try:
            info["num_open_files"] = len(p.open_files())
        except Exception:
            info["num_open_files"] = None
        try:
            info["children"] = [friendly_name(c.name()) for c in p.children()]
        except Exception:
            info["children"] = []
    return info


def system_metrics() -> dict[str, Any]:
    vm = psutil.virtual_memory()
    out: dict[str, Any] = {
        "cpu_pct": psutil.cpu_percent(interval=None),  # since last call (primed by poller)
        "mem_pct": vm.percent,
        "mem_used_gb": round(vm.used / (1024 ** 3), 1),
        "mem_total_gb": round(vm.total / (1024 ** 3), 1),
    }
    try:
        bat = psutil.sensors_battery()
        if bat is not None:
            out["battery_pct"] = round(bat.percent)
            out["on_battery"] = not bat.power_plugged
    except Exception:
        pass
    return out


# ---- presence --------------------------------------------------------------
def idle_seconds() -> Optional[float]:
    """Seconds since the last keyboard/mouse input — the core 'is Jake here' signal."""
    if not _WIN:
        return None
    import ctypes

    class _LII(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    lii = _LII()
    lii.cbSize = ctypes.sizeof(_LII)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
        return None
    tick = ctypes.windll.kernel32.GetTickCount()
    # GetTickCount wraps ~49.7 days; max() keeps a wrap from reading as huge idle.
    return max(0.0, (tick - lii.dwTime) / 1000.0)


# ---- gpu / network / files -------------------------------------------------
def gpu() -> Optional[dict[str, Any]]:
    """NVIDIA GPU load + the compute processes using it (detects training runs).
    Returns None when nvidia-smi isn't present."""
    import subprocess
    try:
        q = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        return None
    if q.returncode != 0 or not q.stdout.strip():
        return None
    util, used, total = (x.strip() for x in q.stdout.strip().splitlines()[0].split(","))
    info: dict[str, Any] = {
        "util_pct": int(float(util)), "mem_used_mb": int(float(used)),
        "mem_total_mb": int(float(total)),
    }
    try:
        p = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=process_name,used_memory",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        procs = []
        for line in p.stdout.strip().splitlines():
            if "," in line:
                nm, mb = line.rsplit(",", 1)
                procs.append({"name": friendly_name(nm.strip().rsplit("\\", 1)[-1]),
                              "mem_mb": int(float(mb))})
        info["procs"] = sorted(procs, key=lambda x: -x["mem_mb"])
    except Exception:
        info["procs"] = []
    return info


def _media_app_name(aumid: str) -> str:
    """Turn a media-session app id into a friendly name. AUMIDs are messy
    ('SpotifyAB.SpotifyMusic_zpd...!Spotify', 'chrome.exe') — map the common ones."""
    low = aumid.lower()
    for frag, name in (("spotify", "Spotify"), ("chrome", "Chrome"), ("firefox", "Firefox"),
                       ("msedge", "Edge"), ("edge", "Edge"), ("vlc", "VLC"),
                       ("music", "Media Player"), ("itunes", "iTunes"), ("tidal", "Tidal")):
        if frag in low:
            return name
    token = aumid.split("!")[0].rsplit("\\", 1)[-1]
    if not token:
        return "media"
    # Some apps register an opaque hex AUMID (a browser PWA shows up as
    # 'CA9422711AE1A81C'). Printing that helps nobody — it reads like a bug.
    bare = token.replace("-", "").replace("_", "")
    if len(bare) >= 8 and all(c in "0123456789abcdefABCDEF" for c in bare):
        return "media"
    return friendly_name(token)


def now_playing() -> Optional[dict[str, Any]]:
    """Current media (Spotify/YouTube/etc.) via the Windows media-transport API.
    Requires the optional `winsdk` package; returns None without it.

    The WinRT calls are async, so we run them on a throwaway thread with its own
    event loop — safe to call from inside Continuum's async server loop."""
    if not _WIN:
        return None
    try:
        import winsdk.windows.media.control as wmc  # type: ignore
    except Exception:
        return None
    import asyncio
    import threading

    box: dict[str, Any] = {}

    def _worker() -> None:
        async def _go():
            mgr = await wmc.GlobalSystemMediaTransportControlsSessionManager.request_async()
            sess = mgr.get_current_session()
            if not sess:
                return None
            props = await sess.try_get_media_properties_async()
            status = int(sess.get_playback_info().playback_status)  # 4=Playing 5=Paused
            title = (props.title or "").strip()
            if not title:
                return None
            app = _media_app_name(sess.source_app_user_model_id or "")
            return {"title": title, "artist": (props.artist or "").strip(),
                    "app": app, "playing": status == 4}

        # new_event_loop() lives INSIDE the guard on purpose. It can fail on its
        # own — no selector available, a policy that refuses, the interpreter
        # tearing down — and when it threw out here the worker thread died with
        # an unhandled exception, `box` stayed empty, and the caller read the
        # `.get("v")` default: None, which it reports as "nothing is playing".
        # A hard failure wearing the costume of a fact, with no log line
        # anywhere. Every path through here now writes `box` and says so.
        loop = None
        try:
            loop = asyncio.new_event_loop()
            box["v"] = loop.run_until_complete(_go())
        except Exception:
            log.debug("now-playing probe failed", exc_info=True)
            box["v"] = None
        finally:
            if loop is not None:  # nothing to close if creation is what failed
                loop.close()

    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    th.join(timeout=3)
    return box.get("v")


_MEDIA_COMMANDS = ("play", "pause", "playpause", "next", "previous", "stop")


def media_control(command: str) -> tuple[bool, str]:
    """Drive whatever is currently playing. Returns (ok, message).

    Same throwaway-thread pattern as now_playing() — the WinRT transport calls
    are async and must not touch Continuum's event loop. This goes through the
    system media-transport session, not app-specific APIs, so it works for
    anything that registers one: Spotify, browsers, VLC, Media Player.
    """
    command = (command or "").strip().lower()
    if command not in _MEDIA_COMMANDS:
        return False, f"unknown media command '{command}'"
    if not _WIN:
        return False, "media control is Windows-only"
    try:
        import winsdk.windows.media.control as wmc  # type: ignore
    except Exception:
        return False, "media control needs the winsdk package"
    import asyncio
    import threading

    box: dict[str, Any] = {}

    def _worker() -> None:
        async def _go():
            mgr = await wmc.GlobalSystemMediaTransportControlsSessionManager.request_async()
            sess = mgr.get_current_session()
            if not sess:
                return False, "nothing is playing"
            # try_*_async returns False when the app refuses the command (e.g.
            # skip on a live stream) — that's a real outcome, not an error.
            if command == "play":
                ok = await sess.try_play_async()
            elif command == "pause":
                ok = await sess.try_pause_async()
            elif command == "playpause":
                ok = await sess.try_toggle_play_pause_async()
            elif command == "next":
                ok = await sess.try_skip_next_async()
            elif command == "previous":
                ok = await sess.try_skip_previous_async()
            else:
                ok = await sess.try_stop_async()
            app = _media_app_name(sess.source_app_user_model_id or "")
            return bool(ok), (f"{command} sent to {app}" if ok
                              else f"{app} refused {command}")

        # Same reasoning as now_playing(): loop creation belongs inside the try,
        # or a failure there kills the thread before `box` is written and the
        # caller is told the command "timed out" — the one explanation that is
        # definitely wrong, and the one that invites a pointless retry.
        loop = None
        try:
            loop = asyncio.new_event_loop()
            box["v"] = loop.run_until_complete(_go())
        except Exception as exc:
            log.debug("media control (%s) failed", command, exc_info=True)
            box["v"] = (False, f"media control failed ({type(exc).__name__})")
        finally:
            if loop is not None:
                loop.close()

    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    th.join(timeout=5)
    return box.get("v", (False, "media control timed out"))


def sleep_monitors() -> tuple[bool, str]:
    """Put the displays into power-save (screen off). Returns (ok, message).

    Broadcasts WM_SYSCOMMAND / SC_MONITORPOWER with lParam 2 (power off), the
    standard Windows way to sleep the screen without sleeping the machine — the
    rest of the session keeps running and any input (mouse/key) wakes it. We use
    SendMessageTimeout with SMTO_ABORTIFHUNG so a single unresponsive top-level
    window can't hang the broadcast (and with it Continuum's event loop).
    """
    if not _WIN:
        return False, "sleeping the monitors is Windows-only"
    try:
        import ctypes

        HWND_BROADCAST = 0xFFFF
        WM_SYSCOMMAND = 0x0112
        SC_MONITORPOWER = 0xF170
        MONITOR_OFF = 2
        SMTO_ABORTIFHUNG = 0x0002

        res = ctypes.c_ulong(0)
        ret = ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, MONITOR_OFF,
            SMTO_ABORTIFHUNG, 1000, ctypes.byref(res))
        if not ret:
            return False, "the system didn't accept the sleep request"
        return True, "monitors sleeping — move the mouse or press a key to wake them"
    except Exception as exc:
        return False, f"couldn't sleep the monitors ({type(exc).__name__})"


def suspend() -> tuple[bool, str]:
    """Put the whole machine into S3 standby. Returns (ok, message).

    Three arguments, and two of them are traps.

    `bHibernate=0` asks for suspend rather than hibernate. Worth calling through
    ctypes rather than the usual `rundll32 powrprof.dll,SetSuspendState 0,1,0`
    recipe: rundll32 parses that argument string loosely and lands on hibernate
    whenever hibernation is enabled — which it is here (`powercfg /a` lists
    Hibernate as available). Hibernate is S4 with the machine powered off, a
    materially different wake path from the S3 this is aiming at.

    `bWakeupEventsDisabled=0` matters most, and it reads backwards: passing 1
    suspends with wake events DISABLED, which is exactly a machine that will not
    answer a magic packet. The whole reason to sleep rather than shut down is
    that it can be woken again, so this argument can only ever be 0 here.

    The call does not return until the machine resumes, so nothing the resume
    needs may be held across it. `deskops` runs it on a thread and the socket to
    the hub simply drops on the way down — expected, and the client redials.
    """
    if not _WIN:
        return False, "sleeping the machine is Windows-only"
    try:
        import ctypes

        # BOOLEAN is a byte; declaring it stops ctypes guessing at widths.
        powrprof = ctypes.windll.powrprof
        powrprof.SetSuspendState.argtypes = [ctypes.c_ubyte] * 3
        powrprof.SetSuspendState.restype = ctypes.c_ubyte
        ok = powrprof.SetSuspendState(0, 0, 0)
        if not ok:
            return False, "the system refused the suspend request"
        # Only reached on resume. Saying so beats "sleeping", which would be a
        # claim about a moment that has already passed by the time it is read.
        return True, "the machine slept and has now resumed"
    except Exception as exc:
        return False, f"couldn't sleep the machine ({type(exc).__name__})"


def restart(delay: int = 5) -> tuple[bool, str]:
    """Reboot the machine after `delay` seconds. Returns (ok, message).

    The delay is not politeness, it is what makes the answer deliverable. This
    runs on the desk at the end of a socket the hub is waiting on; rebooting at
    t=0 tears that socket down before the result crosses it, so the phone that
    asked sees a timeout and cannot distinguish "rebooting now" from "never
    heard me". A few seconds is longer than the round trip and shorter than
    anyone's patience.

    `shutdown.exe` rather than `ExitWindowsEx`: it already owns the privilege
    juggling, the delay, and a documented abort path (`shutdown /a`) for the
    window in which someone changes their mind.
    """
    if not _WIN:
        return False, "restarting the machine is Windows-only"
    delay = max(0, min(int(delay), 600))
    try:
        import subprocess

        proc = subprocess.run(
            ["shutdown", "/r", "/t", str(delay), "/c", "Continuum: restarting"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            return False, f"the system refused the restart ({detail or proc.returncode})"
        return True, f"restarting in {delay}s — `shutdown /a` at the desk cancels it"
    except Exception as exc:
        return False, f"couldn't restart the machine ({type(exc).__name__})"


def wifi_ssid() -> Optional[str]:
    """Connected Wi-Fi network name — a cheap proxy for where Jake is."""
    if not _WIN:
        return None
    import subprocess
    try:
        r = subprocess.run(["netsh", "wlan", "show", "interfaces"],
                           capture_output=True, text=True, timeout=4,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        return None
    for line in r.stdout.splitlines():
        s = line.strip()
        # Match "SSID : x" but not "BSSID : ..."
        if s.lower().startswith("ssid") and not s.lower().startswith("bssid") and ":" in s:
            val = s.split(":", 1)[1].strip()
            return val or None
    return None


def recent_files(limit: int = 12) -> list[dict[str, Any]]:
    """Recently opened documents (Windows Recent shortcuts) + fresh Downloads,
    newest first. Uses the .lnk stem as the document name (no COM needed)."""
    import glob
    out: list[dict[str, Any]] = []
    appdata = os.environ.get("APPDATA")
    home = os.path.expanduser("~")
    roots = []
    if appdata:
        roots.append((os.path.join(appdata, r"Microsoft\Windows\Recent"), "*.lnk"))
    roots.append((os.path.join(home, "Downloads"), "*"))
    for folder, pat in roots:
        try:
            for path in glob.glob(os.path.join(folder, pat)):
                if os.path.isdir(path):
                    continue
                name = os.path.basename(path)
                src = "recent" if pat == "*.lnk" else "downloads"
                if name.lower().endswith(".lnk"):
                    name = name[:-4]
                # Recent holds folder/app jumplist junk too — keep only things that
                # look like actual opened documents (have a file extension).
                if src == "recent" and (("---source=" in name) or "." not in name):
                    continue
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    continue
                out.append({"name": name, "mtime": mtime, "source": src})
        except Exception:
            continue
    out.sort(key=lambda f: f["mtime"], reverse=True)
    # de-dup by name, keep newest
    seen: set[str] = set()
    uniq = []
    for f in out:
        if f["name"] in seen:
            continue
        seen.add(f["name"])
        uniq.append(f)
    return uniq[:limit]


def clipboard_text(max_len: int = 400) -> Optional[str]:
    """Current clipboard text (truncated). Privacy-sensitive — only ever called
    through the gated read tool, never sampled ambiently."""
    if not _WIN:
        return None
    import ctypes

    CF_UNICODETEXT = 13
    u = ctypes.windll.user32
    k = ctypes.windll.kernel32
    # HANDLE/pointer returns are 64-bit — declare them or ctypes truncates to int
    # and dereferencing the truncated address segfaults.
    u.GetClipboardData.restype = ctypes.c_void_p
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalLock.argtypes = [ctypes.c_void_p]
    k.GlobalUnlock.argtypes = [ctypes.c_void_p]
    if not u.OpenClipboard(0):
        return None
    try:
        if not u.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None
        handle = u.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = k.GlobalLock(handle)
        if not ptr:
            return None
        try:
            text = ctypes.c_wchar_p(ptr).value or ""
        finally:
            k.GlobalUnlock(handle)
    finally:
        u.CloseClipboard()
    text = text.strip()
    if len(text) > max_len:
        text = text[:max_len] + f"… (+{len(text) - max_len} chars)"
    return text or None


def set_clipboard_text(text: str) -> tuple[bool, str]:
    """Put text on the clipboard. Returns (ok, message).

    Destructive by nature: the clipboard holds exactly one thing, so writing
    throws away whatever Jake had copied. The tool layer warns about that; here
    we just do it correctly.
    """
    if not _WIN:
        return False, "clipboard writing is Windows-only"
    if not text:
        return False, "nothing to write"
    import ctypes

    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    u = ctypes.windll.user32
    k = ctypes.windll.kernel32
    # Same 64-bit handle trap as the reader: undeclared restypes get truncated
    # to int, and memmove into a truncated address corrupts memory.
    k.GlobalAlloc.restype = ctypes.c_void_p
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalLock.argtypes = [ctypes.c_void_p]
    k.GlobalUnlock.argtypes = [ctypes.c_void_p]
    k.GlobalFree.argtypes = [ctypes.c_void_p]
    u.SetClipboardData.restype = ctypes.c_void_p
    u.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]

    buf = ctypes.create_unicode_buffer(text)
    size = ctypes.sizeof(buf)
    if not u.OpenClipboard(0):
        return False, "another app is holding the clipboard open"
    try:
        u.EmptyClipboard()
        handle = k.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            return False, "could not allocate clipboard memory"
        ptr = k.GlobalLock(handle)
        if not ptr:
            k.GlobalFree(handle)
            return False, "could not lock clipboard memory"
        try:
            ctypes.memmove(ptr, buf, size)
        finally:
            k.GlobalUnlock(handle)
        if not u.SetClipboardData(CF_UNICODETEXT, handle):
            # Only free on failure — on success the system owns the handle and
            # freeing it here would be a double free.
            k.GlobalFree(handle)
            return False, "the clipboard rejected the write"
    finally:
        u.CloseClipboard()
    return True, f"copied {len(text)} chars to the clipboard"
