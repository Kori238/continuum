"""Budget store — behaviour tests against a throwaway database.

Same fail-closed opening as _registry_test.py: every CONTINUUM_*_DB/_DIR var is
redirected into a temp dir and the run aborts if any Config.load() field still
points inside the live data directory. Fourteen stores now, not thirteen.

The interesting cases here are the ones that would quietly ruin the feature:
warnings repeating on every 5-second tick, grace resetting when Continuum
restarts, and a parked raise surviving a later cut.
"""
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="continuum-budget-test-"))
for var, leaf in [
    ("CONTINUUM_MEMORY_DB", "memory.db"), ("CONTINUUM_CONVERSATIONS_DB", "conversations.db"),
    ("CONTINUUM_SCHEDULER_DB", "scheduler.db"), ("CONTINUUM_MESSAGES_DB", "messages.db"),
    ("CONTINUUM_ROUTINES_DB", "routines.db"), ("CONTINUUM_WORKOUTS_DB", "workouts.db"),
    ("CONTINUUM_ACTIVITY_DB", "activity.db"), ("CONTINUUM_NUTRITION_DB", "nutrition.db"),
    ("CONTINUUM_BODY_DB", "body.db"), ("CONTINUUM_HEALTH_DB", "health.db"),
    ("CONTINUUM_LEDGER_DB", "ledger.db"), ("CONTINUUM_FLASHCARDS_DB", "flashcards.db"),
    ("CONTINUUM_BUDGET_DB", "budget.db"),
]:
    os.environ[var] = str(TMP / leaf)
os.environ["CONTINUUM_UPLOADS_DIR"] = str(TMP / "uploads")

sys.path.insert(0, str(Path(__file__).parent))
from continuum.config import Config              # noqa: E402
from continuum.activity import ActivityStore     # noqa: E402
from continuum import budget as B                # noqa: E402

cfg = Config.load()
LIVE = str((Path(__file__).parent / "data").resolve()).lower()
for name in vars(cfg):
    v = getattr(cfg, name)
    if isinstance(v, str) and v and ("_db" in name or "_dir" in name):
        if str(Path(v).resolve()).lower().startswith(LIVE):
            sys.exit(f"REFUSING TO RUN: {name} still points at live data ({v})")
print(f"sandbox: {TMP}")

FAILED = []


def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"   got={got!r} want={want!r}"))
    if not ok:
        FAILED.append(label)


def truthy(label, got):
    ok = bool(got)
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"   got={got!r}"))
    if not ok:
        FAILED.append(label)


# Anchor to 10:00 today so nothing straddles midnight if the suite runs late.
_d = datetime.now()
NOON = datetime(_d.year, _d.month, _d.day, 10, 0, 0).timestamp()
DAY = datetime.fromtimestamp(NOON).strftime("%Y-%m-%d")

activity = ActivityStore(cfg)


def seg(device, app, proc, start_offset_s, seconds):
    """Write a closed segment straight into activity.db — test fixture only."""
    db = sqlite3.connect(cfg.activity_db)
    s = NOON + start_offset_s
    db.execute("INSERT INTO segments(device, app, proc, title, start_ts, end_ts, seconds)"
               " VALUES(?,?,?,?,?,?,?)",
               (device, app, proc, app, s, s + seconds, int(seconds)))
    db.commit()
    db.close()


# ---- categorising ---------------------------------------------------------
bs = B.BudgetStore(cfg)
check("Game.exe -> game (Elderfield)", bs.categorise("Welcome to Elderfield", "Game.exe"), "game")
check("VALORANT -> game", bs.categorise("VALORANT", "VALORANT-Win64-Shipping.exe"), "game")
check("phone Hearthstone pkg -> game",
      bs.categorise("Hearthstone", "com.blizzard.wtcg.hearthstone"), "game")
check("browser YouTube -> video (app rule, proc is firefox)",
      bs.categorise("YouTube", "firefox.exe"), "video")
