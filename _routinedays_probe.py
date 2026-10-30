"""How often does "only these days" silently become "every day"?

`edit_routine`'s `days` is the one field in the routine schemas with neither a
description nor an enum, so the model is guessing at the wire format. This asks
what happens to each guess - not by reading `_norm_days`, but by driving the
real tool and then reading the stored row back.

The shape under suspicion is the one already on record: a failed NARROWING that
widens. "daily" is not a neutral fallback - it is the widest value the field
has, so an unparseable request to restrict an item to two days turns it on for
all seven, and the tool still answers "updated".
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="continuum-routinedays-"))
os.environ["CONTINUUM_ROUTINES_DB"] = str(TMP / "routines.db")
sys.path.insert(0, str(Path(__file__).parent))

from continuum.config import Config              # noqa: E402
from continuum.routines import RoutineStore, _applies  # noqa: E402
from continuum.tools import ToolLoop, ROUTINE_TOOLS    # noqa: E402

cfg = Config.load()
assert str(Path(cfg.routines_db).resolve()).startswith(str(TMP.resolve())), \
    "REFUSING TO RUN - routines_db is not in the sandbox"

store = RoutineStore(cfg)
loop = ToolLoop.__new__(ToolLoop)
loop._routines = store
loop._bus = None        # the handler publishes an event; nothing here listens

# Phrasings a model reaches for when the schema tells it nothing. The last two
# are the documented format and are the control: if THEY widen, the probe is
# broken rather than the code.
GUESSES = [
    "Mon,Thu", "mon,thu", "monday,thursday", "Monday, Thursday",
    "Tue/Thu", "M,W,F", "mondays and thursdays", "weekly",
    "1,4", "weekdays",
]

print(f"{'sent':<24} {'stored':<12} {'active on':<22} verdict")
print("-" * 78)
widened = []
for g in GUESSES:
    rid = store.add(f"probe {g}", days="1,4")   # start NARROW, so widening shows
    try:
        store.update(rid, days=g)
    except ValueError:
        # A refusal is the GOOD outcome for something genuinely ambiguous: it is
        # loud, the caller is told the formats, and the item keeps the days it
        # had. Only a silent widening is the defect.
        row = next(r for r in store.all() if r["id"] == rid)
        print(f"{g!r:<24} {row['days']:<12} {'(unchanged)':<22} refused, loudly")
        continue
    row = next(r for r in store.all() if r["id"] == rid)
    on = [d for d in range(7) if _applies(row["days"], d)]
    names = "".join("MTWTFSS"[d] if d in on else "." for d in range(7))
    bad = len(on) == 7 and g not in ("daily",)
    if bad:
        widened.append(g)
    print(f"{g!r:<24} {row['days']:<12} {names} ({len(on)}/7){'':<7} "
          f"{'WIDENED TO EVERY DAY' if bad else 'ok'}")

print()
print(f"{len(widened)} of {len(GUESSES)} guesses silently turned a 2-3 day item into a daily one:")
for g in widened:
    print(f"  {g!r}")

# The other half: what the tool TELLS the caller it did.
print("\n--- what edit_routine reports back ---")
store.add("Gym", days="1,4")
for g in ("Mon,Thu", "1,4"):
    said = asyncio.run(loop._routine_action("edit_routine", {"title": "Gym", "days": g}))
    row = next(r for r in store.all() if r["title"] == "Gym")
    # NOT `stored != sent`: normalising 'Mon,Thu' to '0,3' is correct and
    # expected. The defect is the REPORT disagreeing with the row, so compare
    # what the tool said against what the row holds.
    flag = "" if f"days={row['days']}" in said else "  <-- the report disagrees with the row"
    print(f"  sent {g!r:<12} -> said {said!r}")
    print(f"  {'':<17}    stored {row['days']!r}{flag}")
