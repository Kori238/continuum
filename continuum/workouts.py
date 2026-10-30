"""Workouts — the local record of Jake's training history.

Speediance (his home gym machine) is the main feed, but this is a general
workout inbox, not a machine-only log: the phone's Health Connect reader also
lands exercise sessions here (e.g. DownDog yoga), reported over the phone link
and ingested by `app/server.py` (`_phone_workout`). Such sessions upsert by a
`hc:<uuid>` training_id and carry no per-set detail — device_type/session_type
stay 0, so the render tools show them by title alone rather than as a machine
session. Because the same real session reaches Health Connect under several
record ids — two data sources writing it, or a live session re-reported as it
lengthens — `upsert_session` content-dedupes hc: rows by (title, start within
~2 min), keeping the most complete copy. And step-based cardio (walking/running)
is excluded from the expenditure sum (`calories_on`) since its burn already
lives in the day's step count.

Speediance's cloud only keeps sessions; it has no notion of *trends*. So we pull
completed sessions into our own store (`data/workouts.db`) and accumulate them,
which means insights can span far more than a single API pull and survive the
integration breaking (see `integrations/speediance.py` on why it might).

Two tables: `sessions` (one row per completed workout, list-level totals) and
`sets` (per-set reps/weight, present only for program sessions — Free Lift
sessions report totals with no per-set breakdown). The store is the single source
the DB-lookup tools and the non-AI web explorers both read; analytics
(volume-over-time, per-exercise progression, estimated 1RM) are computed here so
both callers agree.

Sync is idempotent: a session upserts by `training_id`, and re-fetching a
session's detail replaces its set rows wholesale, so pulling the same window
twice changes nothing.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .config import Config

log = logging.getLogger("continuum.workouts")

# Health Connect session titles whose calories are ALREADY captured by the day's
# step count — walking/running/hiking burn is step-derived, so counting these
# sessions on top of `steps_kcal` would double-count the same effort. They still
# render in the Workouts list; they're only excluded from the energy-expenditure
# sum (`calories_on`). Strength, yoga, cycling etc. don't move the step counter,
# so they stay additive.
STEP_ACTIVITIES = {"walking", "walk", "running", "run", "hiking", "hike"}


def _epley_1rm(weight: float, reps: int) -> float:
    """Estimated one-rep max (Epley). reps<=1 → the weight itself."""
    if weight <= 0 or reps <= 0:
        return 0.0
    return weight * (1 + reps / 30.0) if reps > 1 else weight


class WorkoutStore:
    def __init__(self, cfg: Config) -> None:
        self._path = Path(cfg.workouts_db)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self._path), check_same_thread=False)
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS sessions(
                training_id TEXT PRIMARY KEY,
                title TEXT DEFAULT 'Workout',
                start_ts REAL, end_ts REAL, duration_s INTEGER DEFAULT 0,
                calories REAL DEFAULT 0, total_volume REAL DEFAULT 0,
                total_energy REAL DEFAULT 0, device_type INTEGER DEFAULT 0,
                session_type INTEGER DEFAULT 0, muscles TEXT DEFAULT '{}',
                set_count INTEGER DEFAULT 0, detail_synced INTEGER DEFAULT 0,
                synced_at REAL)"""
        )
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS sets(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                training_id TEXT, set_index INTEGER,
                exercise_id TEXT, exercise_name TEXT,
                reps INTEGER DEFAULT 0, weight_kg REAL DEFAULT 0)"""
        )
        self._db.execute("CREATE INDEX IF NOT EXISTS ix_sets_tid ON sets(training_id)")
        self._db.execute("CREATE INDEX IF NOT EXISTS ix_sets_ex ON sets(exercise_name)")
        self._db.commit()

    # ---- ingest -------------------------------------------------------------
    def upsert_session(self, s: dict[str, Any]) -> bool:
        """Insert or update one normalised session (from the client). Returns
        True if it was a new row. Preserves detail_synced/set_count on update so
        a list-level re-pull doesn't wipe already-fetched set detail."""
        tid = str(s.get("training_id") or "")
        if not tid:
            return False
        # Health Connect gives each record a *source-specific* id, so the same real
        # session reaches us under several `hc:<uuid>` ids and slips past the
        # training_id key — the "my walk shows up twice" bug. Two causes: two data
        # sources write the same session (Fitbit app + Google Fit), and a live
        # session gets re-reported under a fresh id as it lengthens (so start
        # matches but duration/calories grow). Collapse by activity + start time:
        # an hc: row with the same title starting within ~2 min is the same
        # session. Keep the most complete copy (longest duration) — drop the new
        # report if it's not longer, else supersede the older row(s) with it.
        superseded = False
        if tid.startswith("hc:") and s.get("start_ts") is not None:
            start = float(s.get("start_ts"))
            new_dur = int(s.get("duration_s") or 0)
            prior = self._db.execute(
                "SELECT training_id, duration_s FROM sessions WHERE training_id LIKE 'hc:%' "
                "AND training_id!=? AND lower(title)=lower(?) AND ABS(start_ts-?)<=120",
                (tid, s.get("title", "Workout"), start)).fetchall()
            if prior:
                if new_dur <= max(int(p[1] or 0) for p in prior):
                    return False   # an equal-or-less-complete re-report; keep what we have
                for old_tid, _ in prior:   # this report is fuller — retire the older copies
                    self._db.execute("DELETE FROM sets WHERE training_id=?", (old_tid,))
                    self._db.execute("DELETE FROM sessions WHERE training_id=?", (old_tid,))
                superseded = True
        existed = self._db.execute(
            "SELECT 1 FROM sessions WHERE training_id=?", (tid,)).fetchone() is not None
        self._db.execute(
            """INSERT INTO sessions(training_id, title, start_ts, end_ts, duration_s,
                    calories, total_volume, total_energy, device_type, session_type,
                    muscles, synced_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(training_id) DO UPDATE SET
                    title=excluded.title, start_ts=excluded.start_ts,
                    end_ts=excluded.end_ts, duration_s=excluded.duration_s,
                    calories=excluded.calories, total_volume=excluded.total_volume,
                    total_energy=excluded.total_energy, device_type=excluded.device_type,
                    session_type=excluded.session_type, muscles=excluded.muscles,
                    synced_at=excluded.synced_at""",
            (tid, s.get("title", "Workout"), s.get("start_ts"), s.get("end_ts"),
             int(s.get("duration_s") or 0), float(s.get("calories") or 0),
             float(s.get("total_volume") or 0), float(s.get("total_energy") or 0),
             int(s.get("device_type") or 0), int(s.get("session_type") or 0),
             json.dumps(s.get("muscles") or {}), time.time()))
        self._db.commit()
        return not existed and not superseded   # a superseded dup isn't a *new* session

    def store_sets(self, training_id: str, sets: list[dict[str, Any]]) -> None:
        """Replace all set rows for a session (idempotent re-sync) and mark its
        detail as fetched — even when empty (a Free Lift session has no sets, and
        we don't want to re-request it forever)."""
        tid = str(training_id)
        self._db.execute("DELETE FROM sets WHERE training_id=?", (tid,))
        for st in sets:
            self._db.execute(
                "INSERT INTO sets(training_id, set_index, exercise_id, exercise_name, "
                "reps, weight_kg) VALUES(?,?,?,?,?,?)",
                (tid, int(st.get("set_index") or 0), str(st.get("exercise_id") or ""),
                 str(st.get("exercise_name") or "Exercise"),
                 int(st.get("reps") or 0), float(st.get("weight_kg") or 0)))
        self._db.execute(
            "UPDATE sessions SET detail_synced=1, set_count=? WHERE training_id=?",
            (len(sets), tid))
        self._db.commit()

    def sync(self, client: Any, days: int = 365, *, detail: bool = True) -> dict[str, Any]:
        """Pull sessions (and, for any without stored detail, their per-set data)
        from Speediance into the store. Network-bound and best-effort: a detail
        fetch that fails is logged and skipped, not fatal — the session still
        lands with its list-level totals. Returns a summary of what changed."""
        sessions = client.list_sessions(days=days)
        new = 0
        for s in sessions:
            if self.upsert_session(s):
                new += 1
        detailed = 0
        if detail:
            # Pass each session's authoritative list `type` so session_detail probes
            # the right namespace first — a trainingId can resolve to two unrelated
            # sessions across the program/free namespaces (see speediance.py).
            for tid, stype in self._sessions_missing_detail():
                try:
                    d = client.session_detail(tid, stype)
                    self.store_sets(tid, d.get("sets") or [])
                    detailed += 1
                except Exception as exc:   # one bad session must not stop the sync
                    log.warning("detail fetch failed for %s: %s", tid, exc)
        return {"pulled": len(sessions), "new": new, "detailed": detailed,
                "total": self.count(), "synced_at": time.time()}

    def _sessions_missing_detail(self) -> list[tuple[str, int]]:
        return [(r[0], int(r[1] or 0)) for r in self._db.execute(
            "SELECT training_id, session_type FROM sessions WHERE detail_synced=0 "
            "ORDER BY start_ts DESC").fetchall()]

    # ---- reads --------------------------------------------------------------
    def count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])

    def last_synced(self) -> Optional[float]:
        r = self._db.execute("SELECT MAX(synced_at) FROM sessions").fetchone()
        return r[0] if r and r[0] else None

    def calories_on(self, date: str) -> float:
        """Workout calories on one LOCAL calendar day (YYYY-MM-DD) that are NOT
        already counted in the day's steps — the training half of a day's energy
        expenditure. Step-based cardio (see STEP_ACTIVITIES) is excluded because
        its burn is already in `steps_kcal`; adding it here would double-count the
        same walk. sqlite resolves the local day from each session's start_ts,
        matching the store's date-keyed rule."""
        placeholders = ",".join("?" for _ in STEP_ACTIVITIES)
        r = self._db.execute(
            "SELECT COALESCE(SUM(calories),0) FROM sessions "
            "WHERE date(start_ts,'unixepoch','localtime')=? "
            f"AND lower(title) NOT IN ({placeholders})",
            (str(date), *STEP_ACTIVITIES)).fetchone()
        return float(r[0] or 0)

    def has_session_on(self, date: str) -> bool:
        """True if any workout falls on the given LOCAL day (YYYY-MM-DD) — the
        'did Jake train today?' check behind the routine auto-tick."""
        r = self._db.execute(
            "SELECT 1 FROM sessions "
            "WHERE date(start_ts,'unixepoch','localtime')=? LIMIT 1",
            (str(date),)).fetchone()
        return r is not None

    def _session_row(self, r: sqlite3.Row) -> dict[str, Any]:
        try:
            muscles = json.loads(r["muscles"] or "{}")
        except (ValueError, TypeError):
            muscles = {}
        return {
            "training_id": r["training_id"], "title": r["title"],
            "start_ts": r["start_ts"], "end_ts": r["end_ts"],
            "duration_s": r["duration_s"], "calories": r["calories"],
            "total_volume": r["total_volume"], "total_energy": r["total_energy"],
            "device_type": r["device_type"], "session_type": r["session_type"],
            "muscles": muscles, "set_count": r["set_count"],
            "date": _fmt_date(r["start_ts"]),
        }

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        self._db.row_factory = sqlite3.Row
        rows = self._db.execute(
            "SELECT * FROM sessions ORDER BY start_ts DESC LIMIT ?", (limit,)).fetchall()
        return [self._session_row(r) for r in rows]

    def session(self, training_id: str) -> Optional[dict[str, Any]]:
        self._db.row_factory = sqlite3.Row
        r = self._db.execute(
            "SELECT * FROM sessions WHERE training_id=?", (str(training_id),)).fetchone()
        if not r:
            return None
        out = self._session_row(r)
        sets = self._db.execute(
            "SELECT set_index, exercise_id, exercise_name, reps, weight_kg "
            "FROM sets WHERE training_id=? ORDER BY exercise_name, set_index",
            (str(training_id),)).fetchall()
        # Group sets by exercise for a readable per-session breakdown.
        by_ex: dict[str, dict[str, Any]] = {}
        for s in sets:
            g = by_ex.setdefault(s["exercise_name"], {"exercise": s["exercise_name"], "sets": []})
            g["sets"].append({"set": s["set_index"] + 1, "reps": s["reps"],
                              "weight_kg": s["weight_kg"],
                              "volume": round(s["reps"] * s["weight_kg"], 1),
                              "est_1rm": round(_epley_1rm(s["weight_kg"], s["reps"]), 1)})
        out["exercises"] = list(by_ex.values())
        return out

    def exercises(self) -> list[dict[str, Any]]:
        """Every exercise Jake has logged sets for, with a quick summary — the
        picker for the per-exercise explorer."""
        self._db.row_factory = sqlite3.Row
        rows = self._db.execute(
            """SELECT s.exercise_name AS name,
                      COUNT(DISTINCT s.training_id) AS sessions,
                      COUNT(*) AS total_sets,
                      MAX(s.weight_kg) AS max_weight,
                      MAX(ss.start_ts) AS last_ts
               FROM sets s JOIN sessions ss ON ss.training_id = s.training_id
               GROUP BY s.exercise_name ORDER BY last_ts DESC""").fetchall()
        return [{"name": r["name"], "sessions": r["sessions"], "total_sets": r["total_sets"],
                 "max_weight": round(r["max_weight"] or 0, 1),
                 "last_date": _fmt_date(r["last_ts"])} for r in rows]

    def exercise_history(self, name: str) -> dict[str, Any]:
        """Per-session progression for one exercise, oldest→newest, ready to plot:
        top-set weight, total volume, best estimated 1RM, and total reps."""
        self._db.row_factory = sqlite3.Row
        rows = self._db.execute(
            """SELECT s.training_id, s.reps, s.weight_kg, ss.start_ts, ss.title
               FROM sets s JOIN sessions ss ON ss.training_id = s.training_id
               WHERE lower(s.exercise_name)=lower(?)
               ORDER BY ss.start_ts""", (name,)).fetchall()
        by_session: dict[str, dict[str, Any]] = {}
        for r in rows:
            tid = r["training_id"]
            g = by_session.setdefault(tid, {
                "training_id": tid, "date": _fmt_date(r["start_ts"]),
                "start_ts": r["start_ts"], "title": r["title"],
                "top_weight": 0.0, "volume": 0.0, "best_1rm": 0.0, "reps": 0, "sets": 0})
            g["top_weight"] = max(g["top_weight"], r["weight_kg"] or 0)
            g["volume"] += (r["reps"] or 0) * (r["weight_kg"] or 0)
            g["best_1rm"] = max(g["best_1rm"], _epley_1rm(r["weight_kg"] or 0, r["reps"] or 0))
            g["reps"] += r["reps"] or 0
            g["sets"] += 1
        points = sorted(by_session.values(), key=lambda p: p.get("start_ts") or 0)
        for p in points:
            p["top_weight"] = round(p["top_weight"], 1)
            p["volume"] = round(p["volume"], 1)
            p["best_1rm"] = round(p["best_1rm"], 1)
        pr = max((p["top_weight"] for p in points), default=0)
        return {"exercise": name, "points": points, "pr_weight": pr,
                "sessions": len(points)}

    def stats(self, days: int = 90) -> dict[str, Any]:
        """Headline metrics + a per-session volume series + muscle distribution
        over the last `days`. Drives the Workouts overview."""
        cutoff = time.time() - max(1, int(days)) * 86400
        self._db.row_factory = sqlite3.Row
        rows = self._db.execute(
            "SELECT * FROM sessions WHERE start_ts >= ? ORDER BY start_ts",
            (cutoff,)).fetchall()
        sessions = [self._session_row(r) for r in rows]
        total_vol = sum(s["total_volume"] for s in sessions)
        total_cal = sum(s["calories"] for s in sessions)
        total_dur = sum(s["duration_s"] for s in sessions)
        muscle_totals: dict[str, int] = {}
        for s in sessions:
            for m, c in (s["muscles"] or {}).items():
                muscle_totals[m] = muscle_totals.get(m, 0) + int(c or 0)
        series = [{"date": s["date"], "start_ts": s["start_ts"], "title": s["title"],
                   "volume": round(s["total_volume"], 1), "calories": round(s["calories"]),
                   "duration_min": round((s["duration_s"] or 0) / 60)}
                  for s in sessions]
        muscles = sorted(({"name": k, "sets": v} for k, v in muscle_totals.items()),
                         key=lambda m: m["sets"], reverse=True)
        return {
            "window_days": days,
            "sessions": len(sessions),
            "total_volume": round(total_vol),
            "total_calories": round(total_cal),
            "total_duration_min": round(total_dur / 60),
            "avg_volume": round(total_vol / len(sessions)) if sessions else 0,
            "series": series,
            "muscles": muscles,
            "last_synced": self.last_synced(),
        }


def _fmt_date(ts: Optional[float]) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return ""
