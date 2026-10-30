"""The create_meal tool against the real store, through the real executor.

Three callers reach add_meal: the tab's route, this tool, and the store's own
API. _nutrition_test.py covers the store and _nutriapi_test.py covers the route
— and a mutation control proved that left `tools.py` completely uncovered, so
`args.get("calories") or 0` could be reintroduced here and every other suite
would stay green. This is the door Jake actually uses most: "change Chili to
lunch" is a sentence, not a form submission.

Sandboxed to a temp dir. CONTINUUM_FDC_KEY and the FatSecret pair are set to
"" rather than left unset, because config's _load_dotenv() uses setdefault and
an unset var gets filled from the real .env — the ingredient path would then
make paid calls from a test run.
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="continuum-nutri-tools-"))
os.environ["CONTINUUM_NUTRITION_DB"] = str(TMP / "nutrition.db")
os.environ["CONTINUUM_FDC_KEY"] = ""
os.environ["CONTINUUM_FATSECRET_ID"] = ""
os.environ["CONTINUUM_FATSECRET_SECRET"] = ""
sys.path.insert(0, str(Path(__file__).parent))

from continuum.config import Config              # noqa: E402
from continuum.nutrition import NutritionStore   # noqa: E402
from continuum.tools import ToolLoop             # noqa: E402

cfg = Config.load()
assert str(Path(cfg.nutrition_db).resolve()).startswith(str(TMP.resolve())), \
    "REFUSING TO RUN - nutrition_db is not in the sandbox"
print(f"sandbox: {TMP}")

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


class FakeBus:
    def __init__(self):
        self.events = []

    async def publish(self, event):
        self.events.append(event)


store = NutritionStore(cfg)
bus = FakeBus()
loop = ToolLoop(cfg, nutrition=store, bus=bus)


def run(tool, /, **args):
    # Positional-only: every one of these tools takes its own `name` argument,
    # which would otherwise collide with the dispatcher's.
    return asyncio.run(loop._nutrition_action(tool, args))


def saved(name):
    return next((m for m in store.meals() if m["name"].lower() == name.lower()), None)


# Macros on every ingredient, so the food-database path is never entered — an
# empty FDC key would otherwise turn this into a lookup-failure test.
CHILI = [{"name": "beef", "calories": 800, "protein_g": 60, "carbs_g": 0, "fat_g": 60},
         {"name": "beans", "calories": 400, "protein_g": 24, "carbs_g": 70, "fat_g": 2}]

out = run("create_meal", name="Chili", ingredients=CHILI, servings=4, meal="dinner")
truthy("the tool reports the per-portion figure", "300kcal" in out)
truthy("the tool names the recipe yield", "makes 4" in out)
m = saved("Chili")
check("the tool divided by servings", m["calories"], 300.0)
check("the tool stored the meal-time", m["meal"], "dinner")
check("the tool stored the ingredients", [i["name"] for i in m["ingredients"]], ["beef", "beans"])

# The bug, at the layer Jake talks to. "change Chili to lunch" must not be a
# request to blank the recipe.
run("create_meal", name="Chili", meal="lunch")
m = saved("Chili")
check("a one-field edit keeps the macros", m["calories"], 300.0)
check("  ... keeps protein too", m["protein_g"], 21.0)
check("  ... keeps servings", m["servings"], 4.0)
check("  ... keeps the ingredient list", [i["name"] for i in m["ingredients"]], ["beef", "beans"])
check("  ... and does change what it named", m["meal"], "lunch")

# A macro edit must not reset the slot either - the same collapse, mirrored.
run("create_meal", name="Chili", calories=1200)
m = saved("Chili")
check("a macro edit keeps the meal-time", m["meal"], "lunch")
check("a supplied macro is still a recipe total", m["calories"], 300.0)

# ...and an explicit 0 must still land, or the fix has traded a blanking bug
# for a field that can never be cleared on purpose.
run("create_meal", name="Chili", calories=0)
m = saved("Chili")
check("an explicit 0 from the tool overwrites", m["calories"], 0.0)
check("  ... without touching protein", m["protein_g"], 21.0)

check("a nameless meal refuses", run("create_meal"), "what should the meal be called?")

# The consequence, end to end: what reaches the diary after an innocent edit.
run("create_meal", name="Stew", ingredients=CHILI, servings=4, meal="dinner")
run("create_meal", name="Stew", meal="lunch")
out = run("log_meal", name="Stew", portions=2)
truthy("logging after a one-field edit posts real calories", "600kcal" in out)

# The write is audited - a silent store mutation can't be traced to the turn.
bus.events.clear()
run("create_meal", name="Soup", calories=200)
check("create_meal published an action", len(bus.events), 1)
check("the audited action is named", bus.events[0].payload["action"], "create_meal")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED")
    sys.exit(1)
print("all checks passed")
