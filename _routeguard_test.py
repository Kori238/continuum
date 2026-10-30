"""A route must turn a store's refusal into an answer the caller can act on.

Two routes, one idea. The stores already refuse bad input correctly; these are
the two lines above them, which is where the refusal either becomes a usable 400
or becomes something worse.

  * PATCH /api/routines/{rid} splatted straight into routines.update() with no
    guard. That was harmless while the only raisers in routines.py were the three
    add* functions - every one of which IS guarded at its route. Making _norm_days
    raise turned update() into a raiser too, and this route answered 500 with an
    empty body for input that POST /api/routines had always answered 400 for.
    The regression arrived with the fix, which is the point of writing it down.

  * POST /api/budget/allowance read `int(payload.get("minutes") or 0)`, and `or`
    collapses "not supplied" into a real zero. Zero is the most restrictive value
    the field has - `0 <= current` is a CUT, so the store applied it IMMEDIATELY
    and blocked the category outright. A typo'd field name, an omitted key and a
    null all did that and answered 200.

The second is the mirror of the `days` bug in _routinedays_test.py: there a
failed narrowing fell back to the WIDEST value, here a missing field fell back to
the NARROWEST one. Same root cause either way - the fallback sat at one end of
the field's range, so a failed request was indistinguishable from a deliberate
extreme one, and nothing anywhere was red.

Both over-corrections are asserted too, because refusing too much is the obvious
way to "fix" either of these: a good PATCH must still store, and an explicit
minutes=0 must still be honoured. Blocking a category on purpose is a thing Jake
can want, and refusing it would trade this bug for an unreachable setting.

WHY THE APP IS BUILT WITHOUT THE LIFESPAN
-----------------------------------------
_budget_api_test.py uses `with TestClient(app)`, which fires startup and runs the
state loop for real - so it keeps every allowance it sets generous, on purpose:
"A test must not be able to close his game."

This file has to set an allowance of 0, which is exactly the case that file
avoids. `used_s >= total_s` is the depletion test, so 0 >= 0 depletes on the
first tick, and a depleted category emits a real desktop toast when enforcing
(and a terminate if a budgeted app is in the foreground). Building the app
WITHOUT the context manager means startup never fires, so there is no loop and
no enforcement, and the 0 case is safe to assert here and nowhere else.
"""
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="continuum-routeguard-"))
for var, leaf in [
    ("CONTINUUM_MEMORY_DB", "memory.db"), ("CONTINUUM_CONVERSATIONS_DB", "conversations.db"),
    ("CONTINUUM_SCHEDULER_DB", "scheduler.db"), ("CONTINUUM_MESSAGES_DB", "messages.db"),
    ("CONTINUUM_ROUTINES_DB", "routines.db"), ("CONTINUUM_WORKOUTS_DB", "workouts.db"),
    ("CONTINUUM_ACTIVITY_DB", "activity.db"), ("CONTINUUM_NUTRITION_DB", "nutrition.db"),
    ("CONTINUUM_BODY_DB", "body.db"), ("CONTINUUM_HEALTH_DB", "health.db"),
    ("CONTINUUM_LEDGER_DB", "ledger.db"), ("CONTINUUM_FLASHCARDS_DB", "flashcards.db"),
    ("CONTINUUM_BUDGET_DB", "budget.db"), ("CONTINUUM_UPLOADS_DIR", "uploads"),
]:
    os.environ[var] = str(TMP / leaf)
os.environ["CONTINUUM_OLLAMA_AUTOSTART"] = "0"
os.environ["CONTINUUM_PROACTIVE"] = "0"
# Set, not unset: config's _load_dotenv() uses setdefault, so an unset var gets
# filled from the real .env and this file would start making paid calls.
for k in ("CONTINUUM_FDC_KEY", "CONTINUUM_FATSECRET_ID", "CONTINUUM_FATSECRET_SECRET"):
    os.environ[k] = ""

sys.path.insert(0, str(Path(__file__).parent))
from continuum.config import Config              # noqa: E402

cfg = Config.load()
LIVE = str((Path(__file__).parent / "data").resolve()).lower()
bad = [f"{n}={getattr(cfg, n)}" for n in vars(cfg)
       if isinstance(getattr(cfg, n), str) and ("_db" in n or "_dir" in n)
       and getattr(cfg, n)
       and str(Path(getattr(cfg, n)).resolve()).lower().startswith(LIVE)]
if bad:
    sys.exit("REFUSING TO RUN - these still point at live data:\n  " + "\n  ".join(bad))
print(f"sandbox: {TMP}")

from fastapi.testclient import TestClient        # noqa: E402
from continuum.app.server import create_app      # noqa: E402

