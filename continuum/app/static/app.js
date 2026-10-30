// Continuum Control — front end.
// Talks to the FastAPI server: pulls status over REST, streams the bus over a
// websocket, and submits tasks. A client-generated correlation id ties each
// submitted task card to the task.result the agent publishes back.

const $ = (id) => document.getElementById(id);
const pending = new Map(); // corr -> card element
// Live turns, kept as data (not just a DOM node) so a working reply survives a
// thread re-render: corr -> { conv, startedAt, phase, steps: [] }. pending holds
// the *element*; inflight holds the *state* used to rebuild it and tick a timer.
const inflight = new Map();
let evCount = 0;
let mainId = null;         // id of the day-to-day main thread

// Surface a message the user needs to see. Errors stay until dismissed —
// a failed write is not something to blink past; everything else auto-clears.
// Returns the node so callers can dismiss their own toast.
function toast(msg, kind = "err", ms = null) {
  const host = $("toasts");
  if (!host) { console.error("[toast]", kind, msg); return null; }
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.setAttribute("role", kind === "err" ? "alert" : "status");
  const x = document.createElement("button");
  x.className = "toast-x"; x.type = "button";
  x.textContent = "✕"; x.title = "dismiss";
  x.setAttribute("aria-label", "dismiss");
  x.addEventListener("click", () => el.remove());
  el.appendChild(x);
  el.appendChild(document.createTextNode(msg));
  host.appendChild(el);
  const life = ms != null ? ms : (kind === "err" ? 0 : 3200);
  if (life > 0) setTimeout(() => el.remove(), life);
  return el;
}

// fetch + JSON with the failure modes that actually happen made loud: a non-2xx, a
// body that isn't JSON, and {"error": ...} at HTTP 200. Throws on all three, so a
// caller can't print "✓ Saved" over a write that never landed.
//
// The error-at-200 case is kept even though Continuum's own handlers no longer do
// it. It costs one comparison and it's the check you don't want to have
// dropped the day something reintroduces the shape.
async function jfetch(url, opts) {
  let r;
  try {
    r = await fetch(url, opts);
  } catch (e) {
    throw new Error("can't reach Continuum — is it running?");
  }
  let data = null, raw = "";
  try { raw = await r.text(); data = raw ? JSON.parse(raw) : null; } catch (e) { /* not json */ }
  if (!r.ok) {
    throw new Error((data && data.error) || raw.slice(0, 160) || `request failed (${r.status})`);
  }
  if (data && typeof data === "object" && !Array.isArray(data) && data.error) {
    throw new Error(data.error);
  }
  return data;
}

// A read that's allowed to fail without taking the whole view down. Returns
// null on failure — never {} — so the caller can tell "the server didn't
// answer" apart from "the server says there's nothing". Those two must never
// render the same: most empty states here ("nothing scheduled", "0 kcal",
// "no sessions") read as fact, so a swallowed failure becomes a lie.
async function jread(url) {
  try { return await jfetch(url); } catch (e) { return null; }
}

// The visible half of jread's contract. jread can tell "didn't answer" from "says
// there's nothing", but that only matters if the tab says so out loud — and every
// loader below used to end in `catch (e) { /* best-effort */ }`, which left the
// last render standing and looking current. The empty states here read as facts
// about Jake's day ("0 kcal · nothing logged yet", "nothing to do 🎉", "No alarms
// yet"), so a dead read quietly asserting one is the whole bug.
//
// The banner goes at the top of the tab's own section rather than replacing the
// content: the stale numbers are still the last true reading and worth seeing, they
// just must not be mistaken for today's. Retry is offered because the usual causes —
// server asleep, lid shut, tailnet hiccup — clear on their own.
// Keyed by the loader function itself: stable, and it means loadOk takes one
// argument that can't drift out of sync with loadFail's. Two loaders sharing a tab
// — Routine runs loadRoutines and loadTodos — therefore own separate banners
// instead of racing each other's away.
const _loadFails = new Map();   // key fn -> {view, what, retry}

function _renderFails(view) {
  const sec = document.getElementById("view-" + view);
  if (!sec) return;
  sec.querySelectorAll(":scope > .loadfail").forEach((n) => n.remove());
  for (const f of _loadFails.values()) {
    if (f.view !== view) continue;
    const b = document.createElement("div");
    b.className = "loadfail";
    b.appendChild(document.createTextNode(
      `Couldn't load ${f.what} — anything below is the last reading, not now. `));
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = "retry";
    btn.addEventListener("click", () => f.retry());
    b.appendChild(btn);
    sec.prepend(b);
  }
}

// `key` defaults to `retry` because they're usually the same function. They come
// apart when the retry needs arguments — loadMemory(query) — and passing a fresh
// arrow as the key would leak a new map entry per failure and never match loadOk.
function loadFail(view, what, retry, key = retry) {
  _loadFails.set(key, { view, what, retry });
  _renderFails(view);
}

// Clear on a good load, so a tab that recovers stops accusing itself. Costs nothing
// when it wasn't failing, which is the overwhelmingly common case — so every loader
// can call it unconditionally.
function loadOk(retry) {
  const f = _loadFails.get(retry);
  if (!f) return;
  _loadFails.delete(retry);
  _renderFails(f.view);
}

// Options for a JSON write. Body omitted entirely for DELETE, which shouldn't
// carry one and whose handlers don't read one.
const jbody = (method, body) => (body === undefined
  ? { method }
  : { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

// A write that reports its own failure. `label` completes "couldn't …", so pass
// a verb phrase: jwrite("tick that off", …) → "couldn't tick that off: …".
//
// Returns {ok, data} rather than throwing, because the interesting decision at
// every call site is what NOT to do on failure — don't clear the input the user
// typed into, don't close the editor, don't claim it saved. Callers should still
// reload afterwards either way: the DOM has usually already moved optimistically
// (a checkbox ticks on click), and re-reading from the server is what snaps it
// back to the truth. Reloading only on success is how a failed write ends up
// looking exactly like a successful one.
async function jwrite(label, url, opts) {
  try {
    return { ok: true, data: await jfetch(url, opts) };
  } catch (e) {
    toast(`couldn't ${label}: ${e.message}`);
    return { ok: false, data: null };
  }
}

function fmtTime(ts) {
  const d = new Date((ts || Date.now() / 1000) * 1000);
  return d.toLocaleTimeString([], { hour12: false });
}

// Date + time for the stamp shown beside each chat message. ts is unix seconds;
// missing ts (a live bubble) falls back to now.
function fmtStamp(ts) {
  const d = new Date((ts || Date.now() / 1000) * 1000);
  return d.toLocaleString([], {
    day: "numeric", month: "short",
    hour: "2-digit", minute: "2-digit", hour12: false,
  });
}

function setPill(el, state, text) {
  el.className = "pill " + state;
  el.innerHTML = `<i class="dot"></i> ${text}`;
}

async function loadStatus() {
  try {
    const r = await fetch("/api/status");
    const s = await r.json();
    const auth = s.cloud_provider === "anthropic" ? "api key"
               : s.cloud_provider === "claude-cli" ? "subscription"
               : s.cloud_provider;
    // This pill used to read "local · llama3.1 · up". Both rungs are Claude now,
    // so it shows the cheap one — the model that tags, extracts and decides
    // whether to speak — and shares the cloud's reachability, because they are
    // the same connection and a separate dot would imply a separate failure.
    setPill($("pill-fast"),
      s.cloud_available ? "ok" : "warn",
      `fast · ${s.fast_model}`);
    setPill($("pill-cloud"),
      s.cloud_available ? "ok" : "warn",
      `cloud · ${s.cloud_model} · ${s.cloud_available ? auth : "unreachable"}`);
    setPill($("pill-memory"),
      s.memory_ready ? "ok" : "bad",
      `memory · ${s.embed_model} · ${s.memory_ready ? s.memory_count + " stored" : "embedder down"}`);
    $("mem-count").textContent = s.memory_count || 0;
    if (document.activeElement !== $("ax")) $("ax").checked = !!s.autoextract;
    $("tools").checked = !!s.tools_enabled;
    if (document.activeElement !== $("actions")) $("actions").checked = !!s.actions_enabled;
    if (document.activeElement !== $("dev")) $("dev").checked = !!s.dev_enabled;
    if (document.activeElement !== $("proactive")) $("proactive").checked = !!s.proactive_enabled;
    mainId = s.main_conversation_id ?? mainId;
    renderDevices(s.devices || []);
  } catch (e) {
    // status is best-effort; the ws connection pill carries the real health.
  }
}

async function loadMemory(query = "") {
  const data = await jread("/api/memory?limit=40" + (query ? "&q=" + encodeURIComponent(query) : ""));
  // `data.count || 0` and `data.core || ""` together rendered a dead read as
  // "Memory 0" over a blank core profile — the agent announcing it had forgotten
  // everything it knows about Jake. It hadn't; the request died.
  if (!data) return loadFail("memory", "your memory", () => loadMemory(query), loadMemory);
  loadOk(loadMemory);
  $("mem-count").textContent = data.count || 0;
  setCoreText(data.core || "");
  renderMemories(data.memories || [], !!query);
}

function setCoreText(text) {
  const el = $("core-text");
  if (document.activeElement !== el) el.value = text; // don't clobber while editing
}

function renderMemories(mems, showScore) {
  const box = $("mem-list");
  if (!mems.length) { box.innerHTML = '<div class="empty">no memories yet</div>'; return; }
  box.innerHTML = "";
  for (const m of mems) box.appendChild(memEl(m, showScore));
}

function memEl(m, showScore) {
  const el = document.createElement("div");
  el.className = "mem";
  el.dataset.id = m.id;
  const imp = m.importance ? `<span class="imp" title="importance">${Math.round(m.importance)}/10</span>` : "";
  const auto = m.source === "extractor" ? `<span class="auto" title="auto-learned from conversation">auto</span>` : "";
  const scope = m.conversation_id
    ? `<span class="mscope conv" title="scoped to conversation #${m.conversation_id}">💬</span>`
    : `<span class="mscope" title="global">🌐</span>`;
  const score = (showScore && m.score) ? `<span class="score">${m.score.toFixed(2)}</span>` : "";
  const del = `<span class="mdel" title="delete memory">✕</span>`;
  const tags = (m.tags || []).length
    ? `<div class="mtags">${m.tags.map((t) => `<span class="mtag">${escapeHtml(t)}</span>`).join("")}</div>` : "";
  el.innerHTML =
    `<div class="row"><span class="kind ${m.kind}">${m.kind}</span>${imp}${auto}${scope}${score}${del}</div>` +
    `<div class="txt">${escapeHtml(m.text)}</div>` + tags +
    `<div class="mrel" style="display:none"></div>`;
  el.querySelector(".mdel").addEventListener("click", (e) => { e.stopPropagation(); deleteMemory(m.id, el); });
  el.querySelector(".txt").addEventListener("click", () => toggleRelated(m.id, el));
  return el;
}

async function deleteMemory(id, el) {
  // Only take it off screen once it's actually gone. The old catch covered the
  // network being down but not the server refusing, so a failed delete still
  // removed the row and decremented the count — the memory was still there.
  const r = await jwrite("delete that memory", "/api/memory/" + id, jbody("DELETE"));
  if (!r.ok) return;
  el.remove();
  const c = $("mem-count"); c.textContent = Math.max(0, (parseInt(c.textContent) || 1) - 1);
}

async function toggleRelated(id, el) {
  const box = el.querySelector(".mrel");
  if (box.style.display !== "none") { box.style.display = "none"; return; }
  box.style.display = "block";
  box.innerHTML = '<span class="muted">finding related…</span>';
  try {
    const d = await (await fetch("/api/memory/" + id + "/related")).json();
    const rel = d.related || [];
    box.innerHTML = rel.length
      ? "<div class='rel-h'>related</div>" + rel.map((r) =>
          `<div class="rel-item">${escapeHtml(r.text)}</div>`).join("")
      : "<span class='muted'>no related memories</span>";
  } catch (e) { box.innerHTML = "<span class='muted'>—</span>"; }
}

function prependMemory(m) {
  const box = $("mem-list");
  const empty = box.querySelector(".empty");
  if (empty) empty.remove();
  box.prepend(memEl(m, false));
  $("mem-count").textContent = (parseInt($("mem-count").textContent) || 0) + 1;
}

function renderDevices(devices) {
  const box = $("devices");
  if (!devices.length) { box.innerHTML = '<div class="empty">no devices yet</div>'; return; }
  box.innerHTML = "";
  for (const d of devices) {
    const el = document.createElement("div");
    el.className = "device";
    // Every one of these is a string the *device* chose — name and os come
    // straight out of the phone's hello frame, caps out of what it claims to
    // offer. Escape them: a device that names itself with a tag would otherwise
    // persist in the devices dict and run in every dashboard opened afterwards.
    const caps = (d.capabilities || []).map((c) => `<span class="cap">${escapeHtml(c)}</span>`).join("");
    el.innerHTML = `<div class="name">${escapeHtml(d.name)}</div>` +
      (d.os ? `<div class="os">${escapeHtml(d.os)}</div>` : "") +
      `<div class="caps">${caps}</div>`;
    box.appendChild(el);
  }
}

// ---- live state ----
let stExpanded = null; // pid whose detail is open

function renderState(s) {
  if (!s || !s.apps) return;
  $("st-count").textContent = s.count || 0;
  $("st-time").textContent = s.time || "";

  renderStatus(s);
  renderActivity(s.activity);

  const sys = s.system || {};
  const metric = (k, v, pct, unit = "%") => {
    const cls = pct >= 90 ? "crit" : pct >= 70 ? "hot" : "";
    return `<div class="metric ${cls}"><div class="k">${k}</div><div class="v">${v}${unit}</div>` +
      (pct != null ? `<div class="bar"><span style="width:${Math.min(100, pct)}%"></span></div>` : "") + `</div>`;
  };
  let m = metric("CPU", sys.cpu_pct ?? "?", sys.cpu_pct) +
          metric("RAM", sys.mem_pct ?? "?", sys.mem_pct);
  if (sys.battery_pct != null) m += metric("BAT", sys.battery_pct, sys.battery_pct);
  $("st-metrics").innerHTML = m;

  const box = $("st-apps");
  box.innerHTML = "";
  for (const a of s.apps) {
    const el = document.createElement("div");
    el.className = "st-app" + (a.foreground ? " fg" : "");
    const when = a.foreground ? "focused now"
               : (a.focused_ago && a.focused_ago !== "not yet" ? "focused " + a.focused_ago : "not focused yet");
    el.innerHTML =
      `<div class="row1">${a.foreground ? '<span class="fgdot"></span>' : ""}` +
      `<span class="nm">${escapeHtml(a.name)}</span>` +
      `<span class="dur">${when}${a.focus_seconds ? " · " + a.focus_for + " total" : ""}</span></div>` +
      `<div class="ttl">${escapeHtml(a.title || "")}</div>` +
      (stExpanded === a.pid ? `<div class="st-detail" id="det-${a.pid}">loading…</div>` : "");
    el.addEventListener("click", () => toggleInspect(a.pid));
    box.appendChild(el);
  }
  renderBrowser(s.browser);
  if (stExpanded != null) loadInspect(stExpanded);
}

function renderStatus(s) {
  const box = $("st-status");
  if (!box) return;
  const chips = [];
  const chip = (cls, txt) => chips.push(`<span class="chip ${cls}">${escapeHtml(txt)}</span>`);

  const p = s.presence || {};
  if (p.state === "away") chip("away", `away · idle ${p.away_for || ""}`);
  else if (p.just_back) chip("back", `just back · was away ${p.was_away || ""}`);
  else if (p.session_for) chip("ok", `at desk · ${p.session_for}`);

  const g = s.gpu || null;
  if (g && (g.util_pct >= 30 || (g.procs && g.procs.length))) {
    const who = (g.procs && g.procs[0]) ? " · " + g.procs[0].name : "";
    chip("gpu", `GPU ${g.util_pct}% · ${(g.mem_used_mb/1024).toFixed(1)}G${who}`);
  }
  const md = s.media || null;
  if (md && md.playing && md.title) chip("media", `▶ ${md.title}${md.artist ? " · " + md.artist : ""}`);
  for (const u of (s.unread || [])) if (u.count) chip("unread", `${u.source} ${u.count}`);
  const net = s.network || {};
  if (net.wifi) chip("net", `📶 ${net.wifi}`);

  box.innerHTML = chips.join("");
}

function renderActivity(act) {
  const box = $("st-activity");
  if (!box) return;
  if (!act || act.length < 2) { box.innerHTML = ""; return; }
  const total = act.reduce((n, a) => n + a.seconds, 0) || 1;
  let h = `<div class="st-ahead">last hour</div><div class="st-abar">`;
  for (const a of act) h += `<span class="seg" style="flex:${a.seconds}" title="${escapeHtml(a.name)} ${a.for}"></span>`;
  h += `</div><div class="st-akeys">` +
       act.map(a => `<span><i></i>${escapeHtml(a.name)} ${a.for}</span>`).join("") + `</div>`;
  box.innerHTML = h;
}

function renderBrowser(bc) {
  const box = $("st-browser");
  if (!box) return;
  if (!bc || !(bc.recent && bc.recent.length)) { box.innerHTML = ""; return; }
  let h = `<div class="st-bhead">Firefox · ${bc.open_count} tab${bc.open_count === 1 ? "" : "s"} · recent first</div>`;
  for (const p of bc.recent) {
    const host = (() => { try { return new URL(p.url).hostname.replace(/^www\./, ""); } catch { return ""; } })();
    h += `<a class="st-tab" href="${escapeHtml(p.url)}" target="_blank" rel="noopener">` +
         `<span class="tt">${escapeHtml(p.title || p.url)}</span>` +
         `<span class="th">${escapeHtml(host)} · ${escapeHtml(p.idle || "")}</span></a>`;
  }
  box.innerHTML = h;
}

async function toggleInspect(pid) {
  stExpanded = (stExpanded === pid) ? null : pid;
  if (stExpanded != null) await loadInspect(stExpanded);
  else { const s = window.__lastState; if (s) renderState(s); }
}

async function loadInspect(pid) {
  const el = $("det-" + pid);
  if (!el) return;
  // Inline rather than a tab banner — this expands one process row, so its failure
  // belongs in that row. jfetch rather than jread because the reason is worth
  // showing here ("no such process" is a real answer), and it folds the old
  // d.error branch and the swallowed transport failure into one path. The old
  // version left the expander blank on a dead request, which reads as a process
  // with nothing worth reporting.
  let d;
  try { d = await jfetch("/api/state/inspect?target=" + pid); }
  catch (e) { el.textContent = e.message; return; }
  const row = (k, v) => v == null || v === "" ? "" : `<span class="kk">${k}</span> ${escapeHtml(String(v))}\n`;
  el.innerHTML =
    row("exe", d.exe) + row("cmdline", d.cmdline) +
    row("memory", (d.memory_mb ?? "?") + " MB") + row("cpu", (d.cpu_pct ?? "?") + "%") +
    row("threads", d.num_threads) + row("open files", d.num_open_files) +
    row("focus", d.focus_for + (d.focused_ago ? " (last " + d.focused_ago + ")" : "")) +
    row("open for", d.open_for) +
    row("user", d.username) + row("children", (d.children || []).join(", ")) +
    row("windows", (d.titles || []).join(" | ")) +
    ((d.browser && d.browser.recent) ? row("tabs",
      d.browser.recent.map(p => (p.title || p.url) + " (" + p.idle + ")").join(" | ")) : "");
}

function topicClass(topic) {
  if (topic === "task.request") return "req";
  if (topic === "task.result") return "res";
  if (topic.startsWith("device")) return "dev";
  return "";
}

function pushEvent(ev) {
  const box = $("events");
  const row = document.createElement("div");
  row.className = "ev";
  // topic and source are attacker-reachable: the phone picks the `kind` half of
  // `phone.<kind>`, and `source` is the device's self-chosen name.
  row.innerHTML =
    `<span class="t">${fmtTime(ev.ts)}</span>` +
    `<span class="topic ${topicClass(ev.topic)}">${escapeHtml(ev.topic)}</span>` +
    `<span class="src">${escapeHtml(ev.source)}</span>`;
  box.appendChild(row);
  box.scrollTop = box.scrollHeight;
  $("ev-count").textContent = ++evCount;
  while (box.children.length > 300) box.removeChild(box.firstChild);
}

// ---- schedule ----
function humanIn(s) {
  s = Math.max(0, s | 0);
  if (s < 60) return s + "s";
  const m = (s / 60) | 0;
  if (m < 60) return m + "m";
  const h = (m / 60) | 0;
  if (h < 24) return h + "h" + (m % 60 ? (m % 60) + "m" : "");
  return ((h / 24) | 0) + "d";
}

async function loadSchedule() {
  const d = await jread("/api/schedule");
  // "nothing scheduled" is a promise that no reminder is coming. Don't make it on
  // the strength of a request that failed.
  if (!d) return loadFail("memory", "the schedule", loadSchedule);
  loadOk(loadSchedule);
  const jobs = d.jobs || [];
  $("sched-count").textContent = jobs.length;
  const box = $("sched-list");
  if (!jobs.length) { box.innerHTML = '<div class="empty">nothing scheduled</div>'; return; }
  box.innerHTML = "";
  for (const j of jobs) {
    const el = document.createElement("div");
    el.className = "sched";
    const icon = j.kind === "reminder" ? "🔔" : "▶";
    const repeat = j.interval_s ? `<span class="repeat">↻ ${humanIn(j.interval_s)}</span>` : "";
    el.innerHTML =
      `<div class="row"><span class="kind">${icon}</span>` +
      `<span class="title">${escapeHtml(j.title)}</span>${repeat}` +
      `<span class="when">in ${humanIn(j.in_seconds)}</span>` +
      `<span class="sdel" title="cancel">✕</span></div>`;
    el.querySelector(".sdel").addEventListener("click", async () => {
      await jwrite("cancel that", "/api/schedule/" + j.id, jbody("DELETE"));
      loadSchedule();
    });
    box.appendChild(el);
  }
}

// ---- conversation thread ----
let activeConv = null;

function threadBox() { return $("thread"); }
function scrollThread() { const b = threadBox(); b.scrollTop = b.scrollHeight; }
function clearEmpty() { const e = threadBox().querySelector(".empty"); if (e) e.remove(); }

function metaLine(res) {
  if (res.error) return `<span class="badge err">error</span>`;
  const isCloud = (res.tier || "").toLowerCase().includes("cloud");
  let h = `<span class="badge ${isCloud ? "cloud" : "local"}">${escapeHtml(res.tier || "")}` +
          `${res.model ? " · " + escapeHtml(res.model) : ""}</span>`;
  if (res.reason) h += `<span>${escapeHtml(res.reason)}</span>`;
  if (res.memories_used > 0) h += `<span class="mem-used" title="memories injected">🧠 ${res.memories_used}</span>`;
  if (res.state_used) h += `<span class="mem-used state" title="live state injected">🖥️</span>`;
  return h;
}

// A small ⧉ button, copied text pulled from the bubble at click time (below).
function copyBtnHtml() {
  return `<button class="copy-btn" type="button" title="copy message">⧉</button>`;
}

// Tool args come two ways: a dict (final tools_used) or a pre-joined string
// (a live agent.step). Render both as "k=v, k=v".
function fmtArgs(args) {
  if (args == null) return "";
  if (typeof args === "string") return args;
  return Object.entries(args).map(([k, v]) => `${k}=${v}`).join(", ");
}

function stepRow(tool, argsText, action) {
  const icon = action ? "⚡" : "🔧";
  const a = argsText ? `<span class="stp-args">(${escapeHtml(argsText)})</span>` : "";
  return `<div class="stp${action ? " act" : ""}"><span class="stp-ic">${icon}</span>` +
         `<span class="stp-fn">${escapeHtml(tool)}</span>${a}</div>`;
}

function _stepsCap(det, n) {
  const cap = det.querySelector(".steps-cap");
  if (cap) cap.textContent = n === 1 ? "1 step" : `${n} steps`;
}

// Replace the step list wholesale — used when the final result arrives with its
// authoritative tool list (local gather + any cloud MCP calls).
function setSteps(msgEl, calls) {
  const det = msgEl.querySelector(".steps");
  if (!det) return;
  det.querySelector(".steps-body").innerHTML =
    (calls || []).map((c) => stepRow(c.tool, fmtArgs(c.args), c.action)).join("");
  _stepsCap(det, (calls || []).length);
  det.hidden = !(calls && calls.length);
}

// Append one live step as it happens, revealing and opening the disclosure so
// Jake watches the work instead of just a spinner.
function addStepTo(msgEl, tool, argsText, action) {
  const det = msgEl.querySelector(".steps");
  if (!det) return;
  det.hidden = false;
  det.open = true;
  const body = det.querySelector(".steps-body");
  body.insertAdjacentHTML("beforeend", stepRow(tool, argsText, action));
  _stepsCap(det, body.children.length);
}

// Plain-language verb for a tool call, shown live in the working badge so the
// current activity reads at a glance without opening the steps list.
const PHRASE = {
  dev_read_file: "reading", dev_write_file: "writing", dev_edit_file: "editing",
  dev_list_files: "listing files", dev_run: "running", dev_diff: "checking the diff",
  dev_revert: "reverting", dev_restart: "restarting",
  recall_memory: "recalling memory", remember: "saving to memory", forget: "forgetting",
  web_search: "searching the web", fetch_url: "fetching a page",
  see_screen: "looking at the screen", inspect_app: "inspecting an app",
  list_open_apps: "checking open apps", browsing_history: "checking browsing history",
  read_messages: "reading messages", recent_messages: "reading messages",
  search_messages: "searching messages", project_state: "checking project state",
  recent_files: "checking recent files", todays_routine: "checking your routine",
  routine_performance: "checking routine history", list_scheduled_tasks: "checking the schedule",
  workout_summary: "reading workouts", recent_workouts: "reading workouts",
  workout_detail: "reading a workout", exercise_progress: "checking progress",
  refresh_workouts: "syncing workouts",
};
function stepPhrase(tool, argsText) {
  const verb = PHRASE[tool] || String(tool || "").replace(/_/g, " ");
  let hint = "";                                   // surface a path/command/query if present
  if (argsText) {
    const first = String(argsText).split(",")[0].split("=").pop().trim();
    if (first) hint = " " + (first.length > 44 ? first.slice(0, 43) + "…" : first);
  }
  return verb + hint;
}

function fmtElapsed(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
}

// A turn is "dead" when whatever was running it goes away mid-flight (app
// restart, power-off, a wedged cloud call) so no task.result ever lands. Without
// a backstop its bubble ticks "thinking…" forever. If a turn runs this long with
// no result AND no new step, treat it as interrupted. Steps reset the clock
// (info.lastActivity), so a genuinely-working turn making tool calls is never
// falsely killed — only a silent, resultless one is.
const WATCHDOG_MS = 180000;

// Retire a live working bubble into a terminal "interrupted" state: drop the
// spinner/meta for an err badge and replace "thinking…" with a plain note.
function markInterrupted(el) {
  const meta = el.querySelector(".meta");
  if (meta) meta.innerHTML = '<span class="badge err">interrupted</span>';
  const t = el.querySelector(".text");
  if (t) {
    t.className = "text err";
    t.textContent = "Interrupted — no response (the turn was cut off before it finished).";
  }
}

// One ticker updates the elapsed clock on every live working bubble, so a long
// turn visibly counts up instead of looking frozen or vanished — and retires any
// turn the watchdog judges dead so it stops ticking and can't be resurrected.
function tickWorking() {
  for (const [corr, info] of inflight) {
    const el = pending.get(corr);
    if (Date.now() - (info.lastActivity || info.startedAt) > WATCHDOG_MS) {
      if (el && el.isConnected) markInterrupted(el);
      pending.delete(corr);
      inflight.delete(corr);  // drop tracking so openConversation won't rebuild it
      continue;
    }
    if (!el || !el.isConnected) continue;
    const t = el.querySelector(".work-el");
    if (t) t.textContent = fmtElapsed(Date.now() - info.startedAt);
  }
}
setInterval(tickWorking, 1000);

function addStep(p) {
  const info = inflight.get(p.request_id);
  if (info) {
    info.steps.push({ tool: p.tool, args: p.args || "", action: !!p.action });
    info.phase = stepPhrase(p.tool, p.args || "");
    info.lastActivity = Date.now();  // fresh step → turn is alive; reset the watchdog
  }
  const el = pending.get(p.request_id);
  if (el) {
    addStepTo(el, p.tool, p.args || "", !!p.action);
    const act = el.querySelector(".work-act");
    if (act && info) act.textContent = info.phase;
  }
}

function attachHtml(attachments) {
  // Attachments persist in the message meta as bare ids; render each as a
  // thumbnail that opens the full image. Tolerates {id} objects too, so the
  // live-submit path and the reopened-thread path share this.
  const ids = (attachments || [])
    .map((a) => (typeof a === "string" ? a : a && a.id))
    .filter(Boolean);
  if (!ids.length) return "";
  return `<div class="msg-imgs">` +
    ids.map((id) =>
      `<a href="/api/upload/${id}" target="_blank" rel="noopener">` +
      `<img class="msg-img" src="/api/upload/${id}" alt="attached image" /></a>`
    ).join("") +
    `</div>`;
}

function userBubble(text, ts, attachments) {
  const d = document.createElement("div");
  d.className = "msg user";
  const body = text ? `<div class="btext">${escapeHtml(text)}</div>` : "";
  d.innerHTML = `<div class="bubble">${attachHtml(attachments)}${body}` +
    `<span class="ts">${fmtStamp(ts)}</span>${copyBtnHtml()}</div>`;
  return d;
}

function assistantBubble(ts) {
  const d = document.createElement("div");
  d.className = "msg asst";
  d.innerHTML = `<div class="bubble"><div class="meta">` +
    `<span class="badge pending"><span class="spinner"></span><span class="work-el">0:00</span></span>` +
    `<span class="work-act">working…</span></div>` +
    `<details class="steps" hidden><summary><span class="steps-cap"></span></summary>` +
      `<div class="steps-body"></div></details>` +
    `<div class="text"><span class="muted">thinking…</span></div>` +
    `<span class="ts">${fmtStamp(ts)}</span>` +
    copyBtnHtml() + `</div>`;
  return d;
}

function fillAssistant(bubble, res) {
  bubble.querySelector(".meta").innerHTML = metaLine(res);
  const det = bubble.querySelector(".steps");
  if (det) { setSteps(bubble, res.tools_used); det.open = false; }  // done: collapse, still expandable
  const t = bubble.querySelector(".text");
  t.className = res.error ? "text err" : "text";
  t.textContent = res.error ? res.error : (res.output || "(empty response)");
  const ts = bubble.querySelector(".ts");        // restamp to when the reply landed
  if (ts) ts.textContent = fmtStamp(res.ts);
}

// Copy any message on demand — text pulled live so a still-streaming assistant
// bubble copies its final answer, not "thinking…".
document.addEventListener("click", (e) => {
  const btn = e.target.closest(".copy-btn");
  if (!btn) return;
  const bubble = btn.closest(".bubble");
  const src = bubble && bubble.querySelector(".text, .btext");
  const txt = src ? src.textContent : "";
  if (!txt) return;
  const done = () => {
    btn.textContent = "✓"; btn.classList.add("copied");
    setTimeout(() => { btn.textContent = "⧉"; btn.classList.remove("copied"); }, 1200);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(txt).then(done).catch(() => fallbackCopy(txt, done));
  } else { fallbackCopy(txt, done); }
});

function fallbackCopy(txt, done) {
  try {
    const ta = document.createElement("textarea");
    ta.value = txt; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.focus(); ta.select();
    document.execCommand("copy"); document.body.removeChild(ta);
    done();
  } catch (e) { /* give up quietly */ }
}

function appendUser(text, attachments) { clearEmpty(); threadBox().appendChild(userBubble(text, undefined, attachments)); scrollThread(); }

function appendPendingAssistant(corr) {
  clearEmpty();
  const el = assistantBubble();
  threadBox().appendChild(el);
  scrollThread();
  pending.set(corr, el);
  inflight.set(corr, { conv: activeConv, startedAt: Date.now(), lastActivity: Date.now(), phase: "", steps: [] });
}

function onResult(res) {
  const bubble = pending.get(res.request_id);
  if (bubble) pending.delete(res.request_id);
  inflight.delete(res.request_id);  // turn is done — stop tracking/ticking it
  if (bubble && bubble.isConnected) {
    // We submitted this turn and its bubble is still on screen — fill it in
    // place, keeping the live step trail. Adopt the conversation id so a "new"
    // break-off's later replies keep landing in the thread it just created.
    fillAssistant(bubble, res);
    if (res.conversation_id) activeConv = res.conversation_id;
    scrollThread();
    loadConversations();  // refresh titles/order
  } else if (res.conversation_id && res.conversation_id === activeConv) {
    // A reply we didn't place: another device sent it, or our own thread was
    // rebuilt mid-turn (switched away and back, which detaches the pending
    // bubble). It belongs to the thread we're viewing, and both the message and
    // the reply are already persisted, so pull the thread fresh from the store.
    // This is what keeps a second device in sync instead of waiting on a manual
    // reopen — openConversation refreshes the conversation list too.
    openConversation(activeConv);
  } else if (res.conversation_id) {
    loadConversations();  // not the open thread; just keep titles/order current
  }
}

// ---- conversation management ----
// activeConv is a conversation id, or the string "new" for a pending task break-off.
function newConversation() {
  activeConv = "new";
  $("conv-title").textContent = "✳ New task";
  threadBox().innerHTML = '<div class="empty">A task break-off from your day-to-day. Ask anything — it becomes its own thread.</div>';
  $("conv-list").style.display = "none";
  $("prompt").focus();
  loadConversations();
}

function goMain() { if (mainId) openConversation(mainId); }

async function loadConversations() {
  const d = await jread("/api/conversations");
  // An empty list would mean every thread is gone, which it never actually is —
  // there's always a main one. Leave the sidebar as it was rather than emptying it.
  if (!d) return loadFail("chat", "your conversations", loadConversations);
  loadOk(loadConversations);
  renderConvList(d.conversations || []);
}

function renderConvList(list) {
  window.__convs = list;
  renderConvTabs(list);
  const box = $("conv-list");
  if (!list.length) { box.innerHTML = '<div class="empty" style="padding:10px">no conversations yet</div>'; return; }
  box.innerHTML = "";
  for (const c of list) {
    const isMain = c.kind === "main";
    const el = document.createElement("div");
    el.className = "conv-item" + (c.id === activeConv ? " active" : "") + (isMain ? " main" : "");
    const tags = (c.tags || []).slice(0, 3).map((t) => `<span class="ci-tag">${escapeHtml(t)}</span>`).join("");
    const title = isMain ? "🏠 " + escapeHtml(c.title) : escapeHtml(c.title);
    const del = isMain ? "" : `<span class="ci-del" title="delete">✕</span>`;
    el.innerHTML =
      `<div class="ci-main"><span class="ci-title">${title}</span>` +
      `<span class="ci-meta">${c.messages} msg</span>${del}</div>` +
      (tags ? `<div class="ci-tags">${tags}</div>` : "");
    el.querySelector(".ci-title").addEventListener("click", () => openConversation(c.id));
    el.querySelector(".ci-meta").addEventListener("click", () => openConversation(c.id));
    const delBtn = el.querySelector(".ci-del");
    if (delBtn) delBtn.addEventListener("click", (e) => { e.stopPropagation(); deleteConversation(c.id); });
    box.appendChild(el);
  }
}

function renderConvTabs(list) {
  const box = $("conv-tabs");
  if (!box) return;
  const main = list.find((c) => c.kind === "main");
  const others = list.filter((c) => c.kind !== "main").slice(0, 6);
  const ordered = (main ? [main] : []).concat(others);
  box.innerHTML = ordered.map((c) => {
    const active = c.id === activeConv ? " active" : "";
    const isMain = c.kind === "main";
    const label = (isMain ? "🏠 " : "") + escapeHtml(c.title);
    return `<div class="conv-tab${active}${isMain ? " main" : ""}" data-id="${c.id}"><span class="ct-title">${label}</span></div>`;
  }).join("");
  box.querySelectorAll(".conv-tab").forEach((el) =>
    el.addEventListener("click", () => openConversation(Number(el.dataset.id))));
}

async function openConversation(id) {
  // A tap, not a background refresh: Jake picked a thread and expects it to open.
  // The old silent return made that read as a dead button — and since activeConv
  // is only assigned below, the tab highlight didn't move either, so there was no
  // sign anything had been attempted at all. jread covers the old conv.error
  // branch too, now that a missing conversation answers 404 instead of 200.
  const conv = await jread("/api/conversations/" + id);
  if (!conv) { toast("couldn't open that conversation"); return; }
  activeConv = conv.id;
  renderConvTabs(window.__convs || []);
  $("conv-title").textContent = conv.kind === "main" ? "🏠 " + conv.title : conv.title;
  const box = threadBox();
  box.innerHTML = "";
  for (const m of conv.messages) {
    if (m.role === "user") { box.appendChild(userBubble(m.content, m.ts, (m.meta || {}).attachments)); }
    else {
      const el = assistantBubble(m.ts);
      el.querySelector(".meta").innerHTML = metaLine(m.meta || {});
      setSteps(el, (m.meta || {}).tools_used);
      const t = el.querySelector(".text"); t.className = "text"; t.textContent = m.content;
      box.appendChild(el);
    }
  }
  // Rebuild any working bubble for this thread from inflight state, so a turn
  // that's still running doesn't disappear just because we re-rendered (tab
  // switch, reconnect). The ticker picks it up again via pending.
  for (const [corr, info] of inflight) {
    if (info.conv !== conv.id) continue;
    const el = assistantBubble();
    el.querySelector(".work-el").textContent = fmtElapsed(Date.now() - info.startedAt);
    el.querySelector(".work-act").textContent = info.phase || "working…";
    for (const s of info.steps) addStepTo(el, s.tool, fmtArgs(s.args), s.action);
    box.appendChild(el);
    pending.set(corr, el);
  }
  $("conv-list").style.display = "none";
  scrollThread();
  loadConversations();
}

async function deleteConversation(id) {
  // Don't navigate away from a thread that's still there. Two ways this fails
  // and neither used to show: the request itself, and the server declining —
  // it refuses to delete the main thread and says so as {"ok": false} at HTTP
  // 200, which jfetch has no way to read as an error.
  const r = await jwrite("delete that conversation", "/api/conversations/" + id, jbody("DELETE"));
  if (!r.ok) return;
  if (r.data && r.data.ok === false) {
    toast("that one can't be deleted — it's the main thread", "warn");
    return;
  }
  if (id === activeConv) newConversation();
  else loadConversations();
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---- websocket ----
let ws;
// Set when we know a relaunch is coming, so the reconnect reloads the page
// instead of silently reattaching to a new instance with stale HTML/JS.
let reloadOnReconnect = false;
// False until the first successful connect, so we only *reconcile* on a genuine
// reconnect — not on the initial page-load connection.
let everConnected = false;

// A dropped-then-restored socket means we may have missed the task.result for any
// turn in flight across the gap — the bus has no replay. Rather than let those
// bubbles tick forever, drop the in-flight tracking and refetch the open thread
// from the store (the source of truth): a turn that finished during the gap was
// persisted before its result published (agent.py), so it now shows correctly; a
// turn that truly died shows as a message with no reply; and one still running
// server-side will still land, because onResult refetches the thread when it
// can't find a pending bubble to fill.
function reconcileAfterReconnect() {
  inflight.clear();
  pending.clear();
  if (typeof activeConv === "number") openConversation(activeConv);
}

// Reconnect timing. The old loop slept a flat 1.5s forever, which is two
// separate problems wearing one coat. A refusal (below) isn't transient, so
// retrying it forty times a minute can't change the answer — it just makes the
// server log a warning per attempt. And on the deliberate restart path, where
// the app relaunches to apply its own edits, every open tab woke on the same
// tick and came back in lockstep. Exponential backoff answers the first, jitter
// smears the herd and answers the second. The ceiling stays in the tens of
// seconds because this is a dashboard someone leaves open all day: it has to
// come back by itself once the server does, without anyone reloading it.
const WS_RETRY_MIN_MS = 1500;
const WS_RETRY_MAX_MS = 30000;
// 1008 is the Guard refusing us (security.py): the allow-list came out empty
// because we couldn't work out who owns the tailnet, or the request looked
// cross-origin. That's a decision, not a blip, so back off to minutes rather
// than seconds — but keep retrying, because the thing that fixes it (tailscaled
// finishing its start-up, CONTINUUM_ALLOWED_LOGINS being set) happens on the
// server, and the tab should notice on its own when it does.
const WS_REFUSED_MIN_MS = 30000;
const WS_REFUSED_MAX_MS = 300000;
let wsRetryMs = WS_RETRY_MIN_MS;
let wsRetryMax = WS_RETRY_MAX_MS;
let wsRetryTimer = null;

// Jitter across the lower half of the current window, so two tabs knocked off
// by the same restart don't wake on the same millisecond and race each other.
// The timer guard matters: a failed connect fires error *and* close, and we only
// want one attempt queued from the pair.
function scheduleReconnect() {
  if (wsRetryTimer) return null;
  const wait = Math.round(wsRetryMs * (0.5 + Math.random() * 0.5));
  wsRetryTimer = setTimeout(() => { wsRetryTimer = null; connect(); }, wait);
  wsRetryMs = Math.min(wsRetryMs * 2, wsRetryMax);
  return wait;
}

// Run one handler in isolation. The event fan-out in onmessage is a flat list of
// independent reactions to a single message, and they used to share one call
// stack: if onResult choked on a malformed task.result, every handler after it
// was skipped without a sound. The ones near the end are exactly the ones you'd
// never notice going missing — loadSchedule and the tools/actions/dev/proactive
// checkbox syncs — so the toggles would sit there showing state the server does
// not actually have. Each handler gets its own try/catch, and a broken one is
// loud instead of silent.
function safely(what, fn) {
  try { fn(); } catch (e) { console.error(`continuum: ws handler failed (${what})`, e); }
}

// safely() only sees synchronous throws, and most of these handlers kick off
// async work nobody awaits (loadSchedule, loadStatus, openConversation…) — a
// rejection a tick later lands nowhere. Without this the only record is a
// devtools console nobody has open on a dashboard. We don't preventDefault: the
// browser's own report is welcome, this just puts our name on it.
window.addEventListener("unhandledrejection", (e) => {
  console.error("continuum: unhandled promise rejection", e && e.reason);
});

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onopen = () => {
    // A connection that actually opened earns a fresh budget — including the
    // ceiling, in case we were previously being refused.
    wsRetryMs = WS_RETRY_MIN_MS;
    wsRetryMax = WS_RETRY_MAX_MS;
    if (reloadOnReconnect) { location.reload(); return; }
    setPill($("pill-conn"), "ok", "connected");
    if (everConnected) reconcileAfterReconnect();
    everConnected = true;
  };
  // The error event carries no detail on purpose — the spec withholds it so a
  // page can't use a socket to probe what's listening — and a close event always
  // follows it, so the retry belongs there, where the code is known. This exists
  // because a connect that never got off the ground used to make no sound at all.
  ws.onerror = () => {
    console.warn("continuum: ws error — the reason, if any, arrives with the close code");
  };
  ws.onclose = (e) => {
    const code = e && e.code;
    const refused = code === 1008;
    if (refused) {
      // Say what actually happened. "disconnected" reads as "the network
      // hiccuped, it'll be back" and sends you looking in the wrong place; this
      // is the server declining to let this browser in at all.
      wsRetryMax = WS_REFUSED_MAX_MS;
      wsRetryMs = Math.max(wsRetryMs, WS_REFUSED_MIN_MS);
      setPill($("pill-conn"), "bad", "refused · not authorised");
    } else {
      wsRetryMax = WS_RETRY_MAX_MS;
      wsRetryMs = Math.min(wsRetryMs, WS_RETRY_MAX_MS);
      setPill($("pill-conn"), "bad", "disconnected");
    }
    const wait = scheduleReconnect();
    // The close code used to be dropped on the floor, which is why a refusal and
    // a dead server looked identical from the front end.
    console.warn(`continuum: ws closed (code ${code}${refused ? " — refused by the server's guard" : ""})` +
                 (wait == null ? ", retry already queued" : `, retrying in ${Math.round(wait / 1000)}s`));
  };
  ws.onmessage = (m) => {
    let msg;
    // A frame that isn't the JSON we expect is the server's problem; it is not a
    // reason for this tab to stop processing every frame that comes after it.
    try {
      msg = JSON.parse(m.data);
    } catch (e) {
      console.error("continuum: unparseable ws frame", e, m.data);
      return;
    }
    if (!msg || typeof msg !== "object") return;   // "null"/a bare number is not a message
    if (msg.type === "hello") {
      safely("hello", () => renderDevices(msg.devices || []));
    } else if (msg.type === "state") {
      safely("state", () => {
        window.__lastState = msg.state;
        renderState(msg.state);
        // keep the dashboard's State tile live while it's the visible view
        if ($("view-dash-mind").classList.contains("active")) loadDashMind();
      });
    } else if (msg.type === "app_restarting") {
      reloadOnReconnect = true;
      setPill($("pill-conn"), "bad", "restarting…");
    } else if (msg.type === "event") {
      const ev = msg.event || {};
      // Topic match and isolation in one helper, so the fan-out below still
      // reads as a flat list of "when this happens, do that" — except now each
      // line stands or falls on its own instead of taking its neighbours down.
      const on = (topic, fn) => {
        const hit = Array.isArray(topic) ? topic.includes(ev.topic) : ev.topic === topic;
        if (hit) safely(ev.topic, fn);
      };
      safely("event row", () => pushEvent(ev));
      on("device.register", () => loadStatus());
      on("agent.step", () => addStep(ev.payload || {}));
      on("task.result", () => onResult(ev.payload || {}));
      on("memory.stored", () => {
        const op = (ev.payload || {}).op;
        // ADD → prepend; UPDATE/NOOP → refresh so supersession/dedup shows correctly.
        if (op === "ADD" && ev.payload.id > 0) prependMemory(ev.payload);
        else if (op === "UPDATE" || op === "NOOP") loadMemory($("mem-search").value.trim());
        loadStatus(); // keep the memory pill's count accurate
      });
      on("memory.deleted", () => {
        const el = $("mem-list").querySelector(`.mem[data-id="${(ev.payload || {}).id}"]`);
        if (el) el.remove();
        loadStatus();
      });
      on("conversation.tagged", () => loadConversations());
      on("question.asked", () => showAsk(ev.payload || {}));
      on("question.answered", () => dismissAsk((ev.payload || {}).id));
      on(["action.performed", "reminder.fired", "schedule.changed"], () => loadSchedule());
      on("memory.core.set", () => setCoreText((ev.payload || {}).text));
      on("memory.autoextract", () => {
        if (document.activeElement !== $("ax")) $("ax").checked = !!(ev.payload || {}).on;
      });
      on("agent.tools", () => { $("tools").checked = !!(ev.payload || {}).on; });
      on("agent.actions", () => {
        if (document.activeElement !== $("actions")) $("actions").checked = !!(ev.payload || {}).on;
      });
      on("agent.dev", () => {
        if (document.activeElement !== $("dev")) $("dev").checked = !!(ev.payload || {}).on;
      });
      on("agent.proactive", () => {
        if (document.activeElement !== $("proactive")) $("proactive").checked = !!(ev.payload || {}).on;
      });
      on("proactive.surfaced", () => {
        const p = ev.payload || {};
        if (p.conversation_id && p.conversation_id === activeConv) {
          const el = assistantBubble();
          el.querySelector(".meta").innerHTML = '<span class="badge local">🌱 proactive</span>';
          const t = el.querySelector(".text"); t.className = "text"; t.textContent = p.message;
          clearEmpty(); threadBox().appendChild(el); scrollThread();
        }
      });
    }
  };
}

// ---- ask_user: a mid-turn question Continuum is BLOCKING on -----------------
// The turn parked itself waiting for an answer; we render tappable options (plus
// a free-text box) and POST the reply to /api/answer, which resolves the parked
// call so the SAME turn continues. The answer goes over HTTP, not the socket,
// because the socket's receive loop is blocked inside the running turn.
function showAsk(p) {
  if (!p || !p.id) return;
  const panel = $("ask-panel");
  let card = panel.querySelector(`.ask-card[data-qid="${p.id}"]`);
  if (!card) {
    card = document.createElement("div");
    card.className = "ask-card";
    card.dataset.qid = p.id;
    panel.appendChild(card);
  }
  card.innerHTML = "";
  const q = document.createElement("div");
  q.className = "ask-q";
  q.textContent = p.question || "";
  card.appendChild(q);

  const opts = document.createElement("div");
  opts.className = "ask-opts";
  (p.options || []).forEach((opt) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "ask-opt";
    b.textContent = opt;
    b.addEventListener("click", () => sendAnswer(p.id, opt, card));
    opts.appendChild(b);
  });
  card.appendChild(opts);

  const form = document.createElement("form");
  form.className = "ask-free";
  const inp = document.createElement("input");
  inp.type = "text";
  inp.placeholder = "or type a reply…";
  const send = document.createElement("button");
  send.type = "submit";
  send.textContent = "Send";
  form.appendChild(inp);
  form.appendChild(send);
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const t = inp.value.trim();
    if (t) sendAnswer(p.id, t, card);
  });
  card.appendChild(form);

  panel.hidden = false;
  (opts.querySelector("button") || inp).focus();
}

