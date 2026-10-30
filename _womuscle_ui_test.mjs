// Tests the SHIPPED renderMuscles by slicing it out of app.js and running it
// against a stub DOM, then checks the classes it emits are actually styled and
// the id it writes into actually exists.
//
// The bug this whole feature came from was "authored, valid, reaching nothing":
// the muscle roll-up was computed and stored and summed and then consumed by
// nobody. So the wiring checks here matter as much as the rendering ones - a
// renderMuscles that is never called from loadWorkouts is the same bug wearing
// a different hat.
//
//   node _womuscle_ui_test.mjs
import { readFileSync } from "node:fs";

const SRC = readFileSync("continuum/app/static/app.js", "utf8").replace(/\r\n/g, "\n");
const CSS = readFileSync("continuum/app/static/app.css", "utf8").replace(/\r\n/g, "\n");
const HTML = readFileSync("continuum/app/static/index.html", "utf8").replace(/\r\n/g, "\n");

function slice(name) {
  const re = new RegExp(`^(?:async )?(?:function ${name}\\(|const ${name} = )`, "m");
  const m = re.exec(SRC);
  if (!m) throw new Error(`could not find ${name} in app.js`);
  let i = SRC.indexOf("{", m.index), depth = 0, end = -1;
  for (let k = i; k < SRC.length; k++) {
    if (SRC[k] === "{") depth++;
    else if (SRC[k] === "}") { depth--; if (!depth) { end = k + 1; break; } }
  }
  if (end < 0) throw new Error(`unbalanced braces for ${name}`);
  return SRC.slice(m.index, end);
}

// ---- stub DOM -------------------------------------------------------------
const NODES = {};
const el = (id) => (NODES[id] ||= { id, innerHTML: "", textContent: "" });
const document = { getElementById: el };
const $ = el;

const src = [slice("escapeHtml"), slice("renderMuscles")].join("\n\n");
const { renderMuscles } = new Function(
  "document", "$", src + "\nreturn { renderMuscles };",
)(document, $);

// ---- assertions -----------------------------------------------------------
const FAILS = [];
function ok(label, cond, extra = "") {
  console.log(`${cond ? "PASS" : "FAIL"}  ${label}${cond ? "" : "   " + extra}`);
  if (!cond) FAILS.push(label);
}
const out = () => el("wo-muscles").innerHTML;
const widths = () => [...out().matchAll(/wo-mfill" style="width:(\d+)%/g)].map((m) => +m[1]);

// The live shape, straight off /api/workouts/stats on the hub.
const LIVE = [
  { name: "Chest", sets: 18 }, { name: "Arms", sets: 12 }, { name: "Back", sets: 11 },
  { name: "Shoulders", sets: 7 }, { name: "Legs", sets: 3 }, { name: "Core", sets: 1 },
];

renderMuscles(LIVE);
ok("THE POINT: a populated distribution renders bars, not the empty state",
  out().includes("wo-mfill") && !out().includes('class="empty"'));
ok("every group gets a row", (out().match(/wo-mrow/g) || []).length === 6);
ok("names reach the screen", LIVE.every((m) => out().includes(`>${m.name}<`)));
ok("set counts reach the screen", out().includes(">18<") && out().includes(">1<"));

// Width is relative to the MAX (so the top bar fills the track); the tooltip
// percentage is of the TOTAL. Two different denominators, easy to cross-wire.
ok("the largest group fills the track", widths()[0] === 100, JSON.stringify(widths()));
ok("widths are scaled to the max, not the total",
  widths()[1] === Math.round((100 * 12) / 18), JSON.stringify(widths()));
ok("the tooltip percentage is of the TOTAL, not the max",
  out().includes(`${Math.round((100 * 18) / 52)}% of the window`));
ok("...and the two denominators really do differ here", Math.round((100 * 18) / 52) !== 100);

// The floor. NOT assertable against LIVE: 1 set of 18 is 6%, already above the
// floor, so the whole clause could be deleted and this fixture would not notice.
// A fixture too small cannot see the bug - it needs a spread wide enough that the
// floor actually binds.
renderMuscles([{ name: "Chest", sets: 60 }, { name: "Core", sets: 1 }]);
ok("a tiny group is floored to a visible bar, not a hairline",
  widths()[1] === 3, JSON.stringify(widths()));
