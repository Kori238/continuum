"""Does the saved-meal store keep what you didn't ask it to change?

`add_meal` is an upsert. It had no coverage at all - 107 test and probe files
in this tree and not one named it - and it treated every unsupplied field as
"set to blank", so the most ordinary edit there is (`add_meal("Chili",
meal="lunch")`) zeroed the macros, reset servings to 1 and dropped the
ingredient list. The meal then logged 0 kcal portions into every later daily
total.

Sandboxed into a temp dir: this suite writes meals and food entries, and must
never touch the real nutrition database.
"""
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from continuum.nutrition import NutritionStore  # noqa: E402

TMP = tempfile.mkdtemp(prefix="nutri-test-")
DB = os.path.join(TMP, "nutrition.db")
assert TMP in DB, "REFUSING TO RUN - the db is not in the sandbox"
print(f"sandbox: {TMP}")

FAILED = []


def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"\n        got={got!r}\n       want={want!r}"))
    if not ok:
        FAILED.append(label)


def store():
    """A fresh store per group, so one group's meals cannot satisfy another's
    assertions - `add_meal` matches on name and these groups reuse names."""
    d = tempfile.mkdtemp(dir=TMP)
    return NutritionStore(types.SimpleNamespace(
        nutrition_db=os.path.join(d, "n.db"), data_dir=d))


CHILI = [{"name": "beef", "calories": 800, "protein_g": 60, "carbs_g": 0, "fat_g": 60},
         {"name": "beans", "calories": 400, "protein_g": 24, "carbs_g": 70, "fat_g": 2}]

# ---- creating -------------------------------------------------------------
n = store()
m = n.add_meal("Chili", ingredients=CHILI, servings=4, meal="dinner")
check("ingredients are summed then split per portion", m["calories"], 300.0)
check("  ... and so are the other three", [m["protein_g"], m["carbs_g"], m["fat_g"]], [21.0, 17.5, 15.5])
check("servings is stored, not just used", m["servings"], 4.0)
check("the meal-time slot is stored", m["meal"], "dinner")
check("the ingredient list is kept as provenance", [i["name"] for i in m["ingredients"]], ["beef", "beans"])

# Direct macros are RECIPE TOTALS, divided the same way. The server docstring
# used to claim they were already per-portion, which the store has never done.
n2 = store()
d = n2.add_meal("Soup", calories=600, protein_g=30, servings=2)
check("direct macros are recipe totals, divided by servings", [d["calories"], d["protein_g"]], [300.0, 15.0])

# ---- the bug: a partial edit must not blank the rest -----------------------
n3 = store()
n3.add_meal("Chili", ingredients=CHILI, servings=4, meal="dinner")
e = n3.add_meal("Chili", meal="lunch")
check("editing only the meal-time keeps the macros", e["calories"], 300.0)
check("editing only the meal-time keeps servings", e["servings"], 4.0)
check("editing only the meal-time keeps the ingredients", [i["name"] for i in e["ingredients"]], ["beef", "beans"])
check("editing only the meal-time actually changes it", e["meal"], "lunch")

# A kept macro must not be re-divided. This is the compounding form of the bug:
# stored macros are already per-portion, so a second write that re-divides them
# by the same servings silently shrinks the meal every time it is touched.
again = n3.add_meal("Chili", meal="dinner")
check("a kept macro is not re-divided on each edit", again["calories"], 300.0)

# The mirror of the check above, and it was the one blind spot a mutation
# control found: every "partial edit" case here supplied `meal` and asserted
# the macros survived, so blanking the meal-time when it ISN'T supplied went
# unnoticed. The slot is what the diary files the entry under.
mt = n3.add_meal("Chili", calories=1200)
check("editing only a macro keeps the meal-time", mt["meal"], "dinner")
check("  ... and the macro edit still took", mt["calories"], 300.0)

# ---- an explicit 0 is a real value, not an absence ------------------------
n4 = store()
n4.add_meal("Water", calories=5, protein_g=1)
z = n4.add_meal("Water", calories=0)
check("an explicit 0 overwrites (None and 0 are different inputs)", z["calories"], 0.0)
check("  ... and leaves the field nobody mentioned alone", z["protein_g"], 1.0)

# ---- supplying macros for an ingredient meal ------------------------------
# Ingredients are provenance, summed once at write time and never re-derived on
# read, so the list survives a direct macro edit rather than being destroyed by
# it. The stored macros were always the source of truth.
n5 = store()
n5.add_meal("Chili", ingredients=CHILI, servings=4)
o = n5.add_meal("Chili", calories=1600)
check("a direct macro edit updates the macro", o["calories"], 400.0)
check("  ... and keeps the ingredient list", [i["name"] for i in o["ingredients"]], ["beef", "beans"])

# ---- re-supplying ingredients replaces them -------------------------------
n6 = store()
n6.add_meal("Chili", ingredients=CHILI, servings=4)
r = n6.add_meal("Chili", ingredients=[{"name": "beef", "calories": 800, "protein_g": 60}])
check("re-supplying ingredients replaces the list", [i["name"] for i in r["ingredients"]], ["beef"])
check("  ... and recomputes from them, keeping the old servings", r["calories"], 200.0)

# ---- guards that already worked, kept so a rewrite cannot drop them --------
n7 = store()
check("a nameless meal is refused", n7.add_meal(""), {})
check("zero servings falls back to 1 rather than dividing by zero",
      n7.add_meal("Odd", calories=100, servings=0)["servings"], 1.0)
check("an unparseable servings falls back to 1",
      n7.add_meal("Odd2", calories=100, servings="four")["servings"], 1.0)
check("an unknown meal-time is dropped, not stored",
      n7.add_meal("Odd3", calories=1, meal="brunch")["meal"], "")
nd = store()
nd.add_meal("Dup", calories=10)
nd.add_meal("DUP", calories=20)
dups = nd.meals()
check("matching is case-insensitive, so this updates rather than duplicates", len(dups), 1)
check("  ... the new value wins", dups[0]["calories"], 20.0)
# The update rewrites `name`, so the last casing supplied is the one stored.
# Asserted because it is a real (if small) side effect of saving: "DUP" renames
# the meal, it does not merely update it.
check("  ... and the last casing supplied is the stored name", dups[0]["name"], "DUP")

# ---- the consequence the bug actually had: logging a blanked meal ----------
n8 = store()
n8.add_meal("Chili", ingredients=CHILI, servings=4, meal="dinner")
n8.add_meal("Chili", meal="lunch")          # the innocent edit
logged = n8.log_meal("Chili", portions=2)
# log_meal returns {meal, portions, entry} - assert on the diary ENTRY, which
# is the thing that lands in the daily total and was reading 0.
check("logging after a partial edit records real calories, not 0",
      logged["entry"]["calories"] if logged else None, 600.0)
check("  ... and the entry inherits the edited meal-time", logged["entry"]["meal"], "lunch")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED")
    sys.exit(1)
print("all checks passed")
