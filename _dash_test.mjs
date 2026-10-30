import fs from "fs";
const src = fs.readFileSync(process.argv[2], "utf8").split(/\r?\n/);
function slice(startRe, endRe) {
  const i = src.findIndex((l) => startRe.test(l));
  for (let j = i; j < src.length; j++) if (endRe.test(src[j])) return src.slice(i, j + 1).join("\n");
}
const code = [
  slice(/^const DASH_DEAD =/, /^const DASH_DEAD =/),
  slice(/^function _dashTile\(/, /^\}$/),
  slice(/^async function loadDashMind\(/, /^\}$/),
].join("\n\n");

// Real shapes, taken from the live server's actual responses.
const LIVE = {
  "/api/status": { memory_count: 214, core: "Jake — London.\nPrefers blunt answers." },
  "/api/messages?limit=1": { messages: [{ app: "WhatsApp", body: "on my way" }] },
  "/api/conversations": { conversations: [{ kind: "main", title: "Main" }, { kind: "side", title: "Continuum audit" }] },
};
async function run(reader) {
  let html = "";
  const grid = { set innerHTML(v) { html = v; }, get innerHTML() { return html; } };
  const ctx = { $: (id) => (id === "dash-mind-grid" ? grid : null), jread: reader,
    escapeHtml: (s) => String(s), toast: () => {},
    window: { __lastState: { apps: [{ name: "firefox", foreground: true }], system: { cpu_pct: 12, mem_pct: 61 } } } };
  await new Function(...Object.keys(ctx), code + "\n; return loadDashMind;")(...Object.values(ctx))();
  return [...html.matchAll(/data-go="([^"]+)"([^>]*)>[\s\S]*?dtile-big">([^<]*)<[\s\S]*?dtile-sub">([^<]*)</g)]
    .map(([, go, a, big, sub]) => ({ go, dead: a.includes("data-dead"), big, sub }));
}
const fails = [];
const ck = (l, c, d="") => { console.log((c ? "  PASS  " : "  FAIL  ") + l + (d ? `  [${d}]` : "")); if (!c) fails.push(l); };

console.log("--- ALL READS OK ---");
let t = await run(async (u) => LIVE[u]);
t.forEach((x) => console.log(`  ${x.go.padEnd(9)} big=${JSON.stringify(x.big).padEnd(12)} dead=${String(x.dead).padEnd(5)} sub=${JSON.stringify(x.sub)}`));
ck("memory shows the real count", t.find((x) => x.go === "memory").big === "214");
ck("memory sub shows core profile line", t.find((x) => x.go === "memory").sub === "Jake — London.");
ck("chat counts conversations", t.find((x) => x.go === "chat").big === "2");
ck("chat names the recent non-main thread", t.find((x) => x.go === "chat").sub === "Continuum audit");
ck("messages shows the app", t.find((x) => x.go === "messages").big === "WhatsApp");
ck("state shows foreground app", t.find((x) => x.go === "state").big === "firefox");
ck("no tile marked dead when all reads work", t.every((x) => !x.dead));

console.log("\n--- ONLY /api/status FAILS (the memory-says-0 bug) ---");
t = await run(async (u) => (u === "/api/status" ? null : LIVE[u]));
t.forEach((x) => console.log(`  ${x.go.padEnd(9)} big=${JSON.stringify(x.big).padEnd(12)} dead=${String(x.dead).padEnd(5)} sub=${JSON.stringify(x.sub)}`));
ck("memory does NOT claim 0", t.find((x) => x.go === "memory").big !== "0" && t.find((x) => x.go === "memory").big !== 0);
ck("memory tile marked retryable", t.find((x) => x.go === "memory").dead === true);
ck("the tiles that DID load are unaffected", t.find((x) => x.go === "chat").big === "2" && !t.find((x) => x.go === "chat").dead);

console.log(fails.length ? `\nFAILED: ${fails.join(", ")}` : "\nALL PASS");
process.exit(fails.length ? 1 : 0);
