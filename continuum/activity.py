"""Activity — the local timeline of where Jake's attention actually went.

`StateTracker` knows "right now" and keeps a rolling ~2h window of focus samples
in memory, but nothing about it survives a restart: close the process and the
whole record of the day is gone. This store is the durable half. It turns the
live foreground signal into **coalesced segments** — one row per continuous run
of an app in front — and accumulates them in `data/activity.db`, so "where did
my day go" is answerable across restarts and across days.

It records **two bodies**. The desktop's foreground app comes from the state
poll loop; the phone reports off the bus. `phone.screen` bounds a phone session
(screen on opens it, screen off closes it). If the phone's accessibility service
is on, `phone.foreground` reports which app is in front, so the phone track
becomes **real per-app segments** — the same shape as the desktop — instead of a
screen-on blur. Without that service (or before its first report) a session
falls back to a generic "Phone" segment, so a phone that only reports screen
state still shows honest screen-on time.

One table, `segments`: `device` ('desktop'|'phone'), `app`, `proc`, `title`,
`start_ts`, `end_ts`, `seconds`. An *open* segment is a real row whose `end_ts`
we extend on every poll — so it's crash-safe (the row is already on disk) and a
live read already includes the in-progress segment. Analytics (per-app totals,
daily screen time, one app's history) are computed here so the non-AI Activity
tab and the two activity tools agree, exactly as the workouts store does for its
callers.

A small set of apps (`_EXCLUDED_APPS`: Continuum's own window, the phone's
launcher/keyboard/settings chrome, the generic screen-on fallback) is filtered
out at *read* time — the rows stay on disk, so it's a view filter, not a delete,
and the list is cheap to change. Every reader (`timeline`, `summary`,
`app_history`) applies it, so totals and the timeline stay consistent.

A browser's foreground segment is relabelled by the site in front (`_SOCIAL_SITES`,
matched on the active tab's URL host) — so YouTube, Instagram, Reddit and the like
split out of the generic `Firefox` bucket as their own apps, while ordinary
browsing keeps the browser's name. This happens at *write* time, so only segments
recorded after it shipped are split; older `Firefox` rows stay generic.

Desktop segments only accumulate while Jake is present: the poll loop passes the
foreground app when he's at the desk and `None` when he's away, which closes the
open segment. So "VS Code 40m" means 40m of real attention, not a window left in
front over lunch — the same rule `StateTracker` uses for its focus counters.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from .config import Config

log = logging.getLogger("continuum.activity")

# A run of an app shorter than this is noise (an alt-tab in passing), so the
# timeline view drops it. Totals still count every second.
MIN_SEGMENT_S = 5

# Apps that are noise on Jake's timeline, filtered at *read* time (the rows stay
# in the store — this is a view filter, not a delete, so the list is cheap to
# change and nothing is lost). Covers Continuum's own window, the phone's
# launcher / keyboard / settings chrome, and the generic screen-on fallback that
# a real per-app foreground report supersedes. Matched case-insensitively on the
# app name; the generic phone session is disambiguated from a real dialer named
# "Phone" by its `proc == 'screen'`.
_EXCLUDED_APPS = {"settings", "gboard", "pixel launcher", "pythonw"}

# Browser process names whose foreground segments we relabel by the site in front
# (below). Kept in step with the tracker's own _BROWSER_PROCS.
_BROWSER_PROCS = {"firefox.exe"}

# Social / media sites split out of the generic "Firefox" bucket, so the Activity
# tab shows "YouTube 40m" as its own row instead of folding it into browsing.
# Matched on the active tab's URL host (any subdomain — "youtube.com" catches
# "www.youtube.com"), first hit wins; everything else keeps the browser's name,
# so ordinary browsing isn't fragmented into a hundred one-off hosts.
_SOCIAL_SITES: tuple[tuple[str, str], ...] = (
    ("youtube.com", "YouTube"), ("youtu.be", "YouTube"),
    ("instagram.com", "Instagram"),
    ("reddit.com", "Reddit"),
    ("x.com", "X (Twitter)"), ("twitter.com", "X (Twitter)"),
    ("tiktok.com", "TikTok"),
    ("twitch.tv", "Twitch"),
    ("facebook.com", "Facebook"),
    ("threads.net", "Threads"),
    ("bsky.app", "Bluesky"),
    ("linkedin.com", "LinkedIn"),
)


def _browser_app(name: str, proc: str, url: Optional[str]) -> str:
    """Relabel a browser foreground segment by the social site it's on, so the
    timeline splits YouTube/Instagram/etc out of the generic browser total. A
    non-browser app, an unknown host, or a missing URL keeps the given name."""
    if (proc or "").strip().lower() not in _BROWSER_PROCS:
        return name
    try:
        host = (urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return name
    for frag, label in _SOCIAL_SITES:
        if host == frag or host.endswith("." + frag):
            return label
    return name


def _excluded(app: Optional[str], proc: Optional[str]) -> bool:
    a = (app or "").strip().lower()
    if a in _EXCLUDED_APPS:
        return True
    # The generic phone screen-on fallback (`Phone`/`screen`); a real app in the
    # foreground carries its package in `proc`, so it survives.
    if a == "phone" and (proc or "").strip().lower() == "screen":
        return True
    return False


class ActivityStore:
    def __init__(self, cfg: Config) -> None:
        self._path = Path(cfg.activity_db)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self._path), check_same_thread=False)
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS segments(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device TEXT DEFAULT 'desktop',
                app TEXT, proc TEXT, title TEXT,
                start_ts REAL, end_ts REAL, seconds INTEGER DEFAULT 0)"""
        )
        self._db.execute("CREATE INDEX IF NOT EXISTS ix_seg_start ON segments(start_ts)")
        self._db.execute("CREATE INDEX IF NOT EXISTS ix_seg_dev ON segments(device)")
        self._db.commit()
        # The currently-open segment per device: {id, app, proc, start}. Held in
        # memory as a cursor onto the open row; the row itself lives on disk.
        self._cur: dict[str, Optional[dict[str, Any]]] = {"desktop": None, "phone": None}
        self._phone_label = "Phone"
        # Whether the phone's screen is believed on. Foreground reports only count
        # while it is, so a stray window event with the screen off can't invent
        # activity — the same principle as the desktop's presence gate.
        self._phone_screen_on = False
        # The desktop feed (state-poll thread) and the phone feed (bus handler
        # thread) both reach this one connection via asyncio.to_thread, so their
        # writes can genuinely interleave. Serialise every public entry point;
        # the private _open/_extend/_close helpers run inside a held lock and
        # never re-acquire, so a plain Lock (not RLock) is correct.
        self._lock = threading.Lock()

    # ---- writing: open / extend / close -------------------------------------
    def _open(self, device: str, app: str, proc: str, title: str, now: float) -> None:
        cur = self._db.execute(
            "INSERT INTO segments(device, app, proc, title, start_ts, end_ts, seconds) "
            "VALUES(?,?,?,?,?,?,0)", (device, app, proc, title, now, now))
        self._db.commit()
        self._cur[device] = {"id": cur.lastrowid, "app": app, "proc": proc, "start": now}

    def _extend(self, device: str, now: float, title: Optional[str] = None) -> None:
        c = self._cur.get(device)
        if not c:
            return
        if title is None:
            self._db.execute("UPDATE segments SET end_ts=?, seconds=? WHERE id=?",
                             (now, int(max(0, now - c["start"])), c["id"]))
        else:
            self._db.execute("UPDATE segments SET end_ts=?, seconds=?, title=? WHERE id=?",
                             (now, int(max(0, now - c["start"])), title, c["id"]))
        self._db.commit()

    def _close(self, device: str, now: Optional[float] = None) -> None:
        if now is not None:
            self._extend(device, now)
        self._cur[device] = None

    # ---- ingest: desktop foreground -----------------------------------------
    def note_desktop(self, fg: Optional[dict[str, Any]]) -> None:
        """Called every state poll with the foreground app view, or `None` when
        Jake is away (or nothing is focused). Same app as the open segment →
        extend it; a different app → close and open a fresh one; `None` → close.

        For a browser, the app name is first resolved to the social site in front
        (via `_browser_app` on the foreground view's `url`), so navigating
        YouTube→Instagram closes one segment and opens another."""
        now = time.time()
        with self._lock:
            if not fg:
                self._close("desktop", now)
                return
            proc = str(fg.get("proc") or "")
            app = _browser_app(str(fg.get("name") or "?"), proc, fg.get("url"))
            title = str(fg.get("title") or "")
            c = self._cur.get("desktop")
            if c and c["app"] == app and c["proc"] == proc:
                self._extend("desktop", now, title)   # same run — refresh title, extend
            else:
                if c:
                    self._extend("desktop", now)
                self._open("desktop", app, proc, title, now)

    # ---- ingest: phone reports (off the bus) --------------------------------
    def on_phone_signal(self, kind: str, payload: dict[str, Any], ts: Optional[float]) -> None:
        """One phone report.

        - `screen` toggles a session open/closed. Opening falls back to a generic
          "Phone" segment; a later `foreground` report refines it to the real app.
        - `foreground` (accessibility service) names the app in front: same app →
          extend, different app → close and open a fresh per-app segment. Only
          while the screen is believed on, so a stray window event can't invent
          activity.
        - any other report extends the open session (the phone is awake and
          talking), but only if one is open — a background notification with the
          screen off doesn't invent activity.
        """
        now = ts or time.time()
        with self._lock:
            if kind == "screen":
                on = bool((payload or {}).get("on"))
                self._phone_screen_on = on
                if on:
                    if self._cur.get("phone"):
                        self._extend("phone", now)
                    else:
                        self._open("phone", self._phone_label, "screen", "screen on", now)
                else:
                    self._close("phone", now)
            elif kind == "foreground":
                if not self._phone_screen_on:
                    return  # no foreground activity while the screen is off
                app = str((payload or {}).get("app") or "").strip() or self._phone_label
                pkg = str((payload or {}).get("package") or "")
                c = self._cur.get("phone")
                if c and c["app"] == app and c["proc"] == pkg:
                    self._extend("phone", now)   # same app — extend
                else:
                    if c:
                        self._extend("phone", now)
                    self._open("phone", app, pkg, app, now)
            elif self._cur.get("phone"):
                self._extend("phone", now)

    def close_phone(self) -> None:
        """Phone went offline — end its open session at the last thing we heard."""
        with self._lock:
            self._phone_screen_on = False
            c = self._cur.get("phone")
            # Don't stretch to "now": the phone's been silent, so its screen-on
            # time ends at the last report, not at the moment we noticed it left.
            if c and c["id"]:
                r = self._db.execute("SELECT end_ts FROM segments WHERE id=?", (c["id"],)).fetchone()
                self._close("phone", r[0] if r else None)
            self._cur["phone"] = None

    # ---- day bounds ---------------------------------------------------------
    @staticmethod
    def _day_bounds(date: Optional[str]) -> tuple[float, float, str]:
        """[start, end) epoch bounds for a local calendar day, plus its label."""
        if date:
            try:
                d = datetime.strptime(date, "%Y-%m-%d")
            except ValueError:
                d = datetime.now()
        else:
            d = datetime.now()
        start = datetime(d.year, d.month, d.day)
        end = start + timedelta(days=1)
        return start.timestamp(), end.timestamp(), start.strftime("%Y-%m-%d")

    @staticmethod
    def _clip(seg_start: float, seg_end: float, lo: float, hi: float) -> float:
        """Seconds of a segment that fall inside [lo, hi)."""
        return max(0.0, min(seg_end, hi) - max(seg_start, lo))

    def _rows_between(self, lo: float, hi: float) -> list[sqlite3.Row]:
        self._db.row_factory = sqlite3.Row
        return self._db.execute(
            "SELECT * FROM segments WHERE end_ts > ? AND start_ts < ? ORDER BY start_ts",
            (lo, hi)).fetchall()

    # ---- reads --------------------------------------------------------------
    def timeline(self, date: Optional[str] = None) -> dict[str, Any]:
        """One day's segments (both bodies), clipped to the day, plus per-app
        desktop totals and headline screen-time figures. Drives the Activity tab."""
        lo, hi, label = self._day_bounds(date)
        with self._lock:
            rows = self._rows_between(lo, hi)
        segments: list[dict[str, Any]] = []
        app_secs: dict[str, float] = {}         # desktop, per-app
        phone_app_secs: dict[str, float] = {}   # phone, per-app (accessibility feed)
        desktop_secs = 0.0
        phone_secs = 0.0
        for r in rows:
            if _excluded(r["app"], r["proc"]):
                continue
            secs = self._clip(r["start_ts"], r["end_ts"], lo, hi)
            if r["device"] == "desktop":
                desktop_secs += secs
                app_secs[r["app"]] = app_secs.get(r["app"], 0.0) + secs
            else:
                phone_secs += secs
                phone_app_secs[r["app"]] = phone_app_secs.get(r["app"], 0.0) + secs
            if secs < MIN_SEGMENT_S:
                continue
            cs = max(r["start_ts"], lo)
            ce = min(r["end_ts"], hi)
            segments.append({
                "device": r["device"], "app": r["app"], "proc": r["proc"],
                "title": r["title"], "start_ts": cs, "end_ts": ce,
                "seconds": int(secs), "for": _hms(secs),
                "start_hm": _hm(cs), "end_hm": _hm(ce),
            })
        apps = sorted(({"name": k, "seconds": int(v), "for": _hms(v)}
                       for k, v in app_secs.items() if v >= 1),
                      key=lambda a: a["seconds"], reverse=True)
        phone_apps = sorted(({"name": k, "seconds": int(v), "for": _hms(v)}
                             for k, v in phone_app_secs.items() if v >= 1),
                            key=lambda a: a["seconds"], reverse=True)
        return {
            "date": label,
            "segments": segments,
            "apps": apps,
            "phone_apps": phone_apps,
            "desktop_seconds": int(desktop_secs),
            "desktop_active": _hms(desktop_secs),
            "phone_seconds": int(phone_secs),
            "phone_screen": _hms(phone_secs),
            "top_app": apps[0]["name"] if apps else None,
        }

    def totals_by_app(self, date: Optional[str] = None) -> list[dict[str, Any]]:
        """One day's seconds grouped by (device, app, proc).

        `timeline()` almost returns this, but its `apps`/`phone_apps` lists drop
        `proc` — and `proc` is the stabler key, so the budget's category rules are
        written against it (a process name or Android package survives a title-bar
        rewrite; a display name doesn't). Rather than have budget.py open a second
        connection to this database and write its own clipping SQL, the query that
        knows about segments stays in the store that owns them.

        Same `_excluded` filter and day-clipping as `timeline()`, deliberately: if
        the budget disagreed with the number on the Activity tab, the tab would be
        the one Jake believed, and rightly."""
        lo, hi, _ = self._day_bounds(date)
        with self._lock:
            rows = self._rows_between(lo, hi)
        acc: dict[tuple[str, str, str], float] = {}
        for r in rows:
            if _excluded(r["app"], r["proc"]):
                continue
            secs = self._clip(r["start_ts"], r["end_ts"], lo, hi)
            if secs <= 0:
                continue
            key = (r["device"], r["app"] or "?", r["proc"] or "")
            acc[key] = acc.get(key, 0.0) + secs
        return [{"device": d, "app": a, "proc": p, "seconds": int(s)}
                for (d, a, p), s in sorted(acc.items(), key=lambda kv: -kv[1])]

    def summary(self, days: int = 7) -> dict[str, Any]:
        """Per-day desktop-active and phone-screen minutes over a window, plus the
        top apps across the whole window. Drives the Activity range strip."""
        days = max(1, min(int(days), 90))
        today0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        window_start = (today0 - timedelta(days=days - 1)).timestamp()
        now = time.time()
        with self._lock:
            rows = self._rows_between(window_start, now)
        # Bucket each segment's seconds into the local day(s) it touches.
        day_desktop: dict[str, float] = {}
        day_phone: dict[str, float] = {}
        app_secs: dict[str, float] = {}
        labels = [(today0 - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
                  for i in range(days)]
        bounds = {lab: self._day_bounds(lab)[:2] for lab in labels}
        for r in rows:
            if _excluded(r["app"], r["proc"]):
                continue
            for lab, (lo, hi) in bounds.items():
                secs = self._clip(r["start_ts"], r["end_ts"], lo, hi)
                if secs <= 0:
                    continue
                if r["device"] == "desktop":
                    day_desktop[lab] = day_desktop.get(lab, 0.0) + secs
                    app_secs[r["app"]] = app_secs.get(r["app"], 0.0) + secs
                else:
                    day_phone[lab] = day_phone.get(lab, 0.0) + secs
        series = [{"date": lab,
                   "desktop_min": round(day_desktop.get(lab, 0.0) / 60),
                   "phone_min": round(day_phone.get(lab, 0.0) / 60)}
                  for lab in labels]
        apps = sorted(({"name": k, "seconds": int(v), "for": _hms(v)}
                       for k, v in app_secs.items() if v >= 60),
                      key=lambda a: a["seconds"], reverse=True)[:12]
        return {
            "window_days": days,
            "series": series,
            "apps": apps,
            "desktop_min": round(sum(day_desktop.values()) / 60),
            "phone_min": round(sum(day_phone.values()) / 60),
        }

    def app_history(self, name: str, days: int = 14) -> dict[str, Any]:
        """Per-day seconds for one app across a window — drives the app drill-down
        ("how much have I used <app> lately"). Matches by app name
        case-insensitively; `device` in the result is where that app was seen
        (desktop vs phone), so the caller can label it. Excluded apps return an
        empty series rather than pretending to have history."""
        days = max(1, min(int(days), 90))
        target = (name or "").strip().lower()
        today0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        window_start = (today0 - timedelta(days=days - 1)).timestamp()
        now = time.time()
        labels = [(today0 - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
                  for i in range(days)]
        bounds = {lab: self._day_bounds(lab)[:2] for lab in labels}
        day_secs: dict[str, float] = {}
        device: Optional[str] = None
        with self._lock:
            rows = self._rows_between(window_start, now)
        for r in rows:
            if (r["app"] or "").strip().lower() != target:
                continue
            if _excluded(r["app"], r["proc"]):
                continue
            device = r["device"]
            for lab, (lo, hi) in bounds.items():
                secs = self._clip(r["start_ts"], r["end_ts"], lo, hi)
                if secs > 0:
                    day_secs[lab] = day_secs.get(lab, 0.0) + secs
        series = [{"date": lab, "seconds": int(day_secs.get(lab, 0.0)),
                   "min": round(day_secs.get(lab, 0.0) / 60)} for lab in labels]
        total = sum(day_secs.values())
        return {
            "app": name,
            "device": device,
            "window_days": days,
            "series": series,
            "total_seconds": int(total),
            "total": _hms(total),
            "active_days": sum(1 for v in day_secs.values() if v >= 60),
            "avg_min": round((total / 60) / max(1, sum(1 for v in day_secs.values() if v >= 60)))
                       if any(v >= 60 for v in day_secs.values()) else 0,
        }

    def count(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM segments").fetchone()[0])


def _hms(seconds: float) -> str:
    """Compact duration: 45s / 12m / 1h05m."""
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m" if m else f"{h}h"


def _hm(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts).strftime("%H:%M")
    except (ValueError, OSError, OverflowError):
        return ""
