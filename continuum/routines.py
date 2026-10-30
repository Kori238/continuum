"""Routines — the recurring structure of Jake's day.

A routine item is something that recurs: a habit to keep (check it off daily), a
time block (morning pages, gym, deep work), or a reminder. Each has an optional
time-of-day and a day-of-week pattern, so "today's routine" is just the items
that apply to today, time-ordered, each flagged done/not-done from a per-day
completion log.

This is the model behind the Routine tab. It's deliberately its own store
(`data/routines.db`) rather than piggybacking the scheduler: the scheduler fires
one-off/interval jobs, whereas routines are a standing daily/weekly template.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time
from datetime import date as _date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from .config import Config

log = logging.getLogger("continuum.routines")

KINDS = ("habit", "block", "reminder")
_PRESETS = {"daily", "weekdays", "weekends"}

# Sections group a routine by part of day; order is how they display top-to-bottom.
SECTIONS = ("morning", "afternoon", "evening", "night", "anytime")


def _norm_section(s: str) -> str:
    s = (s or "anytime").strip().lower()
    return s if s in SECTIONS else "anytime"


def _section_rank(s: str) -> int:
    try:
        return SECTIONS.index(s)
    except ValueError:
        return len(SECTIONS)


# Wall-clock hour → part of day, for timed items that weren't given a section
# explicitly. Boundaries: morning <12:00, afternoon <17:00, evening <21:00,
# otherwise night. Checked in order.
_SECTION_BOUNDS = ((12, "morning"), (17, "afternoon"), (21, "evening"))


def _section_for_time(time_str: str) -> Optional[str]:
    """Derive a part-of-day from a HH:MM time, or None if it can't be parsed."""
    try:
        hh = int((time_str or "").split(":")[0])
    except (ValueError, IndexError):
        return None
    if not 0 <= hh <= 23:
        return None
    for bound, name in _SECTION_BOUNDS:
        if hh < bound:
            return name
    return "night"


def _effective_section(section: str, time_str: str) -> str:
    """The section to display/group an item under: an explicit section always
    wins; otherwise fall back to the part of day implied by its time."""
    if section and section != "anytime":
        return section
    return _section_for_time(time_str) or "anytime"


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _weekday_of(date_str: str) -> int:
    return datetime.strptime(date_str, "%Y-%m-%d").weekday()  # Mon=0 … Sun=6


def _applies(days: str, weekday: int) -> bool:
    days = (days or "daily").strip().lower()
    if days == "daily":
        return True
    if days == "weekdays":
        return weekday < 5
    if days == "weekends":
        return weekday >= 5
    # CSV of weekday numbers, e.g. "0,2,4"
    try:
        return weekday in {int(x) for x in days.split(",") if x.strip() != ""}
    except ValueError:
        return True


