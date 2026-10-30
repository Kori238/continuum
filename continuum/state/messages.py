"""Read the messages currently visible in the Discord / Slack windows.

On-screen only: Windows UI Automation reads the message list the user is already
looking at — no tokens, no API keys, nothing that touches either app's servers
or violates their terms. It sees exactly what's on screen (what you've scrolled
to), which is all an ambient assistant needs to answer "who just messaged me".

Requires the optional `uiautomation` package; degrades to an empty result
without it, off Windows, or if an app isn't open. The UIA walk is done on a
dedicated thread with its own COM init so it's safe to call from async code and
never blocks the event loop for long.
"""
from __future__ import annotations

import logging
import re
import threading
from typing import Any, Optional

from . import probe

# The verbose tooltip timestamp Discord duplicates on every row, e.g.
# "Wednesday, 19 August 2026 21:44" — we keep the short "21:44" instead.
_LONGDATE = re.compile(r"^\w+day,\s+\d{1,2}\s+\w+\s+\d{4}(\s+\d{1,2}:\d{2})?$")
_SERVERTAG = re.compile(r"\s*Server Tag:\s*\S+")
_EMOJI = re.compile(r"^:[a-z0-9_+\-]+:$")  # reaction shortcodes like :thumbsup:
# Row hover-toolbar buttons that leak into the a11y tree — not message content.
_BTN = {"click to react", "add reaction", "reply", "forward", "more", "react",
        "edit", "pin", "delete", "more actions", "reply in thread", "save for later",
        "get link", "mark unread", "remind me"}

log = logging.getLogger("continuum.state.messages")

# proc name -> friendly label.
_APPS = {"discord.exe": "Discord", "slack.exe": "Slack"}

# Sidebar / chrome labels that show up in the tree but aren't messages.
_NOISE = ("members", "member", "server tag:", "message requests", "nitro",
          "add friends", "search", "mute", "deafen")

_MAX_NODES = 8000  # hard cap on tree nodes visited per window


def _windows_by_proc() -> dict[str, int]:
    out: dict[str, int] = {}
    for w in probe.list_windows():
        b = probe.process_brief(w["pid"])
        if b and b["proc"].lower() in _APPS:
            out.setdefault(b["proc"].lower(), w["pid"])
    return out


def _texts(ctrl, budget: list[int], depth: int = 0) -> list[str]:
    """Ordered text fragments under one message row (author, time, body, …)."""
    acc: list[str] = []
    if budget[0] <= 0 or depth > 12:
        return acc
    for c in ctrl.GetChildren():
        budget[0] -= 1
        if budget[0] <= 0:
            break
        try:
            if c.ControlTypeName == "TextControl":
                nm = (c.Name or "").strip()
                if nm:
                    acc.append(nm)
        except Exception:
            pass
        acc += _texts(c, budget, depth + 1)
    return acc


def _clean_row(fragments: list[str]) -> str:
    seen: list[str] = []
    for f in fragments:
        f = _SERVERTAG.sub("", " ".join(f.split())).strip()
        if not f or _LONGDATE.match(f) or _EMOJI.match(f):
            continue
        low = f.lower()
        if low in _BTN:                   # hover-toolbar buttons
            continue
        if low == "also send to":         # Slack compose-box artifact
            break
        if not seen or seen[-1] != f:
            seen.append(f)
    line = " · ".join(dict.fromkeys(seen))
    return " ".join(line.split())


def _find(ctrl, pred, budget: list[int], depth: int = 0):
    """First descendant matching pred(name_lower, control_type)."""
    if budget[0] <= 0 or depth > 26:
        return None
    for c in ctrl.GetChildren():
        budget[0] -= 1
        if budget[0] <= 0:
            break
        try:
            nm = (c.Name or "").strip().lower()
            ct = c.ControlTypeName
        except Exception:
            nm, ct = "", "?"
        if pred(nm, ct):
            return c
        hit = _find(c, pred, budget, depth + 1)
        if hit is not None:
            return hit
    return None


def _scrape_window(win, proc: str, limit: int) -> list[str]:
    budget = [_MAX_NODES]
    # Prefer the real message-list container so we skip the sidebar entirely.
    if proc == "discord.exe":
        container = _find(win, lambda nm, ct: ct == "ListControl" and nm.startswith("messages in"), budget)
    else:  # slack
        container = _find(win, lambda nm, ct: ct == "ListControl" and any(
            k in nm for k in ("message", "thread", "conversation", "direct message", "activity")), budget)
    rows_parent = container or win

    out: list[str] = []
    for row in rows_parent.GetChildren():
        try:
            ct = row.ControlTypeName
        except Exception:
            continue
        if ct not in ("ListItemControl", "GroupControl"):
            continue
        line = _clean_row(_texts(row, budget))
        if len(line) < 6:
            continue
        low = line.lower()
        if not container and any(low == n or low.startswith(n) for n in _NOISE):
            continue  # only filter noise when we couldn't scope to a message list
        out.append(line)
    # Bottom of the list is the most recent; keep the tail.
    return out[-limit:]


def _read_sync(app: Optional[str], limit: int) -> dict[str, list[str]]:
    try:
        import uiautomation as auto  # noqa: F401
    except Exception:
        return {}
    import uiautomation as auto

    wanted = None
    if app:
        app = app.strip().lower()
        wanted = next((p for p, label in _APPS.items()
                       if app in (p, p[:-4], label.lower())), None)

    procs = _windows_by_proc()
    if wanted:
        procs = {wanted: procs[wanted]} if wanted in procs else {}

    root = auto.GetRootControl()
    tops = {w.ProcessId: w for w in root.GetChildren() if _safe_pid(w)}

    result: dict[str, list[str]] = {}
    for proc, pid in procs.items():
        win = tops.get(pid)
        if win is None:
            continue
        try:
            msgs = _scrape_window(win, proc, limit)
        except Exception:
            log.debug("scrape failed for %s", proc, exc_info=True)
            msgs = []
        if msgs:
            result[_APPS[proc]] = msgs
    return result


def _safe_pid(ctrl) -> bool:
    try:
        return ctrl.ProcessId is not None
    except Exception:
        return False


def read_messages(app: Optional[str] = None, limit: int = 15) -> dict[str, list[str]]:
    """Visible Discord/Slack messages, newest last: {"Slack": [...], "Discord": [...]}.
    `app` optionally narrows to one ('discord' or 'slack')."""
    box: dict[str, Any] = {}

    def _worker() -> None:
        try:
            box["v"] = _read_sync(app, limit)
        except Exception:
            log.debug("read_messages worker failed", exc_info=True)
            box["v"] = {}

    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    th.join(timeout=8)
    return box.get("v", {})
