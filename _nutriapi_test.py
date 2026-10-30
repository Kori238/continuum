"""The saved-meal route end-to-end through the real create_app().

_nutrition_test.py covers the store. This covers the two lines above it, which
is where the bug actually reached Jake: the route wrote
`body.get("calories") or 0`, and `or` collapses "field not supplied" into a
real zero — exactly the distinction add_meal depends on to leave an unsupplied
field alone. The store can be perfectly correct and the meal still gets blanked
one frame higher up.

Sandboxed the same way as _fcapi_test.py: every store redirected into a temp
dir, and the run aborts before create_app() if any Config path still resolves
inside live data.
"""
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="continuum-nutri-api-"))
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
os.environ["CONTINUUM_FDC_KEY"] = ""
os.environ["CONTINUUM_FATSECRET_ID"] = ""
os.environ["CONTINUUM_FATSECRET_SECRET"] = ""

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

from fastapi.testclient import TestClient         # noqa: E402
from continuum.app.server import create_app       # noqa: E402

# Without the client= tuple every request is refused with "no tailnet identity".
client = TestClient(create_app(), client=("127.0.0.1", 51000))

FAILED = []


def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"\n        got={got!r}\n       want={want!r}"))
    if not ok:
        FAILED.append(label)


def save(**body):
    r = client.post("/api/nutrition/meals", json=body)
    assert r.status_code == 200, (r.status_code, r.text[:300])
    return r.json()


CHILI = [{"name": "beef", "calories": 800, "protein_g": 60, "carbs_g": 0, "fat_g": 60},
         {"name": "beans", "calories": 400, "protein_g": 24, "carbs_g": 70, "fat_g": 2}]

m = save(name="Chili", ingredients=CHILI, servings=4, meal="dinner")
check("the route sums ingredients and splits per portion", m["calories"], 300.0)
check("the route stores servings", m["servings"], 4.0)
check("the route stores the meal-time", m["meal"], "dinner")

# The bug, at the layer it actually shipped in.
e = save(name="Chili", meal="lunch")
check("a partial POST keeps the macros", e["calories"], 300.0)
check("a partial POST keeps servings", e["servings"], 4.0)
check("a partial POST keeps the ingredients", [i["name"] for i in e["ingredients"]], ["beef", "beans"])
check("a partial POST changes what it named", e["meal"], "lunch")

# And the same field sent as a real zero must still take effect, or the fix has
# simply traded a blanking bug for an un-editable field.
z = save(name="Chili", calories=0)
check("an explicit 0 in the body still overwrites", z["calories"], 0.0)
check("  ... without touching protein", z["protein_g"], 21.0)
# The mirror: a body that omits `meal` must not file the meal under nothing.
# Every partial-POST check above sends a meal-time, so a route that blanked an
# unsent one passed all of them.
check("  ... and without blanking the meal-time", z["meal"], "lunch")

check("a nameless meal is a 400",
      client.post("/api/nutrition/meals", json={"calories": 1}).status_code, 400)

# The consequence: what lands in the diary after an innocent edit.
save(name="Stew", ingredients=CHILI, servings=4, meal="dinner")
save(name="Stew", meal="lunch")
r = client.post("/api/nutrition/meals/log", json={"name": "Stew", "portions": 2})
check("logging after a partial edit posts real calories", r.status_code, 200)
check("  ... and they are not zero", r.json()["entry"]["calories"], 600.0)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED")
    sys.exit(1)
print("all checks passed")
