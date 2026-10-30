"""What can only be done at Jake's desk — and how to get there from anywhere.

Before the hub moved to Linux, "the desk" and "the process asking about the desk"
were the same machine, so every one of these lived inline in `tools.py` as a
direct call into Windows. After the move they were still called, still on the
hub, and still answered: `list_open_apps` said "no open apps", `read_clipboard`
said "clipboard is empty", `browsing_history` said "no browser history
available". Each is a claim about Jake's desktop, and each was derived from a VM
in Germany. `recent_files` and `inspect_app` were worse — `~/Downloads` and
psutil both work perfectly well on Linux, so they returned real data about
entirely the wrong machine.

The shape of the fix is the same one `notify()` got: the capability doesn't
belong to whichever process happens to hold it, it belongs to the *desk*, and
the desk may be on the other end of a socket. So this module holds each
operation once, as a function returning a plain dict, and a router that decides
per call where to run it:

  1. a desktop client is attached  -> send it down the socket
  2. otherwise, this host is a desk -> do it here, as it always did
  3. otherwise                      -> say plainly that nobody can answer

Both ends import this. The client's command table *is* `OPS`, so a capability
cannot exist on one side and be missing on the other — the failure that made
`notify` look implemented for a month when only the receiver existed.

Why dicts rather than the prose the model reads: the wire needs something
JSON-able, and the phrasing belongs next to the other tool output in `tools.py`.
Ops that are inherently one sentence ("launched Spotify") return `{"message":
...}` and skip the ceremony.

Errors come back as `{"error": ...}` rather than raising. A tool handler that
gets an error dict can tell the model what went wrong and *which machine* it
went wrong on; an exception crossing a socket loses both.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from typing import Any, Callable, Optional

log = logging.getLogger("continuum.deskops")

# Model-supplied text reaching `start`, which re-parses its command line: see the
# long note in `_open_app`. Duplicated deliberately from tools.py's early gate —
# that one refuses before the action is audited, this one guards the subprocess
# itself, and the subprocess is what actually matters.
_SAFE_APP_NAME = re.compile(r"^[A-Za-z0-9 ._:\\/+'-]+$")

# A screenshot crosses the wire base64'd, which costs a third on top. uvicorn
# accepts 16MB frames, so this is not the transport's limit — it's a refusal to
# spend ten seconds shipping something the vision model will downscale anyway.
# `screen.capture` already caps the long edge at 1600px; blowing past this means
# something is wrong, and an error beats a stalled socket.
MAX_SCREENSHOT_B64 = 6 * 1024 * 1024

# A rendered character portrait is a real image, not a downscaled screenshot, so
# it gets its own larger ceiling. A 832x1216 PNG lands around 1.5MB, ~2MB once
# base64'd; this leaves room for a hi-res pass without approaching uvicorn's
# 16MB frame limit.
MAX_IMAGE_B64 = 12 * 1024 * 1024

# Generation is the one desk op measured in minutes. A 24-step render with a
# hi-res second pass runs 30-90s on the 4070 SUPER, and longer if the model has
# to be swapped into VRAM first, so the HTTP read here has to outlast that — and
# the command timeout on the hub side has to outlast this.
A1111_HTTP_TIMEOUT = 300.0

# The identity renderer's ceiling. Three sampler passes (layout, face detail,
# composite) plus insightface's model load on a cold process, against A1111's single
# pass — measured at 60-170s per render across six scenes. The ceiling is sized for a
# cold start on a card that has just handed its memory back, not for the normal case.
#
# Up here beside its A1111 peer and not next to `_idrender`, because `TIMEOUTS` is
# built immediately below and a constant defined further down the file is a NameError
# at import — which is to say, an outage, not a bug report.
IDRENDER_TIMEOUT = 600.0

NO_DESK = ("no desktop is reachable — Continuum is running on the hub and no "
           "desktop client is attached, so nothing here can see Jake's machine")

NO_RENDERER = ("Stable Diffusion is not running on Jake's PC — A1111 is started "
               "by hand and holds several GB of VRAM, so it is usually off")

# Most ops answer instantly. These do not, and the default 20s would cut them off
# mid-work and report a timeout for something that was about to succeed.
# `close_tab` is the pointed one: `close_firefox_tabs` has its own 20s internal
# budget and sleeps 0.15s per tab driving UIAutomation, so a 20s command timeout
# is guaranteed to lose that race rather than merely risk it.
TIMEOUTS: dict[str, float] = {
    "see_screen": 45.0,
    "close_tab": 45.0,
    "read_messages": 40.0,   # scrapes Discord/Slack windows via UIAutomation
    "project_state": 30.0,   # four sequential git spawns at 5s each
    "recent_files": 30.0,    # thousands of stats when Downloads is large
    # Must exceed A1111_HTTP_TIMEOUT, or the hub gives up on a render the desk
    # is still working on — and then retries it, queueing a second job behind
    # the first on a GPU that can only do one at a time.
    "txt2img": A1111_HTTP_TIMEOUT + 30.0,
    # Its own entry rather than sharing `txt2img`'s, because the identity path is
    # three sampler passes to A1111's one and folding them into a single number
    # would mean either cutting identity renders short or making every wedged A1111
    # hold the hub for twice as long. The two renderers fail on different clocks.
    "idrender": IDRENDER_TIMEOUT + 30.0,
}


def local_desk() -> bool:
    """Is *this* process running on a machine someone could be sitting at?

    Windows, because every probe underneath this module is a Win32 call —
    `GetLastInputInfo` for idle, PowerShell for toasts, UIAutomation for tabs and
    message windows, the clipboard and media keys through user32. `probe.py`
    gates itself on exactly this test (`_WIN`, line 20); this is the same
    question asked one level up, where it can change the routing instead of just
    returning an error.

    Note what this is NOT: "is a desktop attached". The hub boots with nothing
    attached and would then answer every one of these itself — which is the
    original bug, not a fallback. A Linux hub is never a desk, whether or not
    Jake's machine has dialled in yet.
    """
    return sys.platform == "win32"


def attached_desk() -> Any:
    """The desktop client on the far end of a socket, or None.

    Imported lazily and defensively: `notify` reaches for the same registry from
    the bottom of the import graph, and neither wants a device import failure to
    turn into a tool crash.
    """
    try:
        from .devices.remote import attached
    except Exception:  # pragma: no cover — devices are always importable
        log.debug("device registry unavailable", exc_info=True)
        return None
    return attached("desktop")


def reachable() -> bool:
    """Can *any* machine answer a desk question right now?

    Asked fresh every time the tool schemas are built, never cached: the hub
    starts with nothing attached, so a value read at construction would be wrong
    for exactly as long as the process lives.
    """
    return attached_desk() is not None or local_desk()


def where() -> str:
    """Which machine is about to answer — for prose that has to say so."""
    dev = attached_desk()
    if dev is not None:
        return getattr(dev, "name", "desktop")
    return "this machine" if local_desk() else "nowhere"


# ---- the operations ---------------------------------------------------------
#
# Every one takes `(tracker, **params)` and returns a dict. `tracker` is the
# live `StateTracker` this host owns, or None. Only two ops need it — `inspect`
# resolves a name against apps it has been watching over time, and
# `project_state` needs the focused pid to find a working directory — but the
# signature stays uniform so `OPS` can be a flat table on both ends.


def _inspect_app(tracker: Any, app: str = "", **_: Any) -> dict[str, Any]:
    if tracker is None:
        return {"error": "no state tracker on this host"}
    return tracker.inspect(app)


def _browsing_history(tracker: Any, limit: int = 12, **_: Any) -> dict[str, Any]:
    from .state import browser
    h = browser.firefox_history(int(limit or 12))
    return h or {}


def _recent_files(tracker: Any, limit: int = 12, **_: Any) -> dict[str, Any]:
    from .state import probe
    return {"files": probe.recent_files(int(limit or 12))}


def _read_messages(tracker: Any, app: Optional[str] = None, limit: int = 12,
                   **_: Any) -> dict[str, Any]:
    from .state import messages
    return {"apps": messages.read_messages(app, int(limit or 12))}


def _read_clipboard(tracker: Any, **_: Any) -> dict[str, Any]:
    from .state import probe
    return {"text": probe.clipboard_text()}


def _write_clipboard(tracker: Any, text: str = "", **_: Any) -> dict[str, Any]:
    from .state import probe
    ok, msg = probe.set_clipboard_text(str(text or ""))
    return {"ok": ok, "message": msg}


def _see_screen(tracker: Any, mode: str = "window", **_: Any) -> dict[str, Any]:
    """Grab the screen. Only the grab — the reading happens wherever the vision
    model is configured, which is the hub, and a screenshot is small enough to
    ship but an API key is not something the desktop should need."""
    from .state import screen
    png, meta = screen.capture(str(mode or "window"))
    b64 = base64.b64encode(png).decode("ascii")
    if len(b64) > MAX_SCREENSHOT_B64:
        return {"error": f"screenshot too large to send ({len(b64) // 1024}KB encoded)"}
    return {"png_b64": b64, "meta": meta}


def _project_state(tracker: Any, path: Optional[str] = None,
                   **_: Any) -> dict[str, Any]:
    """Git snapshot of a project. Without a path, use the directory of the
    currently-focused app (the editor/terminal being worked in).

    Lives here rather than in `tools.py` because both halves are desk-bound: the
    focused pid comes from this host's tracker, and `psutil.Process(pid).cwd()`
    can only be asked of the machine running that process. On the hub it used to
    resolve the *hub's* foreground — which, headless, is nothing — and then fall
    through to reporting no project in focus. Plausible, and wrong.
    """
    cwd = path
    if not cwd and tracker is not None:
        fg = (tracker.basic() or {}).get("foreground")
        if fg:
            try:
                import psutil
                cwd = psutil.Process(fg["pid"]).cwd()
            except Exception:
                cwd = None
    if not cwd or not os.path.isdir(cwd):
        return {"text": "no project directory in focus (pass a path)"}

    def _git(*a: str):
        # `text=True` alone decodes with the ANSI codepage on Windows (cp1252
        # here), so any commit message or filename carrying UTF-8 bytes — an
        # accent, an em-dash, an emoji — comes back mojibake and gets quoted to
        # Jake as if that were the real text.
        return subprocess.run(["git", "-C", cwd, *a], capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=5,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    try:
        root = _git("rev-parse", "--show-toplevel")
        if root.returncode != 0:
            return {"text": f"{cwd} is not a git repository"}
        top = root.stdout.strip()
        branch = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        # `status` is the one call whose failure lies rather than blanks: a
        # non-zero exit gives empty stdout, which counts as zero dirty files and
        # reports a clean tree. "You have no uncommitted work" is a claim Jake
        # might act on, so it has to be earned, not defaulted.
        status = _git("status", "--porcelain")
        if status.returncode != 0:
            return {"text": (f"{os.path.basename(top)} [{top}]: branch "
                             f"{branch or '(unknown)'}, but `git status` failed "
                             f"({status.stderr.strip()[:120] or 'no output'}) "
                             f"— uncommitted-file count unknown")}
        dirty = [ln for ln in status.stdout.splitlines() if ln.strip()]
        last = _git("log", "-1", "--format=%h %s (%cr)").stdout.strip()
        return {"text": (f"{os.path.basename(top)} [{top}]: branch "
                         f"{branch or '(unknown)'}, {len(dirty)} uncommitted "
                         f"file(s), last commit: {last or '(none)'}")}
    except Exception as exc:
        return {"error": f"could not read git state: {exc}"}


def _open_url(tracker: Any, url: str = "", **_: Any) -> dict[str, Any]:
    url = str(url or "").strip()
    if not url.startswith(("http://", "https://")):
        return {"error": "only http(s) URLs"}
    # `webbrowser.open()` reports whether it found a browser to hand the URL to,
    # and on a headless box it returns False rather than raising — which is how
    # the hub spent a month reporting "opened <url>" for pages nobody saw.
    if not webbrowser.open(url):
        return {"error": f"no browser available on this machine to open {url}"}
    return {"message": f"opened {url}"}


def _open_app(tracker: Any, name: str = "", **_: Any) -> dict[str, Any]:
    app = str(name or "").strip()
    if not app:
        return {"error": "no app name"}
    # `shell=False` buys nothing here, because the child *is* cmd.exe and it
    # re-parses the flat command line `list2cmdline` builds. That function quotes
    # only on whitespace, so any metacharacter without a space went through raw:
    # `open_app("notepad&calc")` became `cmd /c start "" notepad&calc` and ran
    # both. `\"` doesn't save us either — cmd ignores backslash-escaping, so
    # `x"&whoami&"y` also executes. Since `app` is model-supplied text (and the
    # model may be summarising an attacker-controlled page or tab at the time),
    # that handed back exactly the capability the separate CONTINUUM_SHELL gate
    # on `run_shell` exists to withhold.
    #
    # Keeping `start` (it resolves PATH *and* the App Paths registry, which is
    # how bare names like "spotify" work at all) means the only safe move is to
    # refuse anything that could be syntax. Allowed: names and real paths.
    if not _SAFE_APP_NAME.match(app):
        return {"error": ("app name may only contain letters, digits, spaces and "
                          ". _ - : \\ / + ' — that looked like shell syntax")}
    if not local_desk():
        return {"error": "launching apps is Windows-only"}
    # CREATE_NO_WINDOW hides the `cmd` shim, not the app it launches — `start`
    # hands that its own window.
    subprocess.Popen(["cmd", "/c", "start", "", app], shell=False,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return {"message": f"launched {app}"}


def _media_control(tracker: Any, command: str = "", **_: Any) -> dict[str, Any]:
    from .state import probe
    ok, msg = probe.media_control(str(command or ""))
    return {"ok": ok, "message": msg}


def _close_tab(tracker: Any, match: str = "", **_: Any) -> dict[str, Any]:
    match = str(match or "").strip()
    # Refuse an empty match here too, so a stray call can't target every tab.
    if not match:
        return {"error": "name which tabs to close — an empty match would target every tab"}
    from .state import tabs
    ok, msg, closed = tabs.close_firefox_tabs(match)
    return {"ok": ok, "message": msg, "closed": closed}


def _sleep_monitors(tracker: Any, **_: Any) -> dict[str, Any]:
    from .state import probe
    ok, msg = probe.sleep_monitors()
    return {"ok": ok, "message": msg}


# ---- machine power ----------------------------------------------------------
#
# The asymmetry worth stating once, because it decides the whole design: sleep
# and restart are things the desk does to ITSELF, so they belong here, on the
# socket the desk already holds open, and they work from anywhere Jake can reach
# the hub. Waking does not and cannot — a sleeping machine holds no socket and
# runs no code. It lives in `app/server.py` against the phone instead, because
# waking is a thing done to the desk from OUTSIDE it, by something on its LAN.
#
# Neither op blocks on the outcome, for the same reason in two forms: the reply
# has to cross the socket before the machine that would carry it goes down.

#: Long enough for the result to travel desk -> hub -> phone, short enough that
#: nobody watching the screen thinks the button missed.
POWER_DELAY_S = 5.0


def _sleep_pc(tracker: Any, delay: float = POWER_DELAY_S, **_: Any) -> dict[str, Any]:
    """Suspend the machine to S3, shortly after replying.

    `probe.suspend()` does not return until the machine RESUMES, which may be
    tomorrow. Calling it inline would leave the hub — and the phone behind it —
    waiting on a request that stays open across the sleep, so the button would
    look broken for exactly as long as the machine was asleep. Deferring to a
    daemon timer turns a blocking call into a scheduled one, and the reply gets
    out first.
    """
    from .state import probe

    if not local_desk():
        return {"error": "sleeping the machine is Windows-only"}

    delay = max(0.0, min(float(delay), 600.0))
    # Daemon, so a pending sleep can never hold up a client shutdown; the resume
    # value is discarded because nothing is left waiting to read it.
    t = threading.Timer(delay, probe.suspend)
    t.daemon = True
    t.start()
    return {"ok": True, "message": f"sleeping in {int(delay)}s"}


def _restart_pc(tracker: Any, delay: int = int(POWER_DELAY_S), **_: Any) -> dict[str, Any]:
    """Reboot the machine. The delay lives in `shutdown.exe`, so this returns at
    once and, unlike sleep, needs no timer of its own."""
    from .state import probe
    ok, msg = probe.restart(delay)
    return {"ok": ok, "message": msg}


# ---- A1111 VRAM lifecycle ---------------------------------------------------
#
# A loaded checkpoint sits in VRAM doing nothing between renders. That is the
# right default while Jake is using it — the weights are what make a render 21s
# instead of 70s — but it costs 6.6GB of a 12GB card indefinitely, which is why
# A1111 has historically been started by hand and closed again.
#
# Unloading is cheap and reversible, so the server can stay up permanently and
# simply give the memory back when idle. The catch found by testing: A1111 does
# *not* reload on demand. A txt2img against an unloaded model returns HTTP 500
# ("Expected all tensors to be on the same device, cpu and cuda:0") because
# `sd_checkpoints_keep_in_cpu` parks the weights in system RAM and nothing moves
# them back. So an unload is only safe when paired with `_ensure_model_loaded`
# below — that pairing is the whole feature, and either half alone is a bug.
A1111_IDLE_UNLOAD_S = 15 * 60.0

# A loaded checkpoint is ~6.6GB; an unloaded one leaves ~20MB of CUDA context
# behind. Anything in between is not a state A1111 produces, so a 1GB line
# separates them with enormous margin either way.
A1111_LOADED_MIN_BYTES = 1024 ** 3


def a1111_base() -> str:
    """Where the renderer is, per *this* machine — never taken from the wire."""
    return (os.environ.get("CONTINUUM_A1111")
            or "http://127.0.0.1:7860").rstrip("/")


def _a1111(path: str, method: str = "GET",
           timeout: float = 30.0) -> Optional[dict]:
    """Call a bare A1111 endpoint. None means "could not ask" — never raises.

    Every caller here is housekeeping around a render, so a renderer that is
    down or wedged must degrade to "do nothing" rather than surface as a failed
    render. The one error that matters, ConnectionRefused, is already reported
    precisely by `_txt2img` itself.
    """
    req = urllib.request.Request(a1111_base() + path, method=method,
                                 headers={"Content-Type": "application/json"},
                                 data=b"{}" if method == "POST" else None)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8") or "{}"
        return json.loads(body)
    except Exception:
        return None


def _a1111_allocated() -> Optional[int]:
    """Bytes of live CUDA tensors, or None if A1111 could not be reached."""
    mem = _a1111("/sdapi/v1/memory", timeout=10.0)
    try:
        return int(mem["cuda"]["allocated"]["current"])   # type: ignore[index]
    except Exception:
        return None


def _last_job_ts(progress: dict) -> Optional[float]:
    """When A1111 last *started* a job, as epoch seconds.

    A1111 stamps this itself, so it covers renders Jake fires from the web UI
    as well as ours — which polling for "busy right now" would miss entirely
    unless the poll happened to land inside a 21s render. It is the start of
    the job rather than the end, but against a 15-minute threshold that skew
    does not matter.
    """
    raw = str((progress.get("state") or {}).get("job_timestamp") or "")
    try:
        return time.mktime(time.strptime(raw, "%Y%m%d%H%M%S"))
    except Exception:
        return None


def _ensure_model_loaded() -> None:
    """Put the checkpoint back in VRAM if the idle watcher took it out.

    Deliberately silent and best-effort: if A1111 is down this does nothing and
    lets the render below produce the canonical NO_RENDERER error, rather than
    inventing a second, worse-worded version of it here.
    """
    allocated = _a1111_allocated()
    if allocated is None or allocated >= A1111_LOADED_MIN_BYTES:
        return
    _a1111("/sdapi/v1/reload-checkpoint", method="POST", timeout=180.0)


def a1111_idle_unload(idle_after: float = A1111_IDLE_UNLOAD_S,
                      floor: float = 0.0) -> Optional[str]:
    """Free the checkpoint's VRAM if nothing has rendered for `idle_after`.

    Returns a log line when it actually unloads, else None. `floor` is the
    watcher's own start time, used when A1111 has never run a job and so has no
    timestamp of its own to age against.

    Refuses to act while a job is running: `job_count` is non-zero for the whole
    of a render, and pulling the weights out from under one would fail Jake's
    image and leave the server in the broken state described above.
    """
    progress = _a1111("/sdapi/v1/progress", timeout=10.0)
    if progress is None:
        return None                      # not running: nothing to free
    if (progress.get("state") or {}).get("job_count"):
        return None                      # mid-render
    # The floor is a *fallback*, not a minimum: an A1111 that has genuinely been
    # idle for hours should give its memory back on the next tick, not wait out
    # another full threshold just because the client restarted a moment ago.
    last = _last_job_ts(progress)
    idle_since = floor if last is None else last
    if not idle_since or (time.time() - idle_since) < idle_after:
        return None
    allocated = _a1111_allocated()
    if allocated is None or allocated < A1111_LOADED_MIN_BYTES:
        return None                      # already unloaded
    if _a1111("/sdapi/v1/unload-checkpoint", method="POST", timeout=120.0) is None:
        return None
    # Plain ASCII: this goes to a log file the Windows console also renders, and
    # an em-dash there arrives as a replacement char.
    return (f"A1111 idle {int(time.time() - idle_since) // 60}m - "
            f"unloaded checkpoint, freed {allocated / 1024 ** 3:.1f}GB of VRAM")


# ---- the identity renderer (ComfyUI + InstantID) ----------------------------
#
# A second renderer, for the look A1111 cannot produce: the same man's face across
# every picture, from one reference photograph and no per-character training. It is
# ComfyUI rather than A1111 because InstantID needs a graph — an IP-adapter, a
# ControlNet and a face analyser wired together — and A1111's txt2img API takes a
# prompt, not a topology.
#
# It runs as a SUBPROCESS in ComfyUI's own interpreter. The pipeline needs cv2,
# insightface and a CUDA onnxruntime; this venv has none of them, and installing a
# second copy of that stack to save a process launch per render is the wrong trade
# against a render measured in tens of seconds. See `idcli.py` for the contract.
#
# BOTH RENDERERS WANT THE SAME 12GB CARD, and that is why the arbitration lives here
# rather than in Jake's head. Nothing else on this machine competes for the card, so
# the desk can simply decide: whoever is about to render
# takes the memory, and the other one gives it back first.
def comfy_root() -> pathlib.Path:
    """Where ComfyUI is, per *this* machine — never taken from the wire.

    Same rule as `a1111_base`, and for a stronger reason: this one resolves to an
    interpreter that then gets executed. A root named over the websocket would be a
    remote-code-execution hole wearing a configuration field.
    """
    return pathlib.Path(os.environ.get("CONTINUUM_COMFYUI")
                        or r"C:\Users\kori\ComfyUI")


def comfy_base() -> str:
    """Where the ComfyUI server is. Local resolution, as above."""
    return (os.environ.get("CONTINUUM_COMFY_URL")
            or "http://127.0.0.1:8188").rstrip("/")


def comfy_free(unload_models: bool = True) -> bool:
    """Ask ComfyUI to give its VRAM back. True if it answered.

    The counterpart to `a1111_idle_unload`, and deliberately simpler: ComfyUI
    reloads a checkpoint on demand, so unloading has no paired `_ensure_loaded`
    obligation and cannot leave the server in the broken half-state A1111 does.
    Best-effort — a ComfyUI that is down has already freed its memory.
    """
    try:
        req = urllib.request.Request(
            comfy_base() + "/free",
            data=json.dumps({"unload_models": unload_models,
                             "free_memory": True}).encode("utf-8"),
            method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30.0):
            return True
    except Exception:
        return False


def a1111_release_for(other: str) -> Optional[str]:
    """Unload A1111's checkpoint so `other` can have the card. None if it refused.

    Unlike `a1111_idle_unload` there is no idle threshold: something needs the memory
    *now*. The mid-render guard stays, and is the one case that must not be overridden
    — Jake rendering from the web UI is a person waiting at a screen, and pulling the
    weights out from under him both loses his image and leaves A1111 in the state
    where every subsequent render fails with a device-mismatch 500.
    """
    progress = _a1111("/sdapi/v1/progress", timeout=10.0)
    if progress is None:
        return None                      # not running: nothing to free
    if (progress.get("state") or {}).get("job_count"):
        return f"A1111 is mid-render; not taking its VRAM for {other}"
    allocated = _a1111_allocated()
    if allocated is None or allocated < A1111_LOADED_MIN_BYTES:
        return None                      # already unloaded
    if _a1111("/sdapi/v1/unload-checkpoint", method="POST", timeout=120.0) is None:
        return None
    return (f"unloaded A1111 checkpoint, freed "
            f"{allocated / 1024 ** 3:.1f}GB of VRAM for {other}")


def _idrender(tracker: Any, payload: Optional[dict] = None,
              **_: Any) -> dict[str, Any]:
    """Render one image with the character's own face. Same contract as `_txt2img`.

    `{"png_b64": ...}` or `{"error": ...}`, byte-for-byte the same shape, because the
    caller is the same `transport` seam and a second error
    vocabulary there would mean two ways of saying "the GPU box is asleep".

    The request names a CHARACTER and three pieces of prose. It cannot name a path, a
    URL, a checkpoint or a graph node — see `idcli.reference_for`. That is stricter
    than the A1111 op beside it, deliberately: a ComfyUI graph is a worse thing to
    accept from the wire than a URL is, because its nodes read and write arbitrary
    files and, with custom nodes installed, execute code.
    """
    req = dict((payload or {}).get("identity_render") or {})
    if not req:
        return {"error": "idrender called without an identity_render block"}

    root = comfy_root()
    python = root / "venv" / "Scripts" / "python.exe"
    script = root / "idcli.py"
    # Checked separately so the message names which half is missing. "ComfyUI is not
    # installed" and "the bridge script was not deployed" need different fixes, and a
    # single combined check would report the wrong one half the time.
    if not python.exists():
        return {"error": f"no ComfyUI interpreter at {python}"}
    if not script.exists():
        return {"error": f"no identity bridge at {script}"}

    freed = a1111_release_for("the identity renderer")
    if freed and "mid-render" in freed:
        # Transient and self-resolving, so it is reported as its own case rather than
        # as a failure: the right response is to try again in a minute, not to debug.
        return {"error": f"RENDERER_BUSY: {freed}"}
    if freed:
        log.info("[idrender] %s", freed)

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="idrender_"))
    try:
        rpath, opath = tmp / "request.json", tmp / "result.json"
        # Bytes, with the encoding spelled out. `subprocess(text=True)` decodes with
        # the locale codec — cp1252 on this machine — and this project has twice been
        # bitten by that silently mangling non-ASCII on the way through.
        rpath.write_bytes(json.dumps(req).encode("utf-8"))
        try:
            proc = subprocess.run(
                [str(python), str(script), "--request", str(rpath),
                 "--result", str(opath)],
                cwd=str(root), capture_output=True, timeout=IDRENDER_TIMEOUT,
                check=False)
        except subprocess.TimeoutExpired:
            return {"error": f"the identity renderer did not finish in "
                             f"{IDRENDER_TIMEOUT:.0f}s"}

        if not opath.exists():
            # No result file at all: the interpreter never reached `main`, or was
            # killed. Distinct from a result file holding an error, which means the
            # render was attempted — the two need different fixes, so they get
            # different messages. stderr is the only evidence in this branch.
            tail = (proc.stderr or b"").decode("utf-8", "replace").strip()[-400:]
            return {"error": f"the identity renderer produced no result "
                             f"(rc={proc.returncode}): {tail or 'no output'}"}
        result = json.loads(opath.read_bytes().decode("utf-8"))
        if "error" in result:
            return {"error": result["error"]}

        png = pathlib.Path(result.get("png") or "")
        # The bridge returns a path and the bytes are read here, so there is exactly
        # one base64 implementation in the round trip. The path comes from the
        # renderer, not the wire, but it is still confined to ComfyUI's output tree —
        # a bug in the bridge should not become an arbitrary file read.
        try:
            inside = png.resolve().is_relative_to((root / "output").resolve())
        except (OSError, ValueError):
            inside = False
        if not inside:
            return {"error": f"the identity renderer named {png}, which is outside "
                             f"its output directory"}
        if not png.exists():
            return {"error": f"the identity renderer reported {png.name}, "
                             f"which does not exist"}
        b64 = base64.b64encode(png.read_bytes()).decode("ascii")
        if len(b64) > MAX_IMAGE_B64:
            return {"error": f"image too large to send ({len(b64) // 1024}KB encoded)"}

        # The scores ride along. They are the only thing that can tell "this is him"
        # from "this is a man" — and `gain` is the one to read, because a plain render
        # of a described man already scores ~0.44 against his own reference with no
        # adapter attached, so a bare identity figure proves nothing on its own.
        # `tile_face_px` rides along beside `face_px` because they are different
        # measurements and only one of them has a threshold. `face_px` is the face in
        # the layout, which on a full-body shot is ~120px and is *supposed* to be — the
        # whole reason for the detail pass is that the final frame's face is small.
        # `tile_face_px` is the face during that pass, and `idrender.MIN_TILE_FACE`
        # (235) applies to it alone. Forwarding only the first invites exactly the
        # mistake `_idwire_probe.py` made on its first run: reading the layout number
        # against the tile cliff, which goes red on a correct render, and a check that
        # fires on the healthy case is a check that gets switched off.
        out: dict[str, Any] = {"png_b64": b64}
        for k in ("identity", "baseline", "gain", "face_px", "tile_face_px",
                  "detailed", "note"):
            if result.get(k) not in (None, ""):
                out[k] = result[k]
        if result.get("note"):
            log.info("[idrender] %s: %s", result.get("character"), result["note"])
        return out
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _txt2img(tracker: Any, payload: Optional[dict] = None,
             **_: Any) -> dict[str, Any]:
    """Render one image on the GPU in Jake's PC.

    Here rather than on the hub because the hub has no GPU and could not reach
    this one anyway: A1111 is launched `--api` with no `--listen`, so it binds
    loopback and is invisible across the tailnet. The desk is the only process
    that can see it, which is exactly what this op table is for.

    The endpoint is resolved *here*, from this machine's own environment, and
    deliberately not taken from the wire. Letting the hub name the URL would
    turn the desktop client into a general-purpose request proxy sitting inside
    Jake's LAN — it would happily POST to a router admin page — and nothing about
    image generation needs that. The hub says what to draw; the desk decides
    where the renderer is.

    A1111 not running is the *expected* state, not a fault: Jake starts it by
    hand when he wants it, and it holds several GB of VRAM, so it is off far
    more often than on. That case is reported as its own error string because
    the caller's correct response differs — there is nothing to retry and
    nothing broken, the renderer is simply not up.
    """
    base = a1111_base()
    # The other half of the arbitration in `a1111_release_for`. Both renderers want
    # the same 12GB card, and an identity render leaves a checkpoint resident in
    # ComfyUI; loading A1111's on top of it is how this fails — as a CUDA OOM
    # several seconds into a render, which reads like a broken prompt. Best-effort
    # and free when ComfyUI is down, which is its usual state.
    comfy_free()
    # The idle watcher may have given this checkpoint's VRAM back. A1111 will
    # not fetch it again by itself — it fails the render instead — so putting it
    # back is this function's job, not the caller's.
    _ensure_model_loaded()
    body = json.dumps(dict(payload or {})).encode("utf-8")
    req = urllib.request.Request(base + "/sdapi/v1/txt2img", data=body,
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=A1111_HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        # ConnectionRefused arrives wrapped in URLError, and is the one failure
        # worth naming precisely — see above.
        if isinstance(getattr(exc, "reason", None), ConnectionRefusedError):
            return {"error": NO_RENDERER}
        return {"error": f"A1111 did not answer: {exc}"}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    images = data.get("images") or []
    if not images:
        return {"error": "A1111 returned no image"}
    b64 = images[0] or ""
    # A1111 sometimes prefixes a data URL header; strip it at the edge so the
    # far side never has to know that.
    if b64.startswith("data:") and "," in b64:
        b64 = b64.split(",", 1)[1]
    if len(b64) > MAX_IMAGE_B64:
        return {"error": f"image too large to send ({len(b64) // 1024}KB encoded)"}
    return {"png_b64": b64}


def op_for(payload: Optional[dict] = None) -> str:
    """Which renderer a txt2img-shaped payload wants: `"txt2img"` or `"idrender"`.

    One line, and it lives here rather than inline at the call site for one reason:
    the caller is in `continuum/app/server.py`, inside a method that needs a running
    event loop and a reachable desktop before it will execute a single statement. A
    conditional there is testable only by a suite that RESTATES it, and a check
    computed from the code it checks asserts self-consistency rather than
    correctness. As a named function it is the real decision, exercised directly,
    and the suite's job at the call site shrinks to "this is who is asked".

    The payload decides, not a setting. The sidecar is attached only when the look
    asks for the identity path (`look.Style.identity_path`), so the look choosing
    the prompt and the look choosing the renderer are one decision made once. A
    second switch on the server could disagree with it, and the disagreement would
    be silent in the worst possible way: the right scene, with the wrong man's face.
    """
    return "idrender" if (payload or {}).get("identity_render") else "txt2img"


OPS: dict[str, Callable[..., dict[str, Any]]] = {
    "inspect_app": _inspect_app,
    "browsing_history": _browsing_history,
    "recent_files": _recent_files,
    "read_messages": _read_messages,
    "read_clipboard": _read_clipboard,
    "write_clipboard": _write_clipboard,
    "see_screen": _see_screen,
    "project_state": _project_state,
    "open_url": _open_url,
    "open_app": _open_app,
    "media_control": _media_control,
    "close_tab": _close_tab,
    "sleep_monitors": _sleep_monitors,
    "sleep_pc": _sleep_pc,
    "restart_pc": _restart_pc,
    "txt2img": _txt2img,
    "idrender": _idrender,
}

# Reading the machine vs. acting on it. The split exists because the tool layer
# gates them differently — actions need `_actions_enabled` and land in the audit
# ledger — and because it documents, in one place, which of these can change
# anything.
READ_OPS = frozenset({"inspect_app", "browsing_history", "recent_files",
                      "read_messages", "read_clipboard", "see_screen",
                      "project_state"})
ACTION_OPS = frozenset(OPS) - READ_OPS


def run_local(op: str, params: Optional[dict] = None,
              tracker: Any = None) -> dict[str, Any]:
    """Carry out one operation on *this* machine. Synchronous — every underlying
    probe is a blocking Win32 call, so both callers hand it to a thread.

    Never raises. The desktop client is answering a hub sitting in
    `RemoteDevice.command()` on a timeout, and a refusal that arrives is
    diagnosable where a silence looks identical to a dead network.
    """
    fn = OPS.get(op)
    if fn is None:
        return {"error": f"unknown desk operation: {op}"}
    try:
        return fn(tracker, **(params or {}))
    except Exception as exc:
        log.exception("desk op %r failed", op)
        return {"error": f"{type(exc).__name__}: {exc}"}


async def run(op: str, params: Optional[dict] = None,
              tracker: Any = None) -> dict[str, Any]:
    """Carry out one operation on whichever machine can actually do it.

    Resolved per call, never cached. A desktop attaches minutes after the hub
    boots and detaches on a closed lid; anything decided at construction would
    outlive the socket it was decided for.
    """
    fn_params = dict(params or {})
    if op not in OPS:
        return {"error": f"unknown desk operation: {op}"}

    device = attached_desk()
    if device is not None:
        try:
            return await device.command(op, fn_params,
                                        timeout=TIMEOUTS.get(op, 20.0))
        except Exception as exc:
            # Includes the capability refusal from `command()` when an older
            # client build doesn't implement this op yet — worth surfacing
            # verbatim, because "desktop does not offer 'see_screen'" tells Jake
            # exactly what to update.
            return {"error": f"the desktop could not do {op}: {exc}"}

    if local_desk():
        return await asyncio.to_thread(run_local, op, fn_params, tracker)

    return {"error": NO_DESK}