async function sendAnswer(qid, choice, card) {
  if (card) {
    if (card.dataset.sent) return; // guard against double-tap / enter race
    card.dataset.sent = "1";
    card.querySelectorAll("button, input").forEach((el) => (el.disabled = true));
  }
  // This is the worst one in the file to drop on the floor. A turn is PARKED
  // inside ask_user waiting on this POST; the card locks itself before the send
  // to stop a double-tap, and the old .catch(() => {}) meant a failed send left
  // a permanently disabled card in front of a permanently stuck turn, with no
  // sign anything had gone wrong. Unlock on failure so it can be retried.
  const r = await jwrite("send that answer", "/api/answer", jbody("POST", { id: qid, choice }));
  if (!r.ok && card) {
    delete card.dataset.sent;
    card.querySelectorAll("button, input").forEach((el) => (el.disabled = false));
  }
  // On success the card is dismissed by the question.answered event, not here.
}

function dismissAsk(qid) {
  const panel = $("ask-panel");
  const card = panel.querySelector(`.ask-card[data-qid="${qid}"]`);
  if (card) card.remove();
  if (!panel.querySelector(".ask-card")) panel.hidden = true;
}

// Images Jake has attached to the message he's composing (uploaded but not yet
// sent). Each is {id, name, url}; cleared once the task is submitted.
let pendingAttachments = [];

function renderAttachStrip() {
  const strip = $("attach-strip");
  if (!strip) return;
  strip.innerHTML = pendingAttachments.map((a, i) =>
    `<span class="attach-chip">` +
    `<img src="${a.url}" alt="${escapeHtml(a.name || "image")}" />` +
    `<button type="button" class="attach-x" data-i="${i}" title="remove">×</button>` +
    `</span>`).join("");
  strip.hidden = pendingAttachments.length === 0;
}

function readDataUrl(file) {
  return new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => res(r.result);
    r.onerror = () => rej(r.error);
    r.readAsDataURL(file);
  });
}

// Re-encode through a canvas to a downscaled JPEG. This is what makes the phone
// work: the gallery hands us whatever the camera saved (often HEIC/HEIF, or a
// 12MP original), and the store only accepts PNG/JPEG/GIF/WEBP and the vision
// path can't read HEIC — so we normalise here rather than reject there. Returns
// a data: URL, or null if the browser couldn't decode the source (then the
// caller falls back to posting the raw bytes and surfaces any error).
function toJpegDataUrl(dataUrl) {
  return new Promise((res) => {
    const img = new Image();
    img.onload = () => {
      const max = 2048; // plenty for reading a label or a recipe; keeps upload small
      const scale = Math.min(1, max / Math.max(img.width, img.height));
      const w = Math.round(img.width * scale), h = Math.round(img.height * scale);
      if (!w || !h) { res(null); return; }
      const canvas = document.createElement("canvas");
      canvas.width = w; canvas.height = h;
      const ctx = canvas.getContext("2d");
      if (!ctx) { res(null); return; }
      ctx.drawImage(img, 0, 0, w, h);
      try { res(canvas.toDataURL("image/jpeg", 0.9)); } catch (_) { res(null); }
    };
    img.onerror = () => res(null);
    img.src = dataUrl;
  });
}

async function uploadFile(file) {
  const raw = await readDataUrl(file);
  // Prefer the canvas re-encode; fall back to the original bytes if the browser
  // couldn't decode it (then the store's own validation gives the real reason).
  const dataUrl = (await toJpegDataUrl(raw)) || raw;
  // Throws rather than toasting: the caller renders the reason inside the
  // composer via showAttachError, which is where Jake is actually looking.
  // jfetch adds the cases the hand-rolled version missed — a non-JSON body
  // (await resp.json() threw "Unexpected token <") and the network being down.
  return await jfetch("/api/upload", jbody("POST", { name: file.name, data: dataUrl }));
}

// Show an attach failure inside the composer. alert() is useless on the phone —
// the WebView's chrome client doesn't handle onJsAlert, so alert() is silently
// dropped there, which is exactly why a failed upload looked like "nothing
// happens". This renders where Jake is actually looking.
function showAttachError(msg) {
  const strip = $("attach-strip");
  if (!strip) return;
  const div = document.createElement("div");
  div.className = "attach-error";
  div.style.cssText = "color:#ff9b9b;font-size:12px;padding:4px 2px;width:100%";
  div.textContent = msg;
  strip.appendChild(div);
  strip.hidden = false;
}

if ($("attach-btn")) {
  $("attach-btn").addEventListener("click", () => $("attach-input").click());
  $("attach-input").addEventListener("change", async (e) => {
    const files = Array.from(e.target.files || []);
    e.target.value = ""; // let the same file be picked again later
    $("attach-strip").querySelectorAll(".attach-error").forEach((n) => n.remove());
    // The native picker can hand back an empty result (e.g. the WebView activity
    // was recreated behind the gallery on a low-memory phone, losing the file
    // callback). That path is otherwise silent — the loop just doesn't run — and
    // reads as "I picked an image and nothing happened". Say so out loud instead.
    if (!files.length) {
      showAttachError("The picker returned no image to attach — nothing came back from the gallery.");
      return;
    }
    const errors = [];
    for (const f of files) {
      try {
        const meta = await uploadFile(f);
        pendingAttachments.push(meta);
        renderAttachStrip();
      } catch (err) {
        errors.push((f.name || "image") + ": " + err.message);
      }
    }
    if (errors.length) showAttachError("Couldn't attach — " + errors.join("; "));
  });
  $("attach-strip").addEventListener("click", (e) => {
    const x = e.target.closest(".attach-x");
    if (!x) return;
    pendingAttachments.splice(Number(x.dataset.i), 1);
    renderAttachStrip();
  });
}

$("task-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const prompt = $("prompt").value.trim();
  const attachIds = pendingAttachments.map((a) => a.id);
  // A message needs either text or at least one image.
  if ((!prompt && !attachIds.length) || !ws || ws.readyState !== WebSocket.OPEN) return;
  const complexity = $("complexity").value;
  const sensitive = $("sensitive").checked;
  const corr = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random())).replace(/-/g, "").slice(0, 12);
  appendUser(prompt, pendingAttachments);
  appendPendingAssistant(corr);
  const isNew = activeConv === "new";
  ws.send(JSON.stringify({
    type: "task", corr, prompt, complexity, sensitive,
    conversation_id: isNew ? null : activeConv, new_task: isNew,
    attachments: attachIds,
  }));
  pendingAttachments = [];
  renderAttachStrip();
  $("prompt").value = "";
  $("prompt").focus();
});

// Ctrl/Cmd+Enter (or plain Enter) sends.
$("prompt").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey || !e.shiftKey)) {
    e.preventDefault();
    $("task-form").requestSubmit();
  }
});

$("conv-new-btn").addEventListener("click", newConversation);
$("conv-history-btn").addEventListener("click", () => {
  const el = $("conv-list");
  const show = el.style.display === "none";
  el.style.display = show ? "block" : "none";
  if (show) loadConversations();
});

// Store an explicit memory (global, or scoped to the active conversation).
$("mem-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = $("mem-input").value.trim();
  if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;
  const kind = /\b(prefer|like|hate|always|never|favou?rite)\b/i.test(text) ? "preference" : "fact";
  const scoped = $("mem-scope").value === "conversation" && activeConv;
  ws.send(JSON.stringify({ type: "remember", text, kind,
    conversation_id: scoped ? activeConv : null }));
  $("mem-input").value = "";
});

// Save the pinned core profile.
$("core-save").addEventListener("click", () => {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({ type: "core", text: $("core-text").value }));
  const btn = $("core-save");
  btn.classList.add("saved"); btn.textContent = "✓ saved";
  setTimeout(() => { btn.classList.remove("saved"); btn.textContent = "Save profile"; }, 1400);
});

// Toggle auto-learning of facts from conversation.
$("ax").addEventListener("change", (e) => {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({ type: "autoextract", on: e.target.checked }));
});

// Toggle agentic tool use.
$("tools").addEventListener("change", (e) => {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({ type: "tools", on: e.target.checked }));
});

// Toggle whether Continuum may TAKE actions (notify, open URLs/apps).
$("actions").addEventListener("change", (e) => {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({ type: "actions", on: e.target.checked }));
});

// Toggle whether Continuum may develop its OWN code (read/edit/run/restart).
$("dev").addEventListener("change", (e) => {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({ type: "dev", on: e.target.checked }));
});

// Toggle whether Continuum may speak up on its own.
$("proactive").addEventListener("change", (e) => {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({ type: "proactive", on: e.target.checked }));
});

// Manual "what's on your mind?" nudge.
$("nudge-btn").addEventListener("click", () => {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "proactive_now" }));
});

// Semantic recall search (debounced); empty box restores the recent list.
let searchTimer;
$("mem-search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  const q = e.target.value.trim();
  searchTimer = setTimeout(() => loadMemory(q), 250);
});

async function loadState() {
  try {
    const s = await (await fetch("/api/state")).json();
    window.__lastState = s;
    renderState(s);
  } catch (e) { /* the ws push will fill it in shortly */ }
}

function _agoShort(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "now";
  if (s < 3600) return Math.floor(s / 60) + "m";
  if (s < 86400) return Math.floor(s / 3600) + "h";
  return Math.floor(s / 86400) + "d";
}

