"""Proactivity — Continuum speaking up on its own, on the heartbeat.

Each beat the heartbeat asks this engine "anything worth saying right now?" The
engine is deliberately reluctant: it only actually evaluates on a slow cadence,
respects quiet hours and a cooldown, de-dupes against what it just said, and the
whole thing is off unless you turn it on. When it does decide to speak, its ONLY
basis (no inventing) is the live state, what it knows about you, your schedule,
your tracked life-streams (nutrition/body/training), and the recent conversation
it must not barge into — and most of the time the right answer is to stay silent.

A surfaced thought is delivered three ways: a desktop notification, an assistant
message appended to your main thread, and a `proactive.surfaced` event on the bus.

This is the seam for all "notices things and reaches out" behavior — today it's
one open-ended model check; deterministic routines (battery low, long focus,
calendar) drop in beside it the same way.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .config import Config
from .events import Event
from .notify import notify
from .significance import Significance

log = logging.getLogger("continuum.proactive")

_PROMPT = """It is {time}. You are Jake's assistant. Decide whether to send him ONE brief, unprompted message right now — the way a thoughtful human assistant would, only when it genuinely helps.

Context (use ONLY this, invent nothing):
- Right now: {state}
- His day so far: {streams}
- About Jake: {facts}
- Scheduled: {schedule}
- Recently in your conversation: {conversation}
- Changed since you last looked: {changes}

That last line is *why you are being asked now*. Lead your judgement with it. If it says nothing changed, this is only a periodic check-in and silence is almost always the right answer.

Good reasons to speak: a gentle well-timed nudge (e.g. a long unbroken work session), something time-sensitive coming up, or a genuinely useful observation grounded in the context above — his logged food, training, steps or body are fair game if there's something real worth noting, not only his routine.
Do not: invent anything, state the obvious, nag, repeat yourself, or re-open something already covered in the recent conversation above. If he's clearly mid-conversation with you, don't cut across it. When in doubt, stay silent.

If nothing clears that bar, reply with exactly: NOTHING
Otherwise reply with ONE warm sentence (max 25 words), nothing else.

Example — if Jake had had one app focused for hours: "You've been heads-down in VS Code for over two hours — might be worth a stretch."""


_ROUTINE_PROMPT = """It is {time}. You are Veto, Jake's assistant. These steps in his routine are past their time and he hasn't done or skipped them yet: {overdue}. {done}
Write ONE short, warm, natural nudge (max 25 words) reminding him, and offer to mark any as skipped if he's not doing them. Vary the wording — don't sound like a form. Reply with only the message."""


_LIVE_WINDOW_S = 240.0  # Jake messaged within this → mid-conversation, hold nudges


def _tokens(s: str) -> set[str]:
    return {w for w in "".join(c if c.isalnum() else " " for c in s.lower()).split() if len(w) > 3}


