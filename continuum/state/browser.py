"""Firefox context — what you're actually looking at, not just "Firefox is open".

Reads the live session-restore file (``sessionstore-backups/recovery.jsonlz4``),
which Firefox rewrites every ~15s with every open window and tab: title, URL,
and ``lastAccessed`` (epoch ms of when that tab was last in front). That gives
us the current page and a recency-ordered list of what's been viewed — the
signal that makes machine state feel like it knows what you're doing.

Pure-Python: the file is Mozilla's ``mozLz4`` container (an 8-byte magic + u32
size + one LZ4 block), decoded here without any third-party dependency. The read
is cached by file mtime so the 5s poller doesn't re-decompress an unchanged file.

Everything degrades to ``None``/empty on any failure (no Firefox, file missing,
format drift) so the rest of Continuum never depends on this succeeding.
"""
from __future__ import annotations

import configparser
import json
import logging
import os
import struct
import time
from typing import Any, Optional

log = logging.getLogger("continuum.state.browser")

# Pages that carry no "what am I looking at" signal — counted, never listed.
_BLANK = {"about:blank", "about:newtab", "about:home", "about:privatebrowsing", ""}


# ---- mozLz4 decode ---------------------------------------------------------
def _lz4_block_decompress(src: bytes, out_size: int) -> bytes:
    """Decode one raw LZ4 block (the format Mozilla wraps in mozLz4)."""
    out = bytearray()
    i, n = 0, len(src)
    while i < n:
        token = src[i]; i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                b = src[i]; i += 1; lit += b
                if b != 255:
                    break
        out += src[i:i + lit]; i += lit
        if i >= n:
            break  # last sequence is literals-only, no match
        offset = src[i] | (src[i + 1] << 8); i += 2
        mlen = token & 0x0F
        if mlen == 15:
            while True:
                b = src[i]; i += 1; mlen += b
                if b != 255:
                    break
        mlen += 4  # minmatch
        start = len(out) - offset
        for j in range(mlen):
            out.append(out[start + j])
    return bytes(out[:out_size]) if out_size else bytes(out)