async function loadMessages(q) {
  const url = "/api/messages?limit=60" + (q ? "&q=" + encodeURIComponent(q) : "");
  const d = await jread(url);
  // "no messages logged yet…" over a dead read is how you miss one.
  if (!d) return loadFail("messages", "your messages", () => loadMessages(q), loadMessages);
  loadOk(loadMessages);
  const rows = d.messages || [];
  $("msg-count").textContent = rows.length;
  const box = $("msg-feed");
  if (!rows.length) { box.innerHTML = '<div class="empty">no messages logged yet…</div>'; return; }
  // newest first for the feed
  box.innerHTML = rows.slice().reverse().map(m => {
    const ctx = m.title ? `${m.app} · ${m.title}` : m.app;
    const who = m.author ? `<span class="mf-who">${escapeHtml(m.author)}</span> ` : "";
    const src = m.source === "notification" ? "🔔" : "🖥";
    return `<div class="mf-item">` +
      `<div class="mf-top"><span class="mf-app">${src} ${escapeHtml(ctx || "")}</span>` +
      `<span class="mf-ago">${_agoShort(m.ts)}</span></div>` +
      `<div class="mf-body">${who}${escapeHtml(m.body || "")}</div></div>`;
  }).join("");
}

// ---- sections + subtabs ----
// Two top-level sections (Mind, Life), each with its own subtab strip and a
// dashboard. All the subtab buttons live in one .tabnav; switching section just
// hides the other section's buttons and lands on that section's last subtab.
const SECTION_OF = {                       // which section each view belongs to
  "dash-mind": "mind", chat: "mind", messages: "mind", state: "mind", memory: "mind",
  "dash-life": "life", routine: "life", workouts: "life", nutrition: "life", body: "life", health: "life", flashcards: "life", activity: "life",
};
const _lastSubtab = { mind: "dash-mind", life: "dash-life" };  // remember per section
let _section = "mind";

function switchTab(view) {
  const section = SECTION_OF[view] || _section;
  _section = section;
  _lastSubtab[section] = view;
  // top-level section highlight
  document.querySelectorAll("#sectionnav .section").forEach((b) =>
    b.classList.toggle("active", b.dataset.section === section));
  // show only this section's subtab buttons, highlight the active one
  document.querySelectorAll("#tabnav .tab").forEach((t) => {
    t.hidden = (t.dataset.section !== section);
    t.classList.toggle("active", t.dataset.view === view);
  });
  document.querySelectorAll(".view").forEach((v) =>
    v.classList.toggle("active", v.id === "view-" + view));
  if (view === "dash-mind") loadDashMind();
  else if (view === "chat") requestAnimationFrame(scrollThread);  // land on the latest turn
  else if (view === "dash-life") loadDashLife();
  else if (view === "routine") { loadRoutines(); loadTodos(); }
  else if (view === "messages") loadMessages();
  // `loadPower` is deliberately outside the `__lastState` guard: the power panel
  // matters most when the desk is OFFLINE, which is precisely when no live state
  // has ever arrived to render. Gating both on it would hide the Wake button in
  // the one situation it exists for.
  else if (view === "state") { loadPower(); if (window.__lastState) renderState(window.__lastState); }
  else if (view === "workouts") loadWorkouts();
  else if (view === "nutrition") loadNutrition();
  else if (view === "body") loadBody();
  else if (view === "health") loadHealth();
  else if (view === "flashcards") loadFlashcards();
  else if (view === "alarms") loadAlarms();
  else if (view === "activity") loadActivity();
  else if (view === "budget") loadBudget();
}
function switchSection(section) {
  switchTab(_lastSubtab[section] || (section === "life" ? "dash-life" : "dash-mind"));
}
document.getElementById("tabnav").addEventListener("click", (e) => {
  const btn = e.target.closest(".tab");
  if (btn) switchTab(btn.dataset.view);
});
document.getElementById("sectionnav").addEventListener("click", (e) => {
  const btn = e.target.closest(".section");
  if (btn) switchSection(btn.dataset.section);
});

// External deep-links into a specific tab. The phone shell launches the WebView
// with a #view hash (e.g. the home-screen routine widget opens "#routine"), so
// honour a hash on load and on any later change. gotoView is also hung on window
// so the native shell can re-navigate an already-open page — no reload, no lost
// chat state — via evaluateJavascript. Unknown/blank views are a safe no-op, so
// an ordinary launch with no hash keeps the default tab.
function gotoView(v) {
  if (v && SECTION_OF[v]) switchTab(v);
}
window.gotoView = gotoView;
window.addEventListener("hashchange", () => gotoView(location.hash.replace(/^#/, "")));
gotoView(location.hash.replace(/^#/, ""));

// ---- dashboards (one per section) ----
// Summary tiles that deep-link into each subtab. Best-effort: a failed fetch
// leaves that tile showing a dash rather than blanking the whole board. Tiles
// carry data-go and are wired by one delegated listener per grid.
// opts: { accent: "r,g,b" hue for this tile, frac: 0..1 draws a progress meter }
// The one sub-caption that means "this number is missing, not zero". Shared so
// _dashTile can recognise it and mark the tile retryable — the tiles below all
// have empty states that read as fact ("0 kcal", "nothing scheduled", "Memory
// 0"), and a failed read must never borrow one.
const DASH_DEAD = "couldn't load — tap to retry";

function _dashTile(go, icon, name, big, sub, opts) {
  opts = opts || {};
  const accent = opts.accent || "91,140,255";
  const meter = (opts.frac != null)
    ? `<div class="dtile-meter"><span style="width:${Math.max(0, Math.min(100, Math.round(opts.frac * 100)))}%"></span></div>`
    : "";
  // Flag the dead ones so _wireDash can honour the "tap to retry" they promise.
  const dead = sub === DASH_DEAD ? " data-dead=\"1\"" : "";
  return `<button class="dtile" data-go="${go}"${dead} style="--tile:${accent}">` +
    `<div class="dtile-top"><span class="dtile-icon">${icon}</span>` +
    `<span class="dtile-name">${name}</span><span class="dtile-arrow">→</span></div>` +
    `<div class="dtile-big">${big}</div>` +
    `<div class="dtile-sub">${sub || ""}</div>${meter}</button>`;
}
function _wireDash(grid) {
  grid.addEventListener("click", (e) => {
    const t = e.target.closest(".dtile");
    if (!t) return;
    // A dead tile reloads the dashboard rather than navigating. switchTab does
    // re-fetch for most views, but not all — "chat" only scrolls and "memory"
    // matches no branch at all — so routing the retry through it would leave
    // those two tiles saying "tap to retry" and then not retrying.
    if (t.dataset.dead) {
      (grid.id === "dash-life-grid" ? loadDashLife : loadDashMind)();
      return;
    }
    switchTab(t.dataset.go);
  });
}

// A radial progress dial for one daily macro target. The arc fills to val/target
// (capped at a full ring); the centre shows the percentage, the caption the raw
// numbers. `warnOver` colours an over-budget ring red — true for calories/carbs/
// fat (going over is bad), false for protein (exceeding the target is good).
function _macroDial(name, accent, val, target, unit, warnOver, note) {
  const C = 201.06;                       // 2·π·r for r=32
  const frac = target > 0 ? val / target : 0;
  const off = C * (1 - Math.max(0, Math.min(1, frac)));
  const over = target > 0 && val > target;
  const warn = over && warnOver !== false;
  // `note` is an optional caption tail — e.g. the sedentary base + today's burn
  // that make up an activity-adjusted calorie target.
  const sub = (target > 0
    ? `${Math.round(val)} / ${Math.round(target)}${unit}`
    : `${Math.round(val)}${unit}`) + (note ? ` · ${note}` : "");
  return `<div class="mdial${warn ? " over" : ""}" style="--dial:${accent}">` +
    `<svg class="mdial-ring" viewBox="0 0 80 80" aria-hidden="true">` +
      `<circle class="mdial-track" cx="40" cy="40" r="32"></circle>` +
      `<circle class="mdial-arc" cx="40" cy="40" r="32" ` +
        `stroke-dasharray="${C.toFixed(1)}" stroke-dashoffset="${off.toFixed(1)}"></circle>` +
      `<text class="mdial-val" x="40" y="41">${target > 0 ? Math.round(frac * 100) + "%" : "—"}</text>` +
    `</svg>` +
    `<div class="mdial-cap"><span class="mdial-name">${name}</span>` +
    `<span class="mdial-sub">${sub}</span></div></div>`;
}

async function loadDashMind() {
  const grid = $("dash-mind-grid");
  if (!grid) return;
  try {
    const [sRaw, mRaw, cRaw] = await Promise.all(
      ["/api/status", "/api/messages?limit=1", "/api/conversations"].map(jread));
    // Same rule as loadDashLife: these tiles have empty states that read as
    // fact, so a failed read must not borrow one. This one had the worst
    // version of it — `status.memory_count ?? 0` rendered a dead /api/status as
    // "Memory 0 · facts & preferences stored", i.e. it told Jake the agent had
    // forgotten everything it knows about him. It hadn't; the request failed.
    const DEAD = DASH_DEAD;
    const status = sRaw || {}, msgs = mRaw || {}, convs = cRaw || {};
    const st = window.__lastState || {};
    // chat — conversation count, most recent title
    const list = convs.conversations || [];
    const recent = list.find((c) => c.kind !== "main") || list[0];
    const cBig = cRaw == null ? "—" : list.length;
    const cSub = cRaw == null ? DEAD
      : (recent ? escapeHtml(recent.title) : (list.length === 1 ? "conversation" : "conversations"));
    // messages — most recent logged message
    const m0 = (msgs.messages || [])[0];
    const msgBig = m0 ? escapeHtml(m0.app || "message") : "—";
    const msgSub = mRaw == null ? DEAD
      : (m0 ? escapeHtml((m0.body || "").slice(0, 64)) : "no messages logged yet");
    // state — foreground app + system load. Sourced from the cached __lastState,
    // not a read here, so it says "?" rather than DEAD when it's missing.
    const fg = (st.apps || []).find((a) => a.foreground);
    const sys = st.system || {};
    const stSub = `CPU ${sys.cpu_pct ?? "?"}% · RAM ${sys.mem_pct ?? "?"}%`;
    // memory — count + first line of core profile
    const core = (status.core || "").split("\n").map((s) => s.trim()).filter(Boolean)[0];
    grid.innerHTML =
      _dashTile("chat", "💬", "Chat", cBig, cSub, { accent: "91,140,255" }) +
      _dashTile("messages", "✉", "Messages", msgBig, msgSub, { accent: "185,139,255" }) +
      _dashTile("state", "📊", "State", fg ? escapeHtml(fg.name) : "—", stSub, { accent: "53,196,138" }) +
      _dashTile("memory", "🧠", "Memory", sRaw == null ? "—" : (status.memory_count ?? 0),
        sRaw == null ? DEAD : (core ? escapeHtml(core) : "facts & preferences stored"),
        { accent: "255,180,84" });
  } catch (e) {
    // Reads can't land here — jread swallows those and the tiles say DEAD instead.
    // Anything reaching this is a render bug, which used to be swallowed silently
    // and left the previous tiles up looking current. Match loadDashLife and say so.
    toast("dashboard failed to render: " + (e.message || e), "warn");
  }
}

async function loadDashLife() {
  const grid = $("dash-life-grid");
  if (!grid) return;
  try {
    const [rRaw, nRaw, wRaw, aRaw, bRaw, hRaw] = await Promise.all(
      ["/api/routines", "/api/nutrition", "/api/workouts?limit=1", "/api/activity",
       "/api/body", "/api/health"].map(jread));
    // Every tile below has an empty state that reads as fact — "nothing
    // scheduled", "0 kcal", "no sessions", "log weight". A read that failed
    // must not borrow one of those; it says so instead. (These used to
    // .catch(() => ({})), so one server hiccup told you you'd done nothing
    // all day — the kind of lie you'd act on by re-logging a meal.)
    const DEAD = DASH_DEAD;
    const routines = rRaw || {}, nut = nRaw || {}, wlist = wRaw || {},
          act = aRaw || {}, bodyd = bRaw || {}, hlth = hRaw || {};
    const nDead = nRaw == null, bDead = bRaw == null;
    const failed = [rRaw, nRaw, wRaw, aRaw, bRaw, hRaw].filter((x) => x == null).length;
    // routine — today's completion
    const t = routines.today || {};
    const rNext = (t.items || []).find((it) => !it.done && !it.skipped);
    const rSub = rRaw == null ? DEAD
      : (rNext ? "next: " + escapeHtml(rNext.title)
               : (t.total ? "all done 🎉" : "nothing scheduled"));
    // nutrition — calories today vs goal
    const tot = nut.totals || {}, g = nut.goals || {};
    const cal = Math.round(tot.calories || 0);
    const nSub = nDead ? DEAD
      : (g.set ? `of ${Math.round(g.calories || 0)} kcal goal`
               : `P${Math.round(tot.protein_g || 0)} C${Math.round(tot.carbs_g || 0)} F${Math.round(tot.fat_g || 0)}`);
    // workouts — last session + weekly volume
    const last = (wlist.sessions || [])[0];
    const wBig = wRaw == null ? "—" : (last ? escapeHtml(last.date || "—") : "—");
    const wSub = wRaw == null ? DEAD
      : (last
        ? `${escapeHtml(last.title || "Workout")} · ${Math.round(last.total_volume || 0).toLocaleString()}kg`
        : "no sessions — hit Sync");
    // activity — active time today
    const aMin = Math.round(((act.desktop_seconds || 0) + (act.phone_seconds || 0)) / 60);
    const aSub = aRaw == null ? DEAD
      : (act.top_app ? "most on " + escapeHtml(act.top_app) : "desktop + phone today");
    // body — BMI (or weight) headline; steps + active burn as the sub
    const bBig = bDead ? "—"
      : (bodyd.bmi ? bodyd.bmi.value
                   : (bodyd.weight_effective_kg ? `${(+bodyd.weight_effective_kg)}kg` : "—"));
    const bSub = bDead ? DEAD
      : (bodyd.bmi
        ? `${escapeHtml(bodyd.bmi.category)} · ${bodyd.steps || 0} steps · ~${bodyd.expenditure_kcal || 0} kcal`
        : (bodyd.weight_effective_kg ? "add height for BMI" : "log weight, height & steps"));
    // health — readiness headline (or last sleep as a fallback); recovery sub
    const hr = hlth.readiness;
    const hBig = hRaw == null ? "—" : (hr ? hr.score : (hlth.asleep_label || "—"));
    const hSub = hRaw == null ? DEAD
      : (hr
        ? `${escapeHtml(hr.category)} · ${hlth.asleep_label || "no sleep"}${hlth.resting_hr ? " · " + hlth.resting_hr + " bpm" : ""}`
        : (hlth.has_data ? "add sleep or HRV for readiness" : "no recovery data yet"));
    const hFrac = hr ? hr.score / 100 : null;
    const rFrac = t.total ? (t.done || 0) / t.total : null;
    const goalCal = Math.round(g.calories || 0);
    const nFrac = (g.set && goalCal) ? cal / goalCal : null;
    // Macro target dials — a radial gauge per goal, above the deep-link tiles.
    const dials = $("dash-life-dials");
    if (dials) {
      // When calories are activity-adjusted, the target = sedentary base + today's
      // active burn; surface both so the dial shows why the goal moved.
      const calNote = g.activity_kcal
        ? `base ${Math.round(g.base_calories || 0)} +${g.activity_kcal}🔥`
        : "";
      // Steps ring toward a conventional 10k daily goal. Shown regardless of
      // whether nutrition targets are set (steps are body data, independent), and
      // never warns on overshoot — beating the step goal is good. Its caption
      // carries the calories those steps burned: the same figure that lifts the
      // adaptive calorie target, so the Steps and Calories dials read together.
      const STEP_GOAL = 10000;
      const stepsDial = bDead ? "" : _macroDial("Steps", "53,196,138", bodyd.steps || 0, STEP_GOAL,
        "", false, `~${bodyd.steps_kcal || 0} kcal🔥`);
      // nDead is checked before g.set: a failed read also leaves g.set false, and
      // "No targets set" is a much worse thing to say to someone who *has* set
      // targets than "couldn't load" is.
      const macroDials = nDead
        ? `<div class="dials-empty">Couldn't load your targets — this is a connection ` +
          `problem, not a change to your goals.</div>`
        : (g.set
          ? _macroDial("Calories", "255,120,90", tot.calories || 0, g.calories || 0, "", true, calNote) +
            _macroDial("Protein", "91,140,255", tot.protein_g || 0, g.protein_g || 0, "g", false) +
            _macroDial("Carbs", "255,180,84", tot.carbs_g || 0, g.carbs_g || 0, "g", true) +
            _macroDial("Fat", "185,139,255", tot.fat_g || 0, g.fat_g || 0, "g", true)
          : `<div class="dials-empty">No targets set — ask me to set your macro goals ` +
            `(or use 🎯 in the Nutrition tab) to see progress dials here.</div>`);
      dials.innerHTML = macroDials + stepsDial;
    }
    grid.innerHTML =
      _dashTile("routine", "🗓", "Routine", rRaw == null ? "—" : `${t.done || 0}/${t.total || 0}`, rSub,
        { accent: "53,196,138", frac: rFrac }) +
      _dashTile("nutrition", "🍎", "Nutrition", nDead ? "—" : `${cal} kcal`, nSub,
        { accent: "255,120,90", frac: nFrac }) +
      _dashTile("body", "⚖", "Body", bBig, bSub, { accent: "255,180,84" }) +
      _dashTile("health", "❤", "Health", hBig, hSub, { accent: "94,201,168", frac: hFrac }) +
      _dashTile("workouts", "🏋", "Workouts", wBig, wSub, { accent: "185,139,255" }) +
      _dashTile("activity", "🕘", "Activity", aRaw == null ? "—" : fmtMin(aMin), aSub,
        { accent: "91,140,255" });
    // One toast for the whole grid, not six.
    if (failed) {
      toast(`${failed} of 6 panels couldn't load — those tiles show "couldn't load", `
        + `not real numbers.`, "warn");
    }
  } catch (e) {
    toast("dashboard failed to render: " + (e.message || e), "warn");
  }
}
_wireDash($("dash-mind-grid"));
_wireDash($("dash-life-grid"));

// ---- routine ----
const _WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const _MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
// UI-only collapse state, kept across the re-renders that follow every tick so a
// section/item you opened stays open. Sections default expanded; subtasks default
// collapsed (only the ids you open are in _rtSubOpen).
const _rtCollapsedSec = new Set();
const _rtSubOpen = new Set();
let _rtToday = null;   // last today() payload, so collapse toggles re-render without a refetch

async function loadRoutines() {
  const d = await jread("/api/routines");
  // An empty today() is a rest day. A failed read is not, and the two rendered
  // identically — which is a day's training quietly written off.
  if (!d) return loadFail("routine", "your routine", loadRoutines);
  loadOk(loadRoutines);
  renderRoutineToday(d.today);
  renderRoutineAll(d.all || []);
}

// ---- Routine sub-tabs (Today | Performance) ----
let _perfDays = 30;
function switchRTab(which) {
  document.querySelectorAll(".rtab").forEach((t) => t.classList.toggle("active", t.dataset.rtab === which));
  document.querySelectorAll(".rtab-view").forEach((v) => v.classList.toggle("active", v.id === "rtab-" + which));
  if (which === "performance") loadPerformance();
}
const _rtabNav = document.getElementById("rtab-nav");
if (_rtabNav) _rtabNav.addEventListener("click", (e) => {
  const b = e.target.closest(".rtab");
  if (b) switchRTab(b.dataset.rtab);
});
const _perfRange = document.getElementById("perf-range");
if (_perfRange) _perfRange.addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  _perfDays = Number(b.dataset.days);
  _perfRange.querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
  loadPerformance();
});

async function loadPerformance() {
  const p = await jread("/api/routines/performance?days=" + _perfDays);
  // Adherence percentages are the one thing here Jake judges himself on. A failed
  // read must not be allowed to render as a bad month.
  if (!p) return loadFail("routine", "your performance", loadPerformance);
  loadOk(loadPerformance);
  renderPerformance(p);
}

function _rateColor(r) {
  if (r == null) return "var(--muted)";
  if (r >= 80) return "#35c48a";
  if (r >= 50) return "#ffb27a";
  return "#ff5a78";
}

function renderPerformance(p) {
  const ov = p.overall || {};
  $("perf-since").textContent = p.since ? `since ${p.since}` : "";
  const stat = $("perf-stat");
  if (!ov.scheduled) {
    stat.innerHTML = '<div class="empty">No routine history yet — this fills in as you complete or skip steps each day.</div>';
    $("perf-trend").innerHTML = ""; $("perf-list").innerHTML = '<div class="empty">check back tomorrow</div>';
    return;
  }
  const rate = ov.rate == null ? "—" : ov.rate + "%";
  stat.innerHTML =
    `<div class="perf-big" style="color:${_rateColor(ov.rate)}">${rate}</div>` +
    `<div class="perf-breakdown">` +
      `<span class="pb done">${ov.done} done</span>` +
      `<span class="pb missed">${ov.missed} missed</span>` +
      `<span class="pb skipped">${ov.skipped} skipped</span>` +
      `<div class="perf-sub">adherence over ${p.window_days} days (skipped days excluded)</div>` +
    `</div>`;

  // daily trend bars
  const trend = $("perf-trend");
  const daily = p.daily || [];
  trend.innerHTML = daily.map((d) => {
    const h = d.rate == null ? 6 : Math.max(6, d.rate);
    const tip = `${d.date}: ${d.rate == null ? "all skipped" : d.rate + "% (" + d.done + "/" + d.scheduled + ")"}`;
    return `<span class="perf-bar" style="height:${h}%;background:${_rateColor(d.rate)}" title="${tip}"></span>`;
  }).join("") || '<div class="empty">no days yet</div>';

  // per-task list
  const list = $("perf-list");
  const rows = (p.per_routine || []).filter((r) => r.scheduled);
  if (!rows.length) { list.innerHTML = '<div class="empty">no tasks scheduled in this window</div>'; return; }
  list.innerHTML = rows.map((r) => {
    const rc = _rateColor(r.rate);
    const streak = r.streak > 0 ? `<span class="perf-streak">🔥${r.streak}</span>` : "";
    return `<div class="perf-row">` +
      `<span class="perf-rtitle">${escapeHtml(r.title)}</span>${streak}` +
      `<div class="perf-track"><span style="width:${r.rate || 0}%;background:${rc}"></span></div>` +
      `<span class="perf-rrate" style="color:${rc}">${r.rate == null ? "—" : r.rate + "%"}</span>` +
      `<span class="perf-rcount">${r.done}/${r.scheduled}</span></div>`;
  }).join("");
}

function renderRoutineToday(t) {
  const box = $("rt-today");
  if (!t) return;
  _rtToday = t;
  const [, m, dd] = t.date.split("-").map(Number);
  $("rt-date").textContent = `${_WD[t.weekday]} ${dd} ${_MON[m - 1]}`;
  const pct = t.total ? Math.round((100 * t.done) / t.total) : 0;
  $("rt-bar-fill").style.width = pct + "%";
  $("rt-progress-label").textContent = `${t.done}/${t.total}` + (t.skipped ? ` · ${t.skipped} skipped` : "");
  if (!t.items.length) { box.innerHTML = '<div class="empty">nothing on today\'s routine yet…</div>'; return; }
  box.innerHTML = "";
  // One "start routine" control for the whole day — it shows on the first
  // time-relative section and stamps a single start every anchored step counts from.
  let startShown = false;
  for (const grp of (t.sections || [{ section: "", items: t.items }])) {
    const collapsed = _rtCollapsedSec.has(grp.section);
    const hdr = document.createElement("div");
    hdr.className = "rt-section-hdr" + (collapsed ? " collapsed" : "");
    // Time-relative sections gain a start control: before it's stamped the steps
    // show "+Nm" and this reads "▶ start routine"; after, it shows the start time
    // and re-stamps to now on click (in case Jake actually began later).
    const startCtl = (grp.anchored && !startShown)
      ? (grp.started
          ? `<button class="rt-start started" data-sec="${escapeHtml(grp.section)}" title="re-stamp the start to now">◷ ${grp.started_at} · restart</button>`
          : `<button class="rt-start" data-sec="${escapeHtml(grp.section)}" title="stamp now as the start; the steps get times counted from here">▶ start routine</button>`)
      : "";
    if (grp.anchored) startShown = true;
    hdr.innerHTML =
      `<span class="rt-section-left"><span class="rt-caret">▾</span>` +
      `<span class="rt-section-name">${escapeHtml(grp.section || "")}</span></span>` +
      startCtl +
      (grp.total != null ? `<span class="rt-section-prog">${grp.done}/${grp.total}</span>` : "");
    if (grp.section) hdr.addEventListener("click", () => {
      if (_rtCollapsedSec.has(grp.section)) _rtCollapsedSec.delete(grp.section);
      else _rtCollapsedSec.add(grp.section);
      renderRoutineToday(_rtToday);   // re-render from cached data, no refetch
    });
    const startBtn = hdr.querySelector(".rt-start");
    if (startBtn) startBtn.addEventListener("click", (e) => {
      e.stopPropagation();   // don't also toggle the section collapse
      startRoutine(startBtn.dataset.sec);
    });
    box.appendChild(hdr);
    if (!collapsed) {
      const body = document.createElement("div");
      body.className = "rt-section-body";
      for (const it of grp.items) renderRoutineItem(body, it);
      box.appendChild(body);
    }
  }
}

function renderRoutineItem(box, it) {
  {
    const wrap = document.createElement("div");
    wrap.className = "rt-item-wrap";
    const time = it.time ? `<span class="rt-time">${it.time}</span>`
               : it.rel ? `<span class="rt-time rel" title="due ${it.rel.replace('+', '')} after the routine starts">${it.rel}</span>`
               : `<span class="rt-time none">–</span>`;
    const note = it.note ? `<div class="rt-note">${escapeHtml(it.note)}</div>` : "";
    const streak = (it.kind === "habit" && it.streak > 0) ? `<span class="rt-streak">🔥${it.streak}</span>` : "";
    const rep = it.repeat_days ? `<span class="rt-rep" title="repeats every ${it.repeat_days} day(s) after it's done; carries over until then">↻${it.repeat_days}d</span>` : "";
    // Subtasks are collapsed by default — the chevron toggle shows the count and
    // opens the checklist (which also holds the "+ add subtask" input, so items
    // with no subtasks yet still expand to gain one).
    const open = _rtSubOpen.has(it.id);
    const subToggle =
      `<span class="rt-subtoggle" data-act="subs" title="subtasks">` +
      `<span class="rt-subcaret">${open ? "▾" : "▸"}</span>` +
      (it.sub_total ? `<span class="rt-subcount">${it.sub_done}/${it.sub_total}</span>`
                    : `<span class="rt-subcount add">＋</span>`) +
      `</span>`;
    const stateClass = it.done ? " done" : (it.skipped ? " skipped" : "");
    const skipTag = it.skipped ? `<span class="rt-skiptag">skipped</span>` : "";
    let html =
      `<div class="rt-item${stateClass}">` +
      `<span class="rt-check" data-act="done" title="done">✓</span>${time}` +
      `<div class="rt-mid"><div class="rt-title">${escapeHtml(it.title)}</div>${note}</div>` +
      `${streak}${rep}${subToggle}${skipTag}` +
      `<span class="rt-skip" data-act="skip" title="skip today">⏭</span>` +
      `<span class="rt-badge ${it.kind}">${it.kind}</span></div>`;
    html += `<div class="rt-subs${open ? "" : " collapsed"}">`;
    for (const s of it.subtasks || []) {
      html += `<div class="rt-sub${s.done ? " done" : ""}" data-sid="${s.id}">` +
        `<span class="rt-subcheck">✓</span><span class="rt-subtitle">${escapeHtml(s.title)}</span>` +
        `<span class="rt-subdel" title="remove subtask">✕</span></div>`;
    }
    html += `<div class="rt-subadd"><input type="text" placeholder="+ add subtask" data-rid="${it.id}" /></div></div>`;
    wrap.innerHTML = html;
    wrap.querySelector('[data-act="done"]').addEventListener("click", () => toggleRoutineDone(it.id, !it.done));
    wrap.querySelector('[data-act="skip"]').addEventListener("click", () => skipRoutine(it.id, !it.skipped));
    wrap.querySelector('[data-act="subs"]').addEventListener("click", () => {
      const openNow = !_rtSubOpen.has(it.id);
      if (openNow) _rtSubOpen.add(it.id); else _rtSubOpen.delete(it.id);
      wrap.querySelector(".rt-subs").classList.toggle("collapsed", !openNow);
      wrap.querySelector(".rt-subcaret").textContent = openNow ? "▾" : "▸";
    });
    wrap.querySelectorAll(".rt-sub").forEach((sEl) => {
      const sid = Number(sEl.dataset.sid);
      sEl.querySelector(".rt-subcheck").addEventListener("click", () => toggleSubtask(sid, !sEl.classList.contains("done")));
      sEl.querySelector(".rt-subdel").addEventListener("click", () => deleteSubtask(sid));
    });
    const addInput = wrap.querySelector(".rt-subadd input");
    addInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); addSubtask(it.id, addInput.value); }
    });
    box.appendChild(wrap);
  }
}

