"""Negative control for _routinedays_test.py.

The defect class here is a failed narrowing that WIDENS, so the mutations that
matter most are the ones that restore a silent fallback. If the suite does not
go red when `_norm_days` answers "daily" instead of raising, the suite is
decoration and the bug is back with a test file standing over it.

The second family is the report. `edit_routine` echoed the value it was SENT,
so a rewritten or rejected `days` still read as a success to the model - the
confirmation and the row disagreed and only the row was true. Mutation 10 puts
that back.

The third is the schema gap the whole thing started as: a field with no
description, which is what made the model guess in the first place.

One mutation is a DELIBERATE SURVIVOR: the wording of the refusal. The suite
asserts the message names the accepted formats, and it must have no opinion
about the sentence around them, or every reword goes red.

PYTHONDONTWRITEBYTECODE is forced for the children: .pyc reuse keys on
(mtime-in-whole-seconds, size), so a same-length mutation written inside the
same second can silently run the ORIGINAL code and report a phantom survivor.
"""
import io
import os
import subprocess
import sys

RT = "continuum/routines.py"
TL = "continuum/tools.py"

MUTATIONS = [
    # ---- the shipped bug: refusal becomes the widest possible value --------
    ("THE SHIPPED BUG: an unparseable token falls back to daily", RT,
     '        else:\n'
     '            raise ValueError(f"don\'t understand the days {days!r} — use {_DAYS_HELP}")',
     '        else:\n'
     '            return "daily"'),
    ("an input that parses to nothing falls back to daily", RT,
     '    if not nums:\n'
     '        raise ValueError(f"don\'t understand the days {days!r} — use {_DAYS_HELP}")',
     '    if not nums:\n'
     '        return "daily"'),
    ("a partial parse keeps the half it understood instead of refusing", RT,
     '        else:\n'
     '            raise ValueError(f"don\'t understand the days {days!r} — use {_DAYS_HELP}")',
     '        else:\n'
     '            continue'),
    # This slot first held `sorted(nums) ... if nums else ""`, which MISSED -
    # and that was a finding about the code, not the suite: the `if not nums`
    # guard four lines above already raised, so the branch was unreachable and
    # no test could ever have covered it. A mutation that cannot change
    # behaviour is not a blind spot; it is a bad mutation. Replaced with one
    # that can.
    ("trailing punctuation from prose turns a good day into a refusal", RT,
     '        tok = tok.strip().strip(".")',
     '        tok = tok.strip()'),

    # ---- THE OVER-CORRECTION: refusing something nobody asked about --------
    ("an omitted value now raises, breaking every add that skips the field", RT,
     '    raw = (days or "").strip().lower()\n'
     '    if not raw:\n'
     '        return "daily"',
     '    raw = (days or "").strip().lower()\n'
     '    if not raw:\n'
     '        raise ValueError("days is required")'),
    ("a preset stops being recognised and is parsed as names", RT,
     '    if raw in _PRESETS:\n        return raw',
     '    if False:\n        return raw'),

    # ---- the table: a wrong day is worse than a refused one ---------------
    ("Monday is retagged, shifting every named day by one", RT,
     '    "mon": 0, "monday": 0,',
     '    "mon": 1, "monday": 1,'),
    ("Sunday is 0, the other convention, silently disagreeing with _applies", RT,
     '    "sun": 6, "sunday": 6,',
     '    "sun": 0, "sunday": 0,'),
    ("single letters are guessed at after all", RT,
     '    "mon": 0, "monday": 0,',
     '    "m": 0, "w": 2, "f": 4,\n    "mon": 0, "monday": 0,'),
    ("an out-of-range number is clamped instead of refused", RT,
     '        if tok.isdigit() and 0 <= int(tok) <= 6:\n            n = int(tok)',
     '        if tok.isdigit():\n            n = min(6, int(tok))'),
    ("plurals stop resolving, so 'mondays' is refused", RT,
     '        elif tok.rstrip("s") in _DAY_NAMES:   # \'mondays\' -> \'monday\'\n'
     '            n = _DAY_NAMES[tok.rstrip("s")]',
     '        elif tok in _DAY_NAMES:\n'
     '            n = _DAY_NAMES[tok]'),
    ("only commas separate, so 'sat sun' and 'mon and thu' are refused", RT,
     '_DAY_SPLIT = re.compile(r"[,/&+;]|\\band\\b|\\s+")',
     '_DAY_SPLIT = re.compile(r",")'),
    ("the result is not sorted, so one pattern has several spellings", RT,
     '    return ",".join(str(n) for n in sorted(nums))',
     '    return ",".join(str(n) for n in nums)'),
    ("a day repeated in the input is stored twice", RT,
     '        if n not in nums:\n            nums.append(n)',
     '        nums.append(n)'),
    ("the refusal names no formats, so the caller cannot recover", RT,
     '_DAYS_HELP = ("\'daily\', \'weekdays\', \'weekends\', weekday names like \'mon,thu\', "\n'
     '              "or weekday numbers with Mon=0 like \'0,3\'")',
     '_DAYS_HELP = "a valid value"'),

    # ---- the report --------------------------------------------------------
    ("THE REPORT BUG: the tool echoes what it was sent, not what was stored", TL,
     '            result = f"updated \'{title}\': " + ", ".join(\n'
     '                f"{k}={row.get(k, v)}" for k, v in fields.items())',
     '            result = f"updated \'{title}\': " + ", ".join(\n'
     '                f"{k}={v}" for k, v in fields.items())'),
    ("a refused edit is reported as a crash instead of an answer", TL,
     '            try:\n'
     '                r.update(rid, **fields)\n'
     '            except ValueError as exc:\n'
     '                return f"could not edit \'{title}\': {exc}"',
     '            r.update(rid, **fields)'),
    ("add_routine stops catching the refusal", TL,
     '            except ValueError as exc:\n'
     '                return f"could not add routine: {exc}"',
     '            except KeyError as exc:\n'
     '                return f"could not add routine: {exc}"'),

    # ---- the schema gap that started it ------------------------------------
    ("edit_routine.days goes back to having no description", TL,
     '            "days": {"type": "string", "description": "which days it recurs: \'daily\', \'weekdays\', \'weekends\', weekday names like \'mon,thu\', or weekday numbers with Mon=0 like \'0,3\'. Single letters (\'M,W,F\') are refused because T and S are ambiguous"},\n'
     '            "repeat_days": {"type": "integer", "description": "repeat every N days after it\'s checked off',
     '            "days": {"type": "string"},\n'
     '            "repeat_days": {"type": "integer", "description": "repeat every N days after it\'s checked off'),
    ("add_routine.days stops documenting the Mon=0 base", TL,
     '            "days": {"type": "string", "description": "which days it recurs: \'daily\' (default), \'weekdays\', \'weekends\', weekday names like \'mon,thu\', or weekday numbers with Mon=0 like \'0,3\'.',
     '            "days": {"type": "string", "description": "which days it recurs: \'daily\' (default), \'weekdays\', \'weekends\', weekday names like \'mon,thu\', or numbers.'),

    # ---- deliberate survivor ----------------------------------------------
    ("DIAL: the refusal is worded differently", RT,
     'raise ValueError(f"don\'t understand the days {days!r} — use {_DAYS_HELP}")\n'
     '        if n not in nums:',
     'raise ValueError(f"unrecognised days {days!r} — use {_DAYS_HELP}")\n'
     '        if n not in nums:'),
]

EXPECTED_SURVIVORS = {m[0] for m in MUTATIONS if m[0].startswith("DIAL:")}

FILES = (RT, TL)
SUITES = ("_routinedays_test.py", "_routine_resolve_test.py")


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
