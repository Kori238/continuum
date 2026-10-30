"""Negative control for _routine_resolve_test.py.

The suite passed 41/41 on its first run, which proves nothing on its own — a
check that cannot fail reads exactly like one that held. So plant the defects
back, one at a time, and confirm it goes red for each.

Two files because the bug spans two layers: `resolve()` decides whether an
ambiguous title is refused, and `_routine_action` decides whether the refusal
reaches the model as something it can act on or as a dead "no routine matching".
A store fix with a caller that reports it as not-found is a worse product than
the silent wrong answer, because now nothing works at all.

Both directions are mutated on purpose:

  * the shipped bug — a plural exact set silently picks one, and
  * the over-correction — exact stops beating substring, so 'Teeth' goes
    ambiguous the moment 'Teeth whitening' exists and Jake's working command
    breaks in exchange for fixing the broken one.

And the schema mutations matter as much as the logic ones: the error message
instructs the model to pass `in_section`, so removing the property from the
schema leaves the advice unfollowable while every logic check stays green.

PYTHONDONTWRITEBYTECODE is forced for the children: .pyc reuse keys on
(mtime-in-whole-seconds, size), so a same-length mutation written inside the
same second can silently run the ORIGINAL code and report a phantom survivor.
"""
import io
import os
import subprocess
import sys

STORE = "continuum/routines.py"
TOOLS = "continuum/tools.py"

MUTATIONS = [
    # ---- the store: the bug exactly as it shipped -------------------------
    ("THE SHIPPED BUG: the exact branch returns the first row it walks past", STORE,
     '        hits = self.candidates(name, section)\n'
     '        return hits[0]["id"] if len(hits) == 1 else None',
     '        name_l = (name or "").strip().lower()\n'
     '        for h in self.candidates(name, section):\n'
     '            if h["title"].strip().lower() == name_l:\n'
     '                return h["id"]\n'
     '        hits = self.candidates(name, section)\n'
     '        return hits[0]["id"] if len(hits) == 1 else None'),
    ("resolve picks the first of ANY plural match, substring included", STORE,
     '        return hits[0]["id"] if len(hits) == 1 else None',
     '        return hits[0]["id"] if hits else None'),
    # The other direction. This passes every "ambiguous is refused" check while
    # breaking the command Jake uses today.
    ("THE OVER-CORRECTION: exact stops beating substring, so any prefix is ambiguous",
     STORE,
     '        exact = [r for r in rows if r["title"].strip().lower() == name]\n'
     '        return exact or [r for r in rows if name in r["title"].strip().lower()]',
     '        return [r for r in rows if name in r["title"].strip().lower()]'),
    ("exact match is dropped entirely, leaving only substring", STORE,
     '        exact = [r for r in rows if r["title"].strip().lower() == name]',
     '        exact = []'),
    # The section filter is the whole disambiguator; without it in_section is
    # decoration and the evening Teeth stays unreachable.
    ("the section filter is ignored, so in_section narrows nothing", STORE,
     '        if (section or "").strip():\n'
     '            want = _norm_section(section)\n'
     '            rows = [r for r in rows if (r["section"] or "") == want]',
     '        if False:\n'
     '            want = _norm_section(section)\n'
     '            rows = [r for r in rows if (r["section"] or "") == want]'),
    ("the section filter matches raw text instead of the normalised form", STORE,
     '            want = _norm_section(section)',
     '            want = section'),
    ("an empty title matches every routine instead of nothing", STORE,
     '        if not name:\n            return []',
     '        if False:\n            return []'),
    ("title matching becomes case-sensitive", STORE,
     '        exact = [r for r in rows if r["title"].strip().lower() == name]\n'
     '        return exact or [r for r in rows if name in r["title"].strip().lower()]',
     '        exact = [r for r in rows if r["title"].strip() == name]\n'
     '        return exact or [r for r in rows if name in r["title"].strip()]'),

    # ---- the caller: a refusal the model cannot act on --------------------
    ("the refusal is reported as 'not found', so the model never retries", TOOLS,
     '            hits = r.candidates(what, sec)',
     '            hits = []'),
    ("the error never mentions in_section, so the advice is missing", TOOLS,
     '            return None, (f"\'{what}\' matches {len(hits)} routines ({where}) — say which "\n'
     '                          f"with in_section, or use a fuller title")',
     '            return None, (f"\'{what}\' matches {len(hits)} routines ({where}) — "\n'
     '                          f"use a fuller title")'),
    ("the error never names the sections, so there is nothing to choose between", TOOLS,
     '            where = ", ".join(h["section"] or "anytime" for h in hits)',
     '            where = "?"'),
    ("in_section is read under the wrong key and silently does nothing", TOOLS,
     '            sec = str(args.get("in_section") or "")',
     '            sec = ""'),
    ("find() consults `section` instead, colliding with edit_routine's move-to", TOOLS,
     '            sec = str(args.get("in_section") or "")',
     '            sec = str(args.get("section") or "")'),
    # A failed narrowing that widens is worse than one that fails.
    ("an unresolvable subtask parent degrades to None, widening the search", TOOLS,
     '            parent = None\n'
     '            if args.get("routine"):\n'
     '                parent, err = find(args["routine"])\n'
     '                if parent is None:\n'
     '                    return err',
     '            parent = find(args.get("routine", ""))[0] if args.get("routine") else None'),

    # ---- the schemas: advice the model cannot follow ----------------------
    ("complete_routine loses the in_section property the error tells it to use", TOOLS,
     '            "title": {"type": "string"},\n'
     '            "in_section": {"type": "string", "enum": ["morning", "afternoon", "evening", "night", "anytime"],\n'
     '                           "description": "only if the title is ambiguous: which part of day the one you mean sits in. Jake has a morning \'Teeth\' and an evening \'Teeth\'; this is how you say which. The tool tells you when it needs this."},\n'
     '            "done": {"type": "boolean", "description": "true to mark done (default), false to un-check"}},',
     '            "title": {"type": "string"},\n'
     '            "done": {"type": "boolean", "description": "true to mark done (default), false to un-check"}},'),
    # A wrong section must not read as "no such routine".
    ("a section that filters everything out reports as a plain not-found", TOOLS,
     '                wider = r.candidates(what) if sec else []',
     '                wider = []'),
    ("the wrong-section message stops naming where the routine actually is", TOOLS,
     '                    where = ", ".join(h["section"] or "anytime" for h in wider)',
     '                    where = "?"'),

    ("add_subtask loses in_section, so an ambiguous parent is a dead end", TOOLS,
     '            "in_section": {"type": "string", "enum": ["morning", "afternoon", "evening", "night", "anytime"],\n'
     '                           "description": "only if the parent title is ambiguous: which part of day it sits in. "\n'
     '                                          "Jake has a morning \'Teeth\' and an evening \'Teeth\'; this is how you say "\n'
     '                                          "which. The tool tells you when it needs this."},\n'
     '            "title": {"type": "string", "description": "the subtask"}},',
     '            "title": {"type": "string", "description": "the subtask"}},'),
]

FILES = (STORE, TOOLS)


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
SUITES = ("_routine_resolve_test.py",)


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
        print(f"  CAUGHT  {label}  [{', '.join(s.replace('_test.py', '') for s in by)}]")
        caught += 1
    else:
        print(f"  MISSED  {label}")
        missed += 1

print(f"\n{caught} caught, {missed} missed, of {len(MUTATIONS)}")
clean = all(_exact(f) == orig[f] for f in orig)
print("sources restored byte-identical:", clean)
sys.exit(1 if missed or not clean else 0)