async function toggleSubtask(sid, done) {
  await jwrite("tick that subtask off", `/api/subtasks/${sid}/done`, jbody("POST", { done }));
  loadRoutines();
}
async function deleteSubtask(sid) {
  await jwrite("delete that subtask", `/api/subtasks/${sid}`, jbody("DELETE"));
  loadRoutines();
}
async function addSubtask(rid, title) {
  title = title.trim();
  if (!title) return;
  await jwrite("add that subtask", `/api/routines/${rid}/subtasks`, jbody("POST", { title }));
  loadRoutines();
}

// ---- to-do list ----
async function loadTodos() {
  const d = await jread("/api/todos");
  // renderTodos paints "nothing to do 🎉" for an empty list — the single most
  // confidently wrong thing this app can say when it simply didn't get an answer.
  if (!d) return loadFail("routine", "your to-do list", loadTodos);
  loadOk(loadTodos);
  renderTodos(d.todos || []);
}
function renderTodos(list) {
  const open = list.filter((t) => !t.done);
  const done = list.filter((t) => t.done);
  $("todo-count").textContent = open.length;
  const box = $("todo-list");
  if (!list.length) { box.innerHTML = '<div class="empty">nothing to do 🎉</div>'; return; }
  box.innerHTML = "";
  for (const t of open.concat(done)) {
    const el = document.createElement("div");
    el.className = "todo" + (t.done ? " done" : "");
    const note = t.note ? `<span class="todo-note">${escapeHtml(t.note)}</span>` : "";
    el.innerHTML =
      `<span class="todo-check">✓</span>` +
      `<span class="todo-title">${escapeHtml(t.title)}</span>${note}` +
      `<span class="todo-del" title="delete">✕</span>`;
    el.querySelector(".todo-check").addEventListener("click", () => toggleTodo(t.id, !t.done));
    el.querySelector(".todo-del").addEventListener("click", () => deleteTodo(t.id));
    box.appendChild(el);
  }
}
async function toggleTodo(id, done) {
  await jwrite("tick that off", `/api/todos/${id}/done`, jbody("POST", { done }));
  loadTodos();
}
async function deleteTodo(id) {
  await jwrite("delete that to-do", `/api/todos/${id}`, jbody("DELETE"));
  loadTodos();
}
const _todoForm = document.getElementById("todo-form");
if (_todoForm) _todoForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const v = $("todo-title").value.trim();
  if (!v) return;
  // Only clear the box once the server has it. Clearing regardless threw away
  // what he'd typed on every failed save.
  const r = await jwrite("add that to-do", "/api/todos", jbody("POST", { title: v }));
  if (r.ok) $("todo-title").value = "";
  loadTodos();
});

let _rtEditing = null;   // id of the routine row currently expanded for editing

function renderRoutineAll(all) {
  $("rt-count").textContent = all.length;
  const box = $("rt-all");
  if (!all.length) { box.innerHTML = '<div class="empty">no routines defined</div>'; return; }
  box.innerHTML = "";
  let section = null;
  for (const r of all) {
    if (r.section !== section) {
      section = r.section;
      const sh = document.createElement("div");
      sh.className = "rt-asection";
      sh.textContent = section || "anytime";
      box.appendChild(sh);
    }
    const days = r.repeat_days ? `↻ every ${r.repeat_days}d`
               : r.days === "daily" ? "daily" : r.days === "weekdays" ? "Mon–Fri"
               : r.days === "weekends" ? "Sat–Sun" : r.days;
    const el = document.createElement("div");
    el.className = "rt-arow" + (r.active ? "" : " off");
    const atime = r.offset_min != null ? `+${r.offset_min}m` : (r.time || "–");
    el.innerHTML =
      `<span class="rt-atime">${atime}</span>` +
      `<span class="rt-atitle">${escapeHtml(r.title)}</span>` +
      `<span class="rt-badge ${r.kind}">${r.kind}</span>` +
      `<span class="rt-adays">${escapeHtml(days)}</span>` +
      `<span class="rt-edit" title="edit">✎</span>` +
      `<span class="rt-del" title="delete">✕</span>`;
    el.querySelector(".rt-atitle").addEventListener("click", () => openRoutineEdit(r.id));
    el.querySelector(".rt-edit").addEventListener("click", () => openRoutineEdit(r.id));
    el.querySelector(".rt-del").addEventListener("click", () => deleteRoutine(r.id));
    box.appendChild(el);
    if (_rtEditing === r.id) box.appendChild(routineEditForm(r));
  }
}

function openRoutineEdit(id) {
  _rtEditing = (_rtEditing === id) ? null : id;
  loadRoutines();
}

function routineEditForm(r) {
  const wrap = document.createElement("div");
  wrap.className = "rt-editform";
  const opt = (val, cur, label) => `<option value="${val}"${val === cur ? " selected" : ""}>${label || val}</option>`;
  wrap.innerHTML =
    `<input class="re-title" type="text" value="${escapeHtml(r.title)}" placeholder="title" />` +
    `<div class="re-row">` +
      `<input class="re-time" type="text" value="${escapeHtml(r.time || "")}" placeholder="time" />` +
      `<select class="re-kind">${opt("habit", r.kind)}${opt("block", r.kind)}${opt("reminder", r.kind)}</select>` +
      `<select class="re-days">${opt("daily", r.days)}${opt("weekdays", r.days)}${opt("weekends", r.days)}</select>` +
      `<select class="re-section">${["morning", "afternoon", "evening", "night", "anytime"].map((s) => opt(s, r.section)).join("")}</select>` +
      `<input class="re-repeat" type="number" min="0" value="${r.repeat_days || 0}" placeholder="↻ days" title="repeat every N days after done (carries over until then); 0 = use the days pattern" />` +
      `<input class="re-offset" type="number" min="0" value="${r.offset_min != null ? r.offset_min : ""}" placeholder="+min" title="time-relative: minutes after the section's 'start routine' (0 = right at the start). Leave blank for a fixed time" />` +
    `</div>` +
    `<input class="re-note" type="text" value="${escapeHtml(r.note || "")}" placeholder="note (optional)" />` +
    `<div class="re-actions"><button class="re-save">Save</button><button class="re-cancel">Cancel</button></div>`;
  wrap.querySelector(".re-save").addEventListener("click", async () => {
    const res = await jwrite("save that routine", `/api/routines/${r.id}`, jbody("PATCH", {
      title: wrap.querySelector(".re-title").value.trim() || r.title,
      time: wrap.querySelector(".re-time").value.trim(),
      kind: wrap.querySelector(".re-kind").value,
      days: wrap.querySelector(".re-days").value,
      section: wrap.querySelector(".re-section").value,
      repeat_days: Number(wrap.querySelector(".re-repeat").value) || 0,
      offset_min: wrap.querySelector(".re-offset").value.trim() === ""
        ? null : Number(wrap.querySelector(".re-offset").value),
      note: wrap.querySelector(".re-note").value.trim(),
    }));
    // Stay in the editor if it didn't save, so the edits are still there to retry.
    if (!res.ok) return;
    _rtEditing = null;
    loadRoutines();
  });
  wrap.querySelector(".re-cancel").addEventListener("click", () => { _rtEditing = null; loadRoutines(); });
  return wrap;
}

async function startRoutine(section) {
  // Stamp now as the section's start; the anchored steps get concrete times.
  await jwrite("start that routine", "/api/routines/start", jbody("POST", { section }));
  loadRoutines();
}

async function toggleRoutineDone(id, done) {
  await jwrite("tick that off", `/api/routines/${id}/done`, jbody("POST", { done }));
  loadRoutines();
}

async function skipRoutine(id, skipped) {
  await jwrite("skip that", `/api/routines/${id}/status`,
               jbody("POST", { status: skipped ? "skipped" : "none" }));
  loadRoutines();
}

async function deleteRoutine(id) {
  await jwrite("delete that routine", `/api/routines/${id}`, jbody("DELETE"));
  loadRoutines();
}

const _rtForm = document.getElementById("rt-form");
if (_rtForm) _rtForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const title = $("rt-title").value.trim();
  if (!title) return;
  const r = await jwrite("add that routine", "/api/routines", jbody("POST", {
    title, time: $("rt-time").value.trim(),
    kind: $("rt-kind").value, days: $("rt-days").value,
    section: $("rt-section").value,
    repeat_days: Number($("rt-repeat").value) || 0,
  }));
  if (r.ok) { $("rt-title").value = ""; $("rt-time").value = ""; $("rt-repeat").value = ""; }
  loadRoutines();
});

// ---- message search ----
let _msgSearchTimer;
const _msgSearch = document.getElementById("msg-search");
if (_msgSearch) _msgSearch.addEventListener("input", (e) => {
  clearTimeout(_msgSearchTimer);
  const q = e.target.value.trim();
  _msgSearchTimer = setTimeout(() => loadMessages(q), 250);
});

// ---- workouts (Speediance explorer — no AI, reads /api/workouts/*) ----
let _woDays = 90;
let _woExercise = "";   // currently-charted exercise, kept across reloads

async function loadWorkouts() {
  const [stats, list, exs] = await Promise.all([
    jread("/api/workouts/stats?days=" + _woDays),
    jread("/api/workouts?limit=30"),
    jread("/api/workouts/exercises"),
  ]);
  // All three or none: renderWorkoutStats takes stats and list together, so a
  // half-load would draw totals against a session list that doesn't match them.
  if (!stats || !list || !exs) return loadFail("workouts", "your training", loadWorkouts);
  loadOk(loadWorkouts);
  renderWorkoutStats(stats, list);
  renderVolChart(stats.series || []);
  renderMuscles(stats.muscles || []);
  renderSessions(list.sessions || []);
  populateExPicker(exs.exercises || []);
}

function renderWorkoutStats(s, list) {
  const sub = $("wo-sub");
  const synced = s.last_synced ? "synced " + fmtStamp(s.last_synced) : "never synced";
  const store = (list && list.count != null) ? `${list.count} in store` : "";
  sub.textContent = [store, synced].filter(Boolean).join(" · ");
  const tile = (v, k) => `<div class="wo-tile"><div class="wo-tv">${v}</div><div class="wo-tk">${k}</div></div>`;
  $("wo-stat").innerHTML =
    tile(s.sessions || 0, "sessions") +
    tile((s.total_volume || 0).toLocaleString(), "kg volume") +
    tile((s.total_calories || 0).toLocaleString(), "kcal") +
    tile(s.total_duration_min || 0, "min trained");
}

function renderVolChart(series) {
  const box = $("wo-vol-chart");
  if (!series.length) { box.innerHTML = '<div class="empty">no sessions in this window</div>'; return; }
  const max = Math.max(...series.map((s) => s.volume), 1);
  box.innerHTML = series.map((s) => {
    const h = Math.max(4, Math.round((100 * s.volume) / max));
    const tip = `${s.date} ${s.title}: ${Math.round(s.volume).toLocaleString()}kg · ${s.duration_min}min · ${s.calories}kcal`;
    return `<span class="wo-bar" style="height:${h}%" title="${escapeHtml(tip)}"></span>`;
  }).join("");
}

// Sets per muscle group over the window, biggest first (the API sorts it).
//
// One colour for every bar, not a light-to-dark ramp: muscle groups are nominal
// — Chest is not "more" than Back — and shading by size would double-encode the
// length the bar already shows. Bar length is the whole encoding.
//
// Speediance names these groups only by a numeric id, and any id we have no
// evidence for arrives as "part 14" rather than a guessed muscle. That is
// deliberate and must survive to the screen, so nothing here prettifies a name.
function renderMuscles(rows) {
  const box = $("wo-muscles");
  if (!rows.length) { box.innerHTML = '<div class="empty">no muscle data in this window</div>'; return; }
  const max = Math.max(...rows.map((m) => m.sets), 1);
  const total = rows.reduce((n, m) => n + m.sets, 0);
  box.innerHTML = rows.map((m) => {
    // A floor, so the smallest group is still a visible bar and not a hairline.
    const w = Math.max(3, Math.round((100 * m.sets) / max));
    const pct = total ? Math.round((100 * m.sets) / total) : 0;
    const tip = `${m.name}: ${m.sets} set${m.sets === 1 ? "" : "s"} · ${pct}% of the window`;
    return `<div class="wo-mrow" title="${escapeHtml(tip)}">` +
      `<span class="wo-mname">${escapeHtml(m.name)}</span>` +
      `<span class="wo-mtrack"><span class="wo-mfill" style="width:${w}%"></span></span>` +
      `<span class="wo-mval">${m.sets}</span>` +
      `</div>`;
  }).join("");
}

function renderSessions(rows) {
  const box = $("wo-sessions");
  if (!rows.length) { box.innerHTML = '<div class="empty">no workouts yet — hit Sync to pull from Speediance</div>'; return; }
  box.innerHTML = rows.map((s) => {
    const mins = Math.round((s.duration_s || 0) / 60);
    const sets = s.set_count ? `${s.set_count} sets` : "totals only";
    return `<div class="wo-srow" data-tid="${escapeHtml(s.training_id)}">` +
      `<span class="wo-sdate">${escapeHtml(s.date || "")}</span>` +
      `<span class="wo-stitle">${escapeHtml(s.title || "Workout")}</span>` +
      `<span class="wo-smeta">${Math.round(s.total_volume).toLocaleString()}kg · ${mins}min · ${Math.round(s.calories)}kcal · ${sets}</span>` +
      `</div><div class="wo-sdetail" id="wo-sd-${escapeHtml(s.training_id)}"></div>`;
  }).join("");
}

// Expand a session inline to its per-exercise set breakdown.
async function toggleSession(tid) {
  const box = document.getElementById("wo-sd-" + tid);
  if (!box) return;
  if (box.classList.contains("open")) { box.classList.remove("open"); box.innerHTML = ""; return; }
  box.classList.add("open"); box.innerHTML = '<div class="wo-loading">loading…</div>';
  try {
    const s = await (await fetch("/api/workouts/" + encodeURIComponent(tid))).json();
    if (!s || s.error || !(s.exercises || []).length) {
      box.innerHTML = '<div class="wo-note">no per-set detail (a Free Lift session reports totals only)</div>';
      return;
    }
    box.innerHTML = s.exercises.map((ex) => {
      const sets = ex.sets.map((st) => `<span class="wo-set">${st.reps}×${(+st.weight_kg).toLocaleString()}kg</span>`).join("");
      return `<div class="wo-exrow"><span class="wo-exname">${escapeHtml(ex.exercise)}</span><span class="wo-exsets">${sets}</span></div>`;
    }).join("");
  } catch (e) { box.innerHTML = '<div class="wo-note">could not load detail</div>'; }
}

function populateExPicker(exs) {
  const sel = $("wo-ex-picker");
  const cur = _woExercise || sel.value;
  sel.innerHTML = '<option value="">choose an exercise…</option>' +
    exs.map((e) => `<option value="${escapeHtml(e.name)}">${escapeHtml(e.name)} · ${e.sessions}× · max ${e.max_weight}kg</option>`).join("");
  if (cur && exs.some((e) => e.name === cur)) { sel.value = cur; }
}

async function loadExerciseProgress(name) {
  _woExercise = name;
  const body = $("wo-ex-body");
  if (!name) { body.innerHTML = '<div class="empty">pick an exercise to chart your progression over time</div>'; return; }
  body.innerHTML = '<div class="wo-loading">loading…</div>';
  try {
    const h = await (await fetch("/api/workouts/exercise?name=" + encodeURIComponent(name))).json();
    renderExerciseProgress(h);
  } catch (e) { body.innerHTML = '<div class="wo-note">could not load progression</div>'; }
}

function renderExerciseProgress(h) {
  const body = $("wo-ex-body");
  const pts = h.points || [];
  if (!pts.length) { body.innerHTML = '<div class="empty">no logged sets for this exercise yet</div>'; return; }
  const first = pts[0], last = pts[pts.length - 1];
  const delta = last.top_weight - first.top_weight;
  const dcls = delta > 0 ? "up" : delta < 0 ? "down" : "flat";
  const arrow = delta > 0 ? "▲" : delta < 0 ? "▼" : "▬";
  const head =
    `<div class="wo-ex-head">` +
    `<span class="wo-pr">PR ${h.pr_weight}kg</span>` +
    `<span class="wo-ex-delta ${dcls}">${arrow} ${delta === 0 ? "no change" : (delta > 0 ? "+" : "") + (+delta.toFixed(1)) + "kg"} over ${h.sessions} session${h.sessions === 1 ? "" : "s"}</span>` +
    `</div>`;
  const weightChart = svgLine(pts.map((p) => ({ v: p.top_weight, tip: `${p.date}: ${p.top_weight}kg top set` })), "#5ad1c4");
  const rmChart = svgLine(pts.map((p) => ({ v: p.best_1rm, tip: `${p.date}: ${p.best_1rm}kg est. 1RM` })), "#ffb27a");
  const table = `<table class="wo-ex-table"><thead><tr><th>date</th><th>top</th><th>est 1RM</th><th>volume</th><th>sets</th></tr></thead><tbody>` +
    pts.slice().reverse().map((p) =>
      `<tr><td>${escapeHtml(p.date)}</td><td>${p.top_weight}kg</td><td>${p.best_1rm}kg</td><td>${Math.round(p.volume).toLocaleString()}kg</td><td>${p.sets}</td></tr>`).join("") +
    `</tbody></table>`;
  body.innerHTML = head +
    `<div class="wo-ex-chart"><div class="wo-chart-head"><span class="wo-chart-label">top-set weight</span><span class="wo-chart-unit">kg</span></div>${weightChart}</div>` +
    `<div class="wo-ex-chart"><div class="wo-chart-head"><span class="wo-chart-label">estimated 1RM</span><span class="wo-chart-unit">kg</span></div>${rmChart}</div>` +
    table;
}

// Minimal dependency-free SVG line+area chart. series: [{v, tip}]. Wide viewBox
// with meet so markers stay round regardless of container width.
function svgLine(series, color) {
  const n = series.length;
  if (!n) return '<div class="empty">no data</div>';
  const W = 640, H = 160, pad = 14;
  const vals = series.map((s) => s.v);
  const max = Math.max(...vals), min = Math.min(...vals);
  const rng = (max - min) || 1;
  const x = (i) => n === 1 ? W / 2 : pad + (i * (W - 2 * pad)) / (n - 1);
  const y = (v) => H - pad - ((v - min) / rng) * (H - 2 * pad);
  const line = series.map((s, i) => `${x(i).toFixed(1)},${y(s.v).toFixed(1)}`).join(" ");
  const area = `${x(0).toFixed(1)},${(H - pad).toFixed(1)} ${line} ${x(n - 1).toFixed(1)},${(H - pad).toFixed(1)}`;
  const dots = series.map((s, i) =>
    `<circle cx="${x(i).toFixed(1)}" cy="${y(s.v).toFixed(1)}" r="3.5" fill="${color}"><title>${escapeHtml(s.tip)}</title></circle>`).join("");
  return `<svg class="wchart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" role="img">` +
    `<polygon points="${area}" fill="${color}" opacity="0.12"/>` +
    `<polyline points="${line}" fill="none" stroke="${color}" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>` +
    `<g>${dots}</g></svg>`;
}

// ---- workouts: event wiring ----
const _woRange = document.getElementById("wo-range");
if (_woRange) _woRange.addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  _woDays = Number(b.dataset.days);
  _woRange.querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
  loadWorkouts();
});
const _woPicker = document.getElementById("wo-ex-picker");
if (_woPicker) _woPicker.addEventListener("change", (e) => loadExerciseProgress(e.target.value));
const _woSessions = document.getElementById("wo-sessions");
if (_woSessions) _woSessions.addEventListener("click", (e) => {
  const row = e.target.closest(".wo-srow");
  if (row) toggleSession(row.dataset.tid);
});
const _woSync = document.getElementById("wo-sync");
if (_woSync) _woSync.addEventListener("click", async () => {
  if (_woSync.classList.contains("busy")) return;
  _woSync.classList.add("busy");
  const orig = _woSync.textContent;
  _woSync.textContent = "⟳ syncing…";
  try {
    // Both surfaces on purpose: the button is where he's looking, but it only
    // has room for ~24 characters and the reasons here are things like a
    // rejected Speediance login that you need to read in full. The toast holds
    // the whole message until dismissed. A non-2xx used to print "✓ +0 new".
    const r = await jwrite("sync workouts", "/api/workouts/sync", jbody("POST", { days: 365 }));
    if (!r.ok) { _woSync.textContent = "⚠ sync failed"; return; }
    _woSync.textContent = `✓ +${(r.data && r.data.new) || 0} new`;
    await loadWorkouts();
  } finally {
    setTimeout(() => { _woSync.textContent = orig; _woSync.classList.remove("busy"); }, 2600);
  }
});

// ---- nutrition (non-AI explorer over /api/nutrition/*) ----
// Calorie + macro intake diary. Day view + macro tiles vs goals + a food logger
// whose picker queries the USDA food database (/api/nutrition/search) and scales
// per-100g macros to the amount eaten. Both Jake and Continuum write the same log.
let _nutDate = null;      // null = today; else "YYYY-MM-DD"
let _nutDays = 7;         // trend window
let _nutPer100 = null;    // per-100g macros of the picked food, for auto-scaling
let _nutSearchTimer = null;

const _NUT_MACROS = [
  { k: "calories", label: "kcal", unit: "" },
  { k: "protein_g", label: "protein", unit: "g" },
  { k: "carbs_g", label: "carbs", unit: "g" },
  { k: "fat_g", label: "fat", unit: "g" },
];

async function loadNutrition() {
  const date = _nutDate || "";
  const [day, summary] = await Promise.all([
    jread("/api/nutrition?date=" + encodeURIComponent(date)),
    jread("/api/nutrition/summary?days=" + _nutDays),
  ]);
  // The worst empty state in the app. renderNutStat does Math.round(t[m.k] || 0),
  // so a dead read painted "0 kcal · nothing logged yet" over a day Jake had in
  // fact eaten — and the obvious thing to do about that screen is log the meal
  // again. Bail before rendering rather than feeding zeroes through; the banner
  // says why, and the previous day's figures stay visible but disowned.
  if (!day || !summary) return loadFail("nutrition", "today's food", loadNutrition);
  loadOk(loadNutrition);
  renderNutDay(day);
  renderNutTrend(summary);
  _nutFillGoals(day.goals || {});
  if (!_nutProviders) renderNutProvider();  // populate the source widget once
  loadMeals();  // refresh the saved-meals list alongside the day
}

function renderNutDay(d) {
  const isToday = (d.date === _today());
  $("nut-date").textContent = isToday ? "Today · " + d.date : d.date;
  $("nut-today").style.visibility = isToday ? "hidden" : "visible";
  const g = d.goals || {};
  $("nut-sub").textContent = d.count
    ? `${d.count} item${d.count === 1 ? "" : "s"}`
    : (g.set ? "nothing logged yet" : "");
  renderNutStat(d.totals || {}, g);
  renderNutEntries(d.meals || []);
}

function renderNutStat(t, g) {
  $("nut-stat").innerHTML = _NUT_MACROS.map((m) => {
    const val = Math.round(t[m.k] || 0);
    const goal = g.set ? Math.round(g[m.k] || 0) : 0;
    let extra = "";
    if (goal) {
      const pct = Math.min(100, Math.round((100 * val) / goal));
      const over = val > goal;
      const left = goal - val;
      extra =
        `<div class="nut-tbar"><span class="${over ? "over" : ""}" style="width:${pct}%"></span></div>` +
        `<div class="nut-tsub">${over ? `${-left}${m.unit} over` : `${left}${m.unit} left`}</div>`;
    }
    return `<div class="nut-tile"><div class="nut-tk">${m.label}</div>` +
      `<div class="nut-tv">${val}<span class="nut-tu">${m.unit}</span>` +
      (goal ? `<span class="nut-tgoal"> / ${goal}</span>` : "") + `</div>${extra}</div>`;
  }).join("");
}

function renderNutEntries(meals) {
  const box = $("nut-entries");
  if (!meals.length) { box.innerHTML = '<div class="empty">nothing logged for this day yet…</div>'; return; }
  box.innerHTML = meals.map((grp) => {
    const rows = grp.entries.map((e) => {
      const amt = (e.qty && e.unit) ? `${(+e.qty)}${e.unit}` : (e.unit || "");
      const by = e.source === "continuum" ? '<span class="nut-by" title="logged by Continuum">🤖</span>' : "";
      const macros = `P${(+e.protein_g)} C${(+e.carbs_g)} F${(+e.fat_g)}`;
      return `<div class="nut-erow" data-id="${e.id}">` +
        `<span class="nut-ename">${by}${escapeHtml(e.name)}${amt ? ` <span class="nut-eamt">${escapeHtml(amt)}</span>` : ""}</span>` +
        `<span class="nut-emacros">${macros}</span>` +
        `<span class="nut-ecal">${Math.round(e.calories)}<span class="nut-tu">kcal</span></span>` +
        `<button class="nut-edel" title="remove">✕</button></div>`;
    }).join("");
    return `<div class="nut-meal"><div class="nut-meal-head"><span>${escapeHtml(grp.meal)}</span>` +
      `<span class="nut-meal-cal">${Math.round(grp.totals.calories)} kcal</span></div>${rows}</div>`;
  }).join("");
}

function renderNutTrend(s) {
  const box = $("nut-trend");
  const series = s.series || [];
  if (!s.logged_days) { box.innerHTML = '<div class="empty">log meals to see your trend</div>'; return; }
  const goal = (s.goals && s.goals.set) ? s.goals.calories : 0;
  const max = Math.max(goal || 0, ...series.map((d) => d.calories), 1);
  box.innerHTML = series.map((d) => {
    const h = d.calories ? Math.max(3, Math.round((100 * d.calories) / max)) : 0;
    const over = goal && d.calories > goal;
    const tip = d.logged
      ? `${d.date}: ${Math.round(d.calories)}kcal · P${d.protein_g} C${d.carbs_g} F${d.fat_g}`
      : `${d.date}: nothing logged`;
    return `<span class="nut-bar-wrap" data-date="${d.date}" title="${escapeHtml(tip)}">` +
      `<span class="nut-bar ${over ? "over" : ""} ${d.date === (_nutDate || _today()) ? "sel" : ""}" style="height:${h}%"></span></span>`;
  }).join("") + (goal ? `<span class="nut-goal-line" style="bottom:${Math.round((100 * goal) / max)}%" title="goal ${goal}kcal"></span>` : "");
  const label = document.querySelector(".nut-trend-label");
  if (label) label.textContent = s.avg
    ? `daily calories · avg ${s.avg.calories}/day over ${s.logged_days} day${s.logged_days === 1 ? "" : "s"}`
    : "daily calories";
}

// ---- provider widget: which food database is answering (FatSecret / USDA) ----
let _nutProviders = null;   // cached {primary, fatsecret_configured, ...}

async function renderNutProvider() {
  // Leave _nutProviders null on failure rather than caching an empty object —
  // loadNutrition retries while it's falsy, so the widget fills itself in once the
  // server is back instead of staying permanently blank.
  const p = await jread("/api/nutrition/providers");
  if (!p) return;
  _nutProviders = p;
  _paintNutProvider();
}

// Base label: what's configured. `served` (set after a search) overrides it with
// which provider actually answered the last lookup, and any fallback note.
function _paintNutProvider(served) {
  const el = $("nut-provider");
  if (!el || !_nutProviders) return;
  const fs = _nutProviders.fatsecret_configured;
  const name = (p) => (p === "fatsecret" ? "FatSecret" : p === "openfoodfacts" ? "Open Food Facts" : "USDA");
  if (served && served.provider) {
    const fell = served.fallback;
    el.className = "nut-provider" + (fell ? " fell" : "");
    el.textContent = (fell ? "↳ " : "") + name(served.provider);
    el.title = served.note
      || `results from ${name(served.provider)}`
      + (fs ? " — FatSecret primary, USDA fallback" : "");
  } else {
    el.className = "nut-provider";
    el.textContent = fs ? "FatSecret · USDA fallback" : "USDA";
    el.title = fs
      ? "FatSecret is primary; USDA is the fallback if it's unreachable or the IP isn't whitelisted"
      : "USDA FoodData Central (FatSecret not configured)";
  }
}

// ---- food picker: search the food database, scale to the amount eaten ----
function renderNutResults(foods, err) {
  const box = $("nut-results");
  if (err) { box.innerHTML = `<div class="nut-rerr">${escapeHtml(err)}</div>`; box.classList.add("open"); return; }
  if (!foods || !foods.length) { box.classList.remove("open"); box.innerHTML = ""; return; }
  box.innerHTML = foods.map((f, i) =>
    `<button type="button" class="nut-result" data-i="${i}">` +
    `<span class="nut-rname">${escapeHtml(f.name)}${f.brand ? ` <span class="nut-rbrand">${escapeHtml(f.brand)}</span>` : ""}</span>` +
    `<span class="nut-rmac">${Math.round(f.calories)}kcal · P${f.protein_g} C${f.carbs_g} F${f.fat_g} <span class="nut-rper">/${escapeHtml(f.basis || "100g")}</span></span>` +
    `</button>`).join("");
  box._foods = foods;
  box.classList.add("open");
}

