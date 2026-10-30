"""Does the Flashcards tab's JS actually match its HTML and CSS?

Three contracts that are invisible until something silently does nothing:

  1. every $("id") the block reaches must exist in index.html (a missing id is
     `null`, and `null.addEventListener` only throws if that line ever runs -
     the wiring is all guarded by `if ($("x"))`, so a typo just goes quiet);
  2. every element the markup declares should be reachable, or it is dead HTML;
  3. every class the JS puts into innerHTML should have a rule, or it renders
     unstyled - which looks like a layout bug, not a missing selector.

Static only. It cannot say the tab WORKS; it says the three files are talking
about the same names. Jake clicks the rest.
"""
import io
import re
import sys

HTML = io.open("continuum/app/static/index.html", encoding="utf-8").read()
JS = io.open("continuum/app/static/app.js", encoding="utf-8").read()
CSS = io.open("continuum/app/static/app.css", encoding="utf-8").read()

FAILED = []


def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"\n        got={got!r}\n       want={want!r}"))
    if not ok:
        FAILED.append(label)


# ---- isolate the three spans ---------------------------------------------
def span(text, start, end, what):
    a, b = text.find(start), text.find(end)
    assert a > 0 and b > a, f"could not isolate the {what} span"
    return text[a:b]


view = span(HTML, '<section class="view" id="view-flashcards">',
            '<!-- ===================== ALARMS', "html")
js = span(JS, "// ===================== FLASHCARDS =====================",
          "// ===================== POWER", "js")
css = span(CSS, "/* ===================== FLASHCARDS ===================== */",
           "/* ---- toasts ----", "css")
# A zero-length span would make every set below empty and every check pass
# vacuously - the "0 groups reports UNREADABLE, never ok" rule.
for name, text in (("html", view), ("js", js), ("css", css)):
    check(f"the {name} span is readable", len(text) > 500, True)
if FAILED:
    sys.exit("spans unreadable - the checks below would be vacuous")

# ---- 1. ids the JS reaches vs ids the HTML declares ----------------------
html_ids = set(re.findall(r'id="([^"]+)"', view))
# ids the JS creates itself, then reads back - they are never in the markup
js_made_ids = set(re.findall(r'id="(fc-[^"]+)"', js))
js_reads = set(re.findall(r'\$\("([^"]+)"\)', js))

missing = sorted(js_reads - html_ids - js_made_ids)
check("every $(id) the JS reads exists in the markup or is made by the JS", missing, [])

# fc-in / fc-check are rendered per question; fc-imp-save inside the drafts box
check("the JS-created ids are the per-question ones only", sorted(js_made_ids),
      ["fc-check", "fc-done-back", "fc-gcard", "fc-imp-save", "fc-in", "fc-show"])

# ---- 2. markup that nothing reaches --------------------------------------
# An id is "reached" by $("id"), by a CSS rule, or by being a container the JS
# fills. Anything else is markup that does nothing.
unreached = sorted(i for i in html_ids
                   if i not in js_reads and f"#{i}" not in css and i != "view-flashcards")
check("no declared id is unreachable", unreached, [])

# ---- 3. classes the JS renders vs classes the CSS styles ----------------
# Needles come from class="..." inside the JS template literals, including the
# conditional `${...}` forms, plus classList add/toggle/remove.
# Strip the interpolations BEFORE matching the attribute, not after. A
# conditional class holds its own quotes - class="fc-card${x ? y : ""}" - and
# [^"]* stops dead at the empty-string branch, so matching first yields shrapnel
# like '${ok' and '===' and silently loses the real class names.
js_flat = re.sub(r"\$\{[^{}]*\}", " ", js)
js_classes = set()
for blob in re.findall(r'class="([^"]*)"', js_flat):
    js_classes.update(w for w in blob.split() if w)
for m in re.findall(r'classList\.(?:add|toggle|remove)\("([^"]+)"', js):
    js_classes.add(m)
# and the ones the markup itself declares
for blob in re.findall(r'class="([^"]*)"', view):
    js_classes.update(blob.split())

# Haystack is the WHOLE stylesheet, not just the flashcards span: .panel, .view
# and .empty are house-wide and styling them locally would be the bug.
styled = set(re.findall(r"\.([a-zA-Z][\w-]*)", CSS))
unstyled = sorted(c for c in js_classes if c not in styled)
check("every class rendered has a rule somewhere in the stylesheet", unstyled, [])

# The check above is blind to a class that arrives through ${...} - stripping the
# interpolation is what makes the literal ones readable, and it takes the
# computed ones with it. That blind spot is where the real bug lived: matching
# tiles were `class="fc-tile ${side}"`, side is "sv" or "en", and "en" is also a
# GENDER class, so every English tile rendered with the en-word border.
#
# So assert the list of class slots filled from a variable rather than deriving
# it. Deriving would re-admit the next one silently; this way a new computed
# class fails here and has to be looked at once.
computed = sorted(set(re.findall(r'class="[^"]*\s\$\{([^{}]*)\}', js)))
check("class slots filled from a variable are the reviewed ones", computed, [
    'escapeHtml(c.gender)',   # fc-g: the gender pill, en|ett - the intended use
    'g.cls',                  # fc-grade: "", "again", "good" - from _FC_GRADES
    'ok ? "ok" : "no"',       # fc-verdict on a typed answer
])

# ---- 4. the routes the JS calls must be the ones the server declares ------
server = io.open("continuum/app/server.py", encoding="utf-8").read()
declared = set(re.findall(r'@app\.\w+\("(/api/flashcards[^"]*)"', server))
called = set()
for u in re.findall(r'"(/api/flashcards[^"`]*)"', js) + re.findall(r'`(/api/flashcards[^`]*)`', js):
    # normalise the template-literal params back to the declared path shape
    u = re.sub(r"\$\{[^}]*\}", "{card_id}", u).split("?")[0].rstrip("/")
    called.add(u)
# The audio route is built by concatenation ("/api/flashcards/audio/" + name),
# so the literal is a bare prefix rather than a whole path. Name the real one
# and drop the prefix, or the probe reports its own parsing as a bad route.
called.discard("/api/flashcards/audio")
called.add("/api/flashcards/audio/{name:path}")
unknown = sorted(u for u in called
                 if u not in declared and u.replace("{card_id}", "{name:path}") not in declared)
check("every route the tab calls is declared by the server", unknown, [])

print()
if FAILED:
    print(f"{len(FAILED)} FAILED")
    sys.exit(1)
print("all checks passed")
