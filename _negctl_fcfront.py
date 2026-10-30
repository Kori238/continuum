"""Negative control for _fcfront_probe.py.

The probe is nine green checks over three files it only ever reads. Nothing in
that arrangement proves a single check can go red - a regex that matches nothing
reports exactly like a contract that holds. So plant, per check, the defect that
check exists to find.

Two of these are defects I actually shipped and the probe actually caught:
the dead fc-autoplay checkbox (nothing read it) and the matching tile whose
`${side}` value "en" collided with the en/ett gender classes. They are here so
that neither can come back quietly.
"""
import io
import os
import subprocess
import sys

HTML = "continuum/app/static/index.html"
JS = "continuum/app/static/app.js"
CSS = "continuum/app/static/app.css"

MUTATIONS = [
    # ---- check: every $(id) the JS reads exists ---------------------------
    ("the markup renames a control the JS still reaches for", HTML,
     '<button class="fc-start" id="fc-start">',
     '<button class="fc-start" id="fc-begin">'),
    ("the JS reaches for an id nobody declares", JS,
     'if ($("fc-start")) $("fc-start").addEventListener("click", _fcStart);',
     'if ($("fc-ghost")) $("fc-ghost").addEventListener("click", _fcStart);'),

    # ---- check: the JS-created ids are the per-question ones only ---------
    # A new JS-made id is not wrong by itself; it is wrong SILENTLY, so the
    # list is asserted rather than derived.
    ("the JS starts minting an id the list has not reviewed", JS,
     '<div class="fc-forms">',
     '<div class="fc-forms" id="fc-newthing">'),

    # ---- check: no declared id is unreachable -----------------------------
    # This is the fc-autoplay class of bug: markup that looks wired and is not.
    ("the markup grows an id that nothing reads", HTML,
     '<button class="fc-quit" id="fc-quit">',
     '<button class="fc-quit" id="fc-quit" data-x="1"><span id="fc-orphan"></span>'),

    # ---- check: every class rendered has a rule ---------------------------
    ("the JS renders a class the stylesheet has never heard of", JS,
     '<div class="fc-forms">',
     '<div class="fc-forms fc-forms-wide">'),
    # .fc-reveal, not .fc-forms: fc-forms is styled twice (once inside a media
    # query), so renaming one rule leaves the class styled and the mutation
    # never creates the defect it is named after. A MISSED there would have been
    # the control's fault, not the probe's - pick a class with exactly one rule.
    ("the stylesheet loses the rule for a class the JS renders", CSS,
     ".fc-reveal { border-top:",
     ".fc-revealed { border-top:"),

    # ---- check: class slots filled from a variable ------------------------
    # The shipped bug, verbatim: side is "sv"|"en", and "en" is a GENDER class,
    # so every English tile rendered with the en-word border.
    ("the matching tile puts its side back into the class attribute", JS,
     'class="fc-tile${side === "sv" ? _fcGenderClass(c) : ""}"',
     'class="fc-tile ${side}${side === "sv" ? _fcGenderClass(c) : ""}"'),

    # ---- check: every route the tab calls is declared ---------------------
    ("the tab calls a study route the server does not serve", JS,
     'await jread("/api/flashcards/study"',
     'await jread("/api/flashcards/studies"'),

    # ---- check: the spans are readable ------------------------------------
    # Without this the probe would happily diff three empty strings and call it
    # nine passes - "0 groups reports UNREADABLE, never ok".
    ("the JS span loses its end marker, so every check goes vacuous", JS,
     "// ===================== POWER",
     "// ===================== POWR"),
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


orig = {f: _exact(f) for f in (HTML, JS, CSS)}
env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")


def probe_passes():
    r = subprocess.run([sys.executable, "_fcfront_probe.py"], capture_output=True, env=env)
    return r.returncode == 0, r.stdout + r.stderr


base, out = probe_passes()
print(f"baseline (no mutation): {'PASS' if base else 'FAIL'}")
if not base:
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
        passed, _ = probe_passes()
    finally:
        io.open(target, "w", encoding="utf-8", newline="").write(src)
    if passed:
        print(f"  MISSED  {label}")
        missed += 1
    else:
        print(f"  CAUGHT  {label}")
        caught += 1

print(f"\n{caught} caught, {missed} missed, of {len(MUTATIONS)}")
clean = all(_exact(f) == orig[f] for f in orig)
print("sources restored byte-identical:", clean)
sys.exit(1 if missed or not clean else 0)
