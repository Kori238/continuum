// Tests the SHIPPED loadFail/loadOk and a representative loader by slicing the real
// functions out of app.js and running them against a stub DOM. Copying the code into
// the test would only prove the copy works.
//
//   node _loadfail_test.mjs
import { readFileSync } from "node:fs";

const SRC = readFileSync("continuum/app/static/app.js", "utf8").replace(/\r\n/g, "\n");

// Slice a top-level `function name(...)` / `const name =` block by brace balance.
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
class El {
  constructor(tag) { this.tag = tag; this.children = []; this.className = ""; this._text = ""; this.parent = null; this.listeners = {}; }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
  set textContent(v) { this._text = String(v); this.children = []; }
  appendChild(c) { c.parent = this; this.children.push(c); return c; }
  prepend(c) { c.parent = this; this.children.unshift(c); return c; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((x) => x !== this); }
  addEventListener(ev, fn) { (this.listeners[ev] ||= []).push(fn); }
  click() { (this.listeners.click || []).forEach((f) => f()); }
  // Only the one selector the code actually uses.
  querySelectorAll(sel) {
    if (sel !== ":scope > .loadfail") throw new Error("unexpected selector " + sel);
    const out = this.children.filter((c) => c.className === "loadfail");
    out.forEach = Array.prototype.forEach.bind(out);
    return out;
  }
  banners() { return this.children.filter((c) => c.className === "loadfail"); }
}
const views = {};
globalThis.document = {
  getElementById: (id) => views[id] || null,
  createElement: (t) => new El(t),
  createTextNode: (t) => { const e = new El("#text"); e._text = t; return e; },
};
for (const v of ["routine", "nutrition", "memory", "workouts"]) views["view-" + v] = new El("section");

// ---- load the real code ---------------------------------------------------
let jreadResult = null, rendered = [];
const ctx = {
  jread: async () => jreadResult,
  renderTodos: (x) => rendered.push(["todos", x]),
  renderRoutineToday: (x) => rendered.push(["rtoday", x]),
  renderRoutineAll: (x) => rendered.push(["rall", x]),
};
const src = [slice("_loadFails"), slice("_renderFails"), slice("loadFail"), slice("loadOk"),
             slice("loadTodos"), slice("loadRoutines")].join("\n\n");
const fn = new Function(...Object.keys(ctx),
  src + "\n return {loadFail, loadOk, loadTodos, loadRoutines, _loadFails};");
const M = fn(...Object.values(ctx));

// ---- checks ---------------------------------------------------------------
let pass = 0, fail = 0;
const check = (name, cond, extra = "") => {
  if (cond) { pass++; console.log(`  ok   ${name}`); }
  else { fail++; console.log(`  FAIL ${name} ${extra}`); }
};
const routine = views["view-routine"];

console.log("failed read");
jreadResult = null; rendered = [];
await M.loadTodos();
check("raises exactly one banner", routine.banners().length === 1);
check("banner names the thing", routine.banners()[0].textContent.includes("your to-do list"));
check("banner disowns what's below", routine.banners()[0].textContent.includes("last reading, not now"));
check("does NOT render an empty list", rendered.length === 0, JSON.stringify(rendered));

console.log("repeated failures");
await M.loadTodos(); await M.loadTodos();
check("still one banner, not three", routine.banners().length === 1, `got ${routine.banners().length}`);
check("map holds one entry", M._loadFails.size === 1, `got ${M._loadFails.size}`);

console.log("second loader in the same view");
await M.loadRoutines();
check("two banners, one per loader", routine.banners().length === 2, `got ${routine.banners().length}`);
const texts = routine.banners().map((b) => b.textContent).join("|");
check("both named", texts.includes("your to-do list") && texts.includes("your routine"), texts);

console.log("recovery");
jreadResult = { todos: [{ id: 1, title: "x" }] };
await M.loadTodos();
check("todos banner cleared", !routine.banners().some((b) => b.textContent.includes("to-do")));
check("routine banner survives its sibling's recovery", routine.banners().length === 1,
  `got ${routine.banners().length}`);
check("now renders", rendered.some((r) => r[0] === "todos"));

jreadResult = { today: {}, all: [] };
await M.loadRoutines();
check("all clear", routine.banners().length === 0, `got ${routine.banners().length}`);
check("map empty", M._loadFails.size === 0, `got ${M._loadFails.size}`);

console.log("retry button");
jreadResult = null; await M.loadTodos();
jreadResult = { todos: [] };
rendered = [];
routine.banners()[0].children.find((c) => c.tag === "button").click();
await new Promise((r) => setTimeout(r, 0));
check("retry re-runs the loader", rendered.some((r) => r[0] === "todos"), JSON.stringify(rendered));
check("retry clears the banner", routine.banners().length === 0);

console.log("\nloadOk on a healthy loader is a no-op");
const before = M._loadFails.size;
M.loadOk(M.loadTodos);
check("no throw, no change", M._loadFails.size === before);

// loadMemory passes an arrow as `retry` (it needs the query) and loadMemory itself as
// `key`. Get that wrong and every failure leaks a fresh map entry that loadOk can
// never match — a banner that multiplies and never clears. This is the shape of a bug
// I wrote and caught mid-edit, so it gets its own check.
console.log("\nseparate key/retry (loadMemory)");
const mem = views["view-memory"];
// jread is bound by value into the new Function, so the stub has to read its
// answer through mutable outer state rather than being swapped out later.
let memArg = null, memRead = null;
const ctx2 = { jread: async (u) => { memArg = u; return memRead; }, $: () => new El("span"),
               setCoreText: () => {}, renderMemories: () => {} };
const M2 = new Function(...Object.keys(ctx2),
  [slice("_loadFails"), slice("_renderFails"), slice("loadFail"), slice("loadOk"),
   slice("loadMemory")].join("\n\n") + "\n return {loadMemory, _loadFails};"
)(...Object.values(ctx2));

memRead = null;
await M2.loadMemory("spara");
await M2.loadMemory("spara");
await M2.loadMemory("spara");
check("three failures leak no extra entries", M2._loadFails.size === 1, `got ${M2._loadFails.size}`);
check("one banner, not three", mem.banners().length === 1, `got ${mem.banners().length}`);
memArg = null;
mem.banners()[0].children.find((c) => c.tag === "button").click();
await new Promise((r) => setTimeout(r, 0));
check("query survived into the retry URL", String(memArg).includes("q=spara"), String(memArg));

memRead = { count: 3, core: "", memories: [] };
await M2.loadMemory("spara");
check("clears on recovery", M2._loadFails.size === 0 && mem.banners().length === 0,
  `size=${M2._loadFails.size} banners=${mem.banners().length}`);

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