check("phone Instagram -> social", bs.categorise("Instagram", "com.instagram.android"), "social")
check("Krita is unbudgeted", bs.categorise("Krita", "krita.exe"), None)
check("Duolingo is unbudgeted", bs.categorise("Duolingo", "com.duolingo"), None)
check("proc wins over app", bs.categorise("Instagram", "Game.exe"), "game")

# ---- spend is derived, and crosses devices --------------------------------
seg("desktop", "Welcome to Elderfield", "Game.exe", 0, 1800)      # 30m
seg("phone", "Hearthstone", "com.blizzard.wtcg.hearthstone", 0, 600)   # 10m
seg("desktop", "Krita", "krita.exe", 0, 3600)                      # 60m, unbudgeted
seg("phone", "Instagram", "com.instagram.android", 0, 900)         # 15m
check("game spend sums both devices", bs.spend_today(activity, NOON)["game"], 2400)
check("social spend", bs.spend_today(activity, NOON)["social"], 900)
check("Krita excluded from every category", bs.spend_today(activity, NOON)["video"], 0)

# ---- asymmetric allowance changes -----------------------------------------
r = bs.set_allowance("game", 60, now=NOON)
check("first allowance applies now", r["applies"], "now")
r = bs.set_allowance("game", 120, now=NOON)
check("raise is deferred", r["applies"], "tomorrow")
check("deferred raise targets tomorrow", r["effective_day"],
      (datetime.fromtimestamp(NOON).date() + timedelta(days=1)).strftime("%Y-%m-%d"))
check("effective allowance unchanged today", bs.allowances(NOON)["game"]["minutes"], 60)
r = bs.set_allowance("game", 30, now=NOON)
check("cut applies now", r["applies"], "now")
check("cut clears the parked raise", bs.allowances(NOON)["game"]["pending_minutes"], None)
check("cut took effect", bs.allowances(NOON)["game"]["minutes"], 30)
# ...and the raise really does land the next day.
bs.set_allowance("game", 90, now=NOON)
tomorrow = NOON + 86400
check("parked raise lands tomorrow", bs.allowances(tomorrow)["game"]["minutes"], 90)
bs.set_allowance("game", 60, now=tomorrow)   # back to a sane number for what follows

# ---- warnings fire once per threshold, not once per poll ------------------
bs2 = B.BudgetStore(cfg)
bs2.set_allowance("game", 60, now=NOON)      # 40m spent of 60m = 66.6%
v = bs2.evaluate(activity, foreground=None, now=NOON)
game = next(c for c in v["categories"] if c["category"] == "game")
check("66% of budget -> warn state", game["state"], "warn")
warns = [a for a in v["actions"] if a["kind"] == "warn"]
check("crossing 50 fires exactly one warning", [a["pct"] for a in warns], [50])
v = bs2.evaluate(activity, foreground=None, now=NOON + 5)
check("second poll 5s later fires nothing", [a for a in v["actions"] if a["kind"] == "warn"], [])
seg("desktop", "Welcome to Elderfield", "Game.exe", 1800, 300)     # +5m -> 45m = 75%
v = bs2.evaluate(activity, foreground=None, now=NOON + 10)
check("crossing 75 fires only 75", [a["pct"] for a in v["actions"] if a["kind"] == "warn"], [75])

# ---- depletion, grace, and the close --------------------------------------
seg("desktop", "Welcome to Elderfield", "Game.exe", 2100, 900)     # +15m -> 60m = 100%
FG = {"name": "Welcome to Elderfield", "proc": "Game.exe"}
v = bs2.evaluate(activity, foreground=FG, now=NOON + 20)
game = next(c for c in v["categories"] if c["category"] == "game")
check("depleted while playing -> grace, not a kill", game["state"], "grace")
check("no close action during grace", [a for a in v["actions"] if a["kind"] == "close"], [])
truthy("90 and 95 warnings also fired",
       {a["pct"] for a in v["actions"] if a["kind"] == "warn"} >= {90, 95})