class RoutineStore:
    def __init__(self, cfg: Config) -> None:
        self._path = Path(cfg.routines_db)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self._path), check_same_thread=False)
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS routines(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL, time TEXT DEFAULT '', days TEXT DEFAULT 'daily',
                kind TEXT DEFAULT 'habit', note TEXT DEFAULT '', section TEXT DEFAULT 'anytime',
                active INTEGER DEFAULT 1, sort REAL, created REAL,
                repeat_days INTEGER DEFAULT 0, offset_min INTEGER)"""
        )
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(routines)")}
        if "section" not in cols:
            self._db.execute("ALTER TABLE routines ADD COLUMN section TEXT DEFAULT 'anytime'")
        # repeat_days>0 → an interval task: it recurs N days after it's checked
        # off (not on a weekday pattern) and carries over day-to-day until done.
        if "repeat_days" not in cols:
            self._db.execute("ALTER TABLE routines ADD COLUMN repeat_days INTEGER DEFAULT 0")
        # offset_min IS NOT NULL → a time-relative ("anchored") item: it has no
        # fixed clock time. Its due-time today is the day's routine start for its
        # section (the `anchors` table below) plus this many minutes — so a
        # morning that begins whenever Jake logs on still has an internal order
        # and concrete times once he says "start routine".
        if "offset_min" not in cols:
            self._db.execute("ALTER TABLE routines ADD COLUMN offset_min INTEGER")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS completions(
                routine_id INTEGER, date TEXT, ts REAL, status TEXT DEFAULT 'done',
                PRIMARY KEY(routine_id, date))"""
        )
        ccols = {r[1] for r in self._db.execute("PRAGMA table_info(completions)")}
        if "status" not in ccols:
            self._db.execute("ALTER TABLE completions ADD COLUMN status TEXT DEFAULT 'done'")
        # Subtasks: an optional checklist under a routine item, checked off per-day
        # (they're part of the daily routine, so they reset each day like the parent).
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS subtasks(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                routine_id INTEGER, title TEXT NOT NULL, sort REAL, created REAL)"""
        )
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS subtask_completions(
                subtask_id INTEGER, date TEXT, ts REAL,
                PRIMARY KEY(subtask_id, date))"""
        )
        # To-dos: one-off tasks that persist across days until done (NOT recurring).
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS todos(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL, note TEXT DEFAULT '', done INTEGER DEFAULT 0,
                created REAL, completed_at REAL, sort REAL)"""
        )
        # Anchors: the day's start time for a section, stamped by `start_routine`.
        # Anchored items (offset_min set) compute their due-times from it. Keyed by
        # (date, section) so it resets at local midnight like completions, and a
        # re-stamp just overwrites the row (Jake restarting because he began late).
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS anchors(
                date TEXT, section TEXT, ts REAL,
                PRIMARY KEY(date, section))"""
        )
        self._db.commit()

    # ---- mutations ----------------------------------------------------------
    def add(self, title: str, time_of_day: str = "", days: str = "daily",
            kind: str = "habit", note: str = "", section: str = "anytime",
            repeat_days: int = 0, offset_min: Any = None) -> int:
        title = title.strip()
        if not title:
            raise ValueError("routine needs a title")
        kind = kind if kind in KINDS else "habit"
        now = time.time()
        off = _norm_offset(offset_min)
        cur = self._db.execute(
            "INSERT INTO routines(title, time, days, kind, note, section, active, sort, created, repeat_days, offset_min) "
            "VALUES(?,?,?,?,?,?,1,?,?,?,?)",
            (title, "" if off is not None else _norm_time(time_of_day), _norm_days(days),
             kind, note.strip(), _norm_section(section), now, now,
             _norm_repeat(repeat_days), off))
        self._db.commit()
        return int(cur.lastrowid)

    def update(self, rid: int, **fields: Any) -> bool:
        allowed = {"title", "time", "days", "kind", "note", "active", "sort",
                   "section", "repeat_days", "offset_min"}
        sets, args = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "time":
                v = _norm_time(v)
            elif k == "days":
                v = _norm_days(v)
            elif k == "section":
                v = _norm_section(v)
            elif k == "repeat_days":
                v = _norm_repeat(v)
            elif k == "offset_min":
                v = _norm_offset(v)  # None → NULL (a fixed-time item again)
            elif k == "kind" and v not in KINDS:
                v = "habit"
            sets.append(f"{k}=?"); args.append(v)
        # Setting an offset makes the item time-relative, so any stored clock time
        # is now meaningless — clear it unless this same update is also setting one.
        if fields.get("offset_min") not in (None, "", "none") and "time" not in fields:
            sets.append("time=?"); args.append("")
        if not sets:
            return False
        args.append(rid)
        self._db.execute(f"UPDATE routines SET {', '.join(sets)} WHERE id=?", args)
        self._db.commit()
        return True

    def delete(self, rid: int) -> bool:
        subs = [r[0] for r in self._db.execute(
            "SELECT id FROM subtasks WHERE routine_id=?", (rid,)).fetchall()]
        self._db.execute("DELETE FROM routines WHERE id=?", (rid,))
        self._db.execute("DELETE FROM completions WHERE routine_id=?", (rid,))
        self._db.execute("DELETE FROM subtasks WHERE routine_id=?", (rid,))
        for sid in subs:
            self._db.execute("DELETE FROM subtask_completions WHERE subtask_id=?", (sid,))
        self._db.commit()
        return True

    # ---- subtasks -----------------------------------------------------------
    def add_subtask(self, routine_id: int, title: str) -> int:
        title = title.strip()
        if not title:
            raise ValueError("subtask needs a title")
        now = time.time()
        cur = self._db.execute(
            "INSERT INTO subtasks(routine_id, title, sort, created) VALUES(?,?,?,?)",
            (routine_id, title, now, now))
        self._db.commit()
        return int(cur.lastrowid)

    def delete_subtask(self, sid: int) -> bool:
        self._db.execute("DELETE FROM subtasks WHERE id=?", (sid,))
        self._db.execute("DELETE FROM subtask_completions WHERE subtask_id=?", (sid,))
        self._db.commit()
        return True

    def set_subtask_done(self, sid: int, done: bool, date_str: Optional[str] = None) -> bool:
        d = date_str or _today_str()
        if done:
            self._db.execute(
                "INSERT OR REPLACE INTO subtask_completions(subtask_id, date, ts) VALUES(?,?,?)",
                (sid, d, time.time()))
        else:
            self._db.execute(
                "DELETE FROM subtask_completions WHERE subtask_id=? AND date=?", (sid, d))
        self._db.commit()
        row = self._db.execute("SELECT routine_id FROM subtasks WHERE id=?", (sid,)).fetchone()
        if row:
            self._sync_parent_from_subtasks(int(row[0]), d)
        return True

    def _sync_parent_from_subtasks(self, rid: int, d: str) -> None:
        """Keep a parent routine in step with its subtasks for day `d`: when every
        subtask is checked off the parent is marked done too; un-ticking one pulls
        an all-done parent back to pending. Only acts on routines that actually
        have subtasks, and never overrides a 'skipped' status."""
        total = self._db.execute(
            "SELECT COUNT(*) FROM subtasks WHERE routine_id=?", (rid,)).fetchone()[0]
        if not total:
            return
        done = self._db.execute(
            "SELECT COUNT(*) FROM subtask_completions sc JOIN subtasks s "
            "ON s.id=sc.subtask_id WHERE s.routine_id=? AND sc.date=?", (rid, d)).fetchone()[0]
        cur = self._db.execute(
            "SELECT status FROM completions WHERE routine_id=? AND date=?", (rid, d)).fetchone()
        cur_status = (cur[0] if cur else "") or ""
        if done >= total and cur_status != "done":
            self.set_status(rid, "done", d)
        elif done < total and cur_status == "done":
            self.set_status(rid, "none", d)

    def resolve_subtask(self, title: str, routine_id: Optional[int] = None) -> Optional[int]:
        title = (title or "").strip().lower()
        if not title:
            return None
        q = "SELECT id, title FROM subtasks"
        args: list[Any] = []
        if routine_id is not None:
            q += " WHERE routine_id=?"; args.append(routine_id)
        rows = self._db.execute(q, args).fetchall()
        for sid, t in rows:
            if t.strip().lower() == title:
                return sid
        hits = [sid for sid, t in rows if title in t.strip().lower()]
        return hits[0] if len(hits) == 1 else None

    # ---- to-dos -------------------------------------------------------------
    def add_todo(self, title: str, note: str = "") -> int:
        title = title.strip()
        if not title:
            raise ValueError("todo needs a title")
        now = time.time()
        cur = self._db.execute(
            "INSERT INTO todos(title, note, done, created, sort) VALUES(?,?,0,?,?)",
            (title, note.strip(), now, now))
        self._db.commit()
        return int(cur.lastrowid)

    def set_todo_done(self, tid: int, done: bool) -> bool:
        self._db.execute("UPDATE todos SET done=?, completed_at=? WHERE id=?",
                         (1 if done else 0, time.time() if done else None, tid))
        self._db.commit()
        return True

    def delete_todo(self, tid: int) -> bool:
        self._db.execute("DELETE FROM todos WHERE id=?", (tid,))
        self._db.commit()
        return True

    def resolve_todo(self, name: str) -> Optional[int]:
        name = (name or "").strip().lower()
        if not name:
            return None
        rows = self._db.execute("SELECT id, title FROM todos WHERE done=0").fetchall()
        for tid, t in rows:
            if t.strip().lower() == name:
                return tid
        hits = [tid for tid, t in rows if name in t.strip().lower()]
        return hits[0] if len(hits) == 1 else None

    def todos(self, include_done: bool = True, done_limit: int = 15) -> list[dict[str, Any]]:
        self._db.row_factory = sqlite3.Row
        open_rows = self._db.execute(
            "SELECT * FROM todos WHERE done=0 ORDER BY sort, created").fetchall()
        out = [{"id": r["id"], "title": r["title"], "note": r["note"], "done": False,
                "created": r["created"]} for r in open_rows]
        if include_done:
            done_rows = self._db.execute(
                "SELECT * FROM todos WHERE done=1 ORDER BY completed_at DESC LIMIT ?",
                (done_limit,)).fetchall()
            out += [{"id": r["id"], "title": r["title"], "note": r["note"], "done": True,
                     "created": r["created"], "completed_at": r["completed_at"]}
                    for r in done_rows]
        return out

    def set_status(self, rid: int, status: str, date_str: Optional[str] = None) -> bool:
        """Per-day status of a routine item: 'done', 'skipped', or 'none' (clears it)."""
        d = date_str or _today_str()
        status = (status or "").strip().lower()
        if status in ("", "none", "pending", "clear"):
            self._db.execute("DELETE FROM completions WHERE routine_id=? AND date=?", (rid, d))
        else:
            if status not in ("done", "skipped"):
                status = "done"
            self._db.execute(
                "INSERT OR REPLACE INTO completions(routine_id, date, ts, status) VALUES(?,?,?,?)",
                (rid, d, time.time(), status))
        self._db.commit()
        return True

    def set_done(self, rid: int, done: bool, date_str: Optional[str] = None) -> bool:
        return self.set_status(rid, "done" if done else "none", date_str)

    def status_of(self, rid: int, date_str: Optional[str] = None) -> str:
        """The per-day status of a routine item — 'done', 'skipped', or '' (pending)
        when no completion row exists for the date."""
        d = date_str or _today_str()
        row = self._db.execute(
            "SELECT status FROM completions WHERE routine_id=? AND date=?", (rid, d)).fetchone()
        return ((row[0] or "done") if row else "")

    # ---- routine start (time-relative anchoring) ----------------------------
    def start_routine(self, section: str = "morning", date_str: Optional[str] = None,
                      ts: Optional[float] = None) -> float:
        """Stamp *now* (or a given ts) as the day's start, so every anchored item
        (offset_min set), whatever its section, gets a concrete due-time counted
        from it. Re-calling overwrites — Jake can restart if he actually began
        later, and it shifts the whole day, not just one section."""
        d = date_str or _today_str()
        at = float(ts if ts is not None else time.time())
        # One start per day: clear any prior anchor first so a re-stamp (Jake began
        # later than he clicked) moves the whole day and _day_anchor never picks up
        # a stale earlier ts.
        self._db.execute("DELETE FROM anchors WHERE date=?", (d,))
        self._db.execute(
            "INSERT INTO anchors(date, section, ts) VALUES(?,?,?)",
            (d, _norm_section(section), at))
        self._db.commit()
        return at

    def clear_anchor(self, section: str = "morning", date_str: Optional[str] = None) -> bool:
        """Un-start the day — every anchored item, in any section, goes back to an
        untimed '+Nm' step waiting on a fresh start."""
        d = date_str or _today_str()
        self._db.execute("DELETE FROM anchors WHERE date=?", (d,))
        self._db.commit()
        return True

    def _day_anchor(self, date_str: str) -> Optional[float]:
        """The day's single routine-start ts, or None. One start now anchors the
        whole day — morning, afternoon and evening steps all count their offsets
        from it — so any anchor row for the date serves; take the earliest if more
        than one somehow exists."""
        row = self._db.execute(
            "SELECT ts FROM anchors WHERE date=? ORDER BY ts LIMIT 1",
            (date_str,)).fetchone()
        return float(row[0]) if row else None

    # ---- reads --------------------------------------------------------------
    def _row(self, r: sqlite3.Row) -> dict[str, Any]:
        section = (r["section"] if "section" in r.keys() else None) or "anytime"
        repeat = (r["repeat_days"] if "repeat_days" in r.keys() else 0) or 0
        off = (r["offset_min"] if "offset_min" in r.keys() else None)
        return {"id": r["id"], "title": r["title"], "time": r["time"], "days": r["days"],
                "kind": r["kind"], "note": r["note"], "section": section,
                "active": bool(r["active"]), "sort": r["sort"], "repeat_days": int(repeat),
                "offset_min": (int(off) if off is not None else None)}

    def all(self) -> list[dict[str, Any]]:
        self._db.row_factory = sqlite3.Row
        rows = [self._row(r) for r in self._db.execute("SELECT * FROM routines").fetchall()]
        rows.sort(key=lambda it: (not it["active"], _section_rank(it["section"]),
                                  it["time"] == "", it["time"], it.get("sort") or 0))
        return rows

    def today(self, date_str: Optional[str] = None) -> dict[str, Any]:
        d = date_str or _today_str()
        wd = _weekday_of(d)
        self._db.row_factory = sqlite3.Row
        status_map = {r[0]: (r[1] or "done") for r in self._db.execute(
            "SELECT routine_id, status FROM completions WHERE date=?", (d,)).fetchall()}
        day_ats = self._day_anchor(d)  # the day's single start ts, for anchored items
        # Subtasks (per-day completion), grouped by their parent routine.
        sub_done = {r[0] for r in self._db.execute(
            "SELECT subtask_id FROM subtask_completions WHERE date=?", (d,)).fetchall()}
        subs_by_routine: dict[int, list[dict[str, Any]]] = {}
        for s in self._db.execute("SELECT id, routine_id, title FROM subtasks ORDER BY sort, id"):
            subs_by_routine.setdefault(s["routine_id"], []).append(
                {"id": s["id"], "title": s["title"], "done": s["id"] in sub_done})
        # Done-completion history, needed by interval ("repeat every N days") tasks:
        # they don't follow a weekday pattern — they reappear N days after they
        # were last checked off, and carry over day-to-day until then.
        done_dates: dict[int, list[str]] = {}
        for cid, cdate in self._db.execute(
                "SELECT routine_id, date FROM completions WHERE status='done'").fetchall():
            done_dates.setdefault(cid, []).append(cdate)
        items = []
        for r in self._db.execute("SELECT * FROM routines WHERE active=1").fetchall():
            rep = (r["repeat_days"] if "repeat_days" in r.keys() else 0) or 0
            if rep > 0:
                if not self._interval_due(r, d, done_dates.get(r["id"], []),
                                          r["id"] in status_map):
                    continue
            elif not _applies(r["days"], wd):
                continue
            it = self._row(r)
            it["section_raw"] = it["section"]
            it["section"] = _effective_section(it["section"], it["time"])
            # Time-relative item: derive today's clock time from its section's
            # start, or leave it untimed with a "+Nm" label until the day starts.
            off = it.get("offset_min")
            if off is not None:
                it["anchored"] = True
                ats = day_ats
                if ats is not None:
                    it["time"] = (datetime.fromtimestamp(ats)
                                  + timedelta(minutes=off)).strftime("%H:%M")
                    it["started"] = True
                else:
                    it["time"] = ""
                    it["started"] = False
                    it["rel"] = _fmt_offset(off)
            st = status_map.get(r["id"], "")
            it["status"] = st
            it["done"] = st == "done"
            it["skipped"] = st == "skipped"
            it["streak"] = self._streak(r["id"], d) if r["kind"] == "habit" else 0
            it["subtasks"] = subs_by_routine.get(r["id"], [])
            it["sub_total"] = len(it["subtasks"])
            it["sub_done"] = sum(1 for s in it["subtasks"] if s["done"])
            items.append(it)
        # Group by section (morning→…→anytime); within a section, timed items
        # first by clock, then untimed in the order they were added (a sequenced
        # routine like a morning list has no times but a real order).
        items.sort(key=lambda it: (_section_rank(it["section"]),
                                   it["time"] == "", it["time"],
                                   it["offset_min"] if it.get("offset_min") is not None else 1e9,
                                   it.get("sort") or 0))
        done_ct = sum(1 for it in items if it["done"])
        skipped_ct = sum(1 for it in items if it["skipped"])
        # Per-section grouping for the UI, preserving the sorted order.
        sections: list[dict[str, Any]] = []
        for it in items:
            if not sections or sections[-1]["section"] != it["section"]:
                sections.append({"section": it["section"], "items": []})
            sections[-1]["items"].append(it)
        for grp in sections:
            grp["done"] = sum(1 for it in grp["items"] if it["done"])
            grp["skipped"] = sum(1 for it in grp["items"] if it["skipped"])
            grp["total"] = len(grp["items"])
            # A section with time-relative steps reflects the day's single start:
            # once it's stamped, every anchored step across the day shares it.
            grp["anchored"] = any(it.get("anchored") for it in grp["items"])
            ats = day_ats
            grp["started"] = bool(grp["anchored"] and ats is not None)
            grp["started_at"] = (datetime.fromtimestamp(ats).strftime("%H:%M")
                                 if grp["started"] else None)
        return {"date": d, "weekday": wd, "items": items, "sections": sections,
                "done": done_ct, "skipped": skipped_ct, "total": len(items)}

    def _interval_due(self, r: sqlite3.Row, d: str, done_list: list[str],
                      has_status: bool) -> bool:
        """Whether an interval task (repeat every N days) should appear on day `d`.

        It becomes due N days after the last day it was checked off — or from the
        day it was created, if never done — and then **carries over** every day
        until it's marked off. Once done it hides for N days. Showing it on any
        day it was actioned (done or skipped) keeps it visible right after ticking
        rather than vanishing mid-interaction; a skip doesn't reset the interval,
        so a skipped task reappears the next day."""
        if has_status:
            return True
        rep = int(r["repeat_days"] or 0)
        prev = max((x for x in done_list if x < d), default=None)
        if prev:
            base = (datetime.strptime(prev, "%Y-%m-%d").date()
                    + timedelta(days=rep)).strftime("%Y-%m-%d")
        else:
            base = datetime.fromtimestamp(r["created"] or 0).strftime("%Y-%m-%d")
        return d >= base  # ISO dates compare lexicographically

    def overdue(self, grace_minutes: float = 45) -> list[dict[str, Any]]:
        """Today's timed steps that are still pending (not done, not skipped) and
        whose time is more than `grace_minutes` in the past — i.e. likely missed."""
        now = datetime.now()
        out: list[dict[str, Any]] = []
        for it in self.today()["items"]:
            if it["status"] or not it["time"]:
                continue  # already done/skipped, or has no time to be "late" against
            try:
                hh, mm = (int(x) for x in it["time"].split(":"))
            except ValueError:
                continue
            due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if (now - due).total_seconds() >= grace_minutes * 60:
                out.append(it)
        return out

    def performance(self, window_days: int = 30) -> dict[str, Any]:
        """How Jake is doing on his routine over the last `window_days`.

        For each past day, work out which routines *applied* (right weekday and
        already created) — that's the denominator — then tally done/skipped/missed
        from the completions log. Adherence excludes skipped days (a deliberate
        skip shouldn't read as a failure)."""
        window_days = max(1, min(int(window_days), 365))
        self._db.row_factory = sqlite3.Row
        # Interval ("repeat every N days") tasks are excluded: their scheduled
        # days aren't a weekday pattern, so a weekday-based denominator would be
        # wrong. Adherence here answers "how am I doing on my daily/weekly routine".
        routines = [r for r in self._db.execute(
            "SELECT id, title, days, kind, created, repeat_days FROM routines WHERE active=1")
            if not (r["repeat_days"] or 0)]
        comp: dict[tuple, str] = {}
        for c in self._db.execute("SELECT routine_id, date, status FROM completions"):
            comp[(c["routine_id"], c["date"])] = c["status"] or "done"

        today = datetime.now().date()
        created_on = {r["id"]: datetime.fromtimestamp(r["created"] or 0).date() for r in routines}
        per: dict[int, dict[str, Any]] = {
            r["id"]: {"id": r["id"], "title": r["title"], "kind": r["kind"],
                      "scheduled": 0, "done": 0, "skipped": 0, "missed": 0} for r in routines}
        daily: list[dict[str, Any]] = []
        earliest: Optional[str] = None

        for offset in range(window_days - 1, -1, -1):
            day = today - timedelta(days=offset)
            ds = day.strftime("%Y-%m-%d")
            wd = day.weekday()
            d_sched = d_done = d_skip = 0
            for r in routines:
                if created_on[r["id"]] > day or not _applies(r["days"], wd):
                    continue
                st = comp.get((r["id"], ds), "")
                p = per[r["id"]]
                p["scheduled"] += 1
                d_sched += 1
                if st == "done":
                    p["done"] += 1; d_done += 1
                elif st == "skipped":
                    p["skipped"] += 1; d_skip += 1
                else:
                    p["missed"] += 1
            if d_sched:
                if earliest is None:
                    earliest = ds
                denom = d_sched - d_skip
                daily.append({"date": ds, "done": d_done, "scheduled": d_sched,
                              "skipped": d_skip,
                              "rate": round(100 * d_done / denom) if denom else None})

        def _rate(p: dict[str, Any]) -> Optional[int]:
            denom = p["scheduled"] - p["skipped"]
            return round(100 * p["done"] / denom) if denom else None

        rows = []
        for p in per.values():
            p["rate"] = _rate(p)
            p["streak"] = self._streak(p["id"], today.strftime("%Y-%m-%d")) if p["kind"] == "habit" else 0
            rows.append(p)
        rows.sort(key=lambda p: (p["rate"] is None, -(p["rate"] or 0), -p["scheduled"]))

        tot_sched = sum(p["scheduled"] for p in rows)
        tot_done = sum(p["done"] for p in rows)
        tot_skip = sum(p["skipped"] for p in rows)
        denom = tot_sched - tot_skip
        return {
            "window_days": window_days,
            "since": earliest,
            "overall": {"scheduled": tot_sched, "done": tot_done, "skipped": tot_skip,
                        "missed": tot_sched - tot_done - tot_skip,
                        "rate": round(100 * tot_done / denom) if denom else None},
            "daily": daily,
            "per_routine": rows,
        }

    def candidates(self, name: str, section: str = "") -> list[dict[str, Any]]:
        """Every routine a title could mean, narrowed by section when given.

        Exact matches win outright rather than merging with the substring ones:
        'Teeth' must not go ambiguous merely because a 'Teeth whitening' also
        exists. Only a plural EXACT set is a real ambiguity."""
        name = (name or "").strip().lower()
        if not name:
            return []
        self._db.row_factory = sqlite3.Row
        rows = [dict(r) for r in self._db.execute(
            "SELECT id, title, section, time FROM routines")]
        if (section or "").strip():
            want = _norm_section(section)
            rows = [r for r in rows if (r["section"] or "") == want]
        exact = [r for r in rows if r["title"].strip().lower() == name]
        return exact or [r for r in rows if name in r["title"].strip().lower()]

    def resolve(self, name: str, section: str = "") -> Optional[int]:
        """Find a routine id by title — the agent refers to routines by name.
        Exact (case-insensitive) first, then unique substring match; `section`
        narrows the search when a title legitimately appears more than once.

        **Ambiguity is refused in BOTH branches.** The substring branch always
        did (`hits[0] if len(hits) == 1 else None`); the exact branch returned
        the first row it happened to walk past, which is how Jake's evening
        'Teeth' became unreachable the moment the morning one existed. Every
        "done with teeth" landed on the morning row — 1 completion against 0 on
        a routine a month old — and the tool reported success every time.

        None reads to the caller as "not found", so the tools call candidates()
        to tell the two apart and name the real choices."""
        hits = self.candidates(name, section)
        return hits[0]["id"] if len(hits) == 1 else None

    def context_line(self) -> str:
        """Compact lines about today's routine + open to-dos, for ambient context."""
        lines: list[str] = []
        t = self.today()
        if t["items"]:
            def _fmt(it: dict) -> str:
                lead = (f"{it['time']} " if it["time"]
                        else (f"{it['rel']} " if it.get("rel") else ""))
                base = lead + it["title"]
                if it.get("sub_total"):
                    base += f" ({it['sub_done']}/{it['sub_total']} subtasks)"
                return base
            done = [it["title"] for it in t["items"] if it["done"]]
            skipped = [it["title"] for it in t["items"] if it["skipped"]]
            bits = [f"Jake's routine today: {t['done']}/{t['total']} done"]
            # "Still to do", grouped by part of day so morning/afternoon/evening
            # read as distinct blocks rather than one flat list.
            _labels = {"morning": "morning", "afternoon": "afternoon",
                       "evening": "evening", "night": "night", "anytime": "anytime"}
            pending = []
            for grp in t.get("sections", []):
                left = [_fmt(it) for it in grp["items"]
                        if not it["done"] and not it["skipped"]]
                if left:
                    pending.append(f"{_labels.get(grp['section'], grp['section'])} — "
                                   + ", ".join(left))
            if pending:
                bits.append("still to do: " + "; ".join(pending))
            # Anchored steps waiting on a start — the clock hasn't begun for them,
            # so they can't be "late"; flag it so a nudge can offer to start it.
            unstarted = sorted({it["section"] for it in t["items"]
                                if it.get("anchored") and not it.get("started")})
            if unstarted:
                bits.append(", ".join(unstarted) + " routine not started yet "
                            "(say 'start routine' to begin the clock)")
            if done:
                bits.append("done: " + ", ".join(done))
            if skipped:
                bits.append("skipped: " + ", ".join(skipped))
            lines.append("- " + "; ".join(bits))
        open_todos = [td["title"] for td in self.todos(include_done=False)]
        if open_todos:
            head = open_todos[:8]
            more = f" (+{len(open_todos) - len(head)} more)" if len(open_todos) > len(head) else ""
            lines.append(f"- Jake's to-do list ({len(open_todos)} open): " + ", ".join(head) + more)
        return "\n".join(lines)

    def _streak(self, rid: int, upto: str) -> int:
        """Consecutive days (ending today if done, else yesterday) this habit was done."""
        rows = self._db.execute(
            "SELECT date FROM completions WHERE routine_id=? AND status='done' "
            "ORDER BY date DESC", (rid,)).fetchall()
        days = {r[0] for r in rows}
        if not days:
            return 0
        from datetime import timedelta
        cur = datetime.strptime(upto, "%Y-%m-%d").date()
        if cur.strftime("%Y-%m-%d") not in days:   # allow streak to hold from yesterday
            cur = cur - timedelta(days=1)
        streak = 0
        while cur.strftime("%Y-%m-%d") in days:
            streak += 1
            cur = cur - timedelta(days=1)
        return streak