class ProactiveEngine:
    def __init__(self, cfg: Config, state: Any = None, memory: Any = None,
                 scheduler: Any = None, conversations: Any = None,
                 routines: Any = None, nutrition: Any = None,
                 body: Any = None, workouts: Any = None, phone: Any = None) -> None:
        self._cfg = cfg
        self._state = state
        self._phone = phone
        self._routines = routines
        self._memory = memory
        self._scheduler = scheduler
        self._conversations = conversations
        self._nutrition = nutrition
        self._body = body
        self._workouts = workouts
        # The cloud provider serves two jobs: phrasing routine nudges, and (by
        # default) the open-ended "is anything worth saying?" judgement. Build it
        # once here so both paths share the one instance.
        from .providers import provider_for, tier_for
        self._cloud_tier = cfg.cloud
        self._cloud_provider = provider_for(cfg, self._cloud_tier)
        # Which rung makes the general judgement gets its OWN setting (config
        # default "cloud"), decoupled from reasoner_tier. This is the single
        # hardest call in the system — decide whether to interrupt, and say
        # something worth reading — and the significance gate already keeps it
        # rare, so the cost is controlled.
        #
        # There is no longer a fallback rung. The old code dropped to the local
        # model when the cloud was unreachable so a missing CLI never muted
        # proactivity; with Ollama gone there is nothing to drop to, and an
        # unreachable cloud means proactivity is simply off until it is back.
        # `maybe_check` already treats a raising provider as "say nothing".
        self._tier = tier_for(cfg, getattr(cfg, "proactive_tier", "cloud"))
        self._provider = provider_for(cfg, self._tier)
        self._enabled = bool(cfg.proactive_enabled)
        # Three different clocks, easy to confuse:
        #   _interval — how often we may *spend a cloud call* at the earliest
        #   _floor    — how long we may go without spending one at all
        #   _cooldown — how long after actually *speaking* before speaking again
        self._interval = cfg.proactive_interval_s
        self._floor = float(getattr(cfg, "proactive_floor_s", 1800.0))
        self._cooldown = cfg.proactive_cooldown_s
        self._gate = Significance(
            float(getattr(cfg, "proactive_significance", 1.0)),
            state=state, routines=routines, scheduler=scheduler,
            nutrition=nutrition, body=body, workouts=workouts)
        self._snapshot: Optional[dict] = None  # baseline; committed only on a real call
        self._last_cloud = 0.0                 # last time we actually spent a call
        self._quiet = (23, 8)  # general proactivity silent 23:00–08:00
        self._last_check = 0.0
        self._last_surface = 0.0
        self._recent: deque[set[str]] = deque(maxlen=8)  # token-sets of recent messages
        # Routine-miss reminders — run independently of general proactivity, with
        # their own earlier-morning quiet window so a 7am step can nudge on time.
        self._routine_reminders = bool(getattr(cfg, "routine_reminders", True))
        self._routine_grace = float(getattr(cfg, "routine_grace_min", 45)) * 60.0
        self._routine_quiet = (float(getattr(cfg, "routine_quiet_start", 23)),
                               float(getattr(cfg, "routine_quiet_end", 6.5)))
        self._routine_cooldown = 20 * 60.0     # min gap between routine nudges
        self._last_routine_surface = 0.0
        self._reminded: set[int] = set()       # routine ids nudged about today
        self._reminded_date = ""
        # Auto-start the routine a set delay after Jake's first device touch of the
        # day. Deterministic (no model), runs beside the routine reminders. Three
        # bits of per-day state, all persisted so a restart can't re-fire it:
        #   _wake_ts       — when we first saw him touch a device today (0 = not yet)
        #   _wake_date     — which day that touch belongs to (resets the wake clock)
        #   _autostart_date— the day we already started/decided (never twice a day)
        self._autostart = bool(getattr(cfg, "routine_autostart", True))
        self._autostart_delay = float(getattr(cfg, "routine_autostart_delay_min", 15)) * 60.0
        self._wake_ts = 0.0
        self._wake_date = ""
        self._autostart_date = ""
        # "Already said that" has to outlive the process. Continuum restarts to
        # apply its own code changes, and in-memory dedup meant every restart
        # re-nudged Jake about the same overdue steps seconds after coming back.
        self._state_path = Path(cfg.routines_db).with_name("proactive.json")
        self._load_state()
        # Routine nudges are phrased by the cloud model (natural, judgment-tuned),
        # falling back to a deterministic template if the cloud tier is unreachable.
        # The provider itself is built once, above, and shared with the general path.
        self._routine_reminder_cloud = bool(getattr(cfg, "routine_reminder_cloud", True))

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, on: bool) -> None:
        self._enabled = bool(on)

    # ---- durable "what I've already said" ----------------------------------
    def _load_state(self) -> None:
        try:
            d = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception:
            return  # missing or corrupt: start quiet-but-fresh, never crash boot
        self._reminded_date = str(d.get("reminded_date") or "")
        self._reminded = {int(i) for i in d.get("reminded") or []}
        self._last_routine_surface = float(d.get("last_routine_surface") or 0.0)
        self._last_surface = float(d.get("last_surface") or 0.0)
        self._wake_ts = float(d.get("wake_ts") or 0.0)
        self._wake_date = str(d.get("wake_date") or "")
        self._autostart_date = str(d.get("autostart_date") or "")
        # Without this a restart looks like "never called the cloud", so the
        # floor fires immediately — and Continuum restarts itself to apply its
        # own edits, which would turn a dev session into a burst of Opus calls.
        self._last_cloud = float(d.get("last_cloud") or 0.0)
        # And the dedup deque, for exactly the same reason — it was the thing
        # this file was written for, and it was the one thing not being written.
        # `dev_restart` is routine here, so an in-memory-only `_recent` meant the
        # next general check could re-surface a near-identical line seconds after
        # coming back. JSON has neither a set nor a deque, so it round-trips as a
        # list of lists; rebuild the exact shape `_is_dup` intersects against
        # (set[str]) and keep the original maxlen or the deque grows unbounded.
        # `recent` is absent from every state file written before this, hence the
        # `or []` — a legacy file must still load.
        try:
            # `isinstance` rather than bare iteration because a string is iterable
            # too: a garbled value would otherwise restore as a deque of
            # single-character "token sets" — harmless to `_is_dup`, which would
            # never match one, but junk that then gets written back out forever.
            recent = [{str(w) for w in toks}
                      for toks in (d.get("recent") or []) if isinstance(toks, list)]
        except Exception:
            log.debug("restoring proactive dedup history failed", exc_info=True)
            recent = []  # garbled key: start fresh rather than refuse to boot
        self._recent = deque(recent, maxlen=self._recent.maxlen)

    def _save_state(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps({
                "reminded_date": self._reminded_date,
                "reminded": sorted(self._reminded),
                "last_routine_surface": self._last_routine_surface,
                "last_surface": self._last_surface,
                "last_cloud": self._last_cloud,
                "wake_ts": self._wake_ts,
                "wake_date": self._wake_date,
                "autostart_date": self._autostart_date,
                # Sorted lists because JSON has no set type; order inside a
                # token-set is meaningless to `_is_dup` (it only intersects),
                # sorting just keeps the file diffable. Deque order *is*
                # meaningful-ish — oldest first — and list() preserves it.
                "recent": [sorted(toks) for toks in self._recent],
            }), encoding="utf-8")
        except Exception:
            log.debug("could not persist proactive state", exc_info=True)

    def _quiet_now(self, now: float, window: Optional[tuple] = None) -> bool:
        dt = datetime.fromtimestamp(now)
        h = dt.hour + dt.minute / 60.0  # fractional hour, so 6.5 == 06:30
        lo, hi = window or self._quiet
        # Both defaults wrap midnight — 23..08 (general), 23..06:30 (routine) —
        # and "after lo OR before hi" is the right test for a wrap. It is also
        # *unconditionally true* for a window that doesn't wrap: set
        # CONTINUUM_ROUTINE_QUIET_START=9 / _END=17 and every hour of the day
        # reads as quiet, so routine reminders and general proactivity go silent
        # forever, `_autostart_check` bails every beat, and nothing raises to say
        # why. So branch on the shape of the window instead of assuming a wrap.
        if lo > hi:
            return h >= lo or h < hi
        if lo == hi:
            # Zero-width: read it as an empty window, i.e. never quiet. Both
            # readings are defensible, but the failure modes are wildly
            # asymmetric — "never shuts up" is obvious within a day and the fix
            # is obvious with it, whereas "never speaks again" is indistinguishable
            # from Continuum being broken and offers no clue where to look.
            return False
        return lo <= h < hi  # plain daytime window, e.g. 09..17

    # ---- heartbeat entrypoint ---------------------------------------------
    async def maybe_check(self, bus: Any) -> Optional[str]:
        """Called every heartbeat. Looking is free; asking the cloud is not."""
        now = time.time()
        # Auto-start the routine a set delay after Jake's first touch of the day.
        # Runs before the away/live-conversation guards below: a phone-only wake
        # (PC still shows "away") must still count, and the delayed start must
        # fire whether or not he's at the desk. It's deterministic and cheap.
        try:
            await self._autostart_check(bus, now)
        except Exception:
            log.exception("routine auto-start check failed")
        if self._is_away():
            # Never call while he's away — nobody is there to read the answer.
            # But mark the baseline so the *return* is a rising edge the gate can
            # see. Only the presence field: leaving the rest of the old baseline
            # intact means drift that happened while he was away (mail piling up,
            # a step going overdue) still scores when he sits back down.
            if self._snapshot is not None:
                self._snapshot["presence"] = "away"
            return None
        # Don't barge into an open exchange. If Jake messaged in the last few
        # minutes we're mid-conversation, and a timer-driven nudge landing now
        # reads as a non-sequitur (the "log dinner?" line dropped into a thread
        # about something else). Hold BOTH paths — overdue steps aren't marked as
        # reminded, so they still nudge once things go quiet, or get folded into a
        # reply naturally before then. Manual check_now() bypasses this: Jake
        # asking directly is not barging in.
        tail = self._thread_tail()
        if self._conversation_live(now, tail):
            return None
        # Routine-miss reminders fire on their own — independent of the general
        # (opt-in) proactive LLM chatter, and on an earlier-morning quiet window.
        if (self._routine_reminders and self._routines
                and not self._quiet_now(now, self._routine_quiet)):
            hit = await self._routine_reminder(bus, now)
            if hit:
                return hit
        # General proactivity: opt-in, standard quiet hours, longer cooldown.
        if not (self._enabled and not self._quiet_now(now)
                and now - self._last_surface >= self._cooldown):
            return None

        # The gate. Snapshotting is local and cheap, so it runs every beat —
        # that's what makes a change detectable within 15s instead of 5 minutes.
        # Cheap is not the same as non-blocking, though: a snapshot is half a
        # dozen sqlite reads and ctypes probes, and this runs on the event loop
        # every ~15s. One concurrent writer holding the sqlite lock would stall
        # every WebSocket client and the phone link for the busy timeout, on the
        # heartbeat. So it goes to a worker thread; `compare` is pure dict diffing
        # and stays here.
        try:
            cur = await asyncio.to_thread(self._gate.snapshot, self._routine_grace / 60.0)
            score, why = self._gate.compare(self._snapshot, cur)
        except Exception:                       # a broken gate must not mute
            log.exception("significance gate failed — falling through to the floor")
            cur, score, why = None, 0.0, []

        due = now - self._last_cloud >= self._floor
        significant = self._gate.is_significant(score) and now - self._last_check >= self._interval
        if not (significant or due):
            return None

        # Only now does the snapshot become the baseline. Committing on every
        # look would let a slow drift accumulate unseen, each step too small to
        # score — the classic way a change detector goes quiet forever.
        if cur is not None:
            self._snapshot = cur
        self._last_check = now
        self._last_cloud = now
        self._save_state()
        log.info("proactive check: %s (score %.2f) — %s",
                 "significant" if significant else "periodic floor", score,
                 "; ".join(why) or "nothing changed")
        return await self._run(bus, now, why, tail)

    def _is_away(self) -> bool:
        if not self._state:
            return False
        try:
            return (self._state.basic().get("presence") or {}).get("state") == "away"
        except Exception:
            return False

    # ---- conversation awareness -------------------------------------------
    def _thread_tail(self, n: int = 6) -> list:
        """The last few turns of the main thread — the exchange any nudge must
        read before speaking into. [] on any failure: a nudge missing context is
        only worse in tone, never a reason to crash the heartbeat."""
        if not self._conversations:
            return []
        try:
            return self._conversations.recent_turns(self._conversations.main(), n)
        except Exception:
            log.debug("reading main thread for proactive context failed", exc_info=True)
            return []

    def _conversation_live(self, now: float, tail: Optional[list] = None) -> bool:
        """Is Jake mid-exchange right now? True if his last message landed within
        the live window, in which case an unprompted nudge would barge in."""
        tail = self._thread_tail() if tail is None else tail
        for m in reversed(tail):
            if getattr(m, "role", "") == "user":
                return (now - float(getattr(m, "ts", 0.0))) < _LIVE_WINDOW_S
        return False

    def _conversation_text(self, tail: Optional[list] = None, n: int = 6) -> str:
        """The tail of the main thread, so the model speaks *into* the ongoing
        conversation instead of ignoring it — and doesn't re-open what was just
        discussed. Only runs on an actual check, so the read cost is negligible."""
        tail = self._thread_tail(n) if tail is None else tail
        out = []
        for m in tail:
            role = "Jake" if getattr(m, "role", "") == "user" else "You"
            text = (getattr(m, "content", "") or "").strip().replace("\n", " ")
            if text:
                out.append(f"{role}: {text[:200]}")
        return "\n".join(out) if out else "(nothing recent)"

    # ---- life-stream awareness --------------------------------------------
    def _streams_text(self, now: Optional[float] = None) -> str:
        """A compact read of Jake's tracked life — food, body, training — so the
        judgement has substance beyond the desk and the routine (the fix for
        'every proactive message is about missed routine'). Every probe guarded;
        a missing or empty stream just drops its line."""
        now = now if now is not None else time.time()
        today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        lines: list[str] = []
        if self._nutrition:
            try:
                d = self._nutrition.day(today)
                t = d.get("totals") or {}
                if d.get("count"):
                    g = d.get("goals") or {}
                    goal = (f" of {int(g['calories'])} goal"
                            if g.get("set") and g.get("calories") else "")
                    lines.append(
                        f"nutrition: {d['count']} item(s), {int(t.get('calories') or 0)} kcal{goal} "
                        f"(P{int(t.get('protein_g') or 0)} C{int(t.get('carbs_g') or 0)} "
                        f"F{int(t.get('fat_g') or 0)})")
                else:
                    lines.append("nutrition: nothing logged yet today")
            except Exception:
                log.debug("nutrition stream read failed", exc_info=True)
        if self._body:
            try:
                b = self._body.day(today)
                bits = []
                if b.get("steps"):
                    bits.append(f"{b['steps']} steps (~{b['steps_kcal']} kcal)")
                if b.get("weight_kg"):
                    bits.append(f"weighed {b['weight_kg']}kg")
                if bits:
                    lines.append("body: " + ", ".join(bits))
            except Exception:
                log.debug("body stream read failed", exc_info=True)
        if self._workouts:
            try:
                recent = self._workouts.recent(3)
                today_w = [s for s in recent if s.get("date") == today]
                if today_w:
                    lines.append(f"training: worked out today — {today_w[0].get('title') or 'session'}")
                elif recent:
                    lines.append(f"training: last workout {recent[0].get('date') or '?'} "
                                 f"— {recent[0].get('title') or 'session'}")
            except Exception:
                log.debug("workout stream read failed", exc_info=True)
        return "; ".join(lines) if lines else "(no tracked activity today)"

    # ---- routine auto-start ------------------------------------------------
    def _awake_now(self) -> bool:
        """Has Jake touched a device? Desktop recent input, or phone screen on.
        Both probes guarded — a missing signal just reads as 'not yet', never
        crashes the heartbeat."""
        if self._state:
            try:
                pres = self._state.basic().get("presence") or {}
                if pres.get("state") == "active":
                    return True
            except Exception:
                log.debug("desktop presence read for auto-start failed", exc_info=True)
        if self._phone:
            try:
                scr = self._phone.get("screen")
                if scr and scr.get("on") and not scr.get("stale"):
                    return True
            except Exception:
                log.debug("phone screen read for auto-start failed", exc_info=True)
        return False

    async def _autostart_check(self, bus: Any, now: float) -> None:
        """Start the day's routine once, a set delay after Jake's first device
        touch — so his anchored morning gets concrete times without him pressing
        'start'. Idempotent per day and persisted, so a restart never re-fires."""
        if not (self._autostart and self._routines):
            return
        today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        # A new day resets the wake clock. (autostart_date carries yesterday's
        # value until we act today, which is exactly what blocks a re-fire.)
        if self._wake_date != today:
            self._wake_date = today
            self._wake_ts = 0.0
            self._save_state()
        if self._autostart_date == today:
            return  # already started or stood down today
        # A 3am phone glance isn't "waking up" — ignore touches during the
        # routine quiet window, so the earliest wake is its morning boundary.
        if self._quiet_now(now, self._routine_quiet):
            return
        # Record the first touch of the day.
        if not self._wake_ts:
            if not self._awake_now():
                return
            self._wake_ts = now
            self._save_state()
            log.info("routine auto-start: first touch %s, starting in %dm",
                     datetime.fromtimestamp(now).strftime("%H:%M"),
                     int(self._autostart_delay // 60))
            return
        # Still waiting out the delay.
        if now - self._wake_ts < self._autostart_delay:
            return
        # Time to act — mark handled first so a crash can't double-start.
        self._autostart_date = today
        self._save_state()
        # Respect a manual start: if the day's already anchored, stand down.
        if self._routines._day_anchor(today) is not None:
            log.info("routine auto-start: day already started manually — standing down")
            return
        try:
            self._routines.start_routine()
        except Exception:
            log.exception("routine auto-start: start_routine failed")
            return
        mins = int(self._autostart_delay // 60)
        msg = (f"Looks like you're up — I've started your routine, anchored to about "
               f"{mins} minutes ago. Tell me if you were up earlier and I'll shift it.")
        await self._surface(msg, bus, now)
        log.info("routine auto-start: started the day's routine")

    async def _routine_reminder(self, bus: Any, now: float) -> Optional[str]:
        """Nudge about routine steps whose time has passed and aren't done/skipped.
        Each step is reminded at most once per day; batched, on a short cooldown."""
        try:
            overdue = self._routines.overdue(self._routine_grace / 60.0)
        except Exception:
            log.exception("routine overdue check failed")
            return None
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._reminded_date:
            self._reminded.clear()
            self._reminded_date = today
        fresh = [it for it in overdue if it["id"] not in self._reminded]
        if not fresh or (now - self._last_routine_surface < self._routine_cooldown):
            return None
        for it in fresh:
            self._reminded.add(it["id"])
        self._last_routine_surface = now
        self._save_state()  # before phrasing: a crash mid-nudge must not re-nudge
        msg = await self._phrase_reminder(fresh, now)
        await self._surface(msg, bus, now)
        return msg

    async def _phrase_reminder(self, fresh: list, now: float) -> str:
        """Cloud model writes the nudge (natural, varied); template is the fallback."""
        listed = ", ".join(f"{it['title']} ({it['time']})" for it in fresh)
        template = (f"Routine nudge — still not done: {listed}. "
                    "Want to knock them out, or should I mark any as skipped?")
        if not self._routine_reminder_cloud or not self._cfg.cloud_available:
            return template
        done_ctx = ""
        try:
            done = [it["title"] for it in self._routines.today()["items"] if it["done"]]
            if done:
                done_ctx = f"He's already done: {', '.join(done)}."
        except Exception:
            # Deliberately non-fatal — a nudge without the "already done" line is
            # still worth sending. But it was also *silent*, and this is exactly
            # the context that stops the cloud model nudging Jake about steps he
            # has already ticked off. Losing it degrades the message in a way you
            # only notice by reading the nudge, so it gets a real warning rather
            # than a debug line nobody has enabled.
            log.warning("could not read today's completed steps for nudge context "
                        "— the nudge may re-raise something already done", exc_info=True)
        prompt = _ROUTINE_PROMPT.format(
            time=datetime.fromtimestamp(now).strftime("%H:%M"), overdue=listed, done=done_ctx)
        try:
            raw = (await self._cloud_provider.generate(self._cloud_tier, prompt)).strip()
            line = raw.split("\n")[0].strip()[:240]
            return line or template
        except Exception:
            log.exception("cloud reminder phrasing failed — using template")
            return template

    async def check_now(self, bus: Any) -> Optional[str]:
        """Force a check regardless of cadence/cooldown (still respects enabled).
        Useful as a manual 'what's on your mind?' nudge. Bypasses the
        significance gate by design — Jake asking *is* the significance — but
        still re-baselines, so an unprompted check doesn't immediately follow."""
        # "Still respects enabled" is what the docstring has always promised and
        # what the body never did: nothing here read `_enabled`, so the UI's nudge
        # button spent an Opus call, surfaced a message, fired a toast and stamped
        # `_last_cloud` with proactivity toggled off — the one switch whose entire
        # job is "don't speak unless spoken to". None is the same answer the caller
        # already gets for "nothing worth saying", so server.py needs no change.
        if not self._enabled:
            log.info("manual proactive check ignored — proactivity is switched off")
            return None
        now = time.time()
        try:
            # Same reasoning as the heartbeat path: this is half a dozen blocking
            # store reads and we are on the event loop.
            self._snapshot = await asyncio.to_thread(
                self._gate.snapshot, self._routine_grace / 60.0)
        except Exception:
            log.debug("snapshot during manual check failed", exc_info=True)
        self._last_cloud = now
        self._save_state()
        return await self._run(bus, now, ["Jake asked directly for a check-in"])

    # ---- the check --------------------------------------------------------
    async def _run(self, bus: Any, now: float, why: Optional[list] = None,
                   tail: Optional[list] = None) -> Optional[str]:
        # The only time Jake doesn't want a nudge is when he's away from the PC.
        if self._state:
            try:
                if (self._state.basic().get("presence") or {}).get("state") == "away":
                    return None
            except Exception:
                # Falling through on a failed probe is the right default — better
                # to risk a nudge he doesn't see than to mute on a flaky read —
                # but it should not happen invisibly. `_run` only executes on a
                # check we were about to spend a cloud call on, so this is at
                # worst one line per call, not one per beat.
                log.warning("presence read before proactive run failed "
                            "— assuming he's at the desk", exc_info=True)
        try:
            msg = await self._evaluate(why, tail)
        except Exception:
            log.exception("proactive evaluate failed")
            return None
        if not msg or self._is_dup(msg):
            return None
        await self._surface(msg, bus, now)
        return msg

    def _prompt_context(self, tail: Optional[list] = None) -> dict[str, str]:
        """Every part of the prompt that costs a blocking read, gathered in one
        synchronous place so the async caller can push the whole lot onto a worker
        thread in a single hop instead of hitting sqlite four separate times on
        the event loop. Nothing is guarded here that wasn't guarded before —
        `_run` already wraps `_evaluate` in a try/except, and `to_thread`
        re-raises into the awaiting coroutine, so the behaviour on failure is
        unchanged."""
        state_txt = self._state.summary_text() if self._state else "(no state)"
        facts = "(none)"
        if self._memory:
            mems = self._memory.recent(8, kind="fact") + self._memory.recent(4, kind="preference")
            facts = "\n".join(f"- {m.text}" for m in mems) or "(none)"
        sched = "(none)"
        if self._scheduler:
            jobs = self._scheduler.list()[:5]
            sched = "\n".join(
                f"- {j.title} in {int(j.next_fire - time.time()) // 60} min" for j in jobs) or "(none)"
        return {"state": state_txt, "facts": facts, "schedule": sched,
                "streams": self._streams_text(),
                "conversation": self._conversation_text(tail)}

    async def _evaluate(self, why: Optional[list] = None,
                        tail: Optional[list] = None) -> Optional[str]:
        # Memory, scheduler, the life-streams and the state summary are all
        # blocking store reads, and this coroutine runs on the event loop. The
        # thread hop is free relative to what comes next — we are about to block
        # on a cloud generate anyway — and it keeps a sqlite lock held by a
        # concurrent write from stalling every WebSocket client meanwhile.
        ctx = await asyncio.to_thread(self._prompt_context, tail)
        when = datetime.now().strftime("%H:%M %a %d %b")
        changes = "; ".join(why) if why else "nothing notable — this is a periodic check-in"
        prompt = _PROMPT.format(time=when, changes=changes, **ctx)
        raw = (await self._provider.generate(self._tier, prompt)).strip()
        if not raw or raw.upper().startswith("NOTHING") or len(raw) < 6:
            return None
        # guard against the model echoing the instruction / being too long
        return raw.split("\n")[0].strip()[:240]

    def _is_dup(self, msg: str) -> bool:
        toks = _tokens(msg)
        if not toks:
            return False
        for prev in self._recent:
            overlap = len(toks & prev) / max(1, len(toks))
            if overlap > 0.6:
                return True
        return False

    async def _surface(self, msg: str, bus: Any, now: float) -> None:
        self._last_surface = now
        self._recent.append(_tokens(msg))
        self._save_state()
        notify("Continuum", msg)
        conv_id = None
        if self._conversations:
            conv_id = self._conversations.main()
            self._conversations.append(conv_id, "assistant", msg, {"proactive": True})
        await bus.publish(Event(topic="proactive.surfaced", source="proactive",
                                payload={"message": msg, "conversation_id": conv_id}))
        log.info("proactive: %s", msg)
