"""Seeing the screen — capture, and the downscale that makes it legible.

The capture itself is the easy part. Two things around it are not, and both are
the reason this is a module rather than four lines inside `tools.py`:

**What to capture.** Jake runs two monitors, 2560x1440 next to 1920x1080. The
union is a 4480x1440 strip that is mostly *not* what he's asking about, and
handing that to a vision model wastes tokens on empty desktop and dilutes the
half that matters. So the default is the **foreground window** — the thing he
is actually looking at. `screen` widens to the monitor that window is on (not
the union), and `all` is the explicit everything-please escape hatch.

**How big.** A vision model's cost and accuracy both turn on resolution, in
opposite directions: too large wastes tokens and slows the turn, too small and
the on-screen text stops being readable — which is most of what a screenshot is
*for*. We cap the long edge at 1600px and downscale by **area-averaging**, not
subsampling. Striding is one line shorter and visibly wrecks small text: it
throws away 3 of every 4 pixels and aliases glyph strokes into noise. Averaging
is what keeps a 10px font legible after a 2x reduction.

Nothing here is written to disk. The PNG goes to the model in memory and is
dropped — a screenshot is the most revealing artefact on the machine, and a
temp file is a leak with a longer half-life than the answer it was for.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger("continuum.screen")

MAX_EDGE = 1600  # long edge handed to the vision model


def _foreground_rect() -> Optional[dict[str, int]]:
    """Bounding box of the focused window, or None if it can't be determined."""
    try:
        import ctypes
        import ctypes.wintypes as wt

        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        r = wt.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return None
        w, h = r.right - r.left, r.bottom - r.top
        # A minimised or degenerate window has a nonsense rect; fall back rather
        # than capture a 1px sliver.
        if w < 80 or h < 80:
            return None
        return {"left": r.left, "top": r.top, "width": w, "height": h}
    except Exception:
        log.debug("foreground rect failed", exc_info=True)
        return None


def _monitor_containing(sct: Any, rect: Optional[dict]) -> dict:
    """The physical monitor a rect's centre falls on. `sct.monitors[0]` is the
    union of every display, so it is only ever right for mode='all'."""
    real = sct.monitors[1:] or sct.monitors
    if not rect:
        return real[0]
    cx = rect["left"] + rect["width"] // 2
    cy = rect["top"] + rect["height"] // 2
    for m in real:
        if (m["left"] <= cx < m["left"] + m["width"]
                and m["top"] <= cy < m["top"] + m["height"]):
            return m
    return real[0]


def _downscale(arr, max_edge: int = MAX_EDGE):
    """Area-average down to `max_edge` by an integer factor. Returns the array
    unchanged when it already fits — no resampling means no softening."""
    import numpy as np

    h, w = arr.shape[:2]
    longest = max(h, w)
    if longest <= max_edge:
        return arr
    f = int(-(-longest // max_edge))  # ceil: the smallest factor that fits
    # Crop the ragged edge so the reshape divides evenly. Losing <f pixels off
    # the right/bottom is invisible; a non-integer resize is not worth the
    # dependency on a real image library.
    h2, w2 = (h // f) * f, (w // f) * f
    if h2 == 0 or w2 == 0:
        return arr
    cropped = arr[:h2, :w2]
    return (cropped.reshape(h2 // f, f, w2 // f, f, arr.shape[2])
            .mean(axis=(1, 3))
            .astype(np.uint8))


def capture(mode: str = "window") -> tuple[bytes, dict[str, Any]]:
    """PNG bytes plus what was actually captured.

    `mode`: 'window' (focused window, the default), 'screen' (the monitor that
    window sits on), or 'all' (every display).

    Raises RuntimeError with an actionable message if mss isn't installed —
    capture is optional, so the caller turns that into a tool result rather
    than letting it break a turn.
    """
    try:
        import mss
        import mss.tools
        import numpy as np
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise RuntimeError(
            "screen capture needs the 'mss' package: pip install mss") from exc

    fg = _foreground_rect()
    # MSS is the non-deprecated spelling; older releases only expose mss.mss.
    opener = getattr(mss, "MSS", None) or mss.mss
    with opener() as sct:
        if mode == "all":
            region, what = sct.monitors[0], "all displays"
        elif mode == "screen" or not fg:
            region = _monitor_containing(sct, fg)
            what = f"the {region['width']}x{region['height']} display"
            if mode == "window":  # asked for the window, couldn't find one
                what += " (no focused window to isolate)"
        else:
            region, what = fg, "the focused window"
        shot = sct.grab(region)
        arr = np.frombuffer(shot.rgb, dtype=np.uint8).reshape(
            shot.height, shot.width, 3)

    small = _downscale(arr)
    h, w = small.shape[:2]
    png = mss.tools.to_png(small.tobytes(), (w, h))
    meta = {
        "what": what, "mode": mode,
        "captured": f"{shot.width}x{shot.height}",
        "sent": f"{w}x{h}",
        "bytes": len(png),
    }
    return png, meta
