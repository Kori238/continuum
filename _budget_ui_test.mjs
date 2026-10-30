// Tests the SHIPPED loadBudget/renderBudget by slicing them out of app.js and
// running them against a stub DOM. Copying the code in would only prove the copy
// works.
//
//   node _budget_ui_test.mjs
import { readFileSync } from "node:fs";

const SRC = readFileSync("continuum/app/static/app.js", "utf8").replace(/\r\n/g, "\n");

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
function el(id) { return (NODES[id] ||= { id, innerHTML: "", textContent: "" }); }
const document = { getElementById: el };

// ---- harness state --------------------------------------------------------
let READS = {};
let FAILED = null, OKED = null, RENDERED = null;
const jread = async (url) => (url in READS ? READS[url] : null);
const loadFail = (view, what) => { FAILED = { view, what }; };
const loadOk = () => { OKED = true; };
const fmtTime = () => "20:15";
const toast = () => {};

const src = [slice("BUD_LABEL"), slice("escapeHtml"), slice("renderBudget"), slice("loadBudget")].join("\n\n");
const { loadBudget, renderBudget } = new Function(
  "document", "jread", "loadFail", "loadOk", "fmtTime", "toast",
  src + "\nreturn { loadBudget, renderBudget };",
)(document, jread, loadFail, loadOk, fmtTime, toast);

// ---- assertions -----------------------------------------------------------
const FAILS = [];
function ok(label, cond, extra = "") {
  console.log(`${cond ? "PASS" : "FAIL"}  ${label}${cond ? "" : "   " + extra}`);
  if (!cond) FAILS.push(label);
}
const has = (id, s) => el(id).innerHTML.includes(s);

const RULES = {
  categories: ["game", "social", "video"],
  rules: [
    { id: 1, field: "proc", match: "game.exe", category: "game" },
    { id: 2, field: "app", match: "instagram", category: "social" },
  ],
};
const STATUS = (cats, extra = {}) => ({
  day: "2026-09-15", untracked: "2h10m", untracked_seconds: 7800,
  overrides_today: 0, categories: cats, ...extra,
});
const CAT = (o) => ({
  category: "game", minutes: 180, allowance_seconds: 10800, override_seconds: 0,
  spent_seconds: 3600, remaining_seconds: 7200, spent: "1h00m", allowance: "3h00m",
  remaining: "2h00m", pct: 33.3, enabled: true, pending_minutes: null,
  pending_day: null, ...o,
});

// A failed read must not paint a budget. The empty state here is a claim about
// Jake's day ("0m used"), which is exactly the trap the loadFail work was about.
READS = {};
FAILED = null; OKED = null; el("bud-cats").innerHTML = "SENTINEL";
await loadBudget();
ok("dead reads -> loadFail", FAILED && FAILED.view === "budget", JSON.stringify(FAILED));
ok("dead reads render nothing", el("bud-cats").innerHTML === "SENTINEL");
ok("dead reads do not clear the banner", OKED === null);

// A partial failure counts as a failure: rules without status would render every
// category as "no limit set" — i.e. announce that nothing is being enforced.
READS = { "/api/budget/rules": RULES, "/api/budget/events": { events: [] } };
FAILED = null; el("bud-cats").innerHTML = "SENTINEL";
await loadBudget();
ok("status alone missing -> loadFail", FAILED !== null);
ok("status alone missing renders nothing", el("bud-cats").innerHTML === "SENTINEL");

READS = {
  "/api/budget": STATUS([CAT({})]),
  "/api/budget/rules": RULES,
  "/api/budget/events": { events: [] },
};
FAILED = null; OKED = null;
await loadBudget();
ok("a good load clears the banner", OKED === true && FAILED === null);