# No `with`: see the module docstring. raise_server_exceptions=False so a 500
# arrives as a 500 instead of being re-raised here - the whole point is to
# assert on the status the CALLER would see.
client = TestClient(create_app(), client=("127.0.0.1", 51000),
                    raise_server_exceptions=False)

FAILED = []


def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"\n        got={got!r}\n       want={want!r}"))
    if not ok:
        FAILED.append(label)


def truthy(label, got):
    ok = bool(got)
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"   got={got!r}"))
    if not ok:
        FAILED.append(label)


# ---- PATCH /api/routines/{rid} -------------------------------------------
rid = client.post("/api/routines", json={"title": "Gym", "days": "1,4"}).json()["id"]


def row():
    return next(r for r in client.get("/api/routines").json()["all"] if r["id"] == rid)


r = client.patch(f"/api/routines/{rid}", json={"days": "mon,thu"})
check("a good PATCH is still 200", r.status_code, 200)
check("...and really narrowed the row", row()["days"], "0,3")

before = row()["days"]
r = client.patch(f"/api/routines/{rid}", json={"days": "M,W,F"})
check("an unparseable days is 400, not 500", r.status_code, 400)
truthy("...and the body names the formats", "weekday names" in r.text)
check("...and the row is untouched", row()["days"], before)

r = client.patch(f"/api/routines/{rid}", json={"days": "whenever I feel like it"})
check("prose is 400 too", r.status_code, 400)
check("...and still leaves the row alone", row()["days"], before)

# The sibling that was always guarded, as the control. If THIS regresses the
# comparison above stops meaning anything.
r = client.post("/api/routines", json={"title": "Yoga", "days": "M,W,F"})
check("POST /api/routines was already 400 and still is", r.status_code, 400)

# The reason the splat exists at all - a client round-tripping a row it just read
# sends the identity fields back. Guarding must not have broken that.
r = client.patch(f"/api/routines/{rid}", json={"id": rid, "rid": rid, "days": "sat sun"})
check("a round-tripped body with id/rid still works", r.status_code, 200)
check("...and narrowed to the weekend", row()["days"], "5,6")

# ---- POST /api/budget/allowance ------------------------------------------
r = client.post("/api/budget/allowance", json={"category": "game", "minutes": 120})
check("a first allowance applies now", r.json()["applies"], "now")

for body, note in [
    ({"category": "game", "mins": 240}, "a typo'd field name"),
    ({"category": "game"}, "an omitted minutes"),
    ({"category": "game", "minutes": None}, "a null minutes"),
    # The empty string earns its own row. It is the ONLY value that clears an
    # `is None` guard while still being falsy, so it is the single input for
    # which `minutes` and `minutes or 0` differ - a blank form field silently
    # became a zero allowance. The negative control found this: a mutation
    # restoring `or 0` below the guard survived, because every other value
    # reaching that line is identical either way.
    ({"category": "game", "minutes": ""}, "a blank minutes"),
]:
    r = client.post("/api/budget/allowance", json=body)
    check(f"{note} is 400, not a silent block", r.status_code, 400)
    truthy(f"...and {note} says what to send", "minutes is required" in r.text)

# THE OVER-CORRECTION. Blocking a category deliberately must still be reachable,
# or the fix has traded a silent zero for an unreachable setting. Safe here only
# because there is no state loop - see the module docstring.
r = client.post("/api/budget/allowance", json={"category": "game", "minutes": 0})
check("an explicit 0 is still accepted", r.status_code, 200)
check("...and is stored as 0", r.json()["minutes"], 0)
check("...and lands now, because a cut always does", r.json()["applies"], "now")

# Controls: the asymmetry the store exists to enforce still works through the
# route, so the guard above is not quietly eating real values.
r = client.post("/api/budget/allowance", json={"category": "game", "minutes": 240})
check("a raise is still deferred to tomorrow", r.json()["applies"], "tomorrow")
check("...with the raised number", r.json()["minutes"], 240)
r = client.post("/api/budget/allowance", json={"category": "game", "minutes": "90"})
check("a numeric string still parses", r.json()["minutes"], 90)
r = client.post("/api/budget/allowance", json={"category": "game", "minutes": -1})
check("a negative is still 400", r.status_code, 400)
r = client.post("/api/budget/allowance", json={"category": "chess", "minutes": 10})
check("an unknown category is still 400", r.status_code, 400)

# The neighbouring route reads `minutes or 0` too, but override() refuses
# `minutes <= 0` outright, so the collapse lands on a refusal rather than on a
# legal extreme. Asserted rather than assumed, because that is the only reason
# it was left alone.
r = client.post("/api/budget/override", json={"category": "game", "minutes": 0})
check("override's own `or 0` lands on a refusal, not a silent no-op", r.status_code, 400)
r = client.post("/api/budget/override", json={"category": "game"})
check("...and an omitted minutes is refused there too", r.status_code, 400)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("all route-guard checks passed")
