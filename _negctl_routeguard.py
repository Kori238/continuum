"""Negative control for _routeguard_test.py.

Both defects are a route mishandling a store's refusal, so the mutations that
matter most are the ones that put each mishandling back: the missing try/except
that turned a 400 into a 500, and the `or 0` that turned a missing field into an
immediate block. If the suite does not go red for those two, it is decoration.

The second family is the over-correction, which is the obvious wrong fix for
both: refuse everything. A good PATCH must still store and an explicit
minutes=0 must still be honoured, so those get mutations too.

SUITES deliberately excludes _budget_api_test.py even though it also covers the
allowance route. That suite runs the app under `with TestClient`, which fires
startup and drives the real state loop - and this control runs its suites ~40
times. The point of this file is to prove _routeguard_test.py can fail; if a
mutation is caught only by the other suite, MISSED is the honest answer, not a
reason to borrow its coverage.

One mutation is a DELIBERATE SURVIVOR: the parenthetical in the refusal message.
The suite asserts the message names the field and must have no opinion about the
hint after it, or every reword goes red.

PYTHONDONTWRITEBYTECODE is forced for the children: .pyc reuse keys on
(mtime-in-whole-seconds, size), so a same-length mutation written inside the
same second can silently run the ORIGINAL code and report a phantom survivor.
"""
import io
import os
import subprocess
import sys

SV = "continuum/app/server.py"
BG = "continuum/budget.py"
RT = "continuum/routines.py"

MUTATIONS = [
    # ---- the shipped bug: a refusal becomes a 500 -------------------------
    ("THE SHIPPED BUG: patch_routine splats with no guard", SV,
     '        try:\n'
     '            return {"ok": await asyncio.to_thread(routines.update, rid, **fields)}\n'
     '        except ValueError as exc:',
     '        if True:\n'
     '            return {"ok": await asyncio.to_thread(routines.update, rid, **fields)}\n'
     '        except ValueError as exc:'),
    ("the guard catches but answers 200, so the caller believes it", SV,
     '            return JSONResponse({"error": str(exc)}, status_code=400)\n\n'
     '    @app.delete("/api/routines/{rid}")',
     '            return JSONResponse({"error": str(exc)}, status_code=200)\n\n'
     '    @app.delete("/api/routines/{rid}")'),
    ("the guard swallows the refusal and reports ok", SV,
     '        except ValueError as exc:\n'
     '            # update() only started raising when `days` stopped falling back to',
     '        except ValueError as exc:\n'
     '            return {"ok": True}\n'
     '            # update() only started raising when `days` stopped falling back to'),
    ("the id/rid filter is dropped, so a round-tripped body 500s", SV,
     '        fields = {k: v for k, v in body.items() if k not in ("rid", "id")}',
     '        fields = dict(body)'),
    ("update() stops normalising days, so a good PATCH stores raw", RT,
     '                v = _norm_days(v)',
     '                v = v'),

    # ---- the shipped bug: a missing field becomes the harshest value ------
    ("THE SHIPPED BUG: `minutes or 0` collapses missing into a real zero", SV,
     '        minutes = payload.get("minutes")\n'
     '        if minutes is None or minutes == "":',
     '        minutes = payload.get("minutes") or 0\n'
     '        if False:'),
    # This slot first held `int(minutes)` -> `int(payload.get("minutes") or 0)`,
    # which MISSED. That was a real blind spot at the time and the control
    # earned its keep: "" is the ONLY value that clears an `is None` guard while
    # still being falsy, so it was the single input for which the two spellings
    # differed, and the suite did not send it.
    #
    # Closing it changed the verdict. With `== ""` in the guard, the only falsy
    # value still reaching that line is 0, and `0 or 0` is 0 - so the mutation
    # can no longer change behaviour. It did not become uninteresting; it became
    # UNREACHABLE, which is a fact about the fix rather than a gap in the suite.
    # Replaced with the mutation that now covers the same ground: take the new
    # clause back out.
    ("the blank-string case falls out of the guard again", SV,
     '        if minutes is None or minutes == "":',
     '        if minutes is None:'),
    ("the refusal is a 200, so a blocked category reads as saved", SV,
     '            return JSONResponse(\n'
     '                {"error": "minutes is required (send 0 to block the category)"},\n'
     '                status_code=400)',
     '            return JSONResponse(\n'
     '                {"error": "minutes is required (send 0 to block the category)"},\n'
     '                status_code=200)'),
    ("the refusal does not name the field, so the caller cannot recover", SV,
     '                {"error": "minutes is required (send 0 to block the category)"},',
     '                {"error": "bad request"},'),

    # ---- THE OVER-CORRECTION: refusing what was always legal --------------
    ("an explicit minutes=0 is refused too, making a block unreachable", SV,
     '        minutes = payload.get("minutes")\n'
     '        if minutes is None or minutes == "":',
     '        minutes = payload.get("minutes")\n'
     '        if not minutes:'),
    ("a good PATCH is refused along with the bad ones", SV,
     '        try:\n'
     '            return {"ok": await asyncio.to_thread(routines.update, rid, **fields)}',
     '        try:\n'
     '            raise ValueError("nope")\n'
     '            return {"ok": await asyncio.to_thread(routines.update, rid, **fields)}'),
    ("a refused PATCH writes the row anyway before refusing", RT,
     '                v = _norm_days(v)',
     '                try:\n'
     '                    v = _norm_days(v)\n'
     '                except ValueError:\n'
     '                    v = "daily"'),

    # ---- the store guards the route leans on ------------------------------
    ("the cut/raise asymmetry inverts, so a cut waits until tomorrow", BG,
     '            if minutes <= current:',
     '            if minutes > current:'),
    ("a negative allowance stops being refused", BG,
     '        if minutes < 0:\n            raise ValueError("minutes cannot be negative")',
     '        if False:\n            raise ValueError("minutes cannot be negative")'),
    ("override stops refusing 0, so its own `or 0` becomes a silent no-op", BG,
     '        if minutes <= 0:\n            raise ValueError("override minutes must be positive")',
     '        if minutes < 0:\n            raise ValueError("override minutes must be positive")'),
    ("an unknown category stops being refused", BG,
     '        if category not in CATEGORIES:\n'
     '            raise ValueError(f"category must be one of {\', \'.join(CATEGORIES)}")\n'
     '        minutes = int(minutes)',
     '        if False:\n'
     '            raise ValueError(f"category must be one of {\', \'.join(CATEGORIES)}")\n'
     '        minutes = int(minutes)'),

    # ---- deliberate survivor ----------------------------------------------
    ("DIAL: the hint after the required-field message is reworded", SV,
     '                {"error": "minutes is required (send 0 to block the category)"},',
     '                {"error": "minutes is required - 0 blocks the category"},'),
]