def _norm_time(t: str) -> str:
    t = (t or "").strip()
    if not t:
        return ""
    for fmt in ("%H:%M", "%I:%M%p", "%I%p", "%H"):
        try:
            return datetime.strptime(t.upper().replace(" ", ""), fmt).strftime("%H:%M")
        except ValueError:
            continue
    return t  # leave as-is if unparseable; UI still shows it


def _norm_offset(v: Any) -> Optional[int]:
    """Minutes-after-start for a time-relative item, or None (a fixed-time item).
    Blank / None / 'none' all mean 'not anchored'."""
    if v is None or (isinstance(v, str) and v.strip().lower() in ("", "none")):
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return max(0, n)


def _fmt_offset(mins: int) -> str:
    """A '+Nm' / '+Nh Nm' label for an anchored step before the day has started."""
    mins = max(0, int(mins))
    if mins < 60:
        return f"+{mins}m"
    h, m = divmod(mins, 60)
    return f"+{h}h" + (f"{m}m" if m else "")


def _norm_repeat(v: Any) -> int:
    """Clamp a repeat interval to a non-negative int; 0 means 'no interval
    repeat' (fall back to the weekday pattern)."""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


# Weekday names, Mon=0 to match datetime.weekday() and the tool schemas. Single
# letters are deliberately absent: 'T' is Tuesday or Thursday and 'S' is
# Saturday or Sunday, and a guess here would be silent and wrong half the time.
# Refusing 'M,W,F' costs one retry; guessing 'T' costs a missed workout.
_DAY_NAMES = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}
_DAY_SPLIT = re.compile(r"[,/&+;]|\band\b|\s+")