function pickFood(f) {
  // Macros are per `ref` of `refUnit` (100 g for USDA / per-100g FatSecret foods,
  // or 1 of a named serving for a non-gram FatSecret food). Store the reference
  // so _nutRecalc can scale by whatever amount Jake enters.
  const ref = (+f.ref_qty) || 100;
  const refUnit = f.ref_unit || "g";
  _nutPer100 = { calories: f.calories, protein_g: f.protein_g, carbs_g: f.carbs_g,
                 fat_g: f.fat_g, ref, refUnit };
  $("nut-name").value = f.name;
  $("nut-name").dataset.fdc = f.fdc_id || "";
  $("nut-unit").value = refUnit;
  $("nut-qty").value = f.serving_g || ref;
  const basis = f.basis || (ref === 100 && refUnit === "g" ? "100g" : `${ref} ${refUnit}`);
  const scaleHint = (refUnit === "g")
    ? "amount in grams auto-scales the macros"
    : `amount in ${escapeHtml(refUnit)}s auto-scales the macros`;
  $("nut-per100").hidden = false;
  $("nut-per100").innerHTML = `per ${escapeHtml(basis)}: ${Math.round(f.calories)}kcal · P${f.protein_g} C${f.carbs_g} F${f.fat_g}` +
    ` — <b>${scaleHint}</b>`;
  _nutRecalc();
  $("nut-results").classList.remove("open");
  $("nut-search").value = "";
  $("nut-qty").focus();
}

// Recompute the macro fields from the picked food's per-reference values and the
// amount entered (grams, or servings for a non-gram food). Only while picked.
function _nutRecalc() {
  if (!_nutPer100) return;
  const qty = parseFloat($("nut-qty").value);
  if (!(qty > 0)) return;
  const f = qty / (_nutPer100.ref || 100);
  $("nut-cal").value = Math.round(_nutPer100.calories * f);
  $("nut-pro").value = +(_nutPer100.protein_g * f).toFixed(1);
  $("nut-carb").value = +(_nutPer100.carbs_g * f).toFixed(1);
  $("nut-fat").value = +(_nutPer100.fat_g * f).toFixed(1);
}

function _nutClearPick() {
  _nutPer100 = null;
  $("nut-name").dataset.fdc = "";
  $("nut-per100").hidden = true;
}

function _nutResetForm() {
  $("nut-add-form").reset();
  _nutClearPick();
  $("nut-results").classList.remove("open");
}

function _nutFillGoals(g) {
  if (document.activeElement && document.activeElement.closest("#nut-goals")) return; // don't stomp editing
  // Prefill the BASE calories (what the store holds), not the activity-adjusted
  // target — otherwise re-saving would bake today's burn into the base.
  $("goal-cal").value = (g.base_calories != null ? g.base_calories : g.calories) || "";
  $("goal-pro").value = g.protein_g || "";
  $("goal-carb").value = g.carbs_g || "";
  $("goal-fat").value = g.fat_g || "";
}

// ---- nutrition: event wiring ----
const _nutSearch = document.getElementById("nut-search");
if (_nutSearch) _nutSearch.addEventListener("input", () => {
  const q = _nutSearch.value.trim();
  clearTimeout(_nutSearchTimer);
  if (q.length < 2) { $("nut-results").classList.remove("open"); return; }
  _nutSearchTimer = setTimeout(async () => {
    try {
      const r = await (await fetch("/api/nutrition/search?limit=7&q=" + encodeURIComponent(q))).json();
      renderNutResults(r.foods, r.error);
      if (!r.error) _paintNutProvider(r);
    } catch (e) { renderNutResults([], "search failed"); }
  }, 350);
});
const _nutResults = document.getElementById("nut-results");
if (_nutResults) _nutResults.addEventListener("click", (e) => {
  const b = e.target.closest(".nut-result");
  if (b && _nutResults._foods) pickFood(_nutResults._foods[+b.dataset.i]);
});
const _nutQty = document.getElementById("nut-qty");
if (_nutQty) _nutQty.addEventListener("input", _nutRecalc);
const _nutName = document.getElementById("nut-name");
if (_nutName) _nutName.addEventListener("input", () => { if (_nutPer100) _nutClearPick(); });

const _nutForm = document.getElementById("nut-add-form");
if (_nutForm) _nutForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = $("nut-name").value.trim();
  const cal = parseFloat($("nut-cal").value);
  if (!name || !(cal >= 0)) return;
  const fdc = $("nut-name").dataset.fdc;
  const body = {
    name, calories: cal,
    protein_g: parseFloat($("nut-pro").value) || 0,
    carbs_g: parseFloat($("nut-carb").value) || 0,
    fat_g: parseFloat($("nut-fat").value) || 0,
    qty: parseFloat($("nut-qty").value) || 0,
    unit: $("nut-unit").value.trim(),
    meal: $("nut-meal").value,
  };
  if (fdc) body.fdc_id = +fdc;
  const btn = $("nut-add-btn"); const orig = btn.textContent;
  btn.disabled = true; btn.textContent = "logging…";
  try {
    // Keep the form as typed if it didn't log — the macros were fiddly to enter.
    const r = await jwrite("log that", "/api/nutrition", jbody("POST", body));
    if (!r.ok) return;
    _nutResetForm();
    _nutDate = null; // a fresh log lands today — show today
    await loadNutrition();
  } finally { btn.disabled = false; btn.textContent = orig; }
});

const _nutEntries = document.getElementById("nut-entries");
if (_nutEntries) _nutEntries.addEventListener("click", async (e) => {
  const del = e.target.closest(".nut-edel");
  if (!del) return;
  const row = del.closest(".nut-erow");
  if (!row) return;
  await jwrite("delete that entry", "/api/nutrition/" + row.dataset.id, jbody("DELETE"));
  await loadNutrition();
});

// goals editor
const _nutGoalsBtn = document.getElementById("nut-goals-btn");
if (_nutGoalsBtn) _nutGoalsBtn.addEventListener("click", () => {
  const g = $("nut-goals"); g.hidden = !g.hidden;
});
const _nutGoalsSave = document.getElementById("nut-goals-save");
if (_nutGoalsSave) _nutGoalsSave.addEventListener("click", async () => {
  // Only send boxes that actually hold a value. An empty box means "leave this
  // target alone", not "set it to zero" — the server merges absent keys. The
  // old `parseFloat(...) || 0` meant that if the prefill had failed (or the
  // form was half filled), one click zeroed every untouched target.
  const fields = [["calories", "goal-cal", "kcal"], ["protein_g", "goal-pro", "protein"],
                  ["carbs_g", "goal-carb", "carbs"], ["fat_g", "goal-fat", "fat"]];
  const body = {};
  for (const [key, id, label] of fields) {
    const raw = ($(id).value || "").trim();
    if (raw === "") continue;
    const v = parseFloat(raw);
    if (!Number.isFinite(v) || v < 0) { toast(`${label} must be a number (0 or more)`); return; }
    body[key] = v;
  }
  if (!Object.keys(body).length) { $("nut-goals").hidden = true; return; }
  try {
    await jfetch("/api/nutrition/goals", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    $("nut-goals").hidden = true;
    toast("targets updated", "ok");
    await loadNutrition();
  } catch (e) {
    toast("couldn't save targets: " + e.message + " — nothing was changed");
  }
});

// date nav + trend range
const _nutPrev = document.getElementById("nut-prev");
if (_nutPrev) _nutPrev.addEventListener("click", () => { _nutShiftDay(-1); });
const _nutNext = document.getElementById("nut-next");
if (_nutNext) _nutNext.addEventListener("click", () => { _nutShiftDay(1); });
const _nutTodayBtn = document.getElementById("nut-today");
if (_nutTodayBtn) _nutTodayBtn.addEventListener("click", () => { _nutDate = null; loadNutrition(); });
function _nutShiftDay(delta) {
  const base = _nutDate ? new Date(_nutDate + "T12:00:00") : new Date();
  base.setDate(base.getDate() + delta);
  const d = _ymd(base);
  _nutDate = (d === _today()) ? null : d;
  loadNutrition();
}
const _nutRange = document.getElementById("nut-range");
if (_nutRange) _nutRange.addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  _nutDays = Number(b.dataset.days);
  _nutRange.querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
  loadNutrition();
});
const _nutTrend = document.getElementById("nut-trend");
if (_nutTrend) _nutTrend.addEventListener("click", (e) => {
  const w = e.target.closest(".nut-bar-wrap");
  if (!w) return;
  _nutDate = (w.dataset.date === _today()) ? null : w.dataset.date;
  loadNutrition();
});

// ---- barcode lookup (manual UPC entry → prefills the food logger) ----
// Resolves a barcode via Open Food Facts (/api/nutrition/barcode) and reuses
// pickFood() to drop the product into the logger, scaled by the amount entered.
async function nutBarcodeLookup() {
  const code = ($("nut-barcode").value || "").replace(/\D/g, "");
  const msg = $("nut-barcode-msg");
  if (!code) { msg.hidden = true; return; }
  msg.hidden = false; msg.className = "nut-barcode-msg"; msg.textContent = "looking up " + code + "…";
  try {
    const r = await (await fetch("/api/nutrition/barcode?upc=" + encodeURIComponent(code))).json();
    if (r.error) { msg.className = "nut-barcode-msg err"; msg.textContent = "lookup failed: " + r.error; return; }
    if (!r.food) { msg.className = "nut-barcode-msg err"; msg.textContent = "no product found for " + code + " — log it manually"; return; }
    pickFood(r.food);
    _paintNutProvider({ provider: "openfoodfacts", note: "barcode lookup via Open Food Facts" });
    msg.className = "nut-barcode-msg ok";
    msg.textContent = "✓ " + r.food.name + (r.food.brand ? " (" + r.food.brand + ")" : "") + " — filled below, set the amount";
    $("nut-barcode").value = "";
  } catch (e) { msg.className = "nut-barcode-msg err"; msg.textContent = "lookup failed"; }
}
const _nutBarcodeBtn = document.getElementById("nut-barcode-btn");
if (_nutBarcodeBtn) _nutBarcodeBtn.addEventListener("click", nutBarcodeLookup);
const _nutBarcode = document.getElementById("nut-barcode");
if (_nutBarcode) _nutBarcode.addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); nutBarcodeLookup(); }
});

// ---- saved meals + meal builder (non-AI explorer over /api/nutrition/meals*) ----
// A saved meal is a reusable definition with PER-PORTION macros, logged by the
// (possibly fractional) portion. Two ways to build one, matching the store:
//   * ingredients — each searched & scaled to its amount, summed and split by
//     `servings` (the server does the same arithmetic; we preview it live);
//   * just macros — the per-portion macros direct (servings forced to 1).
let _mbMode = "ingredients";

function loadMeals() {
  fetch("/api/nutrition/meals").then((r) => r.json())
    .then((d) => renderMeals(d.meals || [])).catch(() => {});
}

function renderMeals(meals) {
  const box = $("nut-meals-list");
  const sub = $("nut-meals-sub");
  if (sub) sub.textContent = meals.length ? (meals.length + " saved") : "";
  if (!meals.length) {
    box.innerHTML = '<div class="empty">no saved meals yet — build one to log it by the portion</div>';
    return;
  }
  box.innerHTML = meals.map((m) => {
    const sv = (+m.servings) || 1;
    const svbit = (sv && sv !== 1) ? ` · makes ${sv}` : "";
    const n = (m.ingredients && m.ingredients.length) || 0;
    const ingbit = n ? ` · ${n} ingredient${n === 1 ? "" : "s"}` : "";
    return `<div class="nut-meal-card" data-id="${m.id}" data-name="${escapeHtml(m.name)}">` +
      `<div class="nmc-main"><div class="nmc-name">${escapeHtml(m.name)}` +
      (m.meal ? `<span class="nmc-tag">${escapeHtml(m.meal)}</span>` : "") + `</div>` +
      `<div class="nmc-mac">${Math.round(m.calories)}kcal · P${m.protein_g} C${m.carbs_g} F${m.fat_g} ` +
      `<span class="nmc-per">/portion</span>${svbit}${ingbit}</div></div>` +
      `<div class="nmc-log">` +
      `<input class="nmc-portions" type="number" step="any" min="0" value="1" title="portions" />` +
      `<select class="nmc-meal" title="log under which meal"><option value="">—</option>` +
      `<option value="breakfast">B</option><option value="lunch">L</option>` +
      `<option value="dinner">D</option><option value="snack">S</option></select>` +
      `<button type="button" class="nmc-logbtn" title="log this meal">log</button>` +
      `<button type="button" class="nmc-del" title="delete meal">✕</button></div></div>`;
  }).join("");
}

// ---- ingredient rows: search a food, scale by amount, sum live ----
function mbAddIngRow() {
  const rows = $("mb-ing-rows");
  const row = document.createElement("div");
  row.className = "nut-ing-row";
  row.innerHTML =
    `<div class="nut-ing-search"><input class="ing-name" type="text" placeholder="ingredient (search & pick)" autocomplete="off" />` +
    `<div class="ing-results"></div></div>` +
    `<input class="ing-amt" type="number" step="any" min="0" placeholder="g" />` +
    `<span class="ing-mac">—</span>` +
    `<button type="button" class="ing-del" title="remove">✕</button>`;
  row._per = null;      // {calories,protein_g,carbs_g,fat_g,ref,unit} once a food is picked
  rows.appendChild(row);
  mbRecalc();
}

function _rowMacros(row) {
  const amt = parseFloat(row.querySelector(".ing-amt").value);
  const p = row._per;
  if (!p || !(amt > 0)) return null;
  const f = amt / (p.ref || 100);
  return { calories: p.calories * f, protein_g: p.protein_g * f,
           carbs_g: p.carbs_g * f, fat_g: p.fat_g * f };
}

function mbRecalc() {
  const rows = Array.prototype.slice.call($("mb-ing-rows").querySelectorAll(".nut-ing-row"));
  const tot = { calories: 0, protein_g: 0, carbs_g: 0, fat_g: 0 };
  rows.forEach((row) => {
    const m = _rowMacros(row);
    const span = row.querySelector(".ing-mac");
    if (m) {
      span.textContent = `${Math.round(m.calories)}kcal P${+m.protein_g.toFixed(1)} C${+m.carbs_g.toFixed(1)} F${+m.fat_g.toFixed(1)}`;
      tot.calories += m.calories; tot.protein_g += m.protein_g;
      tot.carbs_g += m.carbs_g; tot.fat_g += m.fat_g;
    } else {
      span.textContent = "—";
    }
  });
  const svRaw = parseFloat($("mb-servings").value);
  const sv = svRaw > 0 ? svRaw : 1;
  const per = (k) => +(tot[k] / sv).toFixed(1);
  $("mb-tot").innerHTML =
    `<div class="mbt-row"><span>recipe total</span><b>${Math.round(tot.calories)}kcal · ` +
    `P${+tot.protein_g.toFixed(1)} C${+tot.carbs_g.toFixed(1)} F${+tot.fat_g.toFixed(1)}</b></div>` +
    `<div class="mbt-row mbt-per"><span>per portion (÷${+sv})</span><b>${Math.round(per("calories"))}kcal · ` +
    `P${per("protein_g")} C${per("carbs_g")} F${per("fat_g")}</b></div>`;
}

function mbSetMode(m) {
  _mbMode = m;
  $("mb-mode-ing").classList.toggle("active", m === "ingredients");
  $("mb-mode-mac").classList.toggle("active", m === "macros");
  $("mb-ing").hidden = m !== "ingredients";
  $("mb-mac").hidden = m !== "macros";
}

function mbReset() {
  $("mb-name").value = ""; $("mb-meal").value = ""; $("mb-servings").value = "1";
  $("mb-cal").value = ""; $("mb-pro").value = ""; $("mb-carb").value = ""; $("mb-fat").value = "";
  $("mb-ing-rows").innerHTML = ""; $("mb-msg").textContent = "";
  mbAddIngRow();
  mbSetMode("ingredients");
}

async function mbSave() {
  const name = $("mb-name").value.trim();
  const msg = $("mb-msg");
  if (!name) { msg.textContent = "give it a name"; return; }
  const meal = $("mb-meal").value;
  let body;
  if (_mbMode === "ingredients") {
    const ingredients = Array.prototype.slice.call($("mb-ing-rows").querySelectorAll(".nut-ing-row"))
      .map((row) => {
        const nm = row.querySelector(".ing-name").value.trim();
        const m = _rowMacros(row);
        if (!nm || !m) return null;
        const amt = parseFloat(row.querySelector(".ing-amt").value) || 0;
        return { name: nm, amount: amt, unit: (row._per && row._per.unit) || "g",
          calories: +m.calories.toFixed(1), protein_g: +m.protein_g.toFixed(1),
          carbs_g: +m.carbs_g.toFixed(1), fat_g: +m.fat_g.toFixed(1) };
      }).filter(Boolean);
    if (!ingredients.length) { msg.textContent = "add at least one ingredient (search & pick it)"; return; }
    body = { name, meal, servings: parseFloat($("mb-servings").value) || 1, ingredients };
  } else {
    body = { name, meal, servings: 1,
      calories: parseFloat($("mb-cal").value) || 0,
      protein_g: parseFloat($("mb-pro").value) || 0,
      carbs_g: parseFloat($("mb-carb").value) || 0,
      fat_g: parseFloat($("mb-fat").value) || 0 };
    if (!(body.calories || body.protein_g || body.carbs_g || body.fat_g)) {
      msg.textContent = "enter the macros"; return;
    }
  }
  const btn = $("mb-save"); const orig = btn.textContent;
  btn.disabled = true; btn.textContent = "saving…";
  try {
    const r = await jwrite("save that meal", "/api/nutrition/meals", jbody("POST", body));
    // Keep the builder open with its contents on failure — it's a long form and
    // "save failed" plus an empty form is the worst of both.
    if (!r.ok) { msg.textContent = "save failed — nothing was stored"; return; }
    mbReset();
    $("nut-builder").hidden = true;
    loadMeals();
  } finally { btn.disabled = false; btn.textContent = orig; }
}

// ---- meal builder: event wiring ----
const _mbBuildBtn = document.getElementById("nut-meal-build-btn");
if (_mbBuildBtn) _mbBuildBtn.addEventListener("click", () => {
  const b = $("nut-builder");
  b.hidden = !b.hidden;
  if (!b.hidden && !$("mb-ing-rows").querySelector(".nut-ing-row")) mbAddIngRow();
});
const _mbIngAdd = document.getElementById("mb-ing-add");
if (_mbIngAdd) _mbIngAdd.addEventListener("click", mbAddIngRow);
const _mbServings = document.getElementById("mb-servings");
if (_mbServings) _mbServings.addEventListener("input", mbRecalc);
const _mbModeIng = document.getElementById("mb-mode-ing");
if (_mbModeIng) _mbModeIng.addEventListener("click", () => mbSetMode("ingredients"));
const _mbModeMac = document.getElementById("mb-mode-mac");
if (_mbModeMac) _mbModeMac.addEventListener("click", () => mbSetMode("macros"));
const _mbSaveBtn = document.getElementById("mb-save");
if (_mbSaveBtn) _mbSaveBtn.addEventListener("click", mbSave);
const _mbCancel = document.getElementById("mb-cancel");
if (_mbCancel) _mbCancel.addEventListener("click", () => { mbReset(); $("nut-builder").hidden = true; });

// ingredient rows: per-row food search + amount → live macros (event delegation)
const _mbIngRows = document.getElementById("mb-ing-rows");
if (_mbIngRows) {
  _mbIngRows.addEventListener("input", (e) => {
    if (e.target.classList.contains("ing-amt")) { mbRecalc(); return; }
    if (!e.target.classList.contains("ing-name")) return;
    const row = e.target.closest(".nut-ing-row");
    row._per = null;  // typing invalidates a previous pick
    const q = e.target.value.trim();
    const res = row.querySelector(".ing-results");
    clearTimeout(row._timer);
    if (q.length < 2) { res.classList.remove("open"); res.innerHTML = ""; mbRecalc(); return; }
    row._timer = setTimeout(async () => {
      try {
        const r = await (await fetch("/api/nutrition/search?limit=6&q=" + encodeURIComponent(q))).json();
        const foods = (r.foods || []).filter((f) => f.per_100g);  // grams-scalable only
        res._foods = foods;
        if (!foods.length) { res.classList.remove("open"); res.innerHTML = ""; return; }
        res.innerHTML = foods.map((f, i) =>
          `<button type="button" class="ing-opt" data-i="${i}">` +
          `<span class="nut-rname">${escapeHtml(f.name)}${f.brand ? ` <span class="nut-rbrand">${escapeHtml(f.brand)}</span>` : ""}</span>` +
          `<span class="nut-rmac">${Math.round(f.calories)}kcal P${f.protein_g} C${f.carbs_g} F${f.fat_g} <span class="nut-rper">/100g</span></span>` +
          `</button>`).join("");
        res.classList.add("open");
      } catch (err) { /* ignore */ }
    }, 350);
  });
  _mbIngRows.addEventListener("click", (e) => {
    const opt = e.target.closest(".ing-opt");
    if (opt) {
      const row = opt.closest(".nut-ing-row");
      const res = row.querySelector(".ing-results");
      const f = (res._foods || [])[+opt.dataset.i];
      if (!f) return;
      row._per = { calories: f.calories, protein_g: f.protein_g, carbs_g: f.carbs_g,
                   fat_g: f.fat_g, ref: (+f.ref_qty) || 100, unit: f.ref_unit || "g" };
      row.querySelector(".ing-name").value = f.name;
      const amtEl = row.querySelector(".ing-amt");
      if (!(parseFloat(amtEl.value) > 0)) amtEl.value = f.serving_g || 100;
      res.classList.remove("open"); res.innerHTML = "";
      mbRecalc();
      return;
    }
    const del = e.target.closest(".ing-del");
    if (del) {
      del.closest(".nut-ing-row").remove();
      if (!_mbIngRows.querySelector(".nut-ing-row")) mbAddIngRow();
      mbRecalc();
    }
  });
}

// saved-meals list: log by the portion, or delete (event delegation)
const _mealsList = document.getElementById("nut-meals-list");
if (_mealsList) _mealsList.addEventListener("click", async (e) => {
  const card = e.target.closest(".nut-meal-card");
  if (!card) return;
  if (e.target.closest(".nmc-logbtn")) {
    const portions = parseFloat(card.querySelector(".nmc-portions").value) || 1;
    const meal = card.querySelector(".nmc-meal").value;
    const btn = e.target.closest(".nmc-logbtn"); const orig = btn.textContent;
    btn.disabled = true; btn.textContent = "…";
    try {
      const r = await jwrite("log that meal", "/api/nutrition/meals/log",
                             jbody("POST", { name: card.dataset.name, portions, meal }));
      if (r.ok) {
        _nutDate = null;  // it lands today — show today
        await loadNutrition();
      }
    } finally { btn.disabled = false; btn.textContent = orig; }
    return;
  }
  if (e.target.closest(".nmc-del")) {
    if (!confirm(`Delete saved meal "${card.dataset.name}"? (Anything already logged from it stays.)`)) return;
    await jwrite("delete that meal", "/api/nutrition/meals/" + card.dataset.id, jbody("DELETE"));
    loadMeals();
  }
});

// ---- activity timeline (non-AI explorer over /api/activity/*) ----
// Two bodies on one timeline: desktop foreground (from the state loop) and phone
// screen sessions (off the bus), both accumulated in the local activity store.
let _actDate = null;      // null = today; else "YYYY-MM-DD"
let _actDays = 7;
let _actDevice = "all";   // all | desktop | phone — which body the day view shows
let _actDay = null;       // last day payload, so a filter change re-renders without a refetch
let _actDetailApp = null; // app currently open in the drill-down, or null

function _ymd(d) {
  return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") +
    "-" + String(d.getDate()).padStart(2, "0");
}
function _today() { return _ymd(new Date()); }        // LOCAL day, to match the server

// ---- budget ---------------------------------------------------------------
// Daily allowances over the Activity timeline. Read-only except for four writes:
// set an allowance, grant an override, toggle enforcement, drop a rule.
//
// The server decides whether a change applies now or tomorrow (cut vs raise) and
// says so in its response. This renders what it's told rather than predicting it
// — if the rule ever changes, the UI shouldn't be a second place that has to be
// kept in step, and a screen that confidently says "saved" about a number that
// won't take effect until tomorrow is worse than one that says nothing.
const BUD_LABEL = { game: "🎮 Games", social: "📱 Feeds", video: "▶ Video" };

async function loadBudget() {
  const [status, rules] = await Promise.all([
    jread("/api/budget"),
    jread("/api/budget/rules"),
  ]);
  const events = await jread("/api/budget/events");
  // Bail before rendering: a dead read must not paint "0m of 3h used", which is
  // a claim about Jake's day, not a blank slate. Same trap as the nutrition tab.
  if (!status || !rules || !events) return loadFail("budget", "your budget", loadBudget);
  loadOk(loadBudget);
  renderBudget(status, rules, events.events || []);
}

function renderBudget(status, rules, events) {
  const cats = status.categories || [];
  const byCat = Object.fromEntries(cats.map((c) => [c.category, c]));
  const all = rules.categories || [];

  const over = status.overrides_today || 0;
  document.getElementById("bud-sub").textContent =
    `${status.day} · ${status.untracked} untracked` +
    (over ? ` · ${over} override${over > 1 ? "s" : ""}` : "");

  document.getElementById("bud-cats").innerHTML = all.map((name) => {
    const c = byCat[name];
    const label = BUD_LABEL[name] || name;
    if (!c) {
      // Unconfigured is unlimited, deliberately — a fresh install must not lock
      // anyone out before they've chosen a number. Say so plainly rather than
      // showing a zeroed bar that looks like a limit already being enforced.
      return `<div class="bud-cat unset" data-cat="${name}">
        <div class="bud-cat-head"><span class="bud-cat-name">${label}</span>
          <span class="bud-cat-figs">no limit set</span></div>
        <div class="bud-set">
          <input type="number" min="0" step="15" placeholder="minutes / day" data-bud-input="${name}">
          <button class="bud-save" data-bud-save="${name}">set a limit</button>
        </div>
      </div>`;
    }
    const pct = Math.min(100, c.pct);
    const state = c.pct >= 100 ? "over" : c.pct >= 90 ? "high" : c.pct >= 50 ? "mid" : "low";
    const marks = [50, 75, 90, 95]
      .map((t) => `<i class="bud-mark" style="left:${t}%" title="${t}%"></i>`).join("");
    return `<div class="bud-cat ${c.enabled ? "" : "off"}" data-cat="${name}">
      <div class="bud-cat-head">
        <span class="bud-cat-name">${label}</span>
        <span class="bud-cat-figs"><b>${c.spent}</b> of ${c.allowance}</span>
        <button class="bud-toggle" data-bud-enabled="${name}" data-on="${c.enabled ? 1 : 0}"
          title="${c.enabled ? "enforcing — click to watch only" : "watching only — click to enforce"}"
          >${c.enabled ? "enforcing" : "watching"}</button>
      </div>
      <div class="bud-bar ${state}"><i style="width:${pct}%"></i>${marks}</div>
      <div class="bud-cat-foot">
        <span class="bud-left">${c.pct >= 100 ? "spent" : c.remaining + " left"}</span>
        ${c.override_seconds ? `<span class="bud-ov">+${Math.round(c.override_seconds / 60)}m granted today</span>` : ""}
        ${c.pending_minutes ? `<span class="bud-pending">→ ${c.pending_minutes}m from ${c.pending_day}</span>` : ""}
      </div>
      <div class="bud-set">
        <input type="number" min="0" step="15" value="${c.minutes}" data-bud-input="${name}">
        <button class="bud-save" data-bud-save="${name}">save</button>
        <span class="bud-spacer"></span>
        <button class="bud-ovbtn" data-bud-override="${name}" data-min="15">+15m</button>
        <button class="bud-ovbtn" data-bud-override="${name}" data-min="30">+30m</button>
      </div>
    </div>`;
  }).join("");

  const KIND = {
    warn: (e) => `${e.detail}% of the day's ${e.category}`,
    depleted: (e) => `${e.category} budget spent — ${e.detail}`,
    grace: (e) => `${e.detail} to wrap up ${e.category}`,
    closed: (e) => `closed ${e.detail || e.category}`,
    override: (e) => `override ${e.detail}`,
    rule: (e) => e.detail,
  };
  document.getElementById("bud-events").innerHTML = events.length
    ? events.slice().reverse().map((e) => `<div class="bud-ev ${e.kind}">
        <span class="bud-ev-t">${fmtTime(e.ts)}</span>
        <span class="bud-ev-k">${e.kind}</span>
        <span class="bud-ev-d">${escapeHtml((KIND[e.kind] || ((x) => x.detail || ""))(e))}</span>
      </div>`).join("")
    : `<div class="empty">nothing yet today</div>`;

  const rs = rules.rules || [];
  document.getElementById("bud-rules-sub").textContent = `${rs.length} rules`;
  document.getElementById("bud-rules").innerHTML = rs.map((r) => `<div class="bud-rule">
      <span class="bud-rule-cat">${BUD_LABEL[r.category] || r.category}</span>
      <code>${escapeHtml(r.match)}</code>
      <span class="bud-rule-field">${r.field}</span>
      <button class="bud-rule-x" data-bud-rule-del="${r.id}" title="stop counting this">×</button>
    </div>`).join("");
}

