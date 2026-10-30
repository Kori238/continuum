"""Negative control for _nutrition_test.py and _nutriapi_test.py.

Plants the defects the saved-meal store is supposed to prevent and confirms a
suite goes RED. Mutations span three files because the bug did: the store
decides what "unsupplied" means, and the two callers decide whether the caller
can even express it — `body.get("calories") or 0` collapses None into a real
zero one frame above a perfectly correct store.

Each mutation runs against BOTH suites and is caught if either goes red. That
is deliberate: a store-level mutation should be caught by the store suite, a
caller-level one only by the API suite, and requiring the right suite to catch
the right mutation is how a caller-level blind spot stays visible.

PYTHONDONTWRITEBYTECODE is forced for the children: .pyc reuse keys on
(mtime-in-whole-seconds, size), so a same-length mutation written inside the
same second can silently run the ORIGINAL code and report a phantom survivor.
"""
import io
import os
import subprocess
import sys

STORE = "continuum/nutrition.py"
SERVER = "continuum/app/server.py"
TOOLS = "continuum/tools.py"

MUTATIONS = [
    # ---- the store: "unsupplied" stops meaning "leave it alone" -----------
    ("an unsupplied field falls back to the default instead of the stored row", STORE,
     "            if supplied is not None:\n                return supplied\n            return row[column] if row else default",
     "            if supplied is not None:\n                return supplied\n            return default"),
    ("an unsupplied macro is written as 0, blanking the meal", STORE,
     '            if v is None:\n                return _num(float((row[column] if row else 0) or 0))',
     '            if v is None:\n                return _num(0.0)'),
    ("a kept macro is re-divided, shrinking the meal on every edit", STORE,
     '            if v is None:\n                return _num(float((row[column] if row else 0) or 0))',
     '            if v is None:\n                return _num(float((row[column] if row else 0) or 0) / servings)'),
    ("an unsupplied ingredient list is dropped rather than kept", STORE,
     '            ing_json = (row["ingredients"] if row else "") or ""',
     '            ing_json = ""'),
    ("an unsupplied meal-time is blanked rather than kept", STORE,
     '        if not supplied_meal:\n            meal = (row["meal"] if row else "") or ""',
     '        if not supplied_meal:\n            meal = meal'),
    # The other direction: a "fix" that keeps everything can never be edited.
    ("a supplied field is ignored, so nothing can be edited at all", STORE,
     "            if supplied is not None:\n                return supplied",
     "            if False:\n                return supplied"),
    ("an explicit 0 is treated as absent, so a macro cannot be zeroed", STORE,
     "        def per(v: Any, column: str) -> float:\n            if v is None:",
     "        def per(v: Any, column: str) -> float:\n            if not v:"),
    ("re-supplied ingredients no longer replace the stored list", STORE,
     '            ing_json = json.dumps(ingredients, ensure_ascii=False)',
     '            ing_json = (row["ingredients"] if row else "") or ""'),
    ("zero servings divides by zero instead of falling back to 1", STORE,
     "        if servings <= 0:\n            servings = 1.0",
     "        if servings < 0:\n            servings = 1.0"),
    # Anchored on the `now =` line above it: find_meal runs the byte-identical
    # query 90 lines down, so the bare string matches 2x.
    ("the upsert stops matching case-insensitively, so an edit forks the meal", STORE,
     '        now = time.time()\n        row = self._db.execute(\n'
     '            "SELECT * FROM meals WHERE name = ? COLLATE NOCASE", (name,)).fetchone()',
     '        now = time.time()\n        row = self._db.execute(\n'
     '            "SELECT * FROM meals WHERE name = ?", (name,)).fetchone()'),

    # ---- the route: the caller cannot express "unsupplied" ---------------
    # Anchored across three lines on purpose. The one-line form is a substring
    # of set_nutrition_goals' deeper-indented identical argument line twelve
    # lines up, so it matched 2x and reported UNREADABLE - which is the control
    # working, but it tested nothing until the anchor got long enough to be
    # unique. `ingredients=` is what distinguishes the meal site from goals.
    ("the route collapses a missing calories into a real 0", SERVER,
     '            calories=body.get("calories"), protein_g=body.get("protein_g"),\n'
     '            carbs_g=body.get("carbs_g"), fat_g=body.get("fat_g"),\n'
     '            ingredients=body.get("ingredients") or None,',
     '            calories=body.get("calories") or 0, protein_g=body.get("protein_g"),\n'
     '            carbs_g=body.get("carbs_g"), fat_g=body.get("fat_g"),\n'
     '            ingredients=body.get("ingredients") or None,'),
    ("the route collapses a missing servings into 1", SERVER,
     '            servings=body.get("servings"),',
     '            servings=body.get("servings") or 1,'),
    ("the route turns a missing meal-time into an empty string", SERVER,
     '            meal=None if body.get("meal") is None else str(body.get("meal")))',
     '            meal=str(body.get("meal") or ""))'),

    # ---- the tool: same collapse, different door -------------------------
    # Both of these survived the first run, because neither suite drove
    # tools.py at all - the route was covered and the door Jake actually
    # talks through was not. _nutritools_test.py exists because of this.
    ("the create_meal tool collapses a missing calories into a real 0", TOOLS,
     '                calories=args.get("calories"), protein_g=args.get("protein_g"),',
     '                calories=args.get("calories") or 0, protein_g=args.get("protein_g"),'),
    ("the create_meal tool turns a missing meal-time into an empty string", TOOLS,
     '                meal=None if args.get("meal") is None else str(args.get("meal")))',
     '                meal=str(args.get("meal") or ""))'),
]

FILES = (STORE, SERVER, TOOLS)


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
SUITES = ("_nutrition_test.py", "_nutriapi_test.py", "_nutritools_test.py")


def run(suite):
    r = subprocess.run([sys.executable, suite], capture_output=True, env=env)
    return r.returncode == 0, r.stdout + r.stderr


# Ground truth first: an UNMUTATED tree must pass both, or every CAUGHT is noise.
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
        print(f"  UNREADABLE {label}: anchor matches {src.count(find)}x in {target}")
        missed += 1
        continue
    io.open(target, "w", encoding="utf-8", newline="").write(src.replace(find, repl, 1))
    try:
        by = [s for s in SUITES if not run(s)[0]]
    finally:
        io.open(target, "w", encoding="utf-8", newline="").write(src)
    if by:
        print(f"  CAUGHT  {label}  [{', '.join(s.replace('_test.py', '') for s in by)}]")
        caught += 1
    else:
        print(f"  MISSED  {label}")
        missed += 1

print(f"\n{caught} caught, {missed} missed, of {len(MUTATIONS)}")
clean = all(_exact(f) == orig[f] for f in orig)
print("sources restored byte-identical:", clean)
sys.exit(1 if missed or not clean else 0)
