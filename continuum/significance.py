"""Has anything actually changed? — the cheap gate in front of an expensive model.

Proactivity used to call the cloud tier on a fixed cadence whether or not
anything had moved: with `CONTINUUM_PROACTIVE_SECONDS=300` that is ~288 Opus
calls a day, most of them looking at an unchanged desk and correctly answering
NOTHING. The model was being asked "is this worth saying?" when the cheaper
question — "is this even different?" — hadn't been asked yet.

This module asks it, locally and for free. It reads the same signals the
proactive prompt reads, diffs them against the previous snapshot, and returns a
score plus the reasons behind it.

It is deliberately not a model. Two properties matter more than cleverness:

  * **It never *fails*.** Every probe is individually guarded — a missing store
    or a raising tracker costs one signal, not the check. Note what that does
    and does not promise: this used to read "it never blocks", which was read as
    a license to call `snapshot()` straight from the heartbeat coroutine. It is
    not one. Guarded means no probe *raises*; every one of them still blocks, on
    sqlite or on a ctypes probe, and a concurrent writer holding the sqlite lock
    blocks it for the busy timeout. `snapshot()` must be called off the event
    loop — see `asyncio.to_thread` at its call sites in `proactive.py`.
  * **The reasons are the point.** They go *into* the prompt. Telling the model
    "he just came back after 40m away and standup is in 12 minutes" is most of
    what makes the judgement good; without it the model re-derives the world
    from scratch every time and has no idea what is new.

Rising edges, not levels. A signal fires on the *transition* — battery crossing
below 25%, focus crossing the 2-hour mark — so a state that stays true doesn't
re-fire every beat and burn the budget it was added to protect.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

log = logging.getLogger("continuum.significance")

# Focus bands in seconds. Crossing one is a rising edge; sitting inside one is
# not. 50 minutes, 1.5 hours, 2.5 hours — roughly "a while", "a long time",
# "you should probably stand up".
_FOCUS_BANDS = (3000.0, 5400.0, 9000.0)
_BATTERY_BANDS = (25, 15, 8)      # falling edges only
_STEP_BANDS = (5000.0, 10000.0, 15000.0)  # everyday-movement milestones
_SOON_S = 900.0                   # a scheduled job entering this window is news
_UNREAD_JUMP = 3                  # smaller bumps are noise


def _band(value: float, bands: tuple) -> int:
    """Index of the highest ascending band `value` has reached."""
    n = 0
    for b in bands:
        if value >= b:
            n += 1
    return n


def _falling_band(value: float, bands: tuple) -> int:
    """Index for descending thresholds — battery falls *through* 25/15/8."""
    n = 0
    for b in bands:
        if value <= b:
            n += 1
    return n


class Significance:
    """Scores how much Jake's world has moved since the last cloud evaluation.

    `snapshot()` is pure observation; `compare()` is pure diff. The caller owns
    when to commit a new baseline — deliberately, because a snapshot should only
    become the baseline when the cloud call it justified actually happened.
    Committing on every look would let a slow drift accumulate unnoticed, each
    individual step too small to score.
    """

    # weight, and whether it alone should be enough to justify a call
    WEIGHTS = {
        "routine_overdue":  1.2,   # a missed step is the whole point of nudging
        "job_soon":         1.1,   # something scheduled is about to land
        "battery":          1.1,
        "workout_done":     1.1,   # a training session landed — worth speaking to on its own
        "presence":         1.0,   # coming *back* — enough on its own
        "focus_band":       1.0,   # crossed into a longer unbroken session
        "unread":           0.8,
        "steps_band":       0.8,   # crossed a step milestone (5k/10k/15k)
        "nutrition_logged": 0.6,   # food logged — enriches more than it triggers (no nudge per snack)
        "routine_done":     0.5,
        "app":              0.4,
        "browser":          0.3,
        "media":            0.2,
    }

    def __init__(self, threshold: float = 1.0, *, state: Any = None,
                 routines: Any = None, scheduler: Any = None,
                 nutrition: Any = None, body: Any = None,
                 workouts: Any = None) -> None:
        self._threshold = float(threshold)
        self._state = state
        self._routines = routines
        self._scheduler = scheduler
        self._nutrition = nutrition
        self._body = body
        self._workouts = workouts

    # ---- observation ------------------------------------------------------
    def snapshot(self, grace_min: float = 45.0) -> dict[str, Any]:
        """Salient signals, flattened. Every probe guarded: a snapshot missing
        one key degrades that signal to "unchanged", which is the safe failure —
        it can only ever cost a call we didn't need to make.

        Blocking, despite the guards — six-plus sqlite reads and ctypes probes.
        Call it from a worker thread, never directly from a coroutine; the
        heartbeat drives it every ~15s and would otherwise hand a slow sqlite
        lock the power to stall every connected client."""
        snap: dict[str, Any] = {"at": time.time()}
        b: dict[str, Any] = {}
        if self._state is not None:
            try:
                b = self._state.basic() or {}
            except Exception:
                log.debug("state probe failed", exc_info=True)

        fg = b.get("foreground") or {}
        snap["app"] = fg.get("name") or ""
        snap["title"] = (fg.get("title") or "")[:120]
        try:
            snap["focus_band"] = _band(float(fg.get("focus_seconds") or 0), _FOCUS_BANDS)
        except Exception:
            snap["focus_band"] = 0

        pres = b.get("presence") or {}
        snap["presence"] = pres.get("state") or "active"

        act = (b.get("browser") or {}).get("active") or {}
        snap["browser"] = (act.get("url") or "")[:200]

        media = b.get("media") or {}
        snap["media"] = (media.get("title") or "")[:120]

        try:
            snap["unread"] = sum(int(u.get("count") or 0) for u in (b.get("unread") or []))
        except Exception:
            snap["unread"] = 0

        sysm = b.get("system") or {}
        bat = sysm.get("battery_pct")
        # `on_battery` is the key probe.py actually sets (not `power_plugged`);
        # plugged in, a low percentage is not news.
        snap["battery_band"] = (
            _falling_band(float(bat), _BATTERY_BANDS)
            if bat is not None and sysm.get("on_battery") else 0)
        snap["battery_pct"] = bat

        snap["overdue"] = []
        snap["done"] = 0
        if self._routines is not None:
            try:
                snap["overdue"] = sorted(int(i["id"]) for i in self._routines.overdue(grace_min))
            except Exception:
                log.debug("overdue probe failed", exc_info=True)
            try:
                snap["done"] = sum(1 for i in self._routines.today()["items"] if i.get("done"))
            except Exception:
                log.debug("routine today probe failed", exc_info=True)

        snap["soon"] = []
        if self._scheduler is not None:
            try:
                now = time.time()
                snap["soon"] = sorted(
                    int(j.id) for j in self._scheduler.list()
                    if 0 <= (j.next_fire - now) <= _SOON_S)
            except Exception:
                log.debug("scheduler probe failed", exc_info=True)

        # Life-stream signals: the streams the tools already expose, so an actual
        # observation (food logged, a workout, a step milestone) can *trigger* a
        # check — not just decorate one. Every probe guarded like the rest.
        today = time.strftime("%Y-%m-%d", time.localtime())
        snap["food_count"] = 0
        if self._nutrition is not None:
            try:
                snap["food_count"] = int(self._nutrition.day()["count"])
            except Exception:
                log.debug("nutrition probe failed", exc_info=True)

        snap["steps_count"] = 0
        snap["steps_band"] = 0
        if self._body is not None:
            try:
                st = int(self._body.day()["steps"])
                snap["steps_count"] = st
                snap["steps_band"] = _band(float(st), _STEP_BANDS)
            except Exception:
                log.debug("body probe failed", exc_info=True)

        snap["workout_today"] = 0
        if self._workouts is not None:
            try:
                snap["workout_today"] = sum(
                    1 for s in self._workouts.recent(10) if s.get("date") == today)
            except Exception:
                log.debug("workout probe failed", exc_info=True)
        return snap

    # ---- the diff ---------------------------------------------------------
    def compare(self, prev: Optional[dict], cur: dict) -> tuple[float, list[str]]:
        """Returns (score, reasons). No baseline means nothing to compare
        against — score 0, so a fresh start waits for the floor rather than
        treating the entire world as new."""
        if not prev:
            return 0.0, []
        score = 0.0
        why: list[str] = []

        def hit(key: str, reason: str) -> None:
            nonlocal score
            score += self.WEIGHTS.get(key, 0.0)
            why.append(reason)

        fresh = [i for i in cur.get("overdue", []) if i not in set(prev.get("overdue", []))]
        if fresh:
            hit("routine_overdue", f"{len(fresh)} routine step(s) newly overdue")

        soon = [i for i in cur.get("soon", []) if i not in set(prev.get("soon", []))]
        if soon:
            hit("job_soon", f"{len(soon)} scheduled item(s) now due within 15 minutes")

        if cur.get("battery_band", 0) > prev.get("battery_band", 0):
            hit("battery", f"battery fell to {cur.get('battery_pct')}% on battery")

        # Only the *return* counts. Jake going away is not worth a call — he
        # isn't there to read the answer, and `_is_away()` bails before this
        # anyway. Coming back after a break is one of the best moments to speak.
        if prev.get("presence") == "away" and cur.get("presence") != "away":
            hit("presence", "just came back to the desk")

        d_unread = int(cur.get("unread", 0)) - int(prev.get("unread", 0))
        if d_unread >= _UNREAD_JUMP:
            hit("unread", f"{d_unread} new unread message(s)")

        if cur.get("focus_band", 0) > prev.get("focus_band", 0):
            hit("focus_band", f"unbroken focus on {cur.get('app') or 'one app'} crossed a longer band")

        d_done = int(cur.get("done", 0)) - int(prev.get("done", 0))
        if d_done > 0:
            hit("routine_done", f"{d_done} routine step(s) completed")

        if cur.get("workout_today", 0) > prev.get("workout_today", 0):
            hit("workout_done", "a workout was logged today")

        if cur.get("steps_band", 0) > prev.get("steps_band", 0):
            hit("steps_band", f"stepped past a milestone — {cur.get('steps_count')} steps today")

        d_food = int(cur.get("food_count", 0)) - int(prev.get("food_count", 0))
        if d_food > 0:
            hit("nutrition_logged", f"{d_food} food item(s) logged today")

        if cur.get("app") != prev.get("app"):
            hit("app", f"switched to {cur.get('app') or 'nothing'}")
        if cur.get("browser") != prev.get("browser"):
            hit("browser", "browsing something different")
        if cur.get("media") != prev.get("media"):
            hit("media", f"now playing: {cur.get('media') or 'nothing'}")

        return round(score, 2), why

    def is_significant(self, score: float) -> bool:
        return score >= self._threshold
