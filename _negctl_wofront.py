"""Negative control for _womuscle_ui_test.mjs.

The feature this guards exists because a roll-up was computed, stored, summed
and consumed by nobody. A front-end suite is unusually good at re-creating that
shape: every render assertion can stay green while the call site is gone, and
nothing on the page moves. So the first mutation deletes the call from
`loadWorkouts`, and mutations 3-6 attack the three files separately - a class
the JS emits with no rule in the CSS, a rule with no paint in it, an id the
HTML never declares. Each of those ships a chart that is technically rendered
and literally invisible.

Two mutations are DELIBERATE SURVIVORS, listed in EXPECTED_SURVIVORS: the bar
height and the row gap are taste, and a suite with an opinion about them would
fail every time someone nudged the spacing. A control that demands 100% caught
pushes you to over-fit; naming the exceptions is how they stay honest.

The JS/CSS/HTML here are LF in the worktree and CRLF in `git archive` output,
so anchors go through _anchor() and every write uses newline="" - reading in
text mode and restoring with newline="" is what silently converted
continuum/workouts.py to LF on the hub while printing "byte-identical: True".
"""
import io
import os
import subprocess
import sys

JS = "continuum/app/static/app.js"
CSS = "continuum/app/static/app.css"
HTML = "continuum/app/static/index.html"

