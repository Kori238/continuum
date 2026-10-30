"""Negative control for the three flashcard suites.

Plants the defects `FlashcardStore.add`'s merge is supposed to prevent and
confirms a suite goes RED. Mutations span three files because the bug did: the
store decides what "unsupplied" means, and the two callers decide whether the
caller can even express it — `str(body.get("pos") or "")` collapses None into a
real blank one frame above a perfectly correct store.

Each mutation runs against ALL THREE suites and the catchers are printed. That
attribution column is the actual finding: a store mutation should be caught by
all three, a route mutation only by _fcapi, a tool mutation only by _fctools. A
mutation caught by nothing is a missing door; one caught by the wrong suite
means a suite is reaching past its layer.

Both directions are mutated on purpose. "Unsupplied gets written blank" is the
bug that shipped; "supplied-but-empty is ignored" is the over-correction that
would pass every merge check while making a wrong gender undeletable.

PYTHONDONTWRITEBYTECODE is forced for the children: .pyc reuse keys on
(mtime-in-whole-seconds, size), so a same-length mutation written inside the
same second can silently run the ORIGINAL code and report a phantom survivor.
"""
import io
import os
import subprocess
import sys

STORE = "continuum/flashcards.py"
SERVER = "continuum/app/server.py"
TOOLS = "continuum/tools.py"

