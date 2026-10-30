"""The scheduler — time-based jobs, fired by Continuum's heartbeat.

Two job kinds:
  * reminder — at a time (once or recurring), show the user a notification.
  * prompt   — at a time (once or recurring), run a prompt through the agent
               (e.g. "every morning, suggest my focus for the day").

Jobs persist in SQLite so they survive restarts. The heartbeat (see the loop in
the app/supervisor) calls `tick()` on each beat: it fires everything due, then
reschedules recurring jobs and drops spent one-shots. This is the seam where
proactive routines grow — anything Continuum should do "on its own, over time"
becomes a job.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .config import Config
from .events import Event
from .notify import notify

log = logging.getLogger("continuum.scheduler")


@dataclass
class Job:
    id: int
    kind: str            # "reminder" | "prompt"
    schedule: str        # "once" | "every"
    title: str
    next_fire: float
    interval_s: Optional[float]
    payload: dict[str, Any]
    enabled: bool
    created: float
    last_fired: Optional[float]
    fire_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "schedule": self.schedule,
            "title": self.title, "next_fire": self.next_fire,
            "in_seconds": max(0, int(self.next_fire - time.time())),
            "interval_s": self.interval_s, "payload": self.payload,
            "enabled": self.enabled, "fire_count": self.fire_count,
        }


class Scheduler:
    def __init__(self, cfg: Config) -> None:
        self._path = Path(cfg.scheduler_db)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self._path), check_same_thread=False)
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS jobs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT, schedule TEXT, title TEXT,
                next_fire REAL, interval_s REAL, payload TEXT,
                enabled INTEGER DEFAULT 1, created REAL, last_fired REAL,
                fire_count INTEGER DEFAULT 0)"""
        )
        self._db.commit()

    # ---- create ------------------------------------------------------------
    def add(self, kind: str, delay_s: float, payload: dict, *, title: str = "",
            repeat_s: Optional[float] = None) -> int:
        now = time.time()
        schedule = "every" if repeat_s else "once"
        cur = self._db.execute(
            "INSERT INTO jobs(kind,schedule,title,next_fire,interval_s,payload,enabled,created,fire_count) "
            "VALUES(?,?,?,?,?,?,1,?,0)",
            (kind, schedule, title or kind, now + max(0.0, delay_s),
             repeat_s, json.dumps(payload), now),
        )
        self._db.commit()
        jid = int(cur.lastrowid)
        log.info("scheduled #%s %s in %.0fs%s: %s", jid, kind, delay_s,
                 f" every {repeat_s:.0f}s" if repeat_s else "", title or payload)
        return jid

    def schedule_reminder(self, delay_s: float, message: str,
                          repeat_s: Optional[float] = None, title: str = "Reminder") -> int:
        return self.add("reminder", delay_s, {"message": message}, title=title, repeat_s=repeat_s)

    def schedule_prompt(self, delay_s: float, prompt: str, complexity: str = "low",
                        repeat_s: Optional[float] = None, title: str = "") -> int:
        return self.add("prompt", delay_s, {"prompt": prompt, "complexity": complexity},
                        title=title or prompt[:40], repeat_s=repeat_s)

    # ---- read / manage -----------------------------------------------------
    def list(self, include_disabled: bool = False) -> list[Job]:
        q = "SELECT * FROM jobs"
        if not include_disabled:
            q += " WHERE enabled=1"
        q += " ORDER BY next_fire"
        return [self._row(r) for r in self._db.execute(q).fetchall()]

    def cancel(self, job_id: int) -> bool:
        cur = self._db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        self._db.commit()
        return bool(cur.rowcount)

    def count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM jobs WHERE enabled=1").fetchone()[0])

    # ---- heartbeat ---------------------------------------------------------
    async def tick(self, bus: Any) -> int:
        """Fire everything due, reschedule/retire, return how many fired."""
        now = time.time()
        due = [self._row(r) for r in self._db.execute(
            "SELECT * FROM jobs WHERE enabled=1 AND next_fire<=? ORDER BY next_fire", (now,)
        ).fetchall()]
        for job in due:
            try:
                await self._fire(job, bus)
            except Exception:
                log.exception("firing job %s failed", job.id)
            self._reschedule(job, now)
        return len(due)

    async def _fire(self, job: Job, bus: Any) -> None:
        if job.kind == "reminder":
            msg = job.payload.get("message", "")
            notify(job.title or "Reminder", msg)
            await bus.publish(Event(topic="reminder.fired", source="scheduler",
                                    payload={"id": job.id, "message": msg}))
        elif job.kind == "prompt":
            # Runs through the agent like any task → lands in the main thread.
            await bus.publish(Event(topic="task.request", source="scheduler",
                                    payload={"title": job.title or "scheduled",
                                             "prompt": job.payload.get("prompt", ""),
                                             "complexity": job.payload.get("complexity", "low"),
                                             "conversation_id": None}))
        log.info("fired job #%s (%s)", job.id, job.kind)

    def _reschedule(self, job: Job, now: float) -> None:
        if job.schedule == "every" and job.interval_s:
            self._db.execute(
                "UPDATE jobs SET next_fire=?, last_fired=?, fire_count=fire_count+1 WHERE id=?",
                (now + job.interval_s, now, job.id))
        else:
            self._db.execute("DELETE FROM jobs WHERE id=?", (job.id,))  # one-shot spent
        self._db.commit()

    @staticmethod
    def _row(r: tuple) -> Job:
        try:
            payload = json.loads(r[6] or "{}")
        except Exception:
            payload = {}
        return Job(id=r[0], kind=r[1], schedule=r[2], title=r[3], next_fire=r[4],
                   interval_s=r[5], payload=payload, enabled=bool(r[7]),
                   created=r[8], last_fired=r[9], fire_count=r[10])
