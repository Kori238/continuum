"""Notify — Continuum reaching out to you. Slice one: a Windows toast.

This is a *capability* the desktop device offers. Later, a phone device offers
push, a speaker offers TTS, etc. — all behind the same idea of "tell Jake."
Always prints to the console too, so the slice completes even if the toast fails.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import threading
import time
from typing import Any

log = logging.getLogger("continuum.notify")

_TOAST_PS = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
        [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$x = $t.GetElementsByTagName('text')
$x.Item(0).AppendChild($t.CreateTextNode($env:CONTINUUM_TITLE)) > $null
$x.Item(1).AppendChild($t.CreateTextNode($env:CONTINUUM_BODY)) > $null
$toast = [Windows.UI.Notifications.ToastNotification]::new($t)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Continuum').Show($toast)
"""

# A dead toast pipe used to be completely silent. The spawn is best-effort by
# design — the console line already carried the message — but "best-effort" had
# quietly become "never works and nobody knows": off Windows the spawn raises
# FileNotFoundError on *every* call, the tool layer still reports "notification
# shown", and the whole notification surface is muted with no evidence. That
# matters now that this is being prepared to run on a Linux host.
#
# Logging every failure would be the other kind of useless: if powershell is
# missing it's missing forever, so we'd emit one warning per agent turn until
# the log was worthless. So the first failure is loud, and after that we go
# quiet for a window and fold the suppressed count into the next line. Full
# per-call detail stays available at DEBUG for anyone actually chasing it.
_FAIL_LOG_EVERY_S = 300.0
_fail_lock = threading.Lock()   # _spawn_toast now runs on executor threads
_fail_last_log = 0.0
_fail_suppressed = 0


def _note_toast_failure(exc: BaseException) -> None:
    global _fail_last_log, _fail_suppressed
    now = time.monotonic()
    with _fail_lock:
        # _fail_last_log starts at 0.0, which is falsy — so the very first
        # failure always gets through, however early in the process it happens.
        if _fail_last_log and (now - _fail_last_log) < _FAIL_LOG_EVERY_S:
            _fail_suppressed += 1
            log.debug("toast failed: %s", exc)
            return
        suppressed, _fail_suppressed = _fail_suppressed, 0
        _fail_last_log = now
    extra = f" (+{suppressed} more since the last line)" if suppressed else ""
    log.warning("desktop toast failed — notifications are muted%s: %s: %s",
                extra, type(exc).__name__, exc, exc_info=True)


def _print_line(title: str, body: str) -> None:
    # Encode-safe: Windows' legacy console (cp1252) can't render emoji.
    line = f"\n  >> [{title}] {body}\n"
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"))


def _spawn_toast(title: str, body: str) -> None:
    """The actual PowerShell call. Blocking — a cold start is 0.5–2s, worst case
    the full 10s timeout — so this must never run on the event loop thread."""
    try:
        # Title and body ride in the ENVIRONMENT rather than being interpolated
        # into the script source above. Both come from untrusted-ish places (the
        # model's own output, remote messages), and a stray quote or a `$(...)`
        # in a notification body would otherwise be *executed* by powershell.
        # As env vars the script reads them as plain data. Don't "simplify" this
        # back into an f-string.
        env = {**os.environ, "CONTINUUM_TITLE": title, "CONTINUUM_BODY": body}
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _TOAST_PS],
            env=env, capture_output=True, timeout=10,
            # Without this a console window flashes on screen for every single
            # toast. The app runs windowless, so the child has no console to
            # inherit and Windows hands it a fresh visible one.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:  # toast is best-effort; console already delivered it
        _note_toast_failure(exc)


# A task with no reference anywhere is only weakly held by the loop, and the GC
# is entitled to collect it mid-flight — the classic way fire-and-forget work
# vanishes on a busy loop. Hold them here until they finish.
_pending: set = set()


def _report_forward(task: asyncio.Task) -> None:
    """A forwarded toast that failed must say so. The whole reason this path
    exists is that the notification surface went quiet without anyone noticing;
    swallowing the error here would rebuild exactly that."""
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        # Same rate limiting as a local toast failure: a desktop that is closed
        # is closed for a while, and one line per agent turn helps nobody.
        _note_toast_failure(exc)


# Where a notification can land, best screen first, and the argument name each
# end expects — the desktop client takes `body`, the Android client takes
# `message`. Keeping the pair together is the point: the two wire shapes are the
# sort of detail that is fine until someone adds a third device and copies the
# wrong one.
#
# The phone rung is not a nicety. Every internal caller — the scheduler's fired
# reminders, the proactive engine's routine nudges, budget enforcement, the end
# of each agent turn — comes through here, and before this they went desktop ->
# local toast -> nothing. On the headless hub "nothing" was literal: a line in a
# journal and `FileNotFoundError: 'powershell'`, logged once and rate-limited.
# So with Jake's PC asleep, every morning-routine nudge was lost while a phone
# sat attached, advertising `notify`, and was never asked.
_ROUTES: tuple[tuple[str, str], ...] = (
    ("desktop", "body"),     # the screen he is actually looking at, when he is
    ("phone", "message"),    # in his pocket otherwise — worse, but not silence
)