document.getElementById("view-budget").addEventListener("click", async (e) => {
  const save = e.target.closest("[data-bud-save]");
  if (save) {
    const cat = save.dataset.budSave;
    const input = document.querySelector(`[data-bud-input="${cat}"]`);
    const minutes = parseInt(input.value, 10);
    if (!Number.isFinite(minutes) || minutes < 0) return toast("give it a number of minutes");
    const r = await jwrite("save that limit", "/api/budget/allowance",
      jbody("POST", { category: cat, minutes }));
    // Report what the *server* did, not what we assumed it would do.
    if (r.ok && r.data) {
      toast(r.data.applies === "tomorrow"
        ? `${minutes}m from tomorrow — raises don't take effect the same day`
        : `${cat} set to ${minutes}m`);
    }
    return loadBudget();
  }
  const ov = e.target.closest("[data-bud-override]");
  if (ov) {
    const cat = ov.dataset.budOverride, minutes = parseInt(ov.dataset.min, 10);
    const r = await jwrite("grant that time", "/api/budget/override",
      jbody("POST", { category: cat, minutes, reason: "from the budget tab" }));
    if (r.ok) toast(`+${minutes}m on ${cat} today — logged`);
    return loadBudget();
  }
  const tog = e.target.closest("[data-bud-enabled]");
  if (tog) {
    await jwrite("change that", "/api/budget/enabled",
      jbody("POST", { category: tog.dataset.budEnabled, enabled: tog.dataset.on !== "1" }));
    return loadBudget();
  }
  const del = e.target.closest("[data-bud-rule-del]");
  if (del) {
    await jwrite("remove that rule", "/api/budget/rules/" + del.dataset.budRuleDel, jbody("DELETE"));
    return loadBudget();
  }
  const rt = e.target.closest("#bud-rules-toggle");
  if (rt) {
    const box = document.getElementById("bud-rules");
    box.hidden = !box.hidden;
    rt.textContent = box.hidden ? "show" : "hide";
  }
});

async function loadActivity() {
  const q = _actDate ? "?date=" + encodeURIComponent(_actDate) : "";
  const [day, summary] = await Promise.all([
    jread("/api/activity" + q),
    jread("/api/activity/summary?days=" + _actDays),
  ]);
  // Bail before assigning _actDay — it's the cache the drill-downs read from, so a
  // failed load used to poison them too, not just this render.
  if (!day || !summary) return loadFail("activity", "your activity", loadActivity);
  loadOk(loadActivity);
  _actDay = day;
  renderActWeek(summary);
  renderActDay(day);
}

// Stable per-app colour from a name hash — the same app is the same hue in the
// timeline, the app bars and the segment list.
function appHue(name) {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) % 360;
  return h;
}
function appColor(name) { return `hsl(${appHue(name)} 60% 58%)`; }

function fmtMin(m) {
  m = Math.round(m || 0);
  if (m < 60) return m + "m";
  const h = Math.floor(m / 60), r = m % 60;
  return r ? `${h}h${String(r).padStart(2, "0")}m` : `${h}h`;
}

function renderActWeek(s) {
  const box = $("act-week");
  const series = (s && s.series) || [];
  if (!series.some((d) => d.desktop_min || d.phone_min)) {
    box.innerHTML = '<div class="empty">no activity yet — the timeline builds as you use your devices</div>';
    return;
  }
  const max = Math.max(...series.map((d) => d.desktop_min + d.phone_min), 1);
  const sel = _actDate || _today();
  box.innerHTML = series.map((d) => {
    const dh = Math.round((96 * d.desktop_min) / max);
    const ph = Math.round((96 * d.phone_min) / max);
    const dow = new Date(d.date + "T00:00").toLocaleDateString(undefined, { weekday: "short" });
    const active = (d.date === sel) ? " active" : "";
    const tip = `${d.date}: ${fmtMin(d.desktop_min)} desktop · ${fmtMin(d.phone_min)} phone`;
    return `<button class="act-day${active}" data-date="${d.date}" title="${escapeHtml(tip)}">` +
      `<span class="act-day-bars"><span class="act-day-d" style="height:${dh}%"></span>` +
      `<span class="act-day-p" style="height:${ph}%"></span></span>` +
      `<span class="act-day-dow">${escapeHtml(dow)}</span>` +
      `<span class="act-day-num">${escapeHtml(d.date.slice(5))}</span></button>`;
  }).join("");
}

function renderActDay(t) {
  const today = _today();
  $("act-date").textContent = (t.date === today) ? `Today · ${t.date}` : t.date;
  $("act-next").disabled = (t.date >= today);
  $("act-sub").textContent = t.top_app ? "most on " + t.top_app : "";
  renderActTiles(t);
  renderTimeline(t);
  renderActApps(_appsForDevice(t));
  renderActSegs(_segsForDevice(t));
}

// The app breakdown for the active device filter. "Both" merges desktop and
// phone apps into one ranked list (each tagged with its device); a single-device
// filter shows just that body's apps.
function _appsForDevice(t) {
  const desk = (t.apps || []).map((a) => ({ ...a, device: "desktop" }));
  const fone = (t.phone_apps || []).map((a) => ({ ...a, device: "phone" }));
  if (_actDevice === "desktop") return desk;
  if (_actDevice === "phone") return fone;
  return [...desk, ...fone].sort((a, b) => b.seconds - a.seconds);
}
function _segsForDevice(t) {
  const segs = t.segments || [];
  return _actDevice === "all" ? segs : segs.filter((s) => s.device === _actDevice);
}

function renderActTiles(t) {
  const tile = (v, k) => `<div class="act-tile"><div class="act-tv">${v}</div><div class="act-tk">${k}</div></div>`;
  let html;
  if (_actDevice === "phone") {
    const apps = t.phone_apps || [];
    html = tile(fmtMin(t.phone_seconds / 60), "phone screen") +
      tile(apps.length, "phone apps") +
      tile(apps[0] ? apps[0].name : "—", "top app") +
      tile((t.segments || []).filter((s) => s.device === "phone").length, "sessions");
  } else if (_actDevice === "desktop") {
    const apps = t.apps || [];
    html = tile(fmtMin(t.desktop_seconds / 60), "at the desk") +
      tile(apps.length, "apps used") +
      tile(apps[0] ? apps[0].name : "—", "top app") +
      tile((t.segments || []).filter((s) => s.device === "desktop").length, "switches");
  } else {
    html = tile(fmtMin(t.desktop_seconds / 60), "at the desk") +
      tile(fmtMin(t.phone_seconds / 60), "phone screen") +
      tile((t.apps || []).length + (t.phone_apps || []).length, "apps used") +
      tile(t.top_app || "—", "top app");
  }
  $("act-stat").innerHTML = html;
}

function _dayStart(dateStr) { return new Date(dateStr + "T00:00").getTime() / 1000; }

function renderTimeline(t) {
  const day0 = _dayStart(t.date), DAY = 86400;
  // Hide the track for a body the filter excludes.
  const row = { desktop: $("act-row-desktop"), phone: $("act-row-phone") };
  row.desktop.style.display = (_actDevice === "phone") ? "none" : "";
  row.phone.style.display = (_actDevice === "desktop") ? "none" : "";
  const tracks = { desktop: $("act-track-desktop"), phone: $("act-track-phone") };
  tracks.desktop.innerHTML = "";
  tracks.phone.innerHTML = "";
  (t.segments || []).forEach((s) => {
    const tr = tracks[s.device];
    if (!tr) return;
    const el = document.createElement("span");
    el.className = "act-block";
    el.style.left = Math.max(0, ((s.start_ts - day0) / DAY) * 100) + "%";
    el.style.width = Math.max(0.5, ((s.end_ts - s.start_ts) / DAY) * 100) + "%";
    el.style.background = appColor(s.app);   // per-app on both tracks now
    el.title = `${s.start_hm}–${s.end_hm} · ${s.app} (${s.for})`;
    el.addEventListener("click", () => openAppDetail(s.app));
    tr.appendChild(el);
  });
  if (t.date === _today()) {   // live "now" marker
    const frac = Math.min(100, Math.max(0, ((Date.now() / 1000 - day0) / DAY) * 100));
    ["desktop", "phone"].forEach((d) => {
      const m = document.createElement("span");
      m.className = "act-now";
      m.style.left = frac + "%";
      tracks[d].appendChild(m);
    });
  }
  $("act-hours").innerHTML = [0, 6, 12, 18, 24].map((h) =>
    `<span style="left:${(h / 24) * 100}%">${String(h).padStart(2, "0")}:00</span>`).join("");
}

function renderActApps(apps) {
  const box = $("act-apps");
  if (!apps.length) { box.innerHTML = '<div class="empty">no activity recorded for this day</div>'; return; }
  const max = apps[0].seconds || 1;
  box.innerHTML = apps.map((a) => {
    const w = Math.max(2, Math.round((100 * a.seconds) / max));
    // Only tag the device when "Both" is showing — a single-device filter makes it redundant.
    const icon = (_actDevice === "all") ? (a.device === "phone" ? "📱 " : "🖥 ") : "";
    return `<button class="act-app" data-app="${escapeHtml(a.name)}" title="${escapeHtml(a.name)} — click for this app across days">` +
      `<span class="act-app-name">${icon}${escapeHtml(a.name)}</span>` +
      `<span class="act-app-bar"><span style="width:${w}%;background:${appColor(a.name)}"></span></span>` +
      `<span class="act-app-t">${escapeHtml(a.for)}</span></button>`;
  }).join("");
}

function renderActSegs(segs) {
  const box = $("act-segs");
  const rows = segs.slice().reverse();   // newest first
  if (!rows.length) { box.innerHTML = '<div class="empty">nothing recorded yet</div>'; return; }
  box.innerHTML = rows.map((s) => {
    const icon = s.device === "phone" ? "📱" : "🖥";
    const detail = (s.title && s.title !== s.app) ? ` — ${escapeHtml(s.title)}` : "";
    return `<button class="act-seg" data-app="${escapeHtml(s.app)}" title="click for ${escapeHtml(s.app)} across days">` +
      `<span class="act-seg-time">${s.start_hm}</span>` +
      `<span class="act-seg-dot" style="background:${appColor(s.app)}"></span>` +
      `<span class="act-seg-app">${icon} ${escapeHtml(s.app)}${detail}</span>` +
      `<span class="act-seg-for">${escapeHtml(s.for)}</span></button>`;
  }).join("");
}

// ---- app drill-down: one app's usage across the selected range ----
async function openAppDetail(name) {
  _actDetailApp = name;
  const box = $("act-detail");
  $("act-detail-title").textContent = name;
  $("act-detail-stat").innerHTML = '<div class="empty">loading…</div>';
  $("act-detail-chart").innerHTML = "";
  box.hidden = false;
  // The overlay is absolute inside the scrollable panel, so align the panel to
  // the top or the overlay would sit wherever the list was scrolled to.
  if (box.parentElement) box.parentElement.scrollTop = 0;
  try {
    const h = await fetch("/api/activity/app?name=" + encodeURIComponent(name) + "&days=" + _actDays)
      .then((r) => r.json());
    if (_actDetailApp === name) renderAppDetail(h);   // ignore a stale response
  } catch (e) {
    $("act-detail-stat").innerHTML = '<div class="empty">failed to load</div>';
  }
}

function closeAppDetail() {
  _actDetailApp = null;
  $("act-detail").hidden = true;
}

function renderAppDetail(h) {
  const dev = h.device === "phone" ? "📱 phone" : (h.device === "desktop" ? "🖥 desktop" : "");
  $("act-detail-title").innerHTML = escapeHtml(h.app) +
    (dev ? ` <span class="act-detail-dev">${dev}</span>` : "");
  const tile = (v, k) => `<div class="act-tile"><div class="act-tv">${v}</div><div class="act-tk">${k}</div></div>`;
  $("act-detail-stat").innerHTML =
    tile(h.total || "0s", `total · ${h.window_days}d`) +
    tile(h.active_days, "active days") +
    tile(h.avg_min ? fmtMin(h.avg_min) : "—", "avg / active day");
  const series = h.series || [];
  if (!series.some((d) => d.seconds)) {
    $("act-detail-chart").innerHTML = '<div class="empty">no usage recorded in this range</div>';
    return;
  }
  const max = Math.max(...series.map((d) => d.seconds), 1);
  $("act-detail-chart").innerHTML = series.map((d) => {
    const ht = Math.max(d.seconds ? 3 : 0, Math.round((100 * d.seconds) / max));
    const dow = new Date(d.date + "T00:00").toLocaleDateString(undefined, { weekday: "short" });
    const tip = `${d.date}: ${fmtMin(d.min)}`;
    return `<button class="act-dbar" data-date="${d.date}" title="${escapeHtml(tip)}">` +
      `<span class="act-dbar-track"><span class="act-dbar-fill" style="height:${ht}%;background:${appColor(h.app)}"></span></span>` +
      `<span class="act-dbar-dow">${escapeHtml(dow)}</span>` +
      `<span class="act-dbar-num">${escapeHtml(d.date.slice(8))}</span></button>`;
  }).join("");
}

// ---- activity: event wiring ----
const _actRange = document.getElementById("act-range");
if (_actRange) _actRange.addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  _actDays = Number(b.dataset.days);
  _actRange.querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
  loadActivity();
  if (_actDetailApp) openAppDetail(_actDetailApp);   // keep the open drill-down in sync with the range
});
const _actWeek = document.getElementById("act-week");
if (_actWeek) _actWeek.addEventListener("click", (e) => {
  const b = e.target.closest(".act-day");
  if (!b) return;
  _actDate = (b.dataset.date === _today()) ? null : b.dataset.date;
  loadActivity();
});

// Device filter — the chips and the timeline track labels both drive it.
function _setActDevice(dev) {
  _actDevice = dev;
  const chips = document.getElementById("act-devs");
  if (chips) chips.querySelectorAll("button").forEach((x) => x.classList.toggle("active", x.dataset.dev === dev));
  if (_actDay) renderActDay(_actDay);   // re-render from the cached payload, no refetch
}
const _actDevs = document.getElementById("act-devs");
if (_actDevs) _actDevs.addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (b) _setActDevice(b.dataset.dev);
});
document.querySelectorAll(".act-tl-label[data-dev]").forEach((lbl) => {
  lbl.addEventListener("click", () => {
    // Clicking a track label filters to it; clicking it again clears back to Both.
    _setActDevice(_actDevice === lbl.dataset.dev ? "all" : lbl.dataset.dev);
  });
});

// App drill-down — click an app bar or a segment to open it; back button / a
// day bar closes it (the day bar also jumps to that day).
const _actApps = document.getElementById("act-apps");
if (_actApps) _actApps.addEventListener("click", (e) => {
  const b = e.target.closest(".act-app");
  if (b) openAppDetail(b.dataset.app);
});
const _actSegs = document.getElementById("act-segs");
if (_actSegs) _actSegs.addEventListener("click", (e) => {
  const b = e.target.closest(".act-seg");
  if (b) openAppDetail(b.dataset.app);
});
const _actDetailClose = document.getElementById("act-detail-close");
if (_actDetailClose) _actDetailClose.addEventListener("click", closeAppDetail);
const _actDetailChart = document.getElementById("act-detail-chart");
if (_actDetailChart) _actDetailChart.addEventListener("click", (e) => {
  const b = e.target.closest(".act-dbar");
  if (!b) return;
  _actDate = (b.dataset.date === _today()) ? null : b.dataset.date;
  closeAppDetail();
  loadActivity();
});
function _actShift(delta) {
  const d = new Date((_actDate || _today()) + "T00:00");
  d.setDate(d.getDate() + delta);
  const next = _ymd(d);
  if (next > _today()) return;
  _actDate = (next === _today()) ? null : next;
  loadActivity();
}
const _actPrev = document.getElementById("act-prev");
if (_actPrev) _actPrev.addEventListener("click", () => _actShift(-1));
const _actNext = document.getElementById("act-next");
if (_actNext) _actNext.addEventListener("click", () => _actShift(1));
const _actToday = document.getElementById("act-today");
if (_actToday) _actToday.addEventListener("click", () => { _actDate = null; loadActivity(); });

// ---- body (non-AI explorer over /api/body/*) ----
// Weight/height → BMI; steps + workouts → active energy expenditure; add food
// intake → energy balance. Writes weight/height/steps here; steps can equally
// arrive from a health feed (POST /api/body/steps, or the phone's phone.steps
// signal from an on-device Health Connect reader). Same date-nav pattern as the
// Nutrition tab, and the same _today()/_ymd() local-day helpers.
let _bodyDate = null;     // null = today; else "YYYY-MM-DD"
let _bodyDays = 30;       // weight-trend window

async function loadBody() {
  const date = _bodyDate || "";
  const [day, summary] = await Promise.all([
    jread("/api/body?date=" + encodeURIComponent(date)),
    jread("/api/body/summary?days=" + _bodyDays),
  ]);
  // Weight is the one number here Jake acts on, and a blank one reads as "you
  // didn't weigh in" — which invites a re-entry. That's how a wrong figure got
  // into body.db once already.
  if (!day || !summary) return loadFail("body", "your body log", loadBody);
  loadOk(loadBody);
  renderBodyDay(day);
  renderBodyTrend(summary);
}

function renderBodyDay(d) {
  const isToday = (d.date === _today());
  $("body-date").textContent = isToday ? "Today · " + d.date : d.date;
  $("body-today").style.visibility = isToday ? "hidden" : "visible";
  const bmi = d.bmi, ew = d.weight_effective_kg;
  $("body-sub").textContent = bmi ? `BMI ${bmi.value} · ${bmi.category}`
    : (ew ? "add your height for a BMI" : "");
  // steps source badge — only interesting when it didn't come from typing
  const src = $("body-src");
  src.textContent = (d.steps && d.steps_source && d.steps_source !== "manual")
    ? `steps via ${d.steps_source}` : "";
  renderBodyStat(d);
  renderBodyBmi(bmi);
  renderBodyEnergy(d);
  // prefill the inputs with the day's stored values (blank if none)
  $("body-weight").value = d.weight_kg != null ? (+d.weight_kg) : "";
  $("body-height").value = d.height_cm != null ? (+d.height_cm) : "";
  $("body-fat").value = d.body_fat_pct != null ? (+d.body_fat_pct) : "";
  $("body-muscle").value = d.muscle_mass_kg != null ? (+d.muscle_mass_kg) : "";
  $("body-steps").value = d.steps ? d.steps : "";
}

function _bodyStatCls(cat) {
  if (cat === "normal" || cat === "underweight") return "good";
  if (cat === "overweight" || cat === "obese") return "warn";
  return "";
}
function _bodyTile(val, unit, k, sub, subcls) {
  return `<div class="body-tile"><div class="body-tk">${k}</div>` +
    `<div class="body-tv">${val}${unit ? `<span class="body-tu">${unit}</span>` : ""}</div>` +
    `<div class="body-tsub ${subcls || ""}">${escapeHtml(sub || "")}</div></div>`;
}
function renderBodyStat(d) {
  const bmi = d.bmi, ew = d.weight_effective_kg, measured = d.weight_kg != null;
  const wVal = ew != null ? (measured ? `${(+ew)}` : `~${(+ew)}`) : "—";
  // Smart-scale composition — shown only when there's a reading (day's own, else
  // last known, flagged with a ~ and "last logged" like the weight tile).
  const bf = d.body_fat_effective_pct, bfMeas = d.body_fat_pct != null;
  const mm = d.muscle_mass_effective_kg, mmMeas = d.muscle_mass_kg != null;
  let tiles =
    _bodyTile(wVal, "kg", "weight",
      measured ? "measured this day" : (ew != null ? "last logged" : "not logged yet")) +
    _bodyTile(bmi ? bmi.value : "—", "", "BMI",
      bmi ? bmi.category : "needs height", bmi ? _bodyStatCls(bmi.category) : "");
  if (bf != null) {
    const sub = d.fat_mass_kg ? `~${(+d.fat_mass_kg)} kg fat` : (bfMeas ? "measured this day" : "last logged");
    tiles += _bodyTile(bfMeas ? `${(+bf)}` : `~${(+bf)}`, "%", "body fat",
      bfMeas ? sub : (d.fat_mass_kg ? sub + " · last logged" : "last logged"));
  }
  if (mm != null) {
    tiles += _bodyTile(mmMeas ? `${(+mm)}` : `~${(+mm)}`, "kg", "muscle",
      mmMeas ? "measured this day" : "last logged");
  }
  tiles +=
    _bodyTile((d.steps || 0).toLocaleString(), "", "steps", `~${d.steps_kcal || 0} kcal`) +
    _bodyTile((d.expenditure_kcal || 0).toLocaleString(), "kcal", "burned", "steps + workouts");
  $("body-stat").innerHTML = tiles;
}

// BMI on a 15–40 scale, with the four WHO zones drawn to their true widths and
// the boundary ticks positioned at their real value (not evenly spaced).
function renderBodyBmi(bmi) {
  const box = $("body-bmi");
  if (!bmi) { box.innerHTML = '<div class="empty">log your weight and height to see your BMI</div>'; return; }
  const lo = 15, hi = 40, span = hi - lo;
  const pct = (v) => Math.max(0, Math.min(100, ((v - lo) / span) * 100));
  box.innerHTML =
    `<div class="body-bmi-read"><span class="body-bmi-val">${bmi.value}</span>` +
    `<span class="body-bmi-cat ${bmi.category}">${bmi.category}</span></div>` +
    `<div class="body-scale">` +
      `<span class="z-under" style="width:${(3.5 / span) * 100}%"></span>` +
      `<span class="z-normal" style="width:${(6.5 / span) * 100}%"></span>` +
      `<span class="z-over" style="width:${(5 / span) * 100}%"></span>` +
      `<span class="z-obese" style="width:${(10 / span) * 100}%"></span>` +
      `<span class="body-scale-marker" style="left:${pct(bmi.value)}%"></span>` +
    `</div>` +
    `<div class="body-scale-ticks">` +
      `<span style="left:${pct(18.5)}%">18.5</span>` +
      `<span style="left:${pct(25)}%">25</span>` +
      `<span style="left:${pct(30)}%">30</span></div>`;
}

function _bodyErow(k, v, extra) {
  return `<div class="body-erow ${extra || ""}"><span class="be-k">${escapeHtml(k)}</span>` +
    `<span class="be-v">${v}</span></div>`;
}
function renderBodyEnergy(d) {
  const box = $("body-energy");
  const sk = d.steps_kcal || 0, wk = d.workout_kcal || 0, exp = d.expenditure_kcal || 0;
  const intake = d.intake_kcal || 0;
  if (!sk && !wk && !intake) {
    box.innerHTML = '<div class="empty">no steps, workouts or food logged for this day</div>'; return;
  }
  const rows = [
    _bodyErow(`walking (${(d.steps || 0).toLocaleString()} steps)`, `~${sk} kcal`),
    _bodyErow("workouts", `${wk} kcal`),
    `<div class="body-erow total"><span class="be-k">active burn</span><span class="be-v">${exp.toLocaleString()} kcal</span></div>`,
  ];
  if (intake) {
    rows.push(_bodyErow("food intake", `${intake.toLocaleString()} kcal`));
    const net = d.balance_kcal, def = net < 0;
    rows.push(`<div class="body-erow balance"><span class="be-k">balance vs active burn</span>` +
      `<span class="be-v ${def ? "deficit" : "surplus"}">${Math.abs(net).toLocaleString()} kcal ${def ? "deficit" : "surplus"}</span></div>`);
  }
  box.innerHTML = rows.join("");
}

// Weight bars, scaled within the observed range (±1 kg) so day-to-day drift is
// actually visible instead of a flat line near the top.
function renderBodyTrend(s) {
  const box = $("body-trend");
  const series = s.series || [];
  const weights = series.filter((d) => d.weight_kg != null).map((d) => d.weight_kg);
  if (!weights.length) { box.innerHTML = '<div class="empty">log your weight to see the trend</div>'; return; }
  const lo = Math.floor(Math.min(...weights) - 1), hi = Math.ceil(Math.max(...weights) + 1);
  const span = Math.max(1, hi - lo);
  const sel = _bodyDate || _today();
  box.innerHTML = series.map((d) => {
    if (d.weight_kg == null) return `<span class="body-bar-wrap" title="${d.date}: —"></span>`;
    const h = Math.max(4, Math.round(((d.weight_kg - lo) / span) * 100));
    const tip = `${d.date}: ${(+d.weight_kg)}kg` + (d.bmi ? ` · BMI ${d.bmi}` : "");
    return `<span class="body-bar-wrap" title="${escapeHtml(tip)}">` +
      `<span class="body-bar ${d.date === sel ? "sel" : ""}" style="height:${h}%"></span></span>`;
  }).join("");
  const label = document.querySelector(".body-trend-label");
  if (label) label.textContent = s.latest_weight ? `weight · latest ${(+s.latest_weight)}kg` : "weight";
}

// ---- body: event wiring ----
const _bodyForm = document.getElementById("body-form");
if (_bodyForm) _bodyForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const day = _bodyDate || undefined;
  const w = $("body-weight").value.trim(), h = $("body-height").value.trim();
  const bf = $("body-fat").value.trim(), mm = $("body-muscle").value.trim();
  const st = $("body-steps").value.trim();
  if (!w && !h && !bf && !mm && st === "") return;
  const btn = $("body-save-btn");
  try {
    // jfetch, not fetch: a bare fetch only rejects on a dead network, so a 500
    // or an {"error":...}-at-200 used to fall straight through to "✓ Saved".
    if (w || h || bf || mm) {
      await jfetch("/api/body", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ weight_kg: w ? +w : null, height_cm: h ? +h : null,
          body_fat_pct: bf ? +bf : null, muscle_mass_kg: mm ? +mm : null, day }) });
    }
    if (st !== "") {
      await jfetch("/api/body/steps", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ count: +st, day, source: "manual" }) });
    }
    if (btn) {
      btn.classList.add("saved"); btn.textContent = "✓ Saved";
      setTimeout(() => { btn.classList.remove("saved"); btn.textContent = "＋ Save for this day"; }, 1400);
    }
    await loadBody();
  } catch (e2) {
    toast("couldn't save body entry: " + (e2.message || e2), "err");
  }
});

function _bodyShiftDay(delta) {
  const base = new Date((_bodyDate || _today()) + "T00:00");
  base.setDate(base.getDate() + delta);
  const d = _ymd(base);
  if (d > _today()) return;                 // no future days
  _bodyDate = (d === _today()) ? null : d;
  loadBody();
}
const _bodyPrev = document.getElementById("body-prev");
if (_bodyPrev) _bodyPrev.addEventListener("click", () => _bodyShiftDay(-1));
const _bodyNext = document.getElementById("body-next");
if (_bodyNext) _bodyNext.addEventListener("click", () => _bodyShiftDay(1));
const _bodyTodayBtn = document.getElementById("body-today");
if (_bodyTodayBtn) _bodyTodayBtn.addEventListener("click", () => { _bodyDate = null; loadBody(); });
const _bodyRange = document.getElementById("body-range");
if (_bodyRange) _bodyRange.addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  _bodyRange.querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
  _bodyDays = +b.dataset.days || 30;
  loadBody();
});

// ---- health (non-AI explorer over /api/health/*) ----
// Sleep (+stages), resting HR, HRV, SpO2, respiratory rate per day, and a derived
// 0–100 readiness score (a heuristic computed in the HealthStore from sleep-vs-8h +
// HRV/resting-HR vs a trailing baseline — Fitbit's own readiness is Premium-only
// and never reaches Health Connect). Writes go to POST /api/health; the same day is
// fed by the phone's Health Connect reader (phone.health) once the Fitbit Air is
// syncing. Same date-nav pattern and _today()/_ymd() local-day helpers as Body.
let _hlDate = null;      // null = today; else "YYYY-MM-DD"
let _hlDays = 30;        // readiness-trend window

async function loadHealth() {
  const date = _hlDate || "";
  const [day, summary] = await Promise.all([
    jread("/api/health?date=" + encodeURIComponent(date)),
    jread("/api/health/summary?days=" + _hlDays),
  ]);
  // _hlHM(min) returns "0m" for null, so a failed read rendered a night of no
  // sleep and a missing readiness score as though they'd been measured.
  if (!day || !summary) return loadFail("health", "your health data", loadHealth);
  loadOk(loadHealth);
  renderHealthDay(day);
  renderHealthTrend(summary);
}

function _hlHM(min) {
  if (!min || min <= 0) return "0m";
  const h = Math.floor(min / 60), m = Math.round(min % 60);
  return h ? `${h}h ${m}m` : `${m}m`;
}

function renderHealthDay(d) {
  const isToday = (d.date === _today());
  $("hl-date").textContent = isToday ? "Today · " + d.date : d.date;
  $("hl-today").style.visibility = isToday ? "hidden" : "visible";
  const r = d.readiness;
  $("hl-sub").textContent = r ? `readiness ${r.score} · ${r.category}`
    : (d.has_data ? "logged — readiness needs sleep or an HRV/HR baseline" : "");
  // source badge — only interesting when it didn't come from typing
  const src = $("hl-src");
  src.textContent = (d.source && d.source !== "manual") ? `via ${d.source}` : "";
  renderHealthStat(d);
  renderHealthReadiness(r);
  renderHealthSleep(d);
  renderHealthVitals(d);
  // prefill the inputs with the day's stored values (blank if none)
  $("hl-sleep-h").value = d.asleep_min ? +(d.asleep_min / 60).toFixed(2) : "";
  $("hl-bed-h").value = d.in_bed_min ? +(d.in_bed_min / 60).toFixed(2) : "";
  $("hl-rhr").value = d.resting_hr != null ? (+d.resting_hr) : "";
  $("hl-hrv").value = d.hrv_ms != null ? (+d.hrv_ms) : "";
  $("hl-spo2").value = d.spo2 != null ? (+d.spo2) : "";
  $("hl-resp").value = d.respiratory_rate != null ? (+d.respiratory_rate) : "";
}