truthy("a depletion notice was raised", any(a["kind"] == "depleted" for a in v["actions"]))
grace_until = game["grace_until"]
truthy("grace window is ~10 minutes",
       590 <= (grace_until - (NOON + 20)) <= 610)

# THE restart bug: grace must live on disk, or every restart is a fresh 10 min.
bs3 = B.BudgetStore(cfg)
v = bs3.evaluate(activity, foreground=FG, now=NOON + 30)
game = next(c for c in v["categories"] if c["category"] == "game")
check("grace survives a restart", round(game["grace_until"], 3), round(grace_until, 3))
check("still in grace, still no close", [a for a in v["actions"] if a["kind"] == "close"], [])

# Alt-tab away: the wrap-up clock shouldn't be spent on something unbudgeted.
v = bs3.evaluate(activity, foreground={"name": "Krita", "proc": "krita.exe"},
                 now=NOON + 20 + 700)
game = next(c for c in v["categories"] if c["category"] == "game")
check("not in use -> no close even past the window",
      [a for a in v["actions"] if a["kind"] == "close"], [])
check("state falls back to depleted when not in use", game["state"], "depleted")

# Back in the game with the window expired -> close.
v = bs3.evaluate(activity, foreground=FG, now=NOON + 20 + 700)
closes = [a for a in v["actions"] if a["kind"] == "close"]
check("expired grace closes the app", len(closes), 1)
check("close names the process", closes[0]["proc"], "Game.exe")
check("grace is spent after the close", bs3.allowances(NOON)["game"]["grace_until"], None)

# Relaunch: a short window, not an instant second kill.
v = bs3.evaluate(activity, foreground=FG, now=NOON + 20 + 800)
game = next(c for c in v["categories"] if c["category"] == "game")
check("relaunch gets a fresh window", game["state"], "grace")
truthy("relaunch window is the short one (~1 min)",
       50 <= (game["grace_until"] - (NOON + 20 + 800)) <= 70)

# ---- the override valve ----------------------------------------------------
bs3.override("game", 30, reason="finishing a run", now=NOON + 20 + 810)
st = bs3.status(activity, now=NOON + 20 + 820)
game = next(c for c in st["categories"] if c["category"] == "game")
check("override adds time today", game["allowance_seconds"], 60 * 60 + 30 * 60)
check("override is counted, not hidden", st["overrides_today"], 1)
check("override leaves the chosen number alone", game["minutes"], 60)
check("override retires the spent grace", bs3.allowances(NOON)["game"]["grace_until"], None)
v = bs3.evaluate(activity, foreground=FG, now=NOON + 20 + 820)
check("back under budget after an override",
      next(c for c in v["categories"] if c["category"] == "game")["state"], "warn")

# ---- status is genuinely read-only ----------------------------------------
before = bs3.events(DAY)
bs3.status(activity, now=NOON + 20 + 830)
bs3.status(activity, now=NOON + 20 + 840)
check("status() writes no events", len(bs3.events(DAY)), len(before))

# ---- a disabled category is measured but not enforced ---------------------
bs3.set_allowance("social", 5, now=NOON)     # 15m spent of 5m = way over
bs3.set_enabled("social", False)
v = bs3.evaluate(activity, foreground={"name": "Instagram", "proc": "com.instagram.android"},
                 now=NOON + 900)
social = next(c for c in v["categories"] if c["category"] == "social")
check("disabled category still measured", social["spent_seconds"], 900)
check("disabled category emits no actions",
      [a for a in v["actions"] if a["category"] == "social"], [])

# ---- untracked time is visible --------------------------------------------
st = bs3.status(activity, now=NOON + 900)
check("Krita's hour shows as untracked", st["untracked_seconds"], 3600)

# ---- input validation ------------------------------------------------------
for bad, args in [("unknown category", ("chess", 60)), ("negative minutes", ("game", -5))]:
    try:
        bs3.set_allowance(*args)
        check(f"rejects {bad}", "accepted", "ValueError")
    except ValueError:
        check(f"rejects {bad}", "ValueError", "ValueError")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("all budget checks passed")