def _first_attached() -> tuple[Any, dict] | tuple[None, None]:
    """The best reachable device and the payload it expects, or (None, None).

    Order is preference, not availability: a phone that is attached does not get
    the toast while the desktop is also attached, because two buzzes for one
    reminder trains Jake to ignore both.
    """
    try:
        from .devices.remote import attached
    except Exception:  # pragma: no cover — devices are always importable
        return None, None
    for label, arg in _ROUTES:
        device = attached(label)
        if device is not None:
            return device, {"arg": arg, "label": label}
    return None, None


def reachable() -> bool:
    """Is there anywhere a notification could actually land?

    Asked by the tool gate, and answered here because this is where the answer
    lives: `_ROUTES` decides where a toast goes, and the PowerShell path below
    is the last resort when no rung of it is attached. A second copy of that
    reasoning in tools.py would be one deploy away from disagreeing with the
    code that actually routes.

    False is the hub's resting state — headless, nothing dialled in — and it is
    exactly the case that made this worth writing down. `notify()` never raises
    there: it prints a console line, finds no device, shells out to a PowerShell
    that does not exist on Linux, swallows the FileNotFoundError and returns
    None, which is indistinguishable from success. So the failure has to be
    caught before the call, by not offering the tool at all.
    """
    device, _ = _first_attached()
    if device is not None:
        return True
    # Imported here rather than at module scope on purpose. notify sits at the
    # bottom of the import graph — half a dozen callers reach it with no device
    # reference — and deskops pulls in the whole desk surface. `local_desk()` is
    # the canonical "is this a machine someone could be sitting at", which is
    # the precondition for the local toast, so it is worth asking rather than
    # keeping a second platform check here that could drift from it.
    try:
        from .deskops import local_desk
    except Exception:  # pragma: no cover — deskops is always importable
        return False
    return local_desk()


def _forward_to_device(title: str, body: str) -> bool:
    """Hand the toast to whichever device is attached over the network.

    Returns True if it was handed off — the caller must then NOT also raise a
    local toast, or the hub would try to toast itself.

    The hub runs headless on Linux, where the PowerShell path below cannot work
    and the real screen is on the other end of a websocket. When Continuum runs
    on the desktop itself nothing is attached, this returns False, and the
    original local behaviour is untouched.
    """
    device, route = _first_attached()
    if device is None:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # `command()` needs a loop to await the device's reply. Without one we
        # cannot send, so fall back rather than pretend it was delivered.
        return False
    try:
        task = loop.create_task(
            device.command("notify", {"title": title, route["arg"]: body}))
    except Exception:
        log.debug("could not schedule the forwarded toast", exc_info=True)
        return False
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    task.add_done_callback(_report_forward)
    return True


def notify(title: str, body: str) -> None:
    """Tell Jake something. Same name, signature and (None) return as always.

    The catch this function exists to dodge: every caller — the end of each
    agent turn, the turn error path, the proactive surfacer, the scheduler's
    fired reminders, the `notify` tool — calls this *synchronously* from inside
    a coroutine and ignores the result. So a 0.5–2s PowerShell cold start was
    being paid on the event loop on literally every reply, freezing the UI
    socket, the phone link and the heartbeat for the whole spawn. It was the
    single biggest latency source in the app.

    Since nobody awaits us and nobody reads a result, we can fix that here
    without touching a single call site: if a loop is running in this thread,
    hand the spawn to a worker thread and return immediately. The toast lands a
    beat later, which is exactly what a notification is allowed to do.

    With no loop running — a plain script, or a deliberate
    `asyncio.to_thread(notify, ...)` — we keep the old blocking behaviour
    verbatim, so anything that already arranged to be off-thread still gets
    "the toast has been attempted" by the time this returns.
    """
    _print_line(title, body)
    # A device on the far end of a socket owns the only screen Jake can see, so
    # it wins over anything this host could raise locally.
    if _forward_to_device(title, body):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    # Both fallbacks below spawn from *outside* their except block on purpose.
    # Spawning inside one chains the handled error into whatever _spawn_toast
    # reports, so a missing powershell logs a traceback led by "RuntimeError: no
    # running event loop" — which reads like the cause and is nothing of the
    # sort. The failure line only earns its keep if it points at the real thing.
    if loop is None:
        _spawn_toast(title, body)  # no loop in this thread — nothing to block
        return
    coro = asyncio.to_thread(_spawn_toast, title, body)
    task = None
    try:
        task = loop.create_task(coro)
    except Exception:
        log.debug("could not schedule the toast off-loop", exc_info=True)
    if task is None:
        # Loop is shutting down, or otherwise won't take work. A brief stall
        # beats dropping the notification outright.
        coro.close()  # or Python warns about a coroutine that was never awaited
        _spawn_toast(title, body)
        return
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def notify_async(title: str, body: str) -> None:
    """Awaitable notify, for callers that want the toast actually attempted
    before they move on. Same work, run off the loop, awaited to completion —
    use this when the ordering matters (e.g. a toast that must precede a
    shutdown); use plain notify() when it doesn't, which is nearly always.

    Forwards to an attached device for the same reason `notify()` does, down the
    same `_ROUTES` ladder, so the two entry points cannot disagree about where a
    notification goes. No recursion risk in doing so: this is also what the
    desktop *client* calls on receiving the forwarded command, and the client
    has no device attached to it, so the chain ends there with a real toast.
    """
    _print_line(title, body)
    device, route = _first_attached()
    if device is not None:
        try:
            await device.command("notify", {"title": title, route["arg"]: body})
        except Exception as exc:
            _note_toast_failure(exc)
        return
    await asyncio.to_thread(_spawn_toast, title, body)