MUTATIONS = [
    # ---- the bug this feature exists to not repeat ------------------------
    ("THE SHIPPED SHAPE: nothing ever calls the renderer", JS,
     "  renderMuscles(stats.muscles || []);\n",
     ""),
    ("the renderer is called with a field that is not the distribution", JS,
     "  renderMuscles(stats.muscles || []);",
     "  renderMuscles(stats.series || []);"),
    ("the [] default is dropped, so a payload without muscles throws", JS,
     "  renderMuscles(stats.muscles || []);",
     "  renderMuscles(stats.muscles);"),

    # ---- rendered but invisible: the three files disagreeing ---------------
    ("the JS emits a class the stylesheet has never heard of", JS,
     '`<span class="wo-mtrack">',
     '`<span class="wo-track">'),
    ("the fill loses its paint and every bar is a clear rectangle", CSS,
     ".wo-mfill { display: block; height: 100%; border-radius: 0 3px 3px 0; background:",
     ".wo-mfill { display: block; height: 100%; border-radius: 0 3px 3px 0; no-paint:"),
    ("the track stops flexing, so every bar is a percentage of nothing", CSS,
     ".wo-mtrack { flex: 1 1 auto;",
     ".wo-mtrack { flex: 0 0 auto;"),
    ("the track stops clipping and full bars spill past the corners", CSS,
     "background: rgba(255,255,255,.045); overflow: hidden; }",
     "background: rgba(255,255,255,.045); }"),
    ("the container the renderer writes into is not in the page", HTML,
     '<div class="wo-muscles" id="wo-muscles">',
     '<div class="wo-muscles" id="wo-muscle">'),
    ("the box ships bare, with no empty state before the first fetch", HTML,
     '<div class="wo-muscles" id="wo-muscles"><div class="empty">no muscle data in this window</div></div>',
     '<div class="wo-muscles" id="wo-muscles"></div>'),
    ("the container itself is unstyled - no panel, no padding", CSS,
     ".wo-muscles { display: flex;",
     ".wo-muscle-box { display: flex;"),

    # ---- the arithmetic ----------------------------------------------------
    ("bar length is scaled to the total, so nothing ever fills the track", JS,
     "    const w = Math.max(3, Math.round((100 * m.sets) / max));",
     "    const w = Math.max(3, Math.round((100 * m.sets) / total));"),
    ("the tooltip percentage is taken against the max, not the window", JS,
     "    const pct = total ? Math.round((100 * m.sets) / total) : 0;",
     "    const pct = total ? Math.round((100 * m.sets) / max) : 0;"),
    ("the floor goes, and a small group becomes a hairline", JS,
     "    const w = Math.max(3, Math.round((100 * m.sets) / max));",
     "    const w = Math.round((100 * m.sets) / max);"),
    ("the max is taken over the wrong field and every bar is identical", JS,
     "  const max = Math.max(...rows.map((m) => m.sets), 1);",
     "  const max = 100;"),
    ("the total is summed wrong, so every percentage is wrong", JS,
     "  const total = rows.reduce((n, m) => n + m.sets, 0);",
     "  const total = rows.length;"),

    # ---- the honesty of the labels ----------------------------------------
    ("the UI prettifies an unmapped id into a guessed muscle name", JS,
     "      `<span class=\"wo-mname\">${escapeHtml(m.name)}</span>` +",
     "      `<span class=\"wo-mname\">${escapeHtml(m.name.replace(/^part \\d+$/, \"Other\"))}</span>` +"),
    ("the name is interpolated unescaped", JS,
     "      `<span class=\"wo-mname\">${escapeHtml(m.name)}</span>` +",
     "      `<span class=\"wo-mname\">${m.name}</span>` +"),
    ("the set count stops reaching the screen", JS,
     "      `<span class=\"wo-mval\">${m.sets}</span>` +",
     "      `<span class=\"wo-mval\"></span>` +"),
    ("a single set reads '1 sets'", JS,
     "${m.sets} set${m.sets === 1 ? \"\" : \"s\"}",
     "${m.sets} sets"),
    ("the UI re-sorts, taking a second opinion on the agent's ranking", JS,
     "  box.innerHTML = rows.map((m) => {",
     "  box.innerHTML = rows.slice().sort((a, b) => a.name < b.name ? -1 : 1).map((m) => {"),
    ("the empty state is dropped and an empty window renders a blank box", JS,
     '  if (!rows.length) { box.innerHTML = \'<div class="empty">no muscle data in this window</div>\'; return; }\n',
     ""),

    # ---- the dataviz rule: nominal groups, one hue -------------------------
    ("a value-ramp is re-introduced, shading bars by the length they already show", JS,
     '      `<span class="wo-mtrack"><span class="wo-mfill" style="width:${w}%"></span></span>` +',
     '      `<span class="wo-mtrack"><span class="wo-mfill" style="width:${w}%;opacity:${0.4 + w / 200}"></span></span>` +'),

    # ---- deliberate survivors: dials the suite must not care about ---------
    ("DIAL: the bars are a little taller", CSS,
     ".wo-mtrack { flex: 1 1 auto; height: 9px;",
     ".wo-mtrack { flex: 1 1 auto; height: 11px;"),
    ("DIAL: the rows sit a little further apart", CSS,
     ".wo-muscles { display: flex; flex-direction: column; gap: 3px;",
     ".wo-muscles { display: flex; flex-direction: column; gap: 5px;"),
]

EXPECTED_SURVIVORS = {m[0] for m in MUTATIONS if m[0].startswith("DIAL:")}

FILES = (JS, CSS, HTML)
SUITES = ("_womuscle_ui_test.mjs",)


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
    r = subprocess.run(["node", suite], capture_output=True, env=env)
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
        caught = any(not run(s)[0] for s in SUITES)
    finally:
        io.open(target, "w", encoding="utf-8", newline="").write(src)
    want = label not in EXPECTED_SURVIVORS
    if caught == want:
        print(f"  {'CAUGHT ' if caught else 'SURVIVED (as designed)'}  {label}")
        good += 1
    else:
        print(f"  {'OVER-FIT' if caught else 'MISSED  '}  {label}")
        bad += 1

print(f"\n{good} correct, {bad} wrong, of {len(MUTATIONS)}"
      f"  ({len(EXPECTED_SURVIVORS)} of them deliberate survivors)")
clean = all(_exact(f) == orig[f] for f in orig)
print("sources restored byte-identical:", clean)
sys.exit(1 if bad or not clean else 0)