function _hlReadCls(cat) {
  if (cat === "high") return "good";
  if (cat === "low") return "warn";
  return "";
}
function _hlTile(val, unit, k, sub, subcls) {
  return `<div class="hl-tile"><div class="hl-tk">${k}</div>` +
    `<div class="hl-tv">${val}${unit ? `<span class="hl-tu">${unit}</span>` : ""}</div>` +
    `<div class="hl-tsub ${subcls || ""}">${escapeHtml(sub || "")}</div></div>`;
}
function renderHealthStat(d) {
  const r = d.readiness;
  const rhrSub = d.resting_hr_baseline ? `base ${d.resting_hr_baseline}` : "no baseline yet";
  const hrvSub = d.hrv_baseline ? `base ${d.hrv_baseline}` : "no baseline yet";
  $("hl-stat").innerHTML =
    _hlTile(r ? r.score : "—", "", "readiness",
      r ? r.category : "needs sleep/vitals", r ? _hlReadCls(r.category) : "") +
    _hlTile(d.asleep_label || "—", "", "sleep",
      d.sleep_efficiency != null ? `${d.sleep_efficiency}% efficiency` : "time asleep") +
    _hlTile(d.resting_hr != null ? (+d.resting_hr) : "—", d.resting_hr != null ? "bpm" : "",
      "resting HR", rhrSub) +
    _hlTile(d.hrv_ms != null ? (+d.hrv_ms) : "—", d.hrv_ms != null ? "ms" : "", "HRV", hrvSub);
}

// Readiness on a 0–100 scale, three zones (low <40, moderate <70, high) with a
// marker at the day's score — mirrors the Body tab's BMI scale.
function renderHealthReadiness(r) {
  const box = $("hl-read");
  if (!r) {
    box.innerHTML = '<div class="empty">no readings for this day — readiness needs sleep or vitals</div>';
    return;
  }
  const pos = Math.max(0, Math.min(100, r.score));
  const comps = r.components || {};
  const chip = (k, label) => comps[k] != null
    ? `<span class="hl-comp"><b>${label}</b> ${comps[k]}</span>` : "";
  box.innerHTML =
    `<div class="hl-read-read"><span class="hl-read-val">${r.score}</span>` +
    `<span class="hl-read-cat ${r.category}">${r.category}</span>` +
    `<span class="hl-read-heur" title="a simple recovery heuristic, not a clinical figure">heuristic</span></div>` +
    `<div class="hl-scale">` +
      `<span class="z-low" style="width:40%"></span>` +
      `<span class="z-mod" style="width:30%"></span>` +
      `<span class="z-high" style="width:30%"></span>` +
      `<span class="hl-scale-marker" style="left:${pos}%"></span>` +
    `</div>` +
    `<div class="hl-scale-ticks"><span style="left:40%">40</span><span style="left:70%">70</span></div>` +
    (Object.keys(comps).length
      ? `<div class="hl-comps">${chip("sleep", "sleep")}${chip("hrv", "HRV")}${chip("rhr", "resting HR")}</div>`
      : "");
}

// Sleep stages as a stacked bar (deep · rem · light · awake) + asleep/efficiency.
function renderHealthSleep(d) {
  const box = $("hl-sleep");
  if (!d.asleep_min && !d.in_bed_min) {
    box.innerHTML = '<div class="empty">no sleep logged for this day</div>';
    return;
  }
  const stages = [
    { k: "deep", v: d.deep_min || 0 },
    { k: "rem", v: d.rem_min || 0 },
    { k: "light", v: d.light_min || 0 },
    { k: "awake", v: d.awake_min || 0 },
  ];
  const staged = stages.reduce((a, s) => a + s.v, 0);
  let bar = "";
  if (staged > 0) {
    const shown = stages.filter((s) => s.v > 0);
    bar = `<div class="hl-stages">` + shown.map((s) =>
      `<span class="hl-stage ${s.k}" style="width:${(s.v / staged) * 100}%" title="${s.k}: ${_hlHM(s.v)}"></span>`).join("") +
      `</div><div class="hl-stage-legend">` + shown.map((s) =>
      `<span class="hl-lg ${s.k}">${s.k} ${_hlHM(s.v)}</span>`).join("") + `</div>`;
  }
  const rows = [_hlErow("asleep", d.asleep_label || "—")];
  if (d.in_bed_min) rows.push(_hlErow("in bed", _hlHM(d.in_bed_min)));
  if (d.sleep_efficiency != null) rows.push(_hlErow("efficiency", `${d.sleep_efficiency}%`));
  box.innerHTML = bar + rows.join("");
}

function _hlErow(k, v, extra) {
  return `<div class="hl-erow ${extra || ""}"><span class="he-k">${escapeHtml(k)}</span>` +
    `<span class="he-v">${v}</span></div>`;
}
function renderHealthVitals(d) {
  const box = $("hl-vitals");
  const rows = [];
  if (d.resting_hr != null) {
    const tag = d.resting_hr_baseline ? ` <span class="he-base">vs ${d.resting_hr_baseline} base</span>` : "";
    rows.push(_hlErow("resting HR", `${(+d.resting_hr)} bpm${tag}`));
  }
  if (d.hrv_ms != null) {
    const tag = d.hrv_baseline ? ` <span class="he-base">vs ${d.hrv_baseline} base</span>` : "";
    rows.push(_hlErow("HRV", `${(+d.hrv_ms)} ms${tag}`));
  }
  if (d.spo2 != null) rows.push(_hlErow("blood oxygen", `${(+d.spo2)}%`));
  if (d.respiratory_rate != null) rows.push(_hlErow("respiratory rate", `${(+d.respiratory_rate)} /min`));
  if (d.skin_temp_delta != null) {
    rows.push(_hlErow("skin temp", `${d.skin_temp_delta > 0 ? "+" : ""}${(+d.skin_temp_delta)}°`));
  }
  box.innerHTML = rows.length ? rows.join("")
    : '<div class="empty">no vitals logged for this day</div>';
}

// Readiness bars over the window, 0–100, coloured by category. Empty days are gaps.
function renderHealthTrend(s) {
  const box = $("hl-trend");
  const series = s.series || [];
  if (!series.some((d) => d.readiness != null)) {
    box.innerHTML = '<div class="empty">log sleep &amp; vitals to see your readiness trend</div>';
    return;
  }
  const sel = _hlDate || _today();
  box.innerHTML = series.map((d) => {
    if (d.readiness == null) return `<span class="hl-bar-wrap" title="${d.date}: —"></span>`;
    const h = Math.max(4, Math.round(d.readiness));
    const cat = d.readiness < 40 ? "low" : (d.readiness < 70 ? "mod" : "high");
    const tip = `${d.date}: readiness ${d.readiness}` + (d.asleep_min ? ` · ${_hlHM(d.asleep_min)} asleep` : "");
    return `<span class="hl-bar-wrap" title="${escapeHtml(tip)}">` +
      `<span class="hl-bar ${cat} ${d.date === sel ? "sel" : ""}" style="height:${h}%"></span></span>`;
  }).join("");
  const label = document.querySelector(".hl-trend-label");
  if (label) label.textContent = s.avg_readiness != null ? `readiness · avg ${s.avg_readiness}` : "readiness";
}

// ---- health: event wiring ----
const _hlForm = document.getElementById("hl-form");
if (_hlForm) _hlForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const day = _hlDate || undefined;
  const num = (id) => { const v = $(id).value.trim(); return v === "" ? null : +v; };
  const payload = { source: "manual" };
  if (day) payload.day = day;
  const sh = num("hl-sleep-h"), bh = num("hl-bed-h");
  if (sh != null) payload.asleep_min = Math.round(sh * 60);
  if (bh != null) payload.in_bed_min = Math.round(bh * 60);
  const rhr = num("hl-rhr"), hrv = num("hl-hrv"), spo2 = num("hl-spo2"), resp = num("hl-resp");
  if (rhr != null) payload.resting_hr = rhr;
  if (hrv != null) payload.hrv_ms = hrv;
  if (spo2 != null) payload.spo2 = spo2;
  if (resp != null) payload.respiratory_rate = resp;
  // nothing but the source/day tags → don't POST an empty day
  const metrics = Object.keys(payload).filter((k) => k !== "source" && k !== "day");
  if (!metrics.length) return;
  const btn = $("hl-save-btn");
  try {
    await jfetch("/api/health", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload) });
    if (btn) {
      btn.classList.add("saved"); btn.textContent = "✓ Saved";
      setTimeout(() => { btn.classList.remove("saved"); btn.textContent = "＋ Save for this day"; }, 1400);
    }
    await loadHealth();
  } catch (e2) {
    toast("couldn't save health entry: " + (e2.message || e2), "err");
  }
});

function _hlShiftDay(delta) {
  const base = new Date((_hlDate || _today()) + "T00:00");
  base.setDate(base.getDate() + delta);
  const d = _ymd(base);
  if (d > _today()) return;                 // no future days
  _hlDate = (d === _today()) ? null : d;
  loadHealth();
}
const _hlPrev = document.getElementById("hl-prev");
if (_hlPrev) _hlPrev.addEventListener("click", () => _hlShiftDay(-1));
const _hlNext = document.getElementById("hl-next");
if (_hlNext) _hlNext.addEventListener("click", () => _hlShiftDay(1));
const _hlTodayBtn = document.getElementById("hl-today");
if (_hlTodayBtn) _hlTodayBtn.addEventListener("click", () => { _hlDate = null; loadHealth(); });
const _hlRange = document.getElementById("hl-range");
if (_hlRange) _hlRange.addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  _hlRange.querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
  _hlDays = +b.dataset.days || 30;
  loadHealth();
});

// ===================== FLASHCARDS =====================
// Swedish vocab on SM-2, ported from the Glosor phone app. Non-AI: this talks
// to /api/flashcards/* directly and no model is in the study loop — the two
// places a model appears are the import box (turning a pasted word list into
// cards) and text-to-speech, both optional and both behind their own key.
//
// Two things shape the whole block. A card has two faces and studies in either
// direction, so direction is a session setting rather than a property of the
// row. And question TYPE is multi-select with one picked at random per card:
// seeing "bok" as a flip, then as dictation, then as en/ett is what makes it
// stick, so a card that can't support a type is skipped for that type rather
// than shown broken.

let _fcBucket = "";              // "source\u001fset"; "" is everything
let _fcTag = "";
let _fcDir = "sv-en";            // sv-en | en-sv | mixed
let _fcCount = 20;
let _fcSession = null;
let _fcStatus = { import: false, audio: false, voices: [], missing_audio: 0 };
let _fcVoice = "";               // sticky across cards, so a session has one narrator
let _fcDrafts = null;            // built-but-unsaved import cards
const _fcModes = new Set(["flip", "write", "choice", "match", "listen", "gender", "cloze", "forms"]);
const _FC_MATCH_BATCH = 5;
const _fcPlayer = new Audio();

// SM-2's four answers. The numbers are the API contract, not labels: 0 is a
// lapse (resets reps, back in ten minutes), and 3/4/5 all pass but schedule
// apart. Collapsing them to right/wrong throws away what the algorithm runs on.
const _FC_GRADES = [
  { q: 0, label: "Again", sub: "&lt;10m", cls: "again" },
  { q: 3, label: "Hard", sub: "", cls: "" },
  { q: 4, label: "Good", sub: "", cls: "good" },
  { q: 5, label: "Easy", sub: "", cls: "" },
];

// Inflection keys the importer produces, with labels that explain the grammar
// in English rather than naming it — "definite plural (the …s)" teaches;
// "def_pl" does not.
const _FC_FORM_LABELS = {
  indef_sg: "indefinite (a/an …)", def_sg: "definite (the …)",
  indef_pl: "plural (…s)", def_pl: "definite plural (the …s)",
  gen_sg: "possessive (…'s)", gen_pl: "plural possessive (…s')",
  present: "present tense", past: "past tense (did)", supine: "supine (have …)",
  imperative: "imperative (do it!)", infinitive: "infinitive (to …)",
  en_form: "en-form", ett_form: "ett-form", plural_form: "plural form",
  comparative: "comparative (more …)", superlative: "superlative (most …)",
};

// A unit separator, written as an escape rather than the raw byte: it is
// invisible in an editor and one stray copy-paste would silently change the
// key. Not a space or a slash, because both occur in real source and set
// names, and "Riv"+"start" must never key the same as "Rivstart"+"".
const _FC_SEP = "\u001f";
const _fcKey = (b) => `${b.source || ""}${_FC_SEP}${b.set || ""}`;

function _fcQuery(extra) {
  const [source, set] = _fcBucket ? _fcBucket.split(_FC_SEP) : ["", ""];
  const p = new URLSearchParams();
  if (source) p.set("source", source);
  if (set) p.set("set", set);
  if (_fcTag) p.set("tag", _fcTag);
  for (const [k, v] of Object.entries(extra || {})) p.set(k, v);
  const s = p.toString();
  return s ? `?${s}` : "";
}

// ---- loading + the manage side ----
async function loadFlashcards() {
  const [d, st] = await Promise.all([
    jread("/api/flashcards" + _fcQuery({ limit: 500 })),
    jread("/api/flashcards/status"),
  ]);
  if (!d) return loadFail("flashcards", "your flashcards", "loadFlashcards");
  loadOk("loadFlashcards");
  if (st) _fcStatus = st;
  _fcRenderBuckets(d.buckets || [], d.tags || []);
  _fcRenderStats(d.stats || {});
  _fcRenderList(d.cards || []);
  _fcRenderTools();
}

function _fcRenderBuckets(buckets, tags) {
  const sel = $("fc-bucket");
  const total = buckets.reduce((n, b) => n + b.total, 0);
  const due = buckets.reduce((n, b) => n + b.due, 0);
  sel.innerHTML = [`<option value="">all cards (${total})${due ? ` · ${due} due` : ""}</option>`]
    .concat(buckets.map((b) => {
      const name = [b.source, b.set].filter(Boolean).join(" · ") || "unfiled";
      return `<option value="${escapeHtml(_fcKey(b))}">${escapeHtml(name)} (${b.total})${b.due ? ` · ${b.due} due` : ""}</option>`;
    })).join("");
  // A reload must not silently widen what Jake picked. If the bucket vanished
  // (its last card was deleted) fall back to everything, but only then.
  sel.value = buckets.some((b) => _fcKey(b) === _fcBucket) ? _fcBucket : "";
  _fcBucket = sel.value;

  const tsel = $("fc-tag");
  tsel.innerHTML = [`<option value="">any word type</option>`]
    .concat(tags.map((t) => `<option value="${escapeHtml(t)}">${escapeHtml(t)}</option>`)).join("");
  tsel.value = tags.includes(_fcTag) ? _fcTag : "";
  _fcTag = tsel.value;
}

function _fcRenderStats(s) {
  const bits = [
    `<span class="fc-chip due">${s.due || 0} due</span>`,
    `<span class="fc-chip">${s.total || 0} cards</span>`,
  ];
  if (s.new) bits.push(`<span class="fc-chip new">${s.new} new</span>`);
  if (s.learned) bits.push(`<span class="fc-chip good">${s.learned} learned</span>`);
  if (s.reviewed_24h) bits.push(`<span class="fc-chip">${s.reviewed_24h} reviewed today</span>`);
  $("fc-stats").innerHTML = bits.join("");
  $("fc-sub").textContent = s.total ? "" : "nothing here yet";
}

function _fcRenderList(cards) {
  $("fc-list-count").textContent = cards.length ? `(${cards.length})` : "";
  if (!cards.length) {
    $("fc-list").innerHTML = `<div class="empty">no cards here — add one above, or import a list…</div>`;
    return;
  }
  $("fc-list").innerHTML = cards.map((c) => {
    const g = c.gender && c.pos === "noun" ? `<span class="fc-g ${escapeHtml(c.gender)}">${escapeHtml(c.gender)}</span>` : "";
    const tags = (c.tags || []).map((t) => `<span class="fc-tag">${escapeHtml(t)}</span>`).join("");
    const bucket = c.bucket ? `<span class="fc-row-bucket">${escapeHtml(c.bucket)}</span>` : "";
    const audio = Object.keys(c.audio || {}).length ? `<span class="fc-row-audio" title="has audio">🔊</span>` : "";
    return `<div class="fc-row${c.due_now ? " due" : ""}">
      <div class="fc-row-main">
        <div class="fc-row-sv">${escapeHtml(c.swedish)}${g}${audio}</div>
        <div class="fc-row-en">${escapeHtml(c.english)}</div>
        <div class="fc-row-meta">${bucket}${tags}${c.notes ? `<span class="fc-note">${escapeHtml(c.notes)}</span>` : ""}</div>
      </div>
      <button type="button" class="fc-del" data-del="${escapeHtml(c.id)}" title="delete this card">✕</button>
    </div>`;
  }).join("");
}

// Import and audio are the only parts that need a key. With none configured
// they hide rather than fail on click — the rest of the tab studies fine.
function _fcRenderTools() {
  $("fc-imp").hidden = !_fcStatus.import;
  const btn = $("fc-voice-btn");
  const missing = _fcStatus.missing_audio || 0;
  btn.hidden = !_fcStatus.audio || !missing;
  btn.textContent = `🔊 generate audio (${missing} missing)`;
}

// ---- session plumbing ----
const _fcShuffle = (a) => a.map((v) => [Math.random(), v]).sort((x, y) => x[0] - y[0]).map((p) => p[1]);
const _fcHasAudio = (c) => Object.keys(c.audio || {}).length > 0;

// Which forms are worth a quiz: present, labelled, and actually different from
// the lemma — asking for the infinitive of "prata" when the card says "prata"
// is not a question.
function _fcFormKeys(c) {
  return Object.entries(c.forms || {})
    .filter(([k, v]) => v && _FC_FORM_LABELS[k] && _fcNorm(v) !== _fcNorm(c.swedish))
    .map(([k]) => k);
}

// Blank the sentence at whichever token IS the word — matching against every
// inflected form, since an example sentence rarely uses the dictionary form.
function _fcCloze(c) {
  if (!c.example_sv) return null;
  const want = new Set([_fcNorm(c.swedish), ...Object.values(c.forms || {}).filter(Boolean).map(_fcNorm)]);
  const parts = c.example_sv.split(/(\s+)/);
  for (let i = 0; i < parts.length; i++) {
    const bare = parts[i].replace(/^[^\p{L}]+|[^\p{L}]+$/gu, "");
    if (bare && want.has(_fcNorm(bare))) {
      const before = parts.slice(0, i).join("");
      const after = parts.slice(i + 1).join("");
      return { text: before + parts[i].replace(bare, "____") + after, answer: bare };
    }
  }
  return null;
}

function _fcModeValid(mode, card, pool) {
  switch (mode) {
    case "listen": return _fcHasAudio(card);
    case "gender": return card.pos === "noun" && (card.gender === "en" || card.gender === "ett");
    case "cloze": return !!_fcCloze(card);
    case "forms": return _fcFormKeys(card).length > 0;
    case "choice": return pool.filter((c) => c.english).length >= 4;
    default: return true;
  }
}

// Which face is the question. Direction is a session setting because a card
// stores two faces and no opinion about which is the front.
function _fcAsk(card) {
  const dir = _fcDir === "mixed" ? (Math.random() < 0.5 ? "sv-en" : "en-sv") : _fcDir;
  return dir === "sv-en"
    ? { prompt: card.swedish, answer: card.english, label: "Swedish", svIsPrompt: true }
    : { prompt: card.english, answer: card.swedish, label: "English", svIsPrompt: false };
}

// Glosor's normaliser: what counts as the same answer typed by a human. Strips
// the article and the infinitive marker, because "en bok" and "bok" are the
// same word and the card can't know which one Jake will type.
function _fcNorm(s) {
  return String(s || "").toLowerCase().trim()
    .replace(/^att\s+/, "").replace(/^(en|ett)\s+/, "")
    .replace(/[.!?,;:]+$/, "").replace(/\s+/g, " ");
}

// An answer may legitimately have alternatives ("to talk / to speak").
const _fcAccepts = (typed, answer) =>
  String(answer || "").split(/[/,;]/).map(_fcNorm).filter(Boolean).includes(_fcNorm(typed));

async function _fcStart() {
  if (!_fcModes.size) return toast("pick at least one question type");
  const d = await jread("/api/flashcards/study" + _fcQuery({
    limit: _fcCount, due_only: $("fc-dueonly").checked ? "true" : "false",
  }));
  if (!d) return toast("couldn't build a session — is Continuum still running?");
  const cards = d.cards || [];
  if (!cards.length) {
    return toast($("fc-dueonly").checked
      ? "nothing due right now — untick 'only due' to practise anyway"
      : "no cards match those filters", "ok");
  }
  _fcSession = {
    queue: _fcShuffle(cards.slice()), pool: cards, done: 0, failedWrites: 0,
    modes: [..._fcModes],
  };
  $("fc-launch").hidden = true;
  $("fc-session").hidden = false;
  $("fc-quit").hidden = false;
  _fcNext();
}

function _fcEndSession() {
  _fcSession = null;
  $("fc-session").hidden = true;
  $("fc-launch").hidden = false;
  $("fc-stage").innerHTML = "";
  loadFlashcards();
}

function _fcProgress() {
  const s = _fcSession;
  if (!s) return;
  // Denominator is done + what's left, not the planned count: a lapsed card is
  // pushed back into the queue, so a fixed total would run past 100%.
  const total = Math.max(1, s.done + s.queue.length);
  $("fc-prog-bar").style.width = `${Math.round((s.done / total) * 100)}%`;
  $("fc-prog-label").textContent = `${s.done} / ${total}`;
}

function _fcNext() {
  const s = _fcSession;
  if (!s) return;
  _fcProgress();
  if (!s.queue.length) return _fcFinish();

  const card = s.queue.shift();
  const usable = s.modes.filter((m) => m !== "match" && _fcModeValid(m, card, s.pool));

  // Matching needs a batch, so it's decided here rather than per card. If there
  // aren't enough partners left, the extras go back and the card takes a
  // different type — never a three-tile matching round that grades on nothing.
  if (s.modes.includes("match") && (!usable.length || Math.random() < 1 / (usable.length + 1))) {
    const batch = [card];
    while (batch.length < _FC_MATCH_BATCH && s.queue.length) {
      const i = s.queue.findIndex((c) => c.english && !batch.some((b) => b.id === c.id));
      if (i < 0) break;
      batch.push(s.queue.splice(i, 1)[0]);
    }
    if (batch.length >= 3) return _fcRenderMatch(batch);
    s.queue.unshift(...batch.slice(1));
  }
  const mode = usable.length ? usable[Math.floor(Math.random() * usable.length)] : "flip";
  ({
    flip: _fcRenderFlip, write: _fcRenderWrite, choice: _fcRenderChoice,
    listen: _fcRenderListen, gender: _fcRenderGender, cloze: _fcRenderCloze,
    forms: _fcRenderForms,
  }[mode] || _fcRenderFlip)(card);
}

function _fcGrade(card, quality, mode) {
  const s = _fcSession;
  if (!s) return;
  s.done += 1;
  if (quality < 3) {
    // Back into the queue a few places on, so it comes round again before the
    // session ends rather than only tomorrow.
    s.queue.splice(Math.min(s.queue.length, 3 + Math.floor(Math.random() * 3)), 0, card);
  }
  // Not awaited: the next card should appear on the tap. jwrite still toasts on
  // failure, and the count is repeated at the finish screen — a session whose
  // reviews all silently failed to save must not look like a session that went
  // fine.
  jwrite("save that review", `/api/flashcards/${encodeURIComponent(card.id)}/review`,
    jbody("POST", { quality, mode })).then((r) => { if (!r.ok && _fcSession === s) s.failedWrites += 1; });
  _fcNext();
}

function _fcFinish() {
  const s = _fcSession;
  const warn = s && s.failedWrites
    ? `<div class="fc-done-warn">${s.failedWrites} review(s) couldn't be saved — those cards keep their old schedule.</div>`
    : "";
  $("fc-quit").hidden = true;
  $("fc-stage").innerHTML = `<div class="fc-done">
    <div class="fc-done-icon">🎉</div>
    <div class="fc-done-n">${s ? s.done : 0} answered</div>
    <div class="fc-done-sub">nice work.</div>${warn}
    <button type="button" class="fc-start" id="fc-done-back">back</button>
  </div>`;
  $("fc-done-back").addEventListener("click", _fcEndSession);
  _fcSession = null;
  _fcProgress();
}

// ---- audio ----
// One pill per speakable form, because the store knows every form worth hearing
// and the phone app could only ever play the first one. The voice toggle is
// sticky across cards: swapping narrator mid-session is disorienting.
function _fcAudioHtml(card) {
  const forms = Object.keys(card.audio || {});
  if (!forms.length) return "";
  const voices = [...new Set(forms.flatMap((f) => Object.keys(card.audio[f] || {})))];
  const swap = voices.length > 1
    ? `<button type="button" class="fc-a-swap" data-swapvoice="1" title="switch voice">⇄ voice</button>` : "";
  return `<div class="fc-audio">` + forms.map((f) =>
    `<button type="button" class="fc-a" data-play="${escapeHtml(f)}">🔊 ${escapeHtml(f)}</button>`
  ).join("") + swap + `</div>`;
}

function _fcPlay(card, form) {
  const byVoice = (card.audio || {})[form] || {};
  const voices = Object.keys(byVoice);
  if (!voices.length) return;
  const v = voices.includes(_fcVoice) ? _fcVoice : voices[0];
  // encode each segment but keep the separator: stored names are "<id>/<form>.mp3"
  // and the route matches the slash on purpose.
  _fcPlayer.src = "/api/flashcards/audio/" + String(byVoice[v]).split("/").map(encodeURIComponent).join("/");
  _fcPlayer.play().catch(() => { /* autoplay blocked until a tap; the button still works */ });
}

// Wires the audio pills inside whatever was just rendered, and optionally plays
// one form straight away.
function _fcBindAudio(card, autoplay, mode) {
  const stage = $("fc-stage");
  stage.querySelectorAll("[data-play]").forEach((b) => {
    b.addEventListener("click", () => _fcPlay(card, b.dataset.play));
  });
  const swap = stage.querySelector("[data-swapvoice]");
  if (swap) {
    swap.addEventListener("click", () => {
      const forms = Object.keys(card.audio || {});
      const voices = [...new Set(forms.flatMap((f) => Object.keys(card.audio[f] || {})))];
      const i = voices.indexOf(voices.includes(_fcVoice) ? _fcVoice : voices[0]);
      _fcVoice = voices[(i + 1) % voices.length];
      toast(`voice: ${_fcVoice}`, "ok", 1200);
      if (forms.length) _fcPlay(card, forms[0]);
    });
  }
  // The checkbox is read here rather than captured at session start, so
  // unticking it silences the rest of the session immediately — which is the
  // situation it exists for: a phone that has just started talking in public.
  // Listening mode ignores it, because a dictation question with no sound is
  // not a harder question, it is an unanswerable one.
  const wanted = autoplay && (mode === "listen" || !$("fc-autoplay") || $("fc-autoplay").checked);
  if (wanted && Object.keys(card.audio || {}).length) {
    setTimeout(() => _fcPlay(card, Object.keys(card.audio)[0]), 250);
  }
}

// ---- shared question chrome ----
const _fcGenderClass = (c) => (c.pos === "noun" && c.gender ? ` ${c.gender}` : "");
const _fcGenderPill = (c) =>
  (c.pos === "noun" && c.gender ? `<span class="fc-g ${escapeHtml(c.gender)}">${escapeHtml(c.gender)}</span>` : "");

function _fcGradeHtml() {
  return `<div class="fc-grades">` + _FC_GRADES.map((g) =>
    `<button type="button" class="fc-grade ${g.cls}" data-q="${g.q}">${g.label}${g.sub ? `<span>${g.sub}</span>` : ""}</button>`
  ).join("") + `</div>`;
}

function _fcBindGrades(card, mode) {
  $("fc-stage").querySelectorAll("[data-q]").forEach((b) => {
    b.addEventListener("click", () => _fcGrade(card, Number(b.dataset.q), mode), { once: true });
  });
}

// The full paradigm, shown on reveal. The phone app stored these and never
// displayed them anywhere — a table at the moment of recall is the cheapest
// teaching this data can do.
function _fcFormsTable(card) {
  const keys = Object.entries(card.forms || {}).filter(([k, v]) => v && _FC_FORM_LABELS[k]);
  if (!keys.length) return "";
  return `<div class="fc-forms">` + keys.map(([k, v]) =>
    `<div class="fc-form"><span>${escapeHtml(_FC_FORM_LABELS[k])}</span><b>${escapeHtml(v)}</b></div>`
  ).join("") + `</div>`;
}

function _fcRevealHtml(card, answer) {
  const ex = card.example_sv
    ? `<div class="fc-ex">${escapeHtml(card.example_sv)}${card.example_en ? `<br><i>${escapeHtml(card.example_en)}</i>` : ""}</div>` : "";
  const notes = card.notes ? `<div class="fc-note-big">${escapeHtml(card.notes)}</div>` : "";
  return `<div class="fc-reveal"><div class="fc-answer">${escapeHtml(answer)}</div>${ex}${notes}${_fcFormsTable(card)}</div>`;
}

