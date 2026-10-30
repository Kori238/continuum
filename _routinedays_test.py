"""A recurrence that cannot be parsed must not become "every day".

`edit_routine`'s `days` was the one field in the routine schemas with neither a
description nor an enum, so the model guessed at the wire format. `_norm_days`
then fell back to "daily" for anything it did not recognise - and "daily" is not
a neutral default, it is the WIDEST value the field has. So a failed narrowing
widened: an item asked to run on two days ran on seven. Measured before the fix,
8 of 10 phrasings a model reaches for did exactly that ('Mon,Thu',
'monday,thursday', 'Tue/Thu', 'mondays and thursdays', ...), and `edit_routine`
answered "updated 'Gym': days=Mon,Thu" while the row said 'daily'.

Same family as the 'Teeth' bug in _routine_resolve_test.py: a lookup that cannot
honour its constraint returning the unconstrained answer rather than refusing.

Three things are asserted, and the third is the one that was missing:

  * the common phrasings parse, so the fix is mostly "understand more" and only
    rarely "refuse";
  * genuinely ambiguous input RAISES rather than widening - and single letters
    are ambiguous on purpose, because 'T' is Tuesday or Thursday;
  * the tool reports the value that was STORED, not the one it was sent, or a
    rejected narrowing still reads to the model as a success.

The over-correction is asserted too: an EMPTY value must still mean "daily".
That is a default nobody asked to override, not a request that failed, and a
fix that refuses it would break every add_routine call that omits the field.
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="continuum-routinedays-"))
os.environ["CONTINUUM_ROUTINES_DB"] = str(TMP / "routines.db")
sys.path.insert(0, str(Path(__file__).parent))

from continuum.config import Config                        # noqa: E402
from continuum.routines import RoutineStore, _norm_days, _applies  # noqa: E402
from continuum.tools import ToolLoop, ROUTINE_TOOLS        # noqa: E402

cfg = Config.load()
assert str(Path(cfg.routines_db).resolve()).startswith(str(TMP.resolve())), \
    "REFUSING TO RUN - routines_db is not in the sandbox"

FAILED = []


def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"   got {got!r} want {want!r}"))
    if not ok:
        FAILED.append(label)


def refuses(label, value):
    try:
        _norm_days(value)
    except ValueError as exc:
        ok = "daily" in str(exc) and "mon,thu" in str(exc)
        print(f"{'PASS' if ok else 'FAIL'}  {label}"
              + ("" if ok else f"   raised, but the message names no formats: {exc}"))
        if not ok:
            FAILED.append(label)
        return
    print(f"FAIL  {label}   did not raise")
    FAILED.append(label)


# ---- the phrasings that used to widen ------------------------------------
# Spelled out rather than generated from _DAY_NAMES: deriving the expectation
# from the table under test asserts self-consistency, not correctness.
for sent, want in [
    ("Mon,Thu", "0,3"),
    ("mon,thu", "0,3"),
    ("monday,thursday", "0,3"),
    ("Monday, Thursday", "0,3"),
    ("Tue/Thu", "1,3"),
    ("mondays and thursdays", "0,3"),
    ("tues & weds", "1,2"),
    ("sat sun", "5,6"),
    ("THURSDAY", "3"),
]:
    check(f"{sent!r} narrows to {want}", _norm_days(sent), want)

# ---- the formats that already worked, unchanged ---------------------------
check("'daily' is preserved", _norm_days("daily"), "daily")
check("'weekdays' is preserved", _norm_days("weekdays"), "weekdays")
check("'weekends' is preserved", _norm_days("weekends"), "weekends")
check("numeric CSV is preserved", _norm_days("1,4"), "1,4")
check("numeric CSV is sorted, so one pattern has one spelling", _norm_days("4,1"), "1,4")
check("a repeated day is not stored twice", _norm_days("mon,monday,0"), "0")
check("Mon is 0, matching datetime.weekday() and the schema", _norm_days("mon"), "0")
check("Sun is 6, not 0 - the other common convention", _norm_days("sun"), "6")

# ---- THE OVER-CORRECTION: an omitted value is not a failed request --------
check("empty means daily", _norm_days(""), "daily")
check("None means daily", _norm_days(None), "daily")
check("whitespace means daily", _norm_days("   "), "daily")

# ---- what must now refuse -------------------------------------------------
refuses("a single letter is refused, because T is Tuesday or Thursday", "M,W,F")
refuses("...even when every letter happens to be unambiguous", "M,W")
refuses("'weekly' names no day at all", "weekly")
refuses("an out-of-range number is refused, not clamped", "9")
refuses("a negative number is refused", "-1")
refuses("prose is refused", "whenever I feel like it")
refuses("a typo'd name is refused rather than dropped", "mondy,thurs")

# A partial parse must refuse WHOLE. Keeping the half it understood is the same
# defect wearing a smaller hat - the caller asked for three days and got two.
refuses("one bad token refuses the whole value", "mon,thu,funday")

# Separators only. Every token is skipped as empty, so the loop finishes without
# ever raising and lands on the "parsed to nothing" guard - a second, separate
# route to the same widening, which the token-level refusal above cannot cover.
refuses("a value of nothing but separators is refused", ",")
refuses("...including one that is only a word-separator", "and")
refuses("...and several", ",,,")

# Trailing punctuation from prose must not turn a good day into a refusal.
check("a trailing period is tolerated", _norm_days("mon, thu."), "0,3")

# ---- the store and the tool ----------------------------------------------
store = RoutineStore(cfg)
loop = ToolLoop.__new__(ToolLoop)
loop._routines = store
loop._bus = None


def run(name, **args):
    return asyncio.run(loop._routine_action(name, args))


rid = store.add("Gym", days="daily")
run("edit_routine", title="Gym", days="Mon,Thu")
row = next(r for r in store.all() if r["id"] == rid)
check("the stored row really narrowed", row["days"], "0,3")
check("...and it is off on Tuesday", _applies(row["days"], 1), False)
check("...and on on Thursday", _applies(row["days"], 3), True)

said = run("edit_routine", title="Gym", days="tue/fri")
row = next(r for r in store.all() if r["id"] == rid)
check("the tool reports the STORED value, not the one it was sent",
      f"days={row['days']}" in said, True)
check("...which for 'tue/fri' is 1,4", row["days"], "1,4")

before = next(r for r in store.all() if r["id"] == rid)["days"]
said = run("edit_routine", title="Gym", days="M,W,F")
after = next(r for r in store.all() if r["id"] == rid)["days"]
check("a refused edit leaves the row alone", after, before)
check("...and says so instead of claiming success", said.startswith("could not edit"), True)
check("...naming the formats the caller should use", "weekday names" in said, True)

# add_routine takes the same path; it already caught ValueError for a blank
# title, so the refusal needs no new plumbing - but that must be true, not assumed.
said = run("add_routine", title="Yoga", days="someday")
check("add_routine refuses the same input", said.startswith("could not add routine"), True)
check("...and no row was created", [r for r in store.all() if r["title"] == "Yoga"], [])
run("add_routine", title="Yoga", days="sun")
check("...while a good one is created",
      next(r["days"] for r in store.all() if r["title"] == "Yoga"), "6")

# ---- the schemas ----------------------------------------------------------
# The bug started as a documentation gap: the model was guessing because the
# field said nothing. Assert that no routine field is left undescribed.
#
# Not "every field must have a description" - that would be a rule I invented,
# and it is false: `title`, `note` and `done` are free text and a boolean whose
# name IS the spec. `days` was different because its VALUE FORMAT is not
# guessable from the name, which is exactly why the model guessed wrong.
#
# So the allowlist below is the set of fields whose name is a sufficient spec,
# and the assertion is equality in BOTH directions: a new field with an
# unguessable format and no description fails, and so does deleting a
# description from one that has one.
SELF_DESCRIBING = {
    "add_routine.title", "edit_routine.new_title", "edit_routine.note",
    "complete_routine.title", "skip_routine.title", "remove_routine.title",
    "complete_subtask.title", "complete_subtask.done",
    "add_todo.title", "add_todo.note",
    "complete_todo.title", "complete_todo.done", "remove_todo.title",
}
undocumented = set()
for t in ROUTINE_TOOLS:
    fn = t["function"]
    for field, spec in (fn.get("parameters", {}).get("properties", {}) or {}).items():
        if not spec.get("description") and not spec.get("enum"):
            undocumented.add(f"{fn['name']}.{field}")
check("no routine field with an unguessable format is left undescribed",
      sorted(undocumented - SELF_DESCRIBING), [])
check("...and none of the self-describing ones quietly lost a description",
      sorted(SELF_DESCRIBING - undocumented), [])

for fn_name in ("add_routine", "edit_routine"):
    spec = next(t["function"]["parameters"]["properties"]["days"]
                for t in ROUTINE_TOOLS if t["function"]["name"] == fn_name)
    d = spec.get("description", "")
    check(f"{fn_name}.days documents the name form", "mon,thu" in d, True)
    check(f"{fn_name}.days documents Mon=0", "Mon=0" in d, True)

print()
print(f"FAILED: {FAILED}" if FAILED else "all checks passed")
sys.exit(1 if FAILED else 0)