MUTATIONS = [
    # ---- the store: "unsupplied" stops meaning "leave it alone" -----------
    # The shipped bug, restored exactly: eight columns written unconditionally.
    ("the update branch writes every soft column, blanking the unsupplied ones", STORE,
     '            self._db.execute(\n'
     '                f"UPDATE cards SET {\', \'.join(f\'{c}=?\' for c in supplied)}, updated=? WHERE id=?",\n'
     '                (*supplied.values(), now, existing["id"]))',
     '            _all = {c: supplied.get(c, d) for c, d in (\n'
     '                ("pos", ""), ("gender", ""), ("tags", "[]"), ("example_sv", ""),\n'
     '                ("example_en", ""), ("forms", "{}"), ("speak", "[]"), ("notes", ""))}\n'
     '            self._db.execute(\n'
     '                f"UPDATE cards SET {\', \'.join(f\'{c}=?\' for c in _all)}, updated=? WHERE id=?",\n'
     '                (*_all.values(), now, existing["id"]))'),
    ("forms alone falls out of the merge", STORE,
     '        if forms is not None:\n            supplied["forms"] = json.dumps(forms, ensure_ascii=False)',
     '        if True:\n            supplied["forms"] = json.dumps(forms or {}, ensure_ascii=False)'),
    ("notes alone falls out of the merge", STORE,
     '        if notes is not None:\n            supplied["notes"] = str(notes).strip()',
     '        if True:\n            supplied["notes"] = str(notes or "").strip()'),
    ("speak alone falls out of the merge", STORE,
     '        if speak is not None:\n            supplied["speak"] = json.dumps(list(speak), ensure_ascii=False)',
     '        if True:\n            supplied["speak"] = json.dumps(list(speak or []), ensure_ascii=False)'),
    ("pos alone falls out of the merge", STORE,
     '        if pos is not None:\n            supplied["pos"] = str(pos).strip().lower()',
     '        if True:\n            supplied["pos"] = str(pos or "").strip().lower()'),
    # The other direction: an "ignore falsy" fix passes every merge check above
    # and makes it impossible to ever clear a field on purpose.
    ("an explicit empty value is treated as unsupplied, so nothing can be cleared", STORE,
     '        if notes is not None:\n            supplied["notes"] = str(notes).strip()',
     '        if notes:\n            supplied["notes"] = str(notes).strip()'),
    ("an explicit empty forms is treated as unsupplied", STORE,
     '        if forms is not None:\n            supplied["forms"] = json.dumps(forms, ensure_ascii=False)',
     '        if forms:\n            supplied["forms"] = json.dumps(forms, ensure_ascii=False)'),
    # A merge that names nothing must not claim the card changed.
    ("a no-op re-add bumps `updated`, so a re-import looks like fresh activity", STORE,
     '            if not supplied:\n                # Nothing named, nothing to say.',
     '            if False:\n                # Nothing named, nothing to say.'),
    # The insert path has nothing to merge with and must still default.
    ("a brand new card inherits the merge blanks wrongly", STORE,
     '        BLANK = {"pos": "", "gender": "", "tags": "[]", "example_sv": "",\n'
     '                 "example_en": "", "forms": "{}", "speak": "[]", "notes": ""}',
     '        BLANK = {"pos": "", "gender": "", "tags": "{}", "example_sv": "",\n'
     '                 "example_en": "", "forms": "[]", "speak": "[]", "notes": ""}'),
    # Merging must never reset scheduling - the reason add() merges at all.
    ("a re-add resets the card to new, undoing weeks of study", STORE,
     '            return self.get(existing["id"])\n        cid = (card_id or "").strip() or new_id()',
     '            s = fresh_srs()\n'
     '            self._db.execute("UPDATE cards SET due=?, interval=?, ease=?, reps=?,'
     ' lapses=?, last_reviewed=? WHERE id=?",\n'
     '                (s["due"], s["interval"], s["ease"], s["reps"], s["lapses"], s["last"],'
     ' existing["id"]))\n'
     '            self._db.commit()\n'
     '            return self.get(existing["id"])\n        cid = (card_id or "").strip() or new_id()'),

    # ---- import_backup: one mapping serving restore AND re-import ----------
    ("import_backup collapses an absent forms into a blank, wiping on re-import", STORE,
     '                tags=c.get("tags"), example_sv=c.get("example_sv"),\n'
     '                example_en=c.get("example_en"), forms=c.get("forms"),',
     '                tags=c.get("tags"), example_sv=c.get("example_sv"),\n'
     '                example_en=c.get("example_en"), forms=c.get("forms") or {},'),
    ("import_backup collapses an absent note into a blank", STORE,
     '                speak=c.get("speak"), notes=c.get("notes"),',
     '                speak=c.get("speak"), notes=c.get("notes") or "",'),

    # ---- the route: the caller cannot express "unsupplied" ---------------
    ("the route collapses a missing pos into a real blank", SERVER,
     '            pos=named("pos"), gender=named("gender"),',
     '            pos=str(body.get("pos") or ""), gender=named("gender"),'),
    ("the route collapses a missing example into a real blank", SERVER,
     '            tags=body.get("tags"), example_sv=named("example_sv"),',
     '            tags=body.get("tags"), example_sv=str(body.get("example_sv") or ""),'),
    ("the route swallows an explicit empty, so the tab cannot clear a field", SERVER,
     '            v = body.get(key)\n            return None if v is None else str(v)',
     '            v = body.get(key)\n            return None if not v else str(v)'),

    # ---- the tool: same collapse, the door Jake actually talks through ----
    ("the add_flashcard tool collapses a missing note into a real blank", TOOLS,
     '                example_sv=named("example_sv"), example_en=named("example_en"),\n'
     '                notes=named("notes"))',
     '                example_sv=named("example_sv"), example_en=named("example_en"),\n'
     '                notes=(args.get("notes") or ""))'),
    ("the add_flashcard tool collapses a missing example into a real blank", TOOLS,
     '                example_sv=named("example_sv"), example_en=named("example_en"),',
     '                example_sv=(args.get("example_sv") or ""), example_en=named("example_en"),'),
    ("the add_flashcard tool blanks pos when the model does not mention it", TOOLS,
     '            pos = None if (p := named("pos")) is None else p.strip().lower()',
     '            pos = (args.get("pos") or "").strip().lower()'),
    ("the add_flashcard tool wipes the tag list when it names no pos or gender", TOOLS,
     '            tags = [t for t in (pos or "", (gender or "") if pos == "noun" else "") if t] or None',
     '            tags = [t for t in (pos or "", (gender or "") if pos == "noun" else "") if t]'),
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
SUITES = ("_fcstore_test.py", "_fcapi_test.py", "_fctools_test.py")


def run(suite):
    r = subprocess.run([sys.executable, suite], capture_output=True, env=env)
    return r.returncode == 0, r.stdout + r.stderr


# Ground truth first: an UNMUTATED tree must pass all three, or every CAUGHT is
# noise. A control whose baseline is red reports a perfect score for nothing.
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