ok("...and the fixture is wide enough that the floor really binds",
  Math.round((100 * 1) / 60) < 3);

// The API sorts by sets descending. Re-sorting in the UI would be a second
// opinion that can silently disagree with the agent's "Most-worked:" line.
renderMuscles(LIVE);  // every assertion reads one shared node, so re-establish it
ok("row order is preserved exactly as given",
  [...out().matchAll(/wo-mname">([^<]+)</g)].map((m) => m[1]).join(",") ===
  LIVE.map((m) => m.name).join(","));

// One hue for every bar: the groups are nominal, so a darker-where-bigger ramp
// would double-encode the length. No per-row colour may be emitted.
ok("no per-row colour is emitted - bar length is the only encoding",
  !/wo-mfill[^>]*(background|color|opacity|filter)\s*:/.test(out()));

renderMuscles([]);
ok("an empty distribution shows the empty state, not a blank box",
  out().includes('class="empty"') && !out().includes("wo-mfill"));

// An id with no evidenced name renders as "part 14" on purpose. If the UI
// prettified that, a guess would reach the screen wearing a real muscle's name.
renderMuscles([{ name: "part 14", sets: 2 }]);
ok("an UNMAPPED id keeps its number on screen", out().includes(">part 14<"));

renderMuscles([{ name: "Core", sets: 1 }]);
ok("one set reads '1 set', not '1 sets'", out().includes("1 set ·"));
renderMuscles([{ name: "Core", sets: 2 }]);
ok("two sets read '2 sets'", out().includes("2 sets ·"));

renderMuscles([{ name: '<img src=x onerror=1>', sets: 1 }]);
ok("a hostile name is escaped", !out().includes("<img src=x"));

// ---- the parts that only fail on screen ------------------------------------
renderMuscles(LIVE);
const emitted = [...new Set([...out().matchAll(/class="([^"${}]+)"/g)]
  .flatMap((m) => m[1].split(/\s+/)))].filter((c) => c.startsWith("wo-"));
ok("renderMuscles emits the classes we think it does",
  emitted.sort().join(",") === "wo-mfill,wo-mname,wo-mrow,wo-mtrack,wo-mval", emitted.join(","));
for (const c of emitted) {
  ok(`.${c} is actually styled`, new RegExp(`\\.${c}[\\s,:{]`).test(CSS));
}

// "A selector exists" is weaker than it looks - a rule can survive a rename and
// still paint nothing. These are the two declarations without which the bar is
// on the page and invisible anyway, so they are asserted, not assumed.
const rule = (sel) => (new RegExp(`^\\${sel}\\s*\\{([^}]*)\\}`, "m").exec(CSS) || [, ""])[1];
ok("the fill actually paints - it declares a background",
  /background/.test(rule(".wo-mfill")), rule(".wo-mfill"));
ok("the track is the flexible element, so the bar has a length to be a fraction of",
  /flex:\s*1/.test(rule(".wo-mtrack")), rule(".wo-mtrack"));
ok("the track clips, so a 100% fill cannot escape its rounded corners",
  /overflow:\s*hidden/.test(rule(".wo-mtrack")), rule(".wo-mtrack"));
ok("the container it writes into exists in index.html", HTML.includes('id="wo-muscles"'));
ok("the container is styled too", /\.wo-muscles[\s,:{]/.test(CSS));
ok("index.html ships an empty state so the box is never a bare rectangle",
  /id="wo-muscles"[^>]*>\s*<div class="empty"/.test(HTML));

// The wiring. Everything above can pass while nothing ever calls it.
const loadW = slice("loadWorkouts");
ok("loadWorkouts CALLS renderMuscles", /renderMuscles\(/.test(loadW));
ok("...with the stats field, not something it invented",
  /renderMuscles\(\s*stats\.muscles/.test(loadW));
ok("...defaulting to [] so a stats payload without muscles cannot throw",
  /renderMuscles\(\s*stats\.muscles\s*\|\|\s*\[\]\s*\)/.test(loadW));

console.log();
console.log(FAILS.length ? `FAILED: ${FAILS.join(", ")}` : "all checks passed");
process.exit(FAILS.length ? 1 : 0);