// Unconfigured categories: "no limit set", never a zeroed bar.
ok("unset category says so", has("bud-cats", "no limit set"));
// One configured category (game) and two unset — so exactly one bar, and the
// two unset ones show no bar at all rather than an empty one reading 0%.
ok("only the configured category gets a bar",
   (el("bud-cats").innerHTML.match(/class="bud-bar/g) || []).length === 1,
   String((el("bud-cats").innerHTML.match(/class="bud-bar/g) || []).length));
ok("unset category is marked", has("bud-cats", 'class="bud-cat unset"'));
ok("all three categories render", ["🎮 Games", "📱 Feeds", "▶ Video"].every((s) => has("bud-cats", s)));

// Threshold colours must track the server's warning points, or the bar lies.
for (const [pct, cls] of [[10, "low"], [33.3, "low"], [60, "mid"], [92, "high"], [100, "over"], [140, "over"]]) {
  renderBudget(STATUS([CAT({ pct })]), RULES, []);
  ok(`pct ${pct} -> .${cls}`, has("bud-cats", `bud-bar ${cls}`), el("bud-cats").innerHTML.match(/bud-bar [a-z]+/)?.[0]);
}
// Over 100% must not overflow the bar.
renderBudget(STATUS([CAT({ pct: 140 })]), RULES, []);
ok("bar width clamps at 100%", has("bud-cats", "width:100%") && !has("bud-cats", "width:140%"));
ok("all four warning ticks are drawn",
   [50, 75, 90, 95].every((t) => has("bud-cats", `left:${t}%`)));

renderBudget(STATUS([CAT({ pct: 100, remaining: "0m" })]), RULES, []);
ok("spent reads 'spent', not '0m left'", has("bud-cats", ">spent<") && !has("bud-cats", "0m left"));

// The deferred raise has to be visible or the asymmetry is a silent surprise.
renderBudget(STATUS([CAT({ pending_minutes: 240, pending_day: "2026-09-16" })]), RULES, []);
ok("a parked raise is shown", has("bud-cats", "240m from 2026-09-16"));
renderBudget(STATUS([CAT({ override_seconds: 1800 })]), RULES, []);
ok("granted override time is shown", has("bud-cats", "+30m granted today"));
renderBudget(STATUS([CAT({ enabled: false })]), RULES, []);
ok("watching-only is visually distinct", has("bud-cats", 'class="bud-cat off"'));
ok("watching-only says 'watching'", has("bud-cats", ">watching<"));

renderBudget(STATUS([CAT({})], { overrides_today: 2 }), RULES, []);
ok("override count surfaces in the header", el("bud-sub").textContent.includes("2 overrides"));
ok("untracked time surfaces in the header", el("bud-sub").textContent.includes("2h10m untracked"));

// Events
renderBudget(STATUS([CAT({})]), RULES, [
  { ts: 1, category: "game", kind: "warn", detail: "50" },
  { ts: 2, category: "game", kind: "override", detail: "+30m from the budget tab" },
]);
ok("events render newest-first",
   el("bud-events").innerHTML.indexOf("override") < el("bud-events").innerHTML.indexOf("50% of the day"));
// Note the entity: event text goes through escapeHtml, so the apostrophe in
// "the day's game" arrives as &#39;. That it's escaped at all is the point.
ok("warn events are readable", has("bud-events", "50% of the day&#39;s game"),
   el("bud-events").innerHTML.slice(0, 160));
renderBudget(STATUS([CAT({})]), RULES, []);
ok("no events -> an honest empty state", has("bud-events", "nothing yet today"));

// Rule list, and escaping — a rule match is free text from a POST body.
renderBudget(STATUS([CAT({})]), RULES, []);
ok("rule count shown", el("bud-rules-sub").textContent === "2 rules");
ok("rules render", has("bud-rules", "game.exe"));
renderBudget(STATUS([CAT({})]), {
  categories: ["game"], rules: [{ id: 9, field: "proc", match: "<img src=x onerror=alert(1)>", category: "game" }],
}, []);
ok("rule text is escaped", !has("bud-rules", "<img src=x") && has("bud-rules", "&lt;img"));

console.log();
if (FAILS.length) { console.log(`${FAILS.length} FAILED: ${FAILS.join(", ")}`); process.exit(1); }
console.log("all budget UI checks passed");
