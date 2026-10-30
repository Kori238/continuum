"""Close Firefox tabs by title — the one *write* Continuum can make to the browser.

Reading the browser (``browser.py``) is pure-Python and read-only. Closing a tab
is a different kind of act: it reaches into Firefox's live UI through Windows UI
Automation — the same accessibility tree ``messages.py`` reads Discord/Slack from
— finds the tabs whose titles match, and invokes each one's "Close tab" button.
Firefox exposes no API for this; the a11y tree is the only handle. Because
Firefox only exposes the close button for the *active* tab, an inactive match is
selected (activated) first so its button exists to invoke — see ``_invoke_close``.

Sensitive by nature, and treated as such by the tool layer: it destroys state (an
open tab, possibly with unsaved input) and a loose match could close the wrong
thing. So ``close_tab`` is an ⚡ action (off by default) that Continuum invokes
*only* on Jake's explicit request — never as a helpful extra — and an empty or
whitespace match is refused here rather than being read as "every tab".

Requires the optional ``uiautomation`` package; degrades to a clear message
without it, off Windows, or when Firefox isn't open. All UIA work runs on a
dedicated daemon thread (its own COM init) so it never blocks the event loop.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional

from . import probe

log = logging.getLogger("continuum.state.tabs")

_MAX_NODES = 8000   # cap on tree nodes visited per pass — matches messages.py
_MAX_CLOSE = 25     # never close more than this in one call — a runaway guard


def _firefox_pids() -> set[int]:
    pids: set[int] = set()
    for w in probe.list_windows():
        b = probe.process_brief(w["pid"])
        if b and b["proc"].lower() == "firefox.exe":
            pids.add(w["pid"])
    return pids


def _kids(ctrl) -> list:
    try:
        return ctrl.GetChildren()
    except Exception:
        return []


def _find(ctrl, pred: Callable, budget: list[int], depth: int = 0):
    """First descendant satisfying pred, depth-first, within the node budget."""
    if budget[0] <= 0 or depth > 30:
        return None
    for c in _kids(ctrl):
        budget[0] -= 1
        if budget[0] <= 0:
            break
        try:
            if pred(c):
                return c
        except Exception:
            pass
        hit = _find(c, pred, budget, depth + 1)
        if hit is not None:
            return hit
    return None


def _tab_toolbars(auto, pids: set[int]) -> list:
    """The 'Browser tabs' toolbar in every open Firefox window."""
    root = auto.GetRootControl()
    bars = []
    for w in _kids(root):
        try:
            if w.ProcessId not in pids:
                continue
        except Exception:
            continue
        bar = _find(w, lambda c: c.ControlTypeName == "ToolBarControl"
                    and (c.Name or "") == "Browser tabs", [_MAX_NODES])
        if bar is not None:
            bars.append(bar)
    return bars


def _tab_items(bar, budget: list[int]) -> list:
    out: list = []

    def walk(ctrl, depth: int = 0) -> None:
        if budget[0] <= 0 or depth > 12:
            return
        for c in _kids(ctrl):
            budget[0] -= 1
            if budget[0] <= 0:
                return
            try:
                if c.ControlTypeName == "TabItemControl":
                    out.append(c)
                    continue  # the close button lives under it; no need to recurse
            except Exception:
                pass
            walk(c, depth + 1)

    walk(bar)
    return out


def _close_button(tab):
    """The tab's 'Close tab' button, or None if it isn't in the a11y tree."""
    return _find(tab, lambda c: c.ControlTypeName == "ButtonControl"
                 and "close" in (c.Name or "").lower(), [400])


def _invoke_close(tab) -> bool:
    """Invoke a tab's 'Close tab' button. Returns True if it fired.

    Firefox only exposes a tab's close (and mute) button in the accessibility
    tree for the *active* tab — an inactive tab shows only its title TextControl,
    so the button is genuinely absent and the lookup returns None. That was the
    'found N matching tab(s) but couldn't close them' bug: every non-foreground
    tab looked uncloseable. So if the button isn't there, select the tab first
    (which activates it and makes Firefox render its controls), then look again.
    """
    import time
    btn = _close_button(tab)
    if btn is None:
        try:
            tab.GetSelectionItemPattern().Select()
        except Exception:
            log.debug("could not select tab to expose its close button", exc_info=True)
        else:
            time.sleep(0.2)  # let Firefox render the now-active tab's controls
            btn = _close_button(tab)
    if btn is None:
        return False
    try:
        btn.GetInvokePattern().Invoke()
        return True
    except Exception:
        pass
    try:
        btn.Click(simulateMove=False, waitTime=0)  # fallback: synthetic click, no cursor travel
        return True
    except Exception:
        return False


def _matches(tab, needle: str) -> bool:
    try:
        return needle in (tab.Name or "").lower()
    except Exception:
        return False


def _run(fn: Callable[[Any], Any], timeout: float, default: Any) -> Any:
    """Run a UIA function on a daemon thread with its own COM, bounded by timeout."""
    try:
        import uiautomation as auto  # noqa: F401
    except Exception:
        return default
    import uiautomation as auto

    box: dict[str, Any] = {}

    def _worker() -> None:
        try:
            # COM must be initialised on this thread before any UIA call, or
            # every lookup raises "CoInitialize has not been called".
            with auto.UIAutomationInitializerInThread():
                box["v"] = fn(auto)
        except Exception:
            log.debug("uia tab worker failed", exc_info=True)
            box["v"] = default

    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    th.join(timeout=timeout)
    return box.get("v", default)


def list_firefox_tabs() -> list[str]:
    """Titles of every open Firefox tab, across all windows. [] if unavailable."""
    pids = _firefox_pids()
    if not pids:
        return []

    def _do(auto) -> list[str]:
        titles: list[str] = []
        for bar in _tab_toolbars(auto, pids):
            for tab in _tab_items(bar, [_MAX_NODES]):
                try:
                    titles.append((tab.Name or "").strip())
                except Exception:
                    pass
        return [t for t in titles if t]

    return _run(_do, timeout=8.0, default=[])


def close_firefox_tabs(match: str, limit: int = _MAX_CLOSE) -> tuple[bool, str, list[str]]:
    """Close every Firefox tab whose title contains ``match`` (case-insensitive).

    Returns (ok, message, closed_titles). Refuses an empty match rather than
    closing everything. ``ok`` is True when at least one tab was closed.
    """
    needle = (match or "").strip().lower()
    if not needle:
        return False, "refused: no tab title to match (won't close every tab)", []
    limit = max(1, min(int(limit or _MAX_CLOSE), _MAX_CLOSE))
    pids = _firefox_pids()
    if not pids:
        return False, "Firefox isn't open", []

    def _do(auto) -> tuple[bool, str, list[str]]:
        # Count matches first so the close loop is bounded even if an invoke
        # silently fails (re-scanning each pass avoids stale tab handles).
        bars = _tab_toolbars(auto, pids)
        if not bars:
            return False, "couldn't reach Firefox's tab bar", []
        n = 0
        for bar in bars:
            n += sum(1 for t in _tab_items(bar, [_MAX_NODES]) if _matches(t, needle))
        if n == 0:
            return False, f"no open tab matches '{match}'", []

        import time
        closed: list[str] = []
        for _ in range(min(n, limit)):
            target = None
            for bar in _tab_toolbars(auto, pids):
                target = _find(bar, lambda c: c.ControlTypeName == "TabItemControl"
                               and _matches(c, needle), [_MAX_NODES])
                if target is not None:
                    break
            if target is None:
                break
            title = (target.Name or "").strip()
            if not _invoke_close(target):
                log.debug("could not invoke close on tab %r", title)
                break  # avoid looping forever on a tab that won't close
            closed.append(title)
            time.sleep(0.15)  # let Firefox reflow the tab strip before the next pass

        if not closed:
            return False, f"found {n} matching tab(s) but couldn't close them", []
        return True, f"closed {len(closed)} tab(s) matching '{match}'", closed

    return _run(_do, timeout=20.0, default=(False, "closing tabs timed out", []))