_DAYS_HELP = ("'daily', 'weekdays', 'weekends', weekday names like 'mon,thu', "
              "or weekday numbers with Mon=0 like '0,3'")


def _norm_days(days: str) -> str:
    """Normalise a recurrence to a stored `days` value, or refuse.

    The old version fell back to "daily" for anything it could not parse, and
    "daily" is not a neutral default - it is the WIDEST value the field has. So
    a failed narrowing widened: an item asked to run on two days ran on seven,
    the tool answered "updated", and nothing anywhere was red. Measured before
    this change, 8 of 10 phrasings a model reaches for ('Mon,Thu',
    'monday,thursday', 'Tue/Thu', ...) did exactly that.

    Two changes, and the second is the one that matters. Names and more
    separators are now understood, so the common guesses simply work; and an
    input that is genuinely not understood RAISES instead of widening. An empty
    value still means "daily", because that is a default nobody asked to
    override rather than a request that failed.
    """
    raw = (days or "").strip().lower()
    if not raw:
        return "daily"
    if raw in _PRESETS:
        return raw
    nums: list[int] = []
    for tok in _DAY_SPLIT.split(raw):
        tok = tok.strip().strip(".")
        if not tok:
            continue
        if tok.isdigit() and 0 <= int(tok) <= 6:
            n = int(tok)
        elif tok.rstrip("s") in _DAY_NAMES:   # 'mondays' -> 'monday'
            n = _DAY_NAMES[tok.rstrip("s")]
        else:
            raise ValueError(f"don't understand the days {days!r} — use {_DAYS_HELP}")
        if n not in nums:
            nums.append(n)
    if not nums:
        raise ValueError(f"don't understand the days {days!r} — use {_DAYS_HELP}")
    return ",".join(str(n) for n in sorted(nums))