EXPECTED_SURVIVORS = {m[0] for m in MUTATIONS if m[0].startswith("DIAL:")}

FILES = (SV, BG, RT)
SUITES = ("_routeguard_test.py",)


def _exact(path):
    """Read with newline="" so CRLF survives the round trip.

    Text mode translates newlines BOTH ways. A control that reads in text mode
    and restores with newline="" silently rewrites a CRLF checkout to LF, and
    its own "restored byte-identical" check re-reads through the same
    translation and cannot see it.
    """
    return io.open(path, encoding="utf-8", newline="").read()


def _anchor(src, text):
    """`text` as it appears in THIS file.

    Anchors are written LF-style. On a CRLF checkout a multi-line anchor then
    matches 0x, which is reported UNREADABLE and counted as a miss - loud, but
    it means the control cannot run anywhere it was deployed.
    """
    if text in src or "\r\n" not in src:
        return text
    return text.replace("\n", "\r\n")


orig = {f: _exact(f) for f in FILES}
env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")


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

good = bad = 0
for label, target, find, repl in MUTATIONS:
    src = orig[target]
    find, repl = _anchor(src, find), _anchor(src, repl)
    if src.count(find) != 1:
        # Not "skip": an anchor that matches 0 or 2 times tested nothing, and
        # printing it as a pass is how a dead control keeps its slot.
        print(f"  UNREADABLE {label}: anchor matches {src.count(find)}x in {target}")
        bad += 1
        continue
    io.open(target, "w", encoding="utf-8", newline="").write(src.replace(find, repl, 1))
    try:
        by = [s for s in SUITES if not run(s)[0]]
    finally:
        io.open(target, "w", encoding="utf-8", newline="").write(src)
    want = label not in EXPECTED_SURVIVORS
    if bool(by) == want:
        print(f"  {'CAUGHT (' + ','.join(by) + ')' if by else 'SURVIVED (as designed)'}  {label}")
        good += 1
    else:
        print(f"  {'OVER-FIT' if by else 'MISSED  '}  {label}")
        bad += 1

print(f"\n{good} correct, {bad} wrong, of {len(MUTATIONS)}"
      f"  ({len(EXPECTED_SURVIVORS)} deliberate survivor)")
clean = all(_exact(f) == orig[f] for f in orig)
print("sources restored byte-identical:", clean)
sys.exit(1 if bad or not clean else 0)