def _read_mozlz4(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if data[:8] != b"mozLz40\0":
        return None
    size = struct.unpack("<I", data[8:12])[0]
    try:
        return _lz4_block_decompress(data[12:], size)
    except Exception:
        log.debug("mozLz4 decode failed for %s", path, exc_info=True)
        return None


# ---- profile discovery -----------------------------------------------------
def _firefox_root() -> Optional[str]:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    root = os.path.join(appdata, "Mozilla", "Firefox")
    return root if os.path.isdir(root) else None


def _recovery_path(profile_dir: str) -> str:
    return os.path.join(profile_dir, "sessionstore-backups", "recovery.jsonlz4")


def _active_profile_dir(root: str) -> Optional[str]:
    """The profile Firefox is actually using: prefer the one whose recovery file
    was written most recently (that's the running session), falling back to
    profiles.ini's default."""
    profiles = os.path.join(root, "Profiles")
    candidates: list[str] = []
    if os.path.isdir(profiles):
        for name in os.listdir(profiles):
            d = os.path.join(profiles, name)
            if os.path.isfile(_recovery_path(d)):
                candidates.append(d)
    if candidates:
        return max(candidates, key=lambda d: os.path.getmtime(_recovery_path(d)))
    # No live session file — fall back to the declared default profile.
    ini = os.path.join(root, "profiles.ini")
    try:
        cp = configparser.ConfigParser()
        cp.read(ini)
        for sec in cp.sections():
            if cp.has_option(sec, "Default") and cp.has_option(sec, "Path"):
                rel = cp.get(sec, "IsRelative", fallback="1") == "1"
                path = cp.get(sec, "Path")
                return os.path.join(root, path.replace("/", os.sep)) if rel else path
    except Exception:
        pass
    return None


# ---- tab extraction --------------------------------------------------------
def _tab_current_entry(tab: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The page a tab is showing now = its current history entry."""
    entries = tab.get("entries") or []
    if not entries:
        return None
    idx = tab.get("index", len(entries)) - 1
    idx = max(0, min(idx, len(entries) - 1))
    return entries[idx]


def _ago(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 45:
        return "just now"
    m = s // 60
    if m < 1:
        return "just now"
    if m < 60:
        return f"{m}m ago"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h ago"
    return f"{h // 24}d ago"


class _Cache:
    __slots__ = ("mtime", "data")

    def __init__(self) -> None:
        self.mtime: float = 0.0
        self.data: Optional[dict[str, Any]] = None


_cache = _Cache()


def firefox_context(recent_limit: int = 6) -> Optional[dict[str, Any]]:
    """Snapshot of the live Firefox session, or None if unavailable.

    Returns::

        {
          "open_count": int,          # real tabs (blanks excluded)
          "window_count": int,
          "active": {"title","url","idle": "just now"},   # most recently focused tab
          "recent": [{"title","url","idle","last_accessed"}, ...],  # recency desc
        }
    """
    root = _firefox_root()
    if not root:
        return None
    profile = _active_profile_dir(root)
    if not profile:
        return None
    path = _recovery_path(profile)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None

    if _cache.data is not None and mtime == _cache.mtime:
        return _refresh_idle(_cache.data)

    raw = _read_mozlz4(path)
    if raw is None:
        return None
    try:
        session = json.loads(raw)
    except Exception:
        return None

    now = time.time()
    tabs: list[dict[str, Any]] = []
    windows = session.get("windows") or []
    for win in windows:
        for tab in win.get("tabs") or []:
            entry = _tab_current_entry(tab)
            if not entry:
                continue
            url = (entry.get("url") or "").strip()
            last = tab.get("lastAccessed")  # epoch ms
            tabs.append({
                "title": (entry.get("title") or url or "").strip(),
                "url": url,
                "last_accessed": last,
                "_last_s": (last / 1000.0) if last else 0.0,
            })

    real = [t for t in tabs if t["url"] not in _BLANK and not t["url"].startswith("about:")]
    real.sort(key=lambda t: t["_last_s"], reverse=True)

    active = None
    if real:
        top = real[0]
        active = {"title": top["title"], "url": top["url"],
                  "last_accessed": top["last_accessed"],
                  "idle": _ago(now - top["_last_s"]) if top["_last_s"] else "unknown"}

    recent = [{
        "title": t["title"], "url": t["url"],
        "last_accessed": t["last_accessed"],
        "idle": _ago(now - t["_last_s"]) if t["_last_s"] else "unknown",
    } for t in real[:recent_limit]]

    data = {
        "open_count": len(real),
        "window_count": len(windows),
        "active": active,
        "recent": recent,
    }
    _cache.mtime = mtime
    _cache.data = data
    return data


def _places_path() -> Optional[str]:
    root = _firefox_root()
    if not root:
        return None
    profile = _active_profile_dir(root)
    if not profile:
        return None
    p = os.path.join(profile, "places.sqlite")
    return p if os.path.isfile(p) else None


def _search_query(url: str) -> Optional[str]:
    """Pull the human search term out of a search-engine URL."""
    try:
        from urllib.parse import urlparse, parse_qs, unquote
        u = urlparse(url)
        host = u.hostname or ""
        qs = parse_qs(u.query)
        for host_frag, key in (("google.", "q"), ("bing.", "q"), ("duckduckgo.", "q"),
                               ("youtube.", "search_query"), ("reddit.", "q")):
            if host_frag in host and key in qs:
                return unquote(qs[key][0])
        if "duckduckgo." in host and u.path.startswith("/") and not u.query and len(u.path) > 1:
            return None
    except Exception:
        return None
    return None


def firefox_history(limit: int = 12) -> Optional[dict[str, Any]]:
    """Recent Firefox history — pages you visited, including tabs since closed —
    plus the search queries among them. Read-only snapshot of places.sqlite.

    Opened with ``immutable=1`` so we can read while Firefox holds the DB; that
    trades away the newest not-yet-checkpointed visits for never touching the
    live file or blocking the browser.
    """
    path = _places_path()
    if not path:
        return None
    import sqlite3

    now = time.time()
    visits: list[dict[str, Any]] = []
    try:
        con = sqlite3.connect(f"file:{path}?immutable=1", uri=True, timeout=1.0)
        try:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT url, title, last_visit_date, visit_count "
                "FROM moz_places WHERE last_visit_date IS NOT NULL "
                "  AND url NOT LIKE 'about:%' AND url NOT LIKE 'moz-extension:%' "
                "ORDER BY last_visit_date DESC LIMIT ?", (limit * 3,)).fetchall()
        finally:
            con.close()
    except Exception:
        log.debug("places.sqlite read failed", exc_info=True)
        return None

    searches: list[dict[str, Any]] = []
    for r in rows:
        last_s = (r["last_visit_date"] or 0) / 1_000_000.0  # microseconds → seconds
        item = {
            "url": r["url"], "title": (r["title"] or r["url"] or "").strip(),
            "visit_count": r["visit_count"],
            "last_visit": last_s, "idle": _ago(now - last_s) if last_s else "unknown",
        }
        q = _search_query(r["url"])
        if q and not any(s["query"].lower() == q.lower() for s in searches):
            searches.append({"query": q, "idle": item["idle"]})
        visits.append(item)

    return {
        "visits": visits[:limit],
        "searches": searches[:8],
    }


def _refresh_idle(data: dict[str, Any]) -> dict[str, Any]:
    """Recompute 'idle' strings against the current clock without re-decoding —
    the session file is unchanged but a few seconds have passed."""
    now = time.time()
    for item in ([data.get("active")] if data.get("active") else []) + list(data.get("recent") or []):
        la = item.get("last_accessed")
        if la:
            item["idle"] = _ago(now - la / 1000.0)
    return data
