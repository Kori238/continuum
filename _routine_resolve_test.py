"""Resolving a routine by title, when Jake owns two with the same title.

His live db has a morning 'Teeth' and an evening 'Teeth' - brushing twice a
day, an entirely reasonable thing to have written down twice. `resolve()`
returned the first exact match, so the evening one had been unreachable by
voice since the day it was created: 26 had a completion, 27 had none.

The correct rule was already four lines below in the same function - the
substring branch refuses a plural match - it had simply never been applied to
the exact branch.

Both directions matter, so both are here:

  * plural exact set must REFUSE rather than silently pick one, and
  * an exact match must still win outright over its own substring neighbours,
    or 'Teeth' goes ambiguous the moment a 'Teeth whitening' exists and the
    fix becomes worse than the bug.
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="continuum-routine-resolve-"))
os.environ["CONTINUUM_ROUTINES_DB"] = str(TMP / "routines.db")
sys.path.insert(0, str(Path(__file__).parent))

from continuum.config import Config              # noqa: E402
from continuum.routines import RoutineStore      # noqa: E402
from continuum.tools import ToolLoop, ROUTINE_TOOLS  # noqa: E402

cfg = Config.load()
assert str(Path(cfg.routines_db).resolve()).startswith(str(TMP.resolve())), \
    "REFUSING TO RUN - routines_db is not in the sandbox"

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


class FakeBus:
    def __init__(self):
        self.events = []

    async def publish(self, event):
        self.events.append(event)


r = RoutineStore(cfg)
loop = ToolLoop(cfg, routines=r, bus=FakeBus())


def run(name, **args):
    return asyncio.run(loop._routine_action(name, args))


# ---- Jake's actual shape ----------------------------------------------------
morning = r.add("Teeth", section="morning")
evening = r.add("Teeth", section="evening")
solo = r.add("Shower", section="morning")
truthy("two routines can share a title", morning != evening)

# ---- store: candidates ------------------------------------------------------
check("both Teeth are candidates", sorted(c["id"] for c in r.candidates("Teeth")),
      sorted([morning, evening]))
check("section narrows to one", [c["id"] for c in r.candidates("Teeth", "evening")],
      [evening])
check("a section with no match yields nothing", r.candidates("Teeth", "night"), [])
check("unknown title yields nothing", r.candidates("Flossing"), [])
check("empty title yields nothing", r.candidates(""), [])

# ---- store: resolve ---------------------------------------------------------
check("THE BUG: an ambiguous title refuses rather than picking one",
      r.resolve("Teeth"), None)
check("the evening Teeth IS reachable when named", r.resolve("Teeth", "evening"), evening)
check("the morning Teeth is still reachable", r.resolve("Teeth", "morning"), morning)
check("an unambiguous title needs no section", r.resolve("Shower"), solo)
check("resolve is case-insensitive", r.resolve("shower"), solo)
check("substring still resolves", r.resolve("show"), solo)

# ---- the other direction: exact must beat substring -------------------------
# Without this, the fix is worse than the bug: every title that happens to be a
# prefix of a longer one would go permanently ambiguous.
whitening = r.add("Teeth whitening", section="evening")
check("exact wins over its own substring neighbour", r.resolve("Shower"), solo)
check("an exact singleton beats a longer sibling",
      r.resolve("Teeth whitening"), whitening)
check("candidates('Teeth') stays the two exact ones, not three",
      sorted(c["id"] for c in r.candidates("Teeth")), sorted([morning, evening]))
check("a substring matching several is still refused", r.resolve("Teeth w"), whitening)
r.delete(whitening)

# ---- the section argument is normalised, not matched raw --------------------
# Found by the negative control: every check above passes a canonical section,
# so `want = section` (no normalising) was invisible. A model writing "Evening"
# or " evening " is entirely ordinary and must still land.
check("a capitalised section still narrows", r.resolve("Teeth", "Evening"), evening)
check("a padded section still narrows", r.resolve("Teeth", "  evening "), evening)
# And the trap underneath it: _norm_section maps anything it doesn't recognise
# to "anytime", so a bad section is a valid-LOOKING filter, not a no-op.
check("an unrecognised section becomes 'anytime', it does not widen",
      r.candidates("Teeth", "teatime"), [])

# ---- through the tool executor ----------------------------------------------
out = run("complete_routine", title="Teeth")
truthy("the tool refuses the ambiguous title", "2 routines" in out)
truthy("...and names the sections it found", "morning" in out and "evening" in out)
truthy("...and says how to disambiguate", "in_section" in out)

out = run("complete_routine", title="Teeth", in_section="evening")
truthy("in_section reaches the evening one", "Teeth" in out and "done" in out)
done_ids = [i["id"] for g in r.today().get("sections", [])
            for i in g["items"] if i.get("done")]
check("THE FIX, END TO END: the evening Teeth is the one that got checked off",
      done_ids, [evening])

out = run("complete_routine", title="Teeth", in_section="morning")
done_ids = sorted(i["id"] for g in r.today().get("sections", [])
                  for i in g["items"] if i.get("done"))
check("and the morning one is independently reachable",
      done_ids, sorted([morning, evening]))

out = run("complete_routine", title="Brushing")
truthy("a title matching nothing still says so plainly", "no routine matching" in out)

# A section that filters everything out is NOT "no such routine" — that is the
# same dead end, one layer up, and a mistyped in_section lands here silently
# because _norm_section turns anything unknown into a real 'anytime' filter.
out = run("complete_routine", title="Teeth", in_section="night")
truthy("a wrong section says the routine exists elsewhere", "no 'Teeth' in night" in out)
truthy("...and names where it actually is", "morning" in out and "evening" in out)
out = run("complete_routine", title="Brushing", in_section="night")
truthy("a title that exists nowhere is still a plain not-found",
       "no routine matching" in out)

out = run("skip_routine", title="Teeth")
truthy("skip refuses too", "2 routines" in out)
out = run("remove_routine", title="Teeth")
truthy("remove refuses too", "2 routines" in out)
check("...and removed nothing", len(r.candidates("Teeth")), 2)
out = run("edit_routine", title="Teeth", note="floss first")
truthy("edit refuses too", "2 routines" in out)

# edit_routine's own `section` means "move it THERE", so the two must not be
# confused: in_section picks the subject, section is the change.
out = run("edit_routine", title="Teeth", in_section="evening", section="night")
truthy("edit can pick one and move it elsewhere", "Teeth" in out)
check("the evening Teeth moved to night", r.candidates("Teeth", "night")[0]["id"], evening)
check("the morning Teeth did not move", r.candidates("Teeth", "morning")[0]["id"], morning)
run("edit_routine", title="Teeth", in_section="night", section="evening")

# ---- subtasks ---------------------------------------------------------------
out = run("add_subtask", routine="Teeth", title="floss")
truthy("add_subtask refuses an ambiguous parent", "2 routines" in out)
out = run("add_subtask", routine="Teeth", in_section="evening", title="floss")
truthy("add_subtask reaches the named parent", "floss" in out)
subs = [i.get("subtasks") or [] for g in r.today().get("sections", [])
        for i in g["items"] if i["id"] == evening]
check("the subtask landed on the evening one", [s["title"] for s in subs[0]], ["floss"])

# A named-but-unresolvable parent must not fall back to None, which means
# "search every routine" - a failed narrowing that silently widens.
out = run("complete_subtask", title="floss", routine="Teeth")
truthy("complete_subtask refuses an ambiguous parent instead of widening",
       "2 routines" in out)
out = run("complete_subtask", title="floss", routine="Teeth", in_section="evening")
truthy("complete_subtask works once the parent is named", "floss" in out)

# ---- the schemas actually offer the disambiguator ---------------------------
# The error message tells the model to use `in_section`; if the schema doesn't
# declare it the advice is unfollowable and the refusal is a dead end.
for fn in ("edit_routine", "complete_routine", "skip_routine", "remove_routine",
           "add_subtask", "complete_subtask"):
    spec = next(t["function"] for t in ROUTINE_TOOLS if t["function"]["name"] == fn)
    truthy(f"{fn} declares in_section", "in_section" in spec["parameters"]["properties"])

print()
print(f"{'FAILED: ' + ', '.join(FAILED) if FAILED else 'all checks passed'}")
sys.exit(1 if FAILED else 0)
