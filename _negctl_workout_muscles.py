"""Negative control for _workout_muscles_test.py.

The defect being guarded is the quiet kind: not a wrong number but a feature
that renders nothing, forever, while every other workout check stays green.
`stats()["muscles"]` was `[]` on 39 of 39 live sessions and nothing anywhere
failed. So the first mutation is the shipped bug restored verbatim — if the
suite does not go red on `"muscles": {}`, the suite is decoration.

The naming mutations matter as much as the plumbing ones. TRAINING_PARTS was
derived from evidence, and the dangerous failure is not a crash but a confident
wrong label: id 14 has never appeared in Jake's data, so anything that makes an
unknown id borrow a neighbour's name would misattribute his training silently
and indefinitely. Those are mutated in both directions — a guess added, and a
known name removed.

PYTHONDONTWRITEBYTECODE is forced for the children: .pyc reuse keys on
(mtime-in-whole-seconds, size), so a same-length mutation written inside the
same second can silently run the ORIGINAL code and report a phantom survivor.
"""
import io
import os
import subprocess
import sys

SPD = "continuum/integrations/speediance.py"
STORE = "continuum/workouts.py"

MUTATIONS = [
    # ---- the shipped bug, verbatim ----------------------------------------
    ("THE SHIPPED BUG: the list payload is discarded again", SPD,
     '            "muscles": _norm_parts(r.get("trainingPartSetsInfoList")),',
     '            "muscles": {},  # not present at list level'),
    ("the roll-up is read under a plausible wrong key", SPD,
     '            "muscles": _norm_parts(r.get("trainingPartSetsInfoList")),',
     '            "muscles": _norm_parts(r.get("trainingPartList")),'),

    # ---- _norm_parts internals --------------------------------------------
    ("set counts are dropped and every part reports 1", SPD,
     '        sets = int(_num(p.get("sets")))',
     '        sets = 1'),
    ("the part id is ignored and everything lands in one bucket", SPD,
     '        name = part_name(p.get("trainingPartId2"))',
     '        name = "Total"'),
    ("entries sharing a name overwrite instead of accumulating", SPD,
     '        out[name] = out.get(name, 0) + sets',
     '        out[name] = sets'),
    ("a zero-set entry is stored as a real 0", SPD,
     '        if sets <= 0:\n            continue',
     '        if False:\n            continue'),
    ("a non-list payload raises instead of degrading", SPD,
     '    if not isinstance(lst, list):\n        return out',
     '    if False:\n        return out'),
    ("a non-dict entry is no longer skipped", SPD,
     '        if not isinstance(p, dict):\n            continue',
     '        if False:\n            continue'),

    # ---- naming: a confident wrong label is the dangerous failure ---------
    ("an UNMAPPED id guesses a neighbour's name instead of keeping its number", SPD,
     '        return TRAINING_PARTS.get(int(pid)) or f"part {int(pid)}"',
     '        return TRAINING_PARTS.get(int(pid)) or TRAINING_PARTS.get(15) or ""'),
    ("an unknown id renders blank, silently merging with other unknowns", SPD,
     '        return TRAINING_PARTS.get(int(pid)) or f"part {int(pid)}"',
     '        return TRAINING_PARTS.get(int(pid)) or ""'),
    ("a junk id raises rather than degrading", SPD,
     '    except (TypeError, ValueError):\n        return "part ?"',
     '    except KeyError:\n        return "part ?"'),
    ("a numeric-string id stops mapping", SPD,
     '        return TRAINING_PARTS.get(int(pid)) or f"part {int(pid)}"',
     '        return TRAINING_PARTS.get(pid) or f"part {int(pid)}"'),
    # The table itself. The suite spells these out as literals rather than
    # deriving them, so a silent retagging must fail.
    ("a part is retagged to the wrong muscle", SPD,
     '    16: "Arms",',
     '    16: "Core",'),
    ("a part is dropped from the table", SPD,
     '    13: "Back",\n',
     ''),
    ("14 is guessed at after all", SPD,
     '    15: "Legs",',
     '    14: "Glutes",\n    15: "Legs",'),

    # ---- the store end ----------------------------------------------------
    ("the store writes the muscles column blank again", STORE,
     '             json.dumps(s.get("muscles") or {}), time.time()))',
     '             json.dumps({}), time.time()))'),
    ("the conflict path stops refreshing muscles, so stored rows never heal", STORE,
     '                    session_type=excluded.session_type, muscles=excluded.muscles,',
     '                    session_type=excluded.session_type, muscles=sessions.muscles,'),
    ("stats stops summing the distribution", STORE,
     '            for m, c in (s["muscles"] or {}).items():\n'
     '                muscle_totals[m] = muscle_totals.get(m, 0) + int(c or 0)',
     '            for m, c in (s["muscles"] or {}).items():\n'
     '                muscle_totals[m] = int(c or 0)'),
    ("the distribution stops being sorted by volume", STORE,
     '                         key=lambda m: m["sets"], reverse=True)',
     '                         key=lambda m: m["name"])'),
]

FILES = (SPD, STORE)


def _exact(path):
    """Read with newline="" so CRLF survives the round trip.

    Text mode translates newlines BOTH ways. A control that reads in text mode
    and restores with newline="" silently rewrites a CRLF checkout to LF, and
    its own "restored byte-identical" check re-reads through the same
    translation and cannot see it. That happened on the hub, to
    continuum/workouts.py, while this printed True.
    """
    return io.open(path, encoding="utf-8", newline="").read()


def _anchor(src, text):
    """`text` as it appears in THIS file.

    Anchors are written with \n. On a CRLF checkout a multi-line anchor then
    matches 0x, which is reported UNREADABLE and counted as a miss - loud, but
    it means the control cannot run anywhere it was deployed.
    """
    if text in src or "\r\n" not in src:
        return text
    return text.replace("\n", "\r\n")


orig = {f: _exact(f) for f in FILES}
env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
SUITES = ("_workout_muscles_test.py",)


def run(suite):
    r = subprocess.run([sys.executable, suite], capture_output=True, env=env)
    return r.returncode == 0, r.stdout + r.stderr


# Ground truth first: an UNMUTATED tree must pass, or every CAUGHT is noise.
for s in SUITES:
    ok, out = run(s)
    print(f"baseline {s}: {'PASS' if ok else 'FAIL'}")
    if not ok:
        print(out.decode("utf-8", "replace")[-2000:])
        sys.exit("baseline is red - controls would be meaningless")

caught = missed = 0
for label, target, find, repl in MUTATIONS:
    src = orig[target]
    find, repl = _anchor(src, find), _anchor(src, repl)
    if src.count(find) != 1:
        # Not "skip": an anchor that matches 0 or 2 times tested nothing, and
        # printing it as a pass is how a dead control keeps its slot.
        print(f"  UNREADABLE {label}: anchor matches {src.count(find)}x in {target}")
        missed += 1
        continue
    io.open(target, "w", encoding="utf-8", newline="").write(src.replace(find, repl, 1))
    try:
        by = [s for s in SUITES if not run(s)[0]]
    finally:
        io.open(target, "w", encoding="utf-8", newline="").write(src)
    if by:
        print(f"  CAUGHT  {label}")
        caught += 1
    else:
        print(f"  MISSED  {label}")
        missed += 1

print(f"\n{caught} caught, {missed} missed, of {len(MUTATIONS)}")
clean = all(_exact(f) == orig[f] for f in orig)
print("sources restored byte-identical:", clean)
sys.exit(1 if missed or not clean else 0)