// ---- flip ----
function _fcRenderFlip(card) {
  const a = _fcAsk(card);
  const pill = a.svIsPrompt ? _fcGenderPill(card) : "";
  $("fc-stage").innerHTML = `
    <div class="fc-card${a.svIsPrompt ? _fcGenderClass(card) : ""}">
      <div class="fc-plabel">${a.label}</div>
      <div class="fc-word">${escapeHtml(a.prompt)}${pill}</div>
      ${_fcAudioHtml(card)}
    </div>
    <div class="fc-answers"><button type="button" class="fc-show" id="fc-show">show answer</button></div>`;
  _fcBindAudio(card, true, "flip");
  $("fc-show").addEventListener("click", () => {
    $("fc-stage").querySelector(".fc-card").insertAdjacentHTML("beforeend", _fcRevealHtml(card, a.answer));
    $("fc-stage").querySelector(".fc-answers").innerHTML = _fcGradeHtml();
    _fcBindGrades(card, "flip");
  }, { once: true });
}

// ---- write (type the answer) ----
function _fcRenderWrite(card) {
  const a = _fcAsk(card);
  $("fc-stage").innerHTML = `
    <div class="fc-card">
      <div class="fc-plabel">type the ${a.svIsPrompt ? "English" : "Swedish"}</div>
      <div class="fc-word sm">${escapeHtml(a.prompt)}${a.svIsPrompt ? _fcGenderPill(card) : ""}</div>
    </div>
    <div class="fc-answers">
      <input id="fc-in" class="fc-input" autocapitalize="none" autocomplete="off" autocorrect="off"
             spellcheck="false" placeholder="${a.svIsPrompt ? "in English…" : "på svenska…"}" />
      <button type="button" class="fc-show" id="fc-check">check</button>
    </div>`;
  _fcTypedAnswer(card, "write", a.answer, () => _fcAudioHtml(card));
}

// ---- listening (dictation) ----
function _fcRenderListen(card) {
  $("fc-stage").innerHTML = `
    <div class="fc-card">
      <div class="fc-plabel">listen &amp; type what you hear</div>
      ${_fcAudioHtml(card)}
    </div>
    <div class="fc-answers">
      <input id="fc-in" class="fc-input" autocapitalize="none" autocomplete="off" autocorrect="off"
             spellcheck="false" placeholder="på svenska…" />
      <button type="button" class="fc-show" id="fc-check">check</button>
    </div>`;
  _fcBindAudio(card, true, "listen");
  _fcTypedAnswer(card, "listen", card.swedish, () => "");
}

// ---- fill the blank ----
function _fcRenderCloze(card) {
  const cz = _fcCloze(card);
  $("fc-stage").innerHTML = `
    <div class="fc-card">
      <div class="fc-plabel">fill the blank</div>
      <div class="fc-sentence">${escapeHtml(cz.text)}</div>
      ${card.example_en ? `<div class="fc-ex"><i>${escapeHtml(card.example_en)}</i></div>` : ""}
    </div>
    <div class="fc-answers">
      <input id="fc-in" class="fc-input" autocapitalize="none" autocomplete="off" autocorrect="off"
             spellcheck="false" placeholder="missing word…" />
      <button type="button" class="fc-show" id="fc-check">check</button>
    </div>`;
  _fcTypedAnswer(card, "cloze", cz.answer, () => _fcAudioHtml(card));
}

// ---- inflection production ----
function _fcRenderForms(card) {
  const keys = _fcFormKeys(card);
  const key = keys[Math.floor(Math.random() * keys.length)];
  $("fc-stage").innerHTML = `
    <div class="fc-card">
      <div class="fc-plabel">${escapeHtml(_FC_FORM_LABELS[key])}</div>
      <div class="fc-word sm">${escapeHtml(card.swedish)}${_fcGenderPill(card)}</div>
      <div class="fc-sub2">${escapeHtml(card.english)}</div>
    </div>
    <div class="fc-answers">
      <input id="fc-in" class="fc-input" autocapitalize="none" autocomplete="off" autocorrect="off"
             spellcheck="false" placeholder="form…" />
      <button type="button" class="fc-show" id="fc-check">check</button>
    </div>`;
  _fcTypedAnswer(card, "forms", card.forms[key], () => _fcAudioHtml(card));
}

// The four typed modes differ only in prompt and expected answer, so the check,
// the reveal and the grade row live here once. Jake still self-grades even
// though the app knows whether he was right: "right but I had to think" and
// "right instantly" schedule differently, and only he knows which it was.
function _fcTypedAnswer(card, mode, answer, extraHtml) {
  const input = $("fc-in");
  const go = () => {
    const ok = _fcAccepts(input.value, answer);
    input.disabled = true;
    const box = $("fc-stage").querySelector(".fc-card");
    box.insertAdjacentHTML("beforeend",
      `<div class="fc-verdict ${ok ? "ok" : "no"}">${ok ? "✓ correct" : `✗ ${escapeHtml(answer)}`}</div>`
      + _fcRevealHtml(card, mode === "write" ? answer : card.english) + extraHtml());
    $("fc-stage").querySelector(".fc-answers").innerHTML = _fcGradeHtml();
    _fcBindAudio(card, false, mode);
    _fcBindGrades(card, mode);
    // Pre-pick the honest answer so a correct one is one more tap, not two.
    const suggest = $("fc-stage").querySelector(`[data-q="${ok ? 4 : 0}"]`);
    if (suggest) suggest.classList.add("suggest");
  };
  $("fc-check").addEventListener("click", go, { once: true });
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") $("fc-check").click(); });
  setTimeout(() => input.focus(), 80);
}

// ---- multiple choice ----
function _fcRenderChoice(card) {
  const a = _fcAsk(card);
  const others = _fcSession.pool.filter((c) => c.id !== card.id && (a.svIsPrompt ? c.english : c.swedish));
  const distractors = _fcShuffle(others).slice(0, 3).map((c) => (a.svIsPrompt ? c.english : c.swedish));
  const options = _fcShuffle([a.answer, ...distractors]);
  $("fc-stage").innerHTML = `
    <div class="fc-card${a.svIsPrompt ? _fcGenderClass(card) : ""}">
      <div class="fc-plabel">what does this mean?</div>
      <div class="fc-word">${escapeHtml(a.prompt)}${a.svIsPrompt ? _fcGenderPill(card) : ""}</div>
      ${a.svIsPrompt ? _fcAudioHtml(card) : ""}
    </div>
    <div class="fc-answers">` + options.map((o) =>
      `<button type="button" class="fc-choice" data-opt="${escapeHtml(o)}">${escapeHtml(o)}</button>`).join("")
    + `</div>`;
  _fcBindAudio(card, a.svIsPrompt, "choice");
  $("fc-stage").querySelectorAll("[data-opt]").forEach((b) => {
    b.addEventListener("click", () => {
      const ok = b.dataset.opt === a.answer;
      $("fc-stage").querySelectorAll("[data-opt]").forEach((x) => {
        x.disabled = true;
        if (x.dataset.opt === a.answer) x.classList.add("correct");
      });
      if (!ok) b.classList.add("wrong");
      setTimeout(() => _fcGrade(card, ok ? 4 : 0, "choice"), 700);
    }, { once: true });
  });
}

// ---- en / ett ----
// No audio here, before or after: every spoken form starts with the article, so
// a play button would read the answer out loud.
function _fcRenderGender(card) {
  $("fc-stage").innerHTML = `
    <div class="fc-card" id="fc-gcard">
      <div class="fc-plabel">en or ett?</div>
      <div class="fc-word">${escapeHtml(card.swedish)}</div>
      <div class="fc-sub2">${escapeHtml(card.english)}</div>
    </div>
    <div class="fc-answers"><div class="fc-grades two">
      <button type="button" class="fc-grade" data-gen="en">en</button>
      <button type="button" class="fc-grade" data-gen="ett">ett</button>
    </div></div>`;
  $("fc-stage").querySelectorAll("[data-gen]").forEach((b) => {
    b.addEventListener("click", () => {
      const ok = b.dataset.gen === card.gender;
      $("fc-stage").querySelectorAll("[data-gen]").forEach((x) => {
        x.disabled = true;
        if (x.dataset.gen === card.gender) x.classList.add("correct");
      });
      if (!ok) b.classList.add("wrong");
      $("fc-gcard").classList.add(card.gender);
      setTimeout(() => _fcGrade(card, ok ? 4 : 0, "gender"), 650);
    }, { once: true });
  });
}

// ---- matching ----
// Grades every card in the batch at once: 4 if it was never part of a wrong
// tap, 0 if it was. A card you eventually paired by elimination is not a card
// you knew.
function _fcRenderMatch(batch) {
  const sv = _fcShuffle(batch.slice());
  const en = _fcShuffle(batch.slice());
  const errored = new Set();
  let sel = null;
  // No side class in here: it would be "sv" or "en", and "en" is also a gender,
  // so every English tile picked up .fc-tile.en and rendered with the en-word
  // border — an English word coloured as if it had Swedish grammatical gender.
  // Nothing read the class anyway; the handler goes by data-side.
  const tile = (c, side, text) =>
    `<button type="button" class="fc-tile${side === "sv" ? _fcGenderClass(c) : ""}" data-id="${escapeHtml(c.id)}" data-side="${side}">${escapeHtml(text)}</button>`;
  $("fc-stage").innerHTML = `
    <div class="fc-plabel center">match the pairs</div>
    <div class="fc-match">
      <div class="fc-col">${sv.map((c) => tile(c, "sv", c.swedish)).join("")}</div>
      <div class="fc-col">${en.map((c) => tile(c, "en", c.english)).join("")}</div>
    </div>`;

  const tiles = [...$("fc-stage").querySelectorAll(".fc-tile")];
  tiles.forEach((t) => t.addEventListener("click", () => {
    if (t.classList.contains("locked")) return;
    if (sel === t) { t.classList.remove("sel"); sel = null; return; }
    if (!sel || sel.dataset.side === t.dataset.side) {
      if (sel) sel.classList.remove("sel");
      sel = t; t.classList.add("sel"); return;
    }
    const a = sel, b = t;
    sel = null; a.classList.remove("sel");
    if (a.dataset.id === b.dataset.id) {
      [a, b].forEach((x) => { x.classList.add("locked"); x.classList.remove("sel"); });
      if (tiles.every((x) => x.classList.contains("locked"))) {
        setTimeout(() => {
          batch.forEach((c) => _fcGradeQuiet(c, errored.has(c.id) ? 0 : 4));
          _fcNext();
        }, 400);
      }
    } else {
      errored.add(a.dataset.id); errored.add(b.dataset.id);
      [a, b].forEach((x) => x.classList.add("wrong"));
      setTimeout(() => [a, b].forEach((x) => x.classList.remove("wrong")), 450);
    }
  }));
}

// A batch grade: same write as _fcGrade but without advancing, since one
// matching round settles several cards at once.
function _fcGradeQuiet(card, quality) {
  const s = _fcSession;
  if (!s) return;
  s.done += 1;
  if (quality < 3) s.queue.splice(Math.min(s.queue.length, 3 + Math.floor(Math.random() * 3)), 0, card);
  jwrite("save that review", `/api/flashcards/${encodeURIComponent(card.id)}/review`,
    jbody("POST", { quality, mode: "match" })).then((r) => { if (!r.ok && _fcSession === s) s.failedWrites += 1; });
}

// ---- add one card by hand ----
async function _fcAdd() {
  const sv = $("fc-add-sv").value.trim();
  const en = $("fc-add-en").value.trim();
  if (!sv || !en) { $("fc-add-msg").textContent = "needs both the Swedish and the English"; return; }
  const r = await jwrite("add that card", "/api/flashcards", jbody("POST", {
    swedish: sv, english: en, notes: $("fc-add-notes").value.trim(),
    source: $("fc-add-source").value.trim(), set: $("fc-add-set").value.trim(),
  }));
  if (!r.ok) return;
  // The bucket inputs stay filled: adding five words from one chapter shouldn't
  // mean retyping the chapter five times.
  for (const id of ["fc-add-sv", "fc-add-en", "fc-add-notes"]) $(id).value = "";
  $("fc-add-msg").textContent = `added ${sv}`;
  $("fc-add-sv").focus();
  loadFlashcards();
}

// ---- import ----
// Two steps on purpose. A mis-gendered noun is far cheaper to fix as a draft
// than after it has become forty rows with audio attached, so nothing is stored
// until Jake has looked at it.
async function _fcImport() {
  const text = $("fc-imp-text").value.trim();
  if (!text) { $("fc-imp-msg").textContent = "paste some words first"; return; }
  $("fc-imp-btn").disabled = true;
  $("fc-imp-msg").textContent = "building cards… (this calls a model, give it a moment)";
  $("fc-imp-preview").hidden = true;
  try {
    const d = await jfetch("/api/flashcards/import", jbody("POST", { text }));
    _fcDrafts = d.cards || [];
    $("fc-imp-msg").textContent = _fcDrafts.length
      ? `${_fcDrafts.length} card(s) ready — check them, drop any that are wrong, then save`
      : "the model didn't return any cards";
    _fcRenderDrafts();
  } catch (e) {
    $("fc-imp-msg").textContent = `import failed: ${e.message}`;
  } finally {
    $("fc-imp-btn").disabled = false;
  }
}

function _fcRenderDrafts() {
  const box = $("fc-imp-preview");
  if (!_fcDrafts || !_fcDrafts.length) { box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = _fcDrafts.map((c, i) => {
    const g = c.gender && c.pos === "noun" ? `<span class="fc-g ${escapeHtml(c.gender)}">${escapeHtml(c.gender)}</span>` : "";
    return `<div class="fc-draft">
      <div class="fc-row-main">
        <div class="fc-row-sv">${escapeHtml(c.swedish || "")}${g}</div>
        <div class="fc-row-en">${escapeHtml(c.english || "")}</div>
        ${c.example_sv ? `<div class="fc-row-meta"><span class="fc-note">${escapeHtml(c.example_sv)}</span></div>` : ""}
      </div>
      <button type="button" class="fc-del" data-drop="${i}" title="don't import this one">✕</button>
    </div>`;
  }).join("") + `<button type="button" class="fc-start" id="fc-imp-save">save ${_fcDrafts.length} card(s)</button>`;
}

async function _fcImportSave() {
  if (!_fcDrafts || !_fcDrafts.length) return;
  const r = await jwrite("save those cards", "/api/flashcards/import/save", jbody("POST", {
    cards: _fcDrafts,
    source: $("fc-imp-source").value.trim(), set: $("fc-imp-set").value.trim(),
  }));
  if (!r.ok) return;
  const d = r.data || {};
  toast(`saved ${d.added || 0} new card(s)${d.updated ? `, updated ${d.updated}` : ""}`, "ok");
  _fcDrafts = null;
  $("fc-imp-text").value = "";
  $("fc-imp-preview").hidden = true;
  $("fc-imp-msg").textContent = "";
  loadFlashcards();
}

// ---- generate missing audio ----
// Capped server-side per call; the response says what's left so this can offer
// another batch rather than looking finished when it isn't.
async function _fcGenerateAudio() {
  const btn = $("fc-voice-btn");
  btn.disabled = true;
  btn.textContent = "🔊 generating…";
  const r = await jwrite("generate audio", "/api/flashcards/voice", jbody("POST", { limit: 40 }));
  btn.disabled = false;
  if (r.ok) {
    const d = r.data || {};
    const left = d.remaining_calls || 0;
    toast(`${d.synthesised || 0} clip(s) generated${left ? ` — ${left} still missing, tap again` : ""}`
      + ((d.errors || []).length ? `; ${d.errors.length} failed` : ""), "ok");
  }
  loadFlashcards();
}

// ---- wiring ----
// All at module top level, guarded, and reading nothing that isn't in the DOM
// at parse time — same as every other tab.
if ($("fc-bucket")) {
  $("fc-bucket").addEventListener("change", (e) => { _fcBucket = e.target.value; loadFlashcards(); });
}
if ($("fc-tag")) {
  $("fc-tag").addEventListener("change", (e) => { _fcTag = e.target.value; loadFlashcards(); });
}
if ($("fc-dir")) {
  $("fc-dir").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-v]");
    if (!b) return;
    _fcDir = b.dataset.v;
    $("fc-dir").querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
  });
}
if ($("fc-count")) {
  $("fc-count").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-v]");
    if (!b) return;
    _fcCount = Number(b.dataset.v);
    $("fc-count").querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
  });
}
if ($("fc-mode")) {
  $("fc-mode").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-v]");
    if (!b) return;
    // Never let the last one go off: an empty set can only produce an error at
    // the point Jake is trying to start, which is the wrong place to learn it.
    if (_fcModes.has(b.dataset.v) && _fcModes.size === 1) return toast("keep at least one question type", "ok", 1400);
    if (_fcModes.has(b.dataset.v)) _fcModes.delete(b.dataset.v); else _fcModes.add(b.dataset.v);
    b.classList.toggle("on", _fcModes.has(b.dataset.v));
  });
}
if ($("fc-start")) $("fc-start").addEventListener("click", _fcStart);
if ($("fc-quit")) $("fc-quit").addEventListener("click", _fcEndSession);
if ($("fc-add-btn")) $("fc-add-btn").addEventListener("click", _fcAdd);
if ($("fc-add-en")) {
  $("fc-add-en").addEventListener("keydown", (e) => { if (e.key === "Enter") _fcAdd(); });
}
if ($("fc-imp-btn")) $("fc-imp-btn").addEventListener("click", _fcImport);
if ($("fc-voice-btn")) $("fc-voice-btn").addEventListener("click", _fcGenerateAudio);
if ($("fc-imp-preview")) {
  $("fc-imp-preview").addEventListener("click", (e) => {
    if (e.target.id === "fc-imp-save") return _fcImportSave();
    const drop = e.target.closest("[data-drop]");
    if (!drop) return;
    _fcDrafts.splice(Number(drop.dataset.drop), 1);
    _fcRenderDrafts();
  });
}
if ($("fc-list")) {
  $("fc-list").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-del]");
    if (!b) return;
    const row = b.closest(".fc-row");
    const sv = row ? row.querySelector(".fc-row-sv").textContent.trim() : "this card";
    if (!confirm(`delete "${sv}"? its audio goes too.`)) return;
    const r = await jwrite("delete that card", `/api/flashcards/${encodeURIComponent(b.dataset.del)}`, jbody("DELETE"));
    if (r.ok) loadFlashcards();
  });
}

// ===================== POWER =====================
// Sleep, wake and restart the desk from wherever Jake is standing.
//
// The asymmetry is worth keeping in view while reading this, because it shapes
// every message below: sleep and restart travel down the socket the desk holds
// open to the hub, so they work from anywhere. Wake cannot — a sleeping machine
// holds no socket — so it goes out over the phone as a Wake-on-LAN broadcast and
// only works while the phone is on home Wi-Fi. The UI never blurs the two.

let _pwArmed = null;       // verb awaiting its confirming second tap
let _pwTimer = null;       // disarm timer, so a stray tap doesn't stay armed

function _pwDisarm() {
  _pwArmed = null;
  clearTimeout(_pwTimer);
  document.querySelectorAll(".pw-btn").forEach((b) => b.classList.remove("armed"));
  document.querySelectorAll(".pw-btn").forEach((b) => {
    if (b.dataset.label) b.textContent = b.dataset.label;
  });
}

async function loadPower() {
  const note = document.getElementById("pw-note");
  const where = document.getElementById("pw-where");
  if (!note) return;
  try {
    const d = await jfetch("/api/power");
    // Disable rather than hide: a greyed button with a reason teaches where the
    // limit is, and these limits (desk asleep, phone away) are the normal case.
    const sleep = document.getElementById("pw-sleep");
    const restart = document.getElementById("pw-restart");
    const wake = document.getElementById("pw-wake");
    [sleep, restart].forEach((b) => { if (b) b.disabled = !d.desk_connected; });
    if (wake) wake.disabled = !(d.wake_configured && d.phone_connected);
    if (where) where.textContent = d.desk_connected ? d.desk_where || "desk" : "desk offline";
    if (!d.desk_connected && !d.wake_configured) {
      note.textContent = "Desk offline, and no MAC configured — set CONTINUUM_DESK_MAC to enable wake.";
    } else if (!d.desk_connected) {
      note.textContent = "Desk is offline. Wake sends a magic packet from your phone — it only works on home Wi-Fi.";
    } else if (!d.wake_configured) {
      note.textContent = "Desk online. Wake is unconfigured (set CONTINUUM_DESK_MAC).";
    } else {
      note.textContent = "Desk online. Sleep and restart work from anywhere; wake needs the phone on home Wi-Fi.";
    }
  } catch (e) {
    note.textContent = "couldn't read power state — " + e.message;
  }
}

document.addEventListener("click", async (ev) => {
  const btn = ev.target.closest(".pw-btn");
  if (!btn || btn.disabled) return;
  const verb = btn.dataset.verb;
  if (!btn.dataset.label) btn.dataset.label = btn.textContent;

  // First tap arms, second fires. Deliberately not a confirm() dialog: this page
  // is loaded inside the phone app's WebView, where a bare WebChromeClient may
  // cancel JS dialogs outright — and a confirmation that silently never appears
  // would turn a two-step action into a one-tap reboot.
  if (_pwArmed !== verb) {
    _pwDisarm();
    _pwArmed = verb;
    btn.classList.add("armed");
    btn.textContent = "tap to confirm";
    _pwTimer = setTimeout(_pwDisarm, 5000);
    return;
  }
  _pwDisarm();

  const note = document.getElementById("pw-note");
  if (note) note.textContent = `asking the ${verb === "wake" ? "phone" : "desk"}…`;
  const r = await jwrite(`${verb} the PC`, "/api/power/" + verb, jbody("POST", {}));
  if (!r.ok) { loadPower(); return; }
  // Report what was actually done, not what was intended. Wake in particular is
  // fire-and-forget: the packet is never acknowledged, so "packet sent" is the
  // strongest true claim available and "your PC is awake" would be a guess.
  if (note) note.textContent = (r.data && r.data.message) || "done";
  // The desk drops off the fabric a few seconds after sleep/restart, so re-read
  // once it has had time to actually go.
  setTimeout(loadPower, verb === "wake" ? 8000 : 12000);
});

// ===================== ALARMS =====================
// Real wake-up alarms, managed on the phone. This tab is a thin remote control
// over /api/alarms; the phone owns scheduling and ringing (see AlarmScheduler.kt).
const _DAY_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
let _alarmDays = new Set();          // selected weekdays for the new-alarm form (Mon=0..Sun=6)

function _daysLabelJs(days) {
  const d = (days || []).map(Number).filter((x) => x >= 0 && x <= 6).sort((a, b) => a - b);
  if (!d.length) return "once";
  const k = d.join(",");
  if (k === "0,1,2,3,4,5,6") return "daily";
  if (k === "0,1,2,3,4") return "weekdays";
  if (k === "5,6") return "weekends";
  return d.map((i) => _DAY_SHORT[i]).join(" ");
}

function _fmtNextFire(ms) {
  if (!ms) return "";
  const dt = new Date(ms);
  const now = new Date();
  const hhmm = dt.toTimeString().slice(0, 5);
  const sameDay = dt.toDateString() === now.toDateString();
  const tomorrow = new Date(now); tomorrow.setDate(now.getDate() + 1);
  if (sameDay) return `today ${hhmm}`;
  if (dt.toDateString() === tomorrow.toDateString()) return `tomorrow ${hhmm}`;
  return `${_DAY_SHORT[(dt.getDay() + 6) % 7]} ${hhmm}`;   // JS Sun=0 → Mon=0
}

async function loadAlarms() {
  const list = document.getElementById("alarm-list");
  const sub = document.getElementById("alarm-sub");
  try {
    const data = await (await fetch("/api/alarms")).json();
    if (!data.connected) {
      if (sub) sub.textContent = "phone offline";
      list.innerHTML = '<div class="empty">Phone not connected — alarms are set and rung on the phone, so it has to be linked and online to manage them.</div>';
      return;
    }
    // The phone can be attached but dozing, in which case the query times out
    // and the server answers connected:true with an empty list plus an error.
    // Reading only `alarms.length` rendered that as "No alarms yet", asserting
    // the phone had none when it had several — and hiding their delete buttons.
    if (data.ok === false || data.error) {
      if (sub) sub.textContent = "couldn't ask the phone";
      list.innerHTML = `<div class="empty">Couldn't read the alarms from your phone — ${escapeHtml(data.error || "it didn't answer")}. These may still be set; this is not a reason to assume there are none.</div>`;
      return;
    }
    const alarms = (data.alarms || []).slice().sort((a, b) =>
      (a.hour * 60 + a.minute) - (b.hour * 60 + b.minute));
    if (sub) sub.textContent = alarms.length ? `${alarms.length} set` : "none set";
    if (!alarms.length) {
      list.innerHTML = '<div class="empty">No alarms yet — set one on the right.</div>';
      return;
    }
    list.innerHTML = alarms.map((a) => {
      const hhmm = String(a.hour).padStart(2, "0") + ":" + String(a.minute).padStart(2, "0");
      const next = _fmtNextFire(a.next_fire_ms);
      return `<div class="alarm-row${a.enabled === false ? " off" : ""}">
        <div class="alarm-when"><span class="alarm-hhmm">${hhmm}</span>
          <span class="alarm-daylabel">${_daysLabelJs(a.days)}</span></div>
        <div class="alarm-meta"><span class="alarm-rowlabel">${escapeHtml(a.label || "Alarm")}</span>
          ${next ? `<span class="alarm-next">next: ${next}</span>` : ""}</div>
        <button class="alarm-del" data-id="${escapeHtml(a.id)}" title="delete this alarm">✕</button>
      </div>`;
    }).join("");
  } catch (e) {
    list.innerHTML = '<div class="empty">couldn\'t load alarms</div>';
  }
}

// Delete (event delegation over the list).
(function wireAlarms() {
  const list = document.getElementById("alarm-list");
  if (list) list.addEventListener("click", async (e) => {
    const btn = e.target.closest(".alarm-del");
    if (!btn) return;
    btn.disabled = true;
    // Worth saying out loud: the phone owns the alarm, so this one fails for a
    // reason the UI can't see — the phone being off the link. Silently reloading
    // an unchanged list just looks like the button is broken.
    await jwrite("delete that alarm", "/api/alarms/" + encodeURIComponent(btn.dataset.id), jbody("DELETE"));
    loadAlarms();
  });

  const refresh = document.getElementById("alarm-refresh");
  if (refresh) refresh.addEventListener("click", loadAlarms);

  const daysBox = document.getElementById("alarm-days");
  const presets = document.getElementById("alarm-presets");
  const hint = document.getElementById("alarm-days-hint");

  function _syncDayChips() {
    if (daysBox) daysBox.querySelectorAll("button").forEach((b) =>
      b.classList.toggle("active", _alarmDays.has(+b.dataset.day)));
    // reflect which preset (if any) the current selection matches
    const label = _daysLabelJs([..._alarmDays]);
    if (presets) presets.querySelectorAll("button").forEach((b) =>
      b.classList.toggle("active", b.dataset.preset === (
        _alarmDays.size === 0 ? "once" : label)));
    if (hint) {
      const t = (document.getElementById("alarm-time") || {}).value || "the set time";
      hint.textContent = _alarmDays.size === 0
        ? `no days picked → rings once, at the next ${t}`
        : `repeats every ${label}`;
    }
  }

  if (presets) presets.addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    const map = { once: [], daily: [0, 1, 2, 3, 4, 5, 6], weekdays: [0, 1, 2, 3, 4], weekends: [5, 6] };
    _alarmDays = new Set(map[b.dataset.preset] || []);
    _syncDayChips();
  });
  if (daysBox) daysBox.addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    const d = +b.dataset.day;
    if (_alarmDays.has(d)) _alarmDays.delete(d); else _alarmDays.add(d);
    _syncDayChips();
  });
  const timeInput = document.getElementById("alarm-time");
  if (timeInput) timeInput.addEventListener("input", _syncDayChips);

  const form = document.getElementById("alarm-form");
  if (form) form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = document.getElementById("alarm-msg");
    const t = (document.getElementById("alarm-time").value || "").split(":");
    if (t.length !== 2) { if (msg) msg.textContent = "pick a time"; return; }
    const payload = {
      hour: +t[0], minute: +t[1],
      label: (document.getElementById("alarm-label").value || "").trim(),
      days: [..._alarmDays].sort((a, b) => a - b),
      enabled: true,
    };
    const btn = document.getElementById("alarm-save-btn");
    if (btn) btn.disabled = true;
    try {
      // jfetch already turns an {"error": ...} body into a throw, so the old
      // explicit r.error branch is folded into the failure path — and a 500 or a
      // non-JSON response, which used to read as success, now lands there too.
      const r = await jwrite("set that alarm", "/api/alarms", jbody("POST", payload));
      if (!r.ok) { if (msg) msg.textContent = "not set — is the phone on the link?"; return; }
      if (msg) msg.textContent = "";
      document.getElementById("alarm-label").value = "";
      await loadAlarms();
    } finally { if (btn) btn.disabled = false; }
  });

  _syncDayChips();
})();

(async function boot() {
  await loadStatus();          // learns mainId
  loadMemory();
  loadState();
  loadDashMind();              // the default landing view
  await loadConversations();
  if (mainId) openConversation(mainId);   // land in the day-to-day main thread
  loadSchedule();
  setInterval(loadSchedule, 20000);       // keep the "fires in" countdown fresh
  loadMessages();
  setInterval(loadMessages, 10000);       // refresh the incoming-message feed
  connect();
})();
