"""Negative control for _fcsmoke_probe.py: plant a defect the store is supposed
to prevent, confirm the suite goes RED, restore. A mutation that survives means
the suite has a blind spot exactly there.

PYTHONDONTWRITEBYTECODE is forced for the child: .pyc reuse keys on
(mtime-in-whole-seconds, size), so a same-length mutation written inside the same
second can silently run the ORIGINAL code and report a phantom survivor.
"""
import io, os, subprocess, sys

TARGET = "continuum/flashcards.py"
MUTATIONS = [
    ("import skips the ms->s conversion",
     'float(srs.get("due", 0)) / 1000.0,      # ms -> s, the one conversion',
     'float(srs.get("due", 0)),'),
    ("audio stored inline in the row",
     "path.write_bytes(data)",
     "path.write_bytes(data); name = __import__('base64').b64encode(data).decode()"),
    ("Hard multiplier neutralised",
     's["interval"] = max(1.0, round(s["interval"] * 0.7))',
     's["interval"] = max(1.0, round(s["interval"] * 1.0))'),
    ("lapse keeps its interval",
     's["interval"] = 0.0\n        s["due"] = now + _RELEARN_S',
     's["due"] = now + _RELEARN_S'),
    ("lapse comes back tomorrow, not in 10min",
     "_RELEARN_S = 10 * 60",
     "_RELEARN_S = 86400"),
    ("delete orphans the audio files",
     "self._audio_path(name).unlink(missing_ok=True)",
     "pass"),
    ("dedupe ignores the bucket",
     """source COLLATE NOCASE, set_name COLLATE NOCASE,
                   swedish COLLATE NOCASE, english COLLATE NOCASE)""",
     "swedish COLLATE NOCASE, english COLLATE NOCASE)"),
    ("ease floor removed",
     's["ease"] = max(_EASE_FLOOR,',
     's["ease"] = min(99.0,'),
    ("review does not append to the log",
     """self._db.execute(
            \"\"\"INSERT INTO reviews(card_id, ts, quality, mode, interval_after, ease_after)
               VALUES(?,?,?,?,?,?)\"\"\",""",
     """self._db.execute(
            \"\"\"SELECT ?,?,?,?,?,?\"\"\","""),
    ("second Good no longer jumps to 6 days",
     's["interval"] = 6.0',
     's["interval"] = 2.0'),
    ("new cards are not due immediately",
     '"due": _now(), "interval": 0.0',
     '"due": _now() + 86400, "interval": 0.0'),
    ("path escape guard defeated",
     'if not str(p).startswith(str(self._audio_dir.resolve())):',
     'if False:'),
    ("populated legacy table dropped anyway",
     "if n:", "if False:"),
    ("re-add resets scheduling",
     "if existing:",
     "if existing and False:"),
]


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


orig = _exact(TARGET)
env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")

def suite_passes():
    r = subprocess.run([sys.executable, "_fcsmoke_probe.py"], capture_output=True, env=env)
    return r.returncode == 0, r.stdout

# Ground truth first: an UNMUTATED tree must pass, or every CAUGHT below is noise.
base, out = suite_passes()
print(f"baseline (no mutation): {'PASS' if base else 'FAIL'}")
if not base:
    print(out.decode("utf-8", "replace")[-1500:])
    sys.exit("baseline is red - controls would be meaningless")

caught = missed = 0
for label, find, repl in MUTATIONS:
    find, repl = _anchor(orig, find), _anchor(orig, repl)
    if find not in orig:
        print(f"  UNREADABLE {label}: anchor not found")
        missed += 1
        continue
    if orig.count(find) != 1:
        print(f"  UNREADABLE {label}: anchor matches {orig.count(find)}x")
        missed += 1
        continue
    io.open(TARGET, "w", encoding="utf-8", newline="").write(orig.replace(find, repl, 1))
    try:
        passed, _ = suite_passes()
    finally:
        io.open(TARGET, "w", encoding="utf-8", newline="").write(orig)
    if passed:
        print(f"  MISSED  {label}")
        missed += 1
    else:
        print(f"  CAUGHT  {label}")
        caught += 1

print(f"\n{caught} caught, {missed} missed, of {len(MUTATIONS)}")
after = _exact(TARGET)
print("source restored byte-identical:", after == orig)
sys.exit(1 if missed or after != orig else 0)
