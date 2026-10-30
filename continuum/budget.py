"""Budget — a daily allowance for the things that eat the day.

Continuum has always been able to *tell* Jake where his attention went. This is
the first store that does something about it. The premise, in his words: not a
brick wall he can never bypass, but "a system put in place to keep me more in
control". That single sentence decides most of the design below — every hard
edge here is a speed bump with a bell on it, not a lock. He is Administrator on
this machine and wrote the watchdog; pretending otherwise would be theatre.

Three ideas carry it:

1. **Spend is derived, never counted.** The seconds come from `activity.segments`
   — the same rows the Activity tab reads — grouped by a category rule. Nothing
   here keeps its own timer. A second counter would drift from the first within a
   day and then the tab and the budget would disagree about how long he'd played,
   which is exactly the argument he'd use to dismiss the whole feature. It also
   means the presence rule comes along for free: `activity` only accrues desktop
   time while he's actually at the desk, so a game left open over dinner doesn't
   eat the allowance.

2. **Lowering is instant, raising waits for tomorrow.** The Ulysses contract, and
   the single most effective software-only mechanism there is. `set_allowance`
   applies a cut immediately and parks a raise in `pending_minutes` against
   tomorrow's date. It costs nothing to implement and it removes the move that
   actually defeats these systems — not hacking the watchdog, just quietly
   editing the number mid-craving.

3. **The escape hatch is deliberate and logged.** `override()` grants extra
   minutes today, writes an `override` row, and shows up in the day's summary
   with a count. Fighting a watchdog teaches you to defeat watchdogs; spending an
   override you have to name teaches you what you spent. The valve is the point —
   without it he'd disable the whole thing the first time it misfired.

**Grace, and why it lives on disk.** At 100% the running app is not killed. Jake
plays VALORANT, and yanking him out mid-match costs a competitive ban — a tool
that punishes you for using it gets uninstalled inside a week. So depletion
starts a grace window (`grace_until`), he gets a notification, and the close
comes when it expires. That state is a **column, not an attribute**: he restarts
Continuum constantly while developing it, and in-memory grace would reset on
every restart into an unlimited supply of ten-minute extensions, which is both a
bypass and one he'd find by accident rather than on purpose.

Warnings fire at 50/75/90/95% — his numbers. Each fires once per category per
day, tracked in `events`, because the evaluation loop runs every 5 seconds and a
toast every 5 seconds past the halfway mark is a feature you turn off.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from .config import Config

log = logging.getLogger("continuum.budget")

# Jake's thresholds, verbatim. Ascending; each fires once per category per day.
WARN_PCT: tuple[int, ...] = (50, 75, 90, 95)

# Wrap-up time granted when an allowance runs out mid-session. Long enough to
# finish a VALORANT round or hit a save point.
DEFAULT_GRACE_MIN = 10.0

# A budgeted app *launched* after the allowance is already gone gets a much
# shorter window — he isn't mid-anything, he's at a launcher, and the only thing
# to wait for is long enough to read the toast explaining why.
RELAUNCH_GRACE_MIN = 1.0

# The categories Jake asked to bound: "gaming and digital entertainment". Kept
# separate rather than pooled because they fail differently — an evening of
# Elderfield is a choice, forty minutes of Reels is not, and one shared pot would
# let the deliberate one eat the allowance the careless one needed.
CATEGORIES: tuple[str, ...] = ("game", "social", "video")

# Seeded from the real 25 days in activity.db on 2026-09-15, not invented. Match
# on `proc` where there is one (a process name or Android package is stable);
# fall back to `app`, which is what the browser-relabelled rows carry — YouTube
# and Instagram arrive as app names via activity's _SOCIAL_SITES, with proc still
# reading firefox.exe, so a proc rule would never see them.
#
# `game.exe` is Welcome to Elderfield. It is also the single most generic
# executable name a game can ship, so it is the one rule here likely to catch
# something it shouldn't — if a second game ever reports as Game.exe the rows
# merge silently and the budget double-counts. Left in because it is 28h of his
# last 25 days and the alternative is not budgeting his biggest game; flagged so
# the surprise is documented rather than discovered.
_SEED_RULES: tuple[tuple[str, str, str], ...] = (
    # (field, match, category) — match is compared lowercased
    ("proc", "game.exe", "game"),                      # Welcome to Elderfield
    ("proc", "valorant-win64-shipping.exe", "game"),
    ("proc", "ravenswatch.exe", "game"),
    ("proc", "hearthstone.exe", "game"),
    ("proc", "goldenswirl.exe", "game"),
    ("proc", "goldenswirldemo.exe", "game"),           # the demo ships a second binary
    ("proc", "synthetik2.exe", "game"),
    ("proc", "overwatch.exe", "game"),
    ("proc", "windblown.exe", "game"),
    ("proc", "com.blizzard.wtcg.hearthstone", "game"),  # the phone's copy
    # The long tail — five binaries worth ~6h that the first draft of this list
    # missed, because it was written from a top-apps summary rather than from the
    # distinct process names in segments. Small games are exactly what a budget
    # leaks through, so the list was checked against the real table instead:
    ("proc", "peak.exe", "game"),                      # PEAK
    ("proc", "stickfight.exe", "game"),                # Stick Fight: The Game
    ("proc", "breach.exe", "game"),                    # Into the Breach
    ("proc", "blooddungeon.exe", "game"),              # Blood Dungeon
    # Short-form feeds. Instagram is the top app on the phone by a distance — the
    # habit he said he'd cut came back while the attention went to games, which is
    # a decent part of why this file exists.
    #
    # These strings are not guesses: they are the exact labels `_SOCIAL_SITES` in
    # activity.py writes when it relabels a browser segment by the site in front.
    # Four of them (X, Threads, Bluesky, Twitch) match nothing in the last 25 days
    # — deliberately pre-emptive, so a new habit lands inside a budget on day one
    # rather than after somebody notices it. If activity's labels are ever
    # reworded, these go quietly dead; that's the cost of matching on a display
    # name, and it's why everything with a stable process name matches on `proc`.
    ("app", "instagram", "social"),
    ("app", "reddit", "social"),
    ("app", "tiktok", "social"),
    ("app", "x (twitter)", "social"),
    ("app", "threads", "social"),
    ("app", "facebook", "social"),
    ("app", "bluesky", "social"),
    ("app", "youtube", "video"),
    ("app", "twitch", "video"),
)


def _today(now: Optional[float] = None) -> str:
    return datetime.fromtimestamp(now or time.time()).strftime("%Y-%m-%d")


def _tomorrow(now: Optional[float] = None) -> str:
    d = datetime.fromtimestamp(now or time.time()).date() + timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def _hm(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 3600}h{(s % 3600) // 60:02d}m" if s >= 3600 else f"{(s % 3600) // 60}m"


class BudgetStore:
    def __init__(self, cfg: Config) -> None:
        self._path = Path(cfg.budget_db)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self._path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        # One lock for the same reason activity.py has one: the state poll thread
        # and the HTTP handlers share a check_same_thread=False connection.
        #
        # **Reentrant**, unlike activity's, and not by preference. Almost every
        # method here is a small locked helper — `_note`, `_fired`, `events`,
        # `_override_seconds` — and they compose: writing an audit row from inside
        # a transaction is the natural thing to write and deadlocks instantly on a
        # plain Lock. It did, in `set_allowance`, on the first run of the tests.
        # A plain Lock would be a landmine for whoever adds the next `_note`.
        self._lock = threading.RLock()

        self._db.execute(
            """CREATE TABLE IF NOT EXISTS rules(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                field TEXT NOT NULL,        -- 'proc' | 'app'
                match TEXT NOT NULL,        -- lowercased process/package/app name
                category TEXT NOT NULL,
                added_ts REAL)"""
        )
        self._db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_rule ON rules(field, match)")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS allowances(
                category TEXT PRIMARY KEY,
                minutes INTEGER NOT NULL,
                -- A raise parked against tomorrow. Cleared by any cut, so
                -- lowering then raising again doesn't stack two pending values.
                pending_minutes INTEGER,
                pending_day TEXT,
                -- Grace is a column, not an attribute: see the module docstring.
                grace_until REAL,
                grace_day TEXT,
                enabled INTEGER DEFAULT 1)"""
        )
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT, ts REAL, category TEXT,
                kind TEXT,      -- warn | depleted | grace | closed | override | rule
                detail TEXT)"""
        )
        self._db.execute("CREATE INDEX IF NOT EXISTS ix_ev_day ON events(day, category)")
        self._db.commit()
        self._seed()

    # ---- setup ------------------------------------------------------------

    def _seed(self) -> None:
        """Insert the starter rules once. `INSERT OR IGNORE` against the unique
        index means re-seeding is free and a rule Jake has since deleted stays
        deleted — the seed is a starting point, not a policy re-asserted on every
        boot."""
        with self._lock:
            have = self._db.execute("SELECT COUNT(*) c FROM rules").fetchone()["c"]
            if not have:
                self._db.executemany(
                    "INSERT OR IGNORE INTO rules(field, match, category, added_ts)"
                    " VALUES(?,?,?,?)",
                    [(f, m, c, time.time()) for f, m, c in _SEED_RULES])
                log.info("budget: seeded %d category rules", len(_SEED_RULES))
            # No default allowance. An unconfigured category is *unlimited*, not
            # zero — a fresh install must never lock him out of his own machine
            # before he has chosen a number.
            self._db.commit()

    # ---- categorising -----------------------------------------------------

    def rules(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, field, match, category FROM rules"
                " ORDER BY category, field, match").fetchall()
        return [dict(r) for r in rows]

    def _rule_map(self) -> dict[tuple[str, str], str]:
        with self._lock:
            rows = self._db.execute("SELECT field, match, category FROM rules").fetchall()
        return {(r["field"], r["match"]): r["category"] for r in rows}

    def categorise(self, app: Optional[str], proc: Optional[str],
                   rules: Optional[dict[tuple[str, str], str]] = None) -> Optional[str]:
        """Which budget this app spends from, or None for unbudgeted.

        `proc` wins over `app` because it's the stabler key — an app's display
        name changes with a title-bar rewrite, a process name doesn't. Unmatched
        is the default and it means *unlimited*: everything Jake hasn't
        deliberately named stays outside the system, so Krita and Duolingo can
        never be caught by a rule he didn't write.
        """
        r = self._rule_map() if rules is None else rules
        p = (proc or "").strip().lower()
        if p and ("proc", p) in r:
            return r[("proc", p)]
        a = (app or "").strip().lower()
        if a and ("app", a) in r:
            return r[("app", a)]
        return None

    def add_rule(self, field: str, match: str, category: str) -> dict[str, Any]:
        field = (field or "").strip().lower()
        match = (match or "").strip().lower()
        category = (category or "").strip().lower()
        if field not in ("proc", "app"):
            raise ValueError("field must be 'proc' or 'app'")
        if not match:
            raise ValueError("match cannot be empty")
        if category not in CATEGORIES:
            raise ValueError(f"category must be one of {', '.join(CATEGORIES)}")
        with self._lock:
            self._db.execute(
                "INSERT INTO rules(field, match, category, added_ts) VALUES(?,?,?,?)"
                " ON CONFLICT(field, match) DO UPDATE SET category=excluded.category",
                (field, match, category, time.time()))
            self._db.commit()
        self._note(category, "rule", f"{field}:{match} -> {category}")
        return {"field": field, "match": match, "category": category}

    def remove_rule(self, rule_id: int) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM rules WHERE id=?", (rule_id,))
            self._db.commit()
            return cur.rowcount > 0

    # ---- allowances -------------------------------------------------------

    def allowances(self, now: Optional[float] = None) -> dict[str, dict[str, Any]]:
        """Effective allowance per configured category, promoting any pending
        raise whose day has arrived. Promotion happens on *read* rather than on a
        timer, so it's correct even if Continuum was off at midnight — which it
        routinely is."""
        today = _today(now)
        out: dict[str, dict[str, Any]] = {}
        with self._lock:
            rows = self._db.execute("SELECT * FROM allowances").fetchall()
            promoted = []
            for r in rows:
                minutes = r["minutes"]
                pending, pday = r["pending_minutes"], r["pending_day"]
                if pending is not None and pday and pday <= today:
                    minutes = pending
                    promoted.append((minutes, r["category"]))
                out[r["category"]] = {
                    "category": r["category"],
                    "minutes": minutes,
                    "enabled": bool(r["enabled"]),
                    "pending_minutes": None if (pending is not None and pday and pday <= today) else pending,
                    "pending_day": None if (pending is not None and pday and pday <= today) else pday,
                    "grace_until": r["grace_until"] if r["grace_day"] == today else None,
                }
            for minutes, cat in promoted:
                self._db.execute(
                    "UPDATE allowances SET minutes=?, pending_minutes=NULL,"
                    " pending_day=NULL WHERE category=?", (minutes, cat))
            if promoted:
                self._db.commit()
        for minutes, cat in promoted:
            log.info("budget: %s allowance raised to %dm (scheduled)", cat, minutes)
        return out

    def set_allowance(self, category: str, minutes: int,
                      now: Optional[float] = None) -> dict[str, Any]:
        """Set a daily allowance. **A cut applies now; a raise applies tomorrow.**

        This asymmetry is the whole mechanism, so it lives here rather than in a
        caller that might forget it. A cut also clears any parked raise — without
        that, "raise to 240, think better of it, cut to 60" would leave the 240
        waiting to land overnight and silently undo the decision.
        """
        category = (category or "").strip().lower()
        if category not in CATEGORIES:
            raise ValueError(f"category must be one of {', '.join(CATEGORIES)}")
        minutes = int(minutes)
        if minutes < 0:
            raise ValueError("minutes cannot be negative")
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM allowances WHERE category=?", (category,)).fetchone()
            if row is None:
                # First time this category is bounded at all. Applies immediately:
                # there is no previous number for it to be a raise *from*, and
                # waiting a day to start would just be friction with no upside.
                self._db.execute(
                    "INSERT INTO allowances(category, minutes, enabled) VALUES(?,?,1)",
                    (category, minutes))
                self._db.commit()
                self._note(category, "rule", f"allowance set to {minutes}m (new)")
                return {"category": category, "minutes": minutes, "applies": "now"}
            current = row["minutes"]
            if minutes <= current:
                self._db.execute(
                    "UPDATE allowances SET minutes=?, pending_minutes=NULL,"
                    " pending_day=NULL WHERE category=?", (minutes, category))
                self._db.commit()
                applies, when = "now", None
            else:
                when = _tomorrow(now)
                self._db.execute(
                    "UPDATE allowances SET pending_minutes=?, pending_day=?"
                    " WHERE category=?", (minutes, when, category))
                self._db.commit()
                applies = "tomorrow"
        self._note(category, "rule",
                   f"allowance {current}m -> {minutes}m ({applies})")
        return {"category": category, "minutes": minutes,
                "applies": applies, "effective_day": when}

    def set_enabled(self, category: str, enabled: bool) -> None:
        with self._lock:
            self._db.execute("UPDATE allowances SET enabled=? WHERE category=?",
                             (1 if enabled else 0, category))
            self._db.commit()
        self._note(category, "rule", "enabled" if enabled else "disabled")

    def override(self, category: str, minutes: int,
                 reason: str = "", now: Optional[float] = None) -> dict[str, Any]:
        """Grant extra minutes for today only. Logged loudly, on purpose.

        Recorded as an `override` event rather than by moving the allowance, so
        the number Jake chose in the cold light of morning stays visible next to
        the number of times he went past it. `status()` surfaces the running count.
        """
        minutes = int(minutes)
        if minutes <= 0:
            raise ValueError("override minutes must be positive")
        if category not in CATEGORIES:
            raise ValueError(f"category must be one of {', '.join(CATEGORIES)}")
        self._note(category, "override", f"+{minutes}m {reason}".strip(), now=now)
        # A fresh override retires the spent grace window, so the next depletion
        # gets a real wrap-up period again instead of an instant close.
        with self._lock:
            self._db.execute(
                "UPDATE allowances SET grace_until=NULL, grace_day=NULL WHERE category=?",
                (category,))
            self._db.commit()
        log.info("budget: override +%dm on %s (%s)", minutes, category, reason or "no reason given")
        return {"category": category, "minutes": minutes}

    # ---- spend ------------------------------------------------------------

    def spend_today(self, activity: Any, now: Optional[float] = None) -> dict[str, int]:
        """Seconds spent per category today, derived from activity.segments.

        Counts **both devices** — an hour of Hearthstone is an hour whether it
        was on the desktop or the phone, and splitting them would just teach him
        to move the habit sideways, which is the failure mode that produced this
        feature in the first place.
        """
        day = _today(now)
        rules = self._rule_map()
        totals: dict[str, int] = {c: 0 for c in CATEGORIES}
        for row in activity.totals_by_app(day):
            cat = self.categorise(row.get("app"), row.get("proc"), rules)
            if cat:
                totals[cat] = totals.get(cat, 0) + int(row.get("seconds") or 0)
        return totals

    def _override_seconds(self, category: str, day: str) -> int:
        with self._lock:
            rows = self._db.execute(
                "SELECT detail FROM events WHERE day=? AND category=? AND kind='override'",
                (day, category)).fetchall()
        total = 0
        for r in rows:
            try:
                total += int((r["detail"] or "").split("m")[0].lstrip("+")) * 60
            except (ValueError, IndexError):
                continue
        return total

    # ---- audit ------------------------------------------------------------

    def _note(self, category: str, kind: str, detail: str = "",
              now: Optional[float] = None) -> None:
        ts = now or time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO events(day, ts, category, kind, detail) VALUES(?,?,?,?,?)",
                (_today(ts), ts, category, kind, detail))
            self._db.commit()

    def _fired(self, category: str, day: str) -> set[str]:
        """Which warn thresholds have already gone off today, so the 5-second
        evaluation loop doesn't toast on every tick past 50%."""
        with self._lock:
            rows = self._db.execute(
                "SELECT detail FROM events WHERE day=? AND category=? AND kind='warn'",
                (day, category)).fetchall()
        return {(r["detail"] or "").strip() for r in rows}

    def events(self, day: Optional[str] = None) -> list[dict[str, Any]]:
        day = day or _today()
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, category, kind, detail FROM events WHERE day=?"
                " ORDER BY ts", (day,)).fetchall()
        return [dict(r) for r in rows]

    # ---- the verdict ------------------------------------------------------

    def evaluate(self, activity: Any, foreground: Optional[dict[str, Any]] = None,
                 now: Optional[float] = None) -> dict[str, Any]:
        """Decide what today looks like, and what — if anything — should happen.

        It reads, writes its own audit rows, and returns a list of `actions` for
        somebody else to carry out. It sends no toast and kills no process. That
        split isn't tidiness, it's the migration: when the referee moves to the hub
        in Germany this function runs there unchanged, and the actions travel down
        a socket to whichever machine is actually holding the app.

        `foreground` is the desktop's current focus (`{"name", "proc"}`) or None.
        It matters because grace is only spent while a budgeted app is genuinely in
        front — alt-tab away and the clock stops, the same rule `activity` already
        uses to accrue the time in the first place.
        """
        now = now or time.time()
        day = _today(now)
        spent = self.spend_today(activity, now)
        allowances = self.allowances(now)
        fg_cat = self.categorise((foreground or {}).get("name"),
                                 (foreground or {}).get("proc")) if foreground else None
        today_events = self.events(day)

        cats: list[dict[str, Any]] = []
        actions: list[dict[str, Any]] = []

        for cat, a in allowances.items():
            base_s = int(a["minutes"]) * 60
            extra_s = self._override_seconds(cat, day)
            total_s = base_s + extra_s
            used_s = int(spent.get(cat, 0))
            in_use = (fg_cat == cat)
            # A disabled category is still measured — Jake can watch what an
            # allowance *would* have cost him before committing to enforce it.
            enforcing = bool(a["enabled"])
            pct = 100.0 if total_s <= 0 else min(999.0, used_s / total_s * 100.0)
            grace_until = a["grace_until"]
            state = "ok"

            fired = self._fired(cat, day)
            for t in WARN_PCT:
                if pct >= t and str(t) not in fired:
                    self._note(cat, "warn", str(t), now=now)
                    if enforcing:
                        actions.append({
                            "kind": "warn", "category": cat, "pct": t,
                            "title": f"{cat.title()} budget {t}% used",
                            "body": f"{_hm(used_s)} of {_hm(total_s)} — "
                                    f"{_hm(max(0, total_s - used_s))} left today.",
                        })
            if pct >= WARN_PCT[0]:
                state = "warn"

            if used_s >= total_s:
                first_time = not any(e["kind"] == "depleted" and e["category"] == cat
                                     for e in today_events)
                if first_time:
                    self._note(cat, "depleted", _hm(used_s), now=now)
                    if enforcing:
                        actions.append({
                            "kind": "depleted", "category": cat,
                            "title": f"{cat.title()} budget spent",
                            "body": (f"{_hm(used_s)} today — "
                                     f"{int(DEFAULT_GRACE_MIN)} minutes to wrap up."
                                     if in_use else f"{_hm(used_s)} today. Back tomorrow."),
                        })
                state = "depleted"

                if in_use and enforcing:
                    if not grace_until:
                        # A full wrap-up the first time it runs out under him; a
                        # short one every time after, because relaunching a game
                        # whose budget is gone isn't a session to be rescued, it's
                        # a decision to be noticed.
                        span = DEFAULT_GRACE_MIN if first_time else RELAUNCH_GRACE_MIN
                        grace_until = now + span * 60
                        with self._lock:
                            self._db.execute(
                                "UPDATE allowances SET grace_until=?, grace_day=?"
                                " WHERE category=?", (grace_until, day, cat))
                            self._db.commit()
                        self._note(cat, "grace", f"{int(span)}m", now=now)
                        state = "grace"
                        if not first_time:
                            actions.append({
                                "kind": "depleted", "category": cat,
                                "title": f"{cat.title()} budget is gone",
                                "body": f"{(foreground or {}).get('name') or cat} "
                                        f"closes in {int(span)} min.",
                            })
                    elif now < grace_until:
                        state = "grace"
                    else:
                        state = "expired"
                        actions.append({
                            "kind": "close", "category": cat,
                            "proc": (foreground or {}).get("proc"),
                            "app": (foreground or {}).get("name"),
                            "title": f"{cat.title()} budget — closing",
                            "body": f"{(foreground or {}).get('name') or cat} is closing. "
                                    f"Grant yourself more time in Continuum if you need it.",
                        })
                        # Spend the window. A relaunch earns a fresh short one
                        # rather than being killed the instant it opens.
                        with self._lock:
                            self._db.execute(
                                "UPDATE allowances SET grace_until=NULL, grace_day=NULL"
                                " WHERE category=?", (cat,))
                            self._db.commit()
                        self._note(cat, "closed",
                                   str((foreground or {}).get("name") or ""), now=now)

            cats.append({
                "category": cat, "minutes": a["minutes"],
                "allowance_seconds": total_s, "override_seconds": extra_s,
                "spent_seconds": used_s, "remaining_seconds": max(0, total_s - used_s),
                "spent": _hm(used_s), "allowance": _hm(total_s),
                "remaining": _hm(max(0, total_s - used_s)),
                "pct": round(pct, 1), "state": state, "in_use": in_use,
                "enabled": enforcing, "grace_until": grace_until,
                "pending_minutes": a["pending_minutes"], "pending_day": a["pending_day"],
            })

        return {"day": day, "categories": cats, "actions": actions}

    def status(self, activity: Any, now: Optional[float] = None) -> dict[str, Any]:
        """Read-only view for the UI and the model.

        Never starts a grace window and never fires a warning — `evaluate()` owns
        every side effect, so opening the tab can't quietly burn wrap-up time Jake
        hasn't used yet.
        """
        now = now or time.time()
        day = _today(now)
        spent = self.spend_today(activity, now)
        allowances = self.allowances(now)
        cats = []
        for cat, a in allowances.items():
            base_s = int(a["minutes"]) * 60
            extra_s = self._override_seconds(cat, day)
            total_s = base_s + extra_s
            used_s = int(spent.get(cat, 0))
            cats.append({
                "category": cat, "minutes": a["minutes"],
                "allowance_seconds": total_s, "override_seconds": extra_s,
                "spent_seconds": used_s, "remaining_seconds": max(0, total_s - used_s),
                "spent": _hm(used_s), "allowance": _hm(total_s),
                "remaining": _hm(max(0, total_s - used_s)),
                "pct": round(100.0 if total_s <= 0 else min(999.0, used_s / total_s * 100.0), 1),
                "enabled": bool(a["enabled"]),
                "pending_minutes": a["pending_minutes"], "pending_day": a["pending_day"],
            })
        # Uncategorised time, so the tab can answer "is this even watching the
        # right things?" without Jake having to trust the rule list on faith.
        rules = self._rule_map()   # hoisted: categorise() would re-read it per row
        untracked = sum(int(r["seconds"]) for r in activity.totals_by_app(day)
                        if not self.categorise(r.get("app"), r.get("proc"), rules))
        return {
            "day": day, "categories": cats,
            "untracked_seconds": untracked, "untracked": _hm(untracked),
            "overrides_today": sum(1 for e in self.events(day) if e["kind"] == "override"),
            "override_seconds_today": sum(self._override_seconds(c, day) for c in CATEGORIES),
        }
