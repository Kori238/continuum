# How the world reaches you

*Addressed to Continuum. One uvicorn app on `127.0.0.1:8848` — HTTP, a
WebSocket, and your own MCP server — behind one security middleware.*

The socket **never leaves loopback**. `tailscale serve` terminates TLS on the
tailnet and proxies in, so no port is open on a real interface. Reaching you
from Jake's phone goes through Tailscale's identity, not through an open port.

## The guard

`Guard` (`app/security.py`) is raw ASGI, not `BaseHTTPMiddleware` — deliberately,
because the latter skips WebSockets, and the WebSocket is where every task and
every dev command is submitted. It is added last, so it wraps everything.

The verdict, in order:

1. **`/mcp`** — requires `Authorization: Bearer <MCP_TOKEN>`, compared with
   `secrets.compare_digest`. The identity check is skipped entirely; the token
   is the whole gate. Without it, `/mcp` is 403 even from loopback.
2. **An identity header** (`Tailscale-User-Login`) — allowed only if the login is
   in the allow-list (`CONTINUUM_ALLOWED_LOGINS`, defaulting to the tailnet
   owner). tailscaled **overwrites** any client-supplied value of that header,
   which is what makes it trustworthy — verified: a spoofed
   `attacker@evil.com` was replaced, not honoured.
3. **Proxied but anonymous** — `X-Forwarded-For` present with no identity is
   refused, so an unknown proxy can't launder tailnet devices through the
   loopback rule below.
4. **Otherwise** — allowed only from loopback.

Fail closed: an empty allow-list means nobody from outside. Refusals are a 403
JSON body for HTTP, and a `1008` policy-violation close for WebSockets — closed
without ever being accepted.

`MCP_TOKEN` is `secrets.token_urlsafe(32)`, generated **at module import** and
never persisted. Every restart invalidates every prior token. It reaches the
`claude` CLI only through argv, never a file.

`/api/whoami` is the honest self-test: it reports the login, the name, and
whether it came `via` the tailnet or loopback.

## MCP — `/mcp`

Your own tools, served in-process over JSON-RPC. Hand-rolled, no SDK, protocol
version `2025-06-18`, server name `continuum`.

Methods: `initialize`, `ping`, `tools/list`, `tools/call`, and `notifications/*`
(no reply). `GET /mcp` is a bare 405 — there is no server-initiated SSE stream.

`tools/list` returns `ToolLoop._schemas()` translated from OpenAI function shape
to MCP `inputSchema`. `tools/call` runs `ToolLoop._execute` — the *same*
function the local ReAct loop calls — then `record_mcp_call(name, args)` on
success only.

That recording matters more than it looks. Cloud-tier tool calls never pass
through `gather()`, so without it a turn that rewrote Jake's routine reported
`tools_used: []` in the UI — a confident chip saying nothing happened. The agent
folds `mcp_calls_since(run_started)` into the turn's metadata, tagged
`via: "mcp"`.

An unknown or failing tool comes back as a **result** with `isError: true`, not
as a JSON-RPC error — so the model reads the failure and can react, rather than
the transport swallowing it.

## WebSocket — `/ws`

Everything interactive. Client sends `{type: ...}`; unknown types are silently
ignored.

**In:** `task` (prompt, `corr`, complexity, sensitive, conversation_id,
new_task, `attachments` — a list of image ids from `POST /api/upload`; a task
with attachments and no prompt is still valid) · `remember` · `core` ·
`autoextract` · `tools` · `actions` · `dev` · `proactive` · `proactive_now`.

**Out:** `hello` (per connection) · `event` (every bus event, via the `*`
fan-out) · `state` (the machine snapshot, ~5 s, deliberately off the bus) ·
`app_restarting`.

The client's `corr` becomes the `Event.id`, which comes back as `request_id` on
`task.result` — that's how a reply finds its pending bubble. A normal chat reply
is not a dedicated message type; it is the ordinary `task.result` bus event
arriving through the fan-out.

Because `corr → bubble` (the `pending` map) is per-client, only the device that
*submitted* the turn has a bubble to fill. So `onResult` has a second path: if a
`task.result` carries no live bubble here but its `conversation_id` is the thread
this client is viewing, it reloads that thread from the store (both the message
and the reply are already persisted before `task.result` is published). That is
what keeps a second device — or the same device after it switched threads mid-
turn, which detaches the pending bubble — in sync without a manual reopen.

### Restart and reconnect

The browser reconnects every 1.5 s forever, which is what makes a relaunch look
seamless. The `app_restarting` broadcast arms a *reload* rather than a silent
reattach. Without the reload the page would reattach to a new process while
still running the old HTML and JS.

Server side, `_deferred_restart` fires on `task.result`: consume the armed flag,
wait 2 s for the reply to flush to every client, broadcast `app_restarting`,
wait 0.3 s, then relaunch off-thread.

A client that *doesn't* take the reload path (a genuine socket drop, a
backgrounded tab, the phone webview) can miss the `task.result` for a turn that
was in flight across the gap — the bus has no replay, so its "thinking…" bubble
would otherwise tick forever. Two backstops close this. A **watchdog** in the
`setInterval` ticker retires any turn with no result or new `agent.step` for
`WATCHDOG_MS` (3 min) to an *interrupted* bubble and drops it from `inflight`
(a fresh step resets the clock, so a genuinely-working turn is never killed). And
on a real reconnect (tracked by `everConnected`, so not the first page-load
connect) the client **reconciles against the store**: it clears
`inflight`/`pending` and refetches the open thread rather than resurrecting
orphaned in-flight bubbles. A turn that finished during the gap was persisted
before its result published, so it shows correctly; one that truly died shows as
a message with no reply; one still running lands later via `onResult`'s refetch.

## HTTP

`GET /` serves the app. `/static/*` serves `app.js` and `app.css`.

- **Status** — `GET /api/status` (live-probes Ollama and the embedder on every
  call, so it does network I/O; `ollama_up` is its own fresh probe, independent
  of the supervisor's cached view), `GET /api/whoami`.
- **Ask** — `POST /api/answer` (`{id, choice}`) delivers Jake's reply to a
  blocking `ask_user` question, resolving the Future the parked turn is waiting
  on. It is an HTTP POST, *not* a WS message, for a load-bearing reason: while a
  turn runs, this socket's receive loop is parked inside `bus.publish` (the agent
  subscriber runs inline), so the WS can't read an inbound `answer` — but a
  separate request handler can, and the question panel was rendered from the
  `question.asked` event the socket's *outbound* fan-out delivered (sending is
  independent of that block). Returns `{ok}` — false if the id is stale/unknown.
- **Routine** — `GET /api/routines`, `GET /api/routines/performance?days=`,
  `POST /api/routines`, `PATCH|DELETE /api/routines/{id}`,
  `POST /api/routines/{id}/done`, `POST /api/routines/{id}/status`.
- **Subtasks** — `POST /api/routines/{id}/subtasks`,
  `POST /api/subtasks/{id}/done`, `DELETE /api/subtasks/{id}`.
- **Todos** — `GET|POST /api/todos`, `POST /api/todos/{id}/done`,
  `DELETE /api/todos/{id}`.
- **Memory** — `GET /api/memory?q=&limit=`, `DELETE /api/memory/{id}`,
  `GET /api/memory/{id}/related`.
- **Conversations** — `GET /api/conversations`,
  `GET /api/conversations/search?q=&k=` (semantic search over past turns — the
  full text of what was said, indexed at its source in `conversations.db`; this
  replaced the retired memory `episode` copies, and also backs the
  `search_conversations` tool), `GET|DELETE /api/conversations/{id}`,
  `GET /api/conversations/{id}/memories`. The literal `/search` path is declared
  **before** `/{id}` (`conv_id: int`) so the path param doesn't swallow it.
- **State** — `GET /api/state`, `GET /api/state/inspect?target=` (pid or app
  name).
- **Messages** — `GET /api/messages?limit=&app=&q=`.
- **Schedule** — `GET /api/schedule`, `DELETE /api/schedule/{id}`.
- **Workouts** — `GET /api/workouts?limit=`, `GET /api/workouts/stats?days=`,
  `GET /api/workouts/exercises`, `GET /api/workouts/exercise?name=`,
  `GET /api/workouts/{training_id}`, `POST /api/workouts/sync`. Back the non-AI
  Workouts tab; all read the `WorkoutStore` except `sync`, which pulls Jake's
  Speediance account into it. The literal `/stats`, `/exercises`, `/exercise`,
  `/sync` paths are declared **before** `/{training_id}` so the path param
  doesn't swallow them.
- **Activity** — `GET /api/activity?date=YYYY-MM-DD` (one day's segments + per-app
  desktop and phone totals + desktop/phone screen time; omit `date` for today),
  `GET /api/activity/summary?days=` (per-day desktop/phone minutes + top apps over
  the window), and `GET /api/activity/app?name=&days=` (one app's per-day usage —
  backs the drill-down you get by clicking an app or a segment in the tab).
  Read-only over the `ActivityStore`; back the non-AI Activity tab. A noise set
  of apps (Continuum's own window, the phone's launcher/keyboard/settings, the
  generic screen-on fallback) is filtered out at read time in the store, so every
  reader agrees.
  The store is *fed* server-side (the state poll loop + a `phone.*` bus
  subscriber), not through these endpoints — there's no write route.
- **Nutrition** — `GET /api/nutrition?date=YYYY-MM-DD` (a day's entries grouped by
  meal + totals + goals; omit `date` for today), `GET /api/nutrition/summary?days=`
  (dense per-day totals + averages), `GET /api/nutrition/search?q=&limit=` (look a
  food up — FatSecret first, USDA fallback; macros for the picker; returns
  `{foods, provider, fallback?, note?, error?}` so the tab can show which database
  answered — never raising), `GET /api/nutrition/barcode?upc=` (resolve a barcode
  (UPC/EAN) via Open Food Facts → `{barcode, food, provider, error?}`, `food`
  null when not found; manual UPC entry in the tab today, and the seam a phone
  scan will POST to next — never raises), `GET /api/nutrition/providers`
  (`{primary, fatsecret_configured, fallback, providers, barcode}` — drives the
  source widget), `GET|POST /api/nutrition/goals` (read/set the
  daily targets), `POST /api/nutrition` (log a food — Jake logging himself from the
  tab; macros are the totals for the amount, already scaled client-side), and
  `DELETE /api/nutrition/{entry_id}`, plus **saved meals** —
  `GET /api/nutrition/meals` (list), `POST /api/nutrition/meals` (save/update by
  name; `ingredients` are summed and split by `servings`, or give per-portion
  macros direct), `POST /api/nutrition/meals/log` (`{name, portions, meal?}` — log
  a saved meal by the — possibly fractional — portion), and
  `DELETE /api/nutrition/meals/{meal_id}`. Back the non-AI Nutrition tab over the
  `NutritionStore`. Unlike Workouts/Activity this tab **writes** (POST/DELETE); the
  same log is written by the `log_food`/`remove_food` tools. Literal `/summary`,
  `/search`, `/barcode`, `/goals` are declared before `/{entry_id}` so the path
  param doesn't swallow them.
- **Body** — `GET /api/body?date=YYYY-MM-DD` (a day's weight, forward-filled
  height, BMI, smart-scale body-fat %/muscle mass (+ derived fat mass), steps and
  derived energy; omit `date` for today),
  `GET /api/body/summary?days=` (dense per-day weight/BMI/body-fat/steps series +
  latest), `POST /api/body` (log weight, height and/or body composition
  `body_fat_pct`/`muscle_mass_kg` — only the fields sent are written),
  `POST /api/body/steps` (set a day's step count — the ingestion seam for step
  data, also reachable from the `phone.steps` bus signal; weight + composition
  land the same way from `phone.weight`). Back the non-AI Body
  tab over the `BodyStore`. The `GET /api/body` payload is **composed** — it folds
  `WorkoutStore.calories_on(day)` and `NutritionStore` intake on top of the body
  store so it can report active expenditure (steps + workouts) and energy balance
  (intake − that burn); see `_body_day()`. This tab **writes** (POST); the same
  data is written by the `log_weight`/`log_steps` tools. Literal `/summary` and
  `/steps` are declared before the bare `/api/body`.
- **Health (recovery)** — `GET /api/health?date=YYYY-MM-DD` (a day's sleep,
  vitals, baselines and derived readiness; omit `date` for today),
  `GET /api/health/summary?days=` (dense per-day sleep/resting-HR/HRV/SpO2/
  readiness series + averages over logged days), `POST /api/health` (log recovery
  metrics — the tab, and the seam a health feed posts to; body carries whatever
  metrics are known, `upsert_day` COALESCE-preserves the rest). Back the non-AI
  Health tab over the `HealthStore`. Unlike `/api/body` this is **store-only** —
  readiness is derived inside the store from its own trailing baselines, composing
  nothing across stores. This tab **writes** (POST); the same day is also written
  by the `log_sleep`/`log_vitals` tools and the `phone.health` bus signal (the
  phone's Health Connect reader). Literal `/summary` is declared before the bare
  `/api/health`.
- **Flashcards** — `GET /api/flashcards?source=&set=&tag=&limit=` (matching cards
  newest-first + every source/set bucket with due counts + the tag vocabulary +
  header stats; omit the filters for all), `GET /api/flashcards/buckets` (just the
  buckets and tags), `GET /api/flashcards/study?source=&set=&tag=&limit=&due_only=`
  (a study set — due soonest-first, topped up with never-seen cards before
  not-yet-due ones unless `due_only`), `GET /api/flashcards/status` (whether
  import and audio have keys behind them, plus how many forms are still silent —
  **booleans only**, never key material, since it renders in a browser),
  `GET /api/flashcards/audio/{name}` (one mp3; the *store* validates the name
  resolves inside the audio dir, so the route never builds the path itself),
  `POST /api/flashcards` (add a card), `POST /api/flashcards/import` (`{text}` —
  turn a pasted word list into **draft** cards via OpenRouter; deliberately does
  not save, because a mis-gendered noun is far cheaper to fix before it becomes
  forty cards with audio attached), `POST /api/flashcards/import/save`
  (`{cards, source, set}` — commit reviewed drafts through the same mapping a
  Glosor backup restores through), `POST /api/flashcards/voice`
  (`{card_ids?, limit}` — generate missing audio; the cap counts **synthesis
  calls** because that is what ElevenLabs bills, and the response reports what is
  left so the tab can offer another batch rather than looking finished),
  `PATCH /api/flashcards/{card_id}` (partial edit; never touches scheduling),
  `POST /api/flashcards/{card_id}/review` (`{quality, mode}` — SM-2's **0–5**,
  not a boolean: Hard/Good/Easy all pass but schedule apart), and
  `DELETE /api/flashcards/{card_id}` (its audio goes too). Back the non-AI
  **Flashcards** tab over the `FlashcardStore`. On the wire the field is `set`;
  in Python it is `set_name`, because `set` is a builtin — the store draws that
  line and the routes follow it rather than inventing a second name. Import and
  audio are the only parts that need an API key; without one they 502 with a
  message naming the setting to fill in, and the rest of the tab studies on.
  Literal segments are declared before `/{card_id}` so the path param doesn't
  swallow them (same ordering rule as the workouts/nutrition routes).
- **Alarms** — `GET /api/alarms` (the phone's alarm set — each with
  id/hour/minute/days/enabled and a computed `next_fire_ms`; returns
  `{alarms, connected}`, and an offline phone is `connected:false`, not an error),
  `POST /api/alarms` (`{hour, minute, label, days:[Mon=0..Sun=6], enabled, id?}` —
  add or, with `id`, replace an alarm), `DELETE /api/alarms/{id}` (remove one).
  Back the non-AI **Alarms** tab, but unlike the other Life tabs these hold **no
  desktop store**: the *phone* owns the alarm set and rings it via `AlarmManager`
  (so a wake-up alarm fires even with this desktop asleep or the socket dropped
  overnight), and each route just proxies `phone.command("list_alarms" /
  "set_alarm" / "cancel_alarm")` down the link. The same set is driven by the
  `set_alarm` / `list_alarms` / `cancel_alarm` MCP tools. Every route needs the
  phone connected.
- **Uploads (chat images)** — `POST /api/upload` (`{name, data}` where `data` is
  base64 or a full `data:` URL — JSON, so no multipart dependency; the browser
  reads the file client-side and posts the bytes. Validates it is really an image
  by magic number, caps size at 20 MB, stores it under `data/uploads/` keyed by a
  random hex id, and returns `{id, name, mime, size, url}`), and
  `GET /api/upload/{id}` (serves a stored image back — the composer thumbnail and
  the inline image when a thread is reopened; jailed to the uploads dir by the
  `AttachmentStore`, so a non-hex id 404s). Back the chat's 📎 attach control over
  the `AttachmentStore` (`continuum/attachments.py`). The id is stashed in the
  user message's `meta.attachments`; the model reads what's in the image with the
  `view_image` tool, not through these routes.
- **App update** — `GET /api/app/version` (the served phone APK's
  `versionCode`/`versionName`/`size`/`builtAt`, read from `android/dist/version.json`
  with a fallback to parsing `build.gradle.kts`), `GET /app/download` (the APK
  itself, `404` if none built). Both sit behind the Guard, so only the tailnet
  can pull a build. The sideloaded phone app checks `/api/app/version` on
  foreground and, on Jake's confirmation, downloads `/app/download` and hands it
  to the system installer — see `android/…/Updater.kt`. `build.sh` writes
  `version.json` next to the APK so the manifest and the binary always agree.

Note `GET /api/conversations/{id}` returns HTTP 200 with `{"error": "not
found"}` rather than a 404.

## The UI

The nav is **two levels**. A top-level section switcher in the header (`#sectionnav`)
picks one of two sections; a second strip under the header (`#tabnav`) shows only
that section's subtabs — `switchTab()` (`app.js`) hides the other section's buttons
with `[hidden]` and highlights the active one. The two sections are:

- **🧠 Mind** — the assistant and the system: **Chat**, **Messages**, **State**,
  **Memory**.
- **🌱 Life** — Jake's day and body: **Routine**, **Workouts**, **Nutrition**,
  **Body**, **Health**, **Flashcards**, **Alarms**, **Activity**.

Each section's **first subtab is a Dashboard** (`view-dash-mind` / `view-dash-life`,
default landing is the Mind dashboard). A dashboard is a grid of summary **tiles**
(`.dtile`) — one per sibling subtab — each showing a headline figure pulled from
that subtab's API (`loadDashMind` reads `/api/status` + `/api/messages` +
`/api/conversations` and the live state; `loadDashLife` reads `/api/routines` +
`/api/nutrition` + `/api/workouts` + `/api/activity` + `/api/body`). Every tile is a deep-link:
clicking it calls `switchTab()` into the matching subtab. Both loaders are
best-effort (a failed fetch leaves a dash, never blanks the board); the Mind
dashboard also re-renders on live `state` WS pushes while it's the visible view.

The subtabs themselves:

- **Chat** 💬 — threads, the task form, the 📎 attach control (pick one or more
  images — a recipe, a label, a photo — which upload to `POST /api/upload` and show
  as removable thumbnails in the composer, then ride the task as `attachments` and
  render inline in the user bubble; the model reads them with `view_image`), and
  the 🔧 tools / ⚡ actions / 🌱 proactive / 🛠 self-dev toggles.
- **Routine** 🗓 — today + performance, todos, all routines. Today groups items
  into **collapsible** part-of-day sections; each item's **subtasks are collapsed
  by default** behind a chevron toggle that also holds the "+ add subtask" input
  (so an item with no subtasks yet still expands to gain one). Ticking all
  subtasks auto-checks the parent. The add/edit forms take a **repeat-every-N-days**
  interval (`repeat_days`) for tasks that recur a fixed number of days after being
  done and carry over until then; collapse state is UI-only (module `Set`s, reset
  on reload — sections default open, subtasks default closed).
- **Messages** ✉, **State** 📊 (live machine state and the raw bus event stream),
  **Memory** 🧠 (core profile, auto-learn, semantic search, scheduled jobs).
- Several **non-AI explorers** that read (and, for Nutrition, Body and Flashcards,
  write) their store directly with no model in the loop: **Workouts** 🏋 (Speediance history),
  **Activity** 🕘 (the attention timeline), **Nutrition** 🍎 (the calorie/macro
  diary — day view + macro-vs-goal tiles + a food logger whose picker queries the
  food database — FatSecret or USDA — with a widget showing which one answered,
  a **barcode** field that resolves a UPC via Open Food Facts and prefills that
  logger, and a **saved-meals** panel: build a reusable meal from per-portion
  macros direct, or from ingredients each searched-and-scaled then summed and
  split by servings — with the recipe total and per-portion preview computed
  live — and log any saved meal by the possibly-fractional portion),
  and **Body** ⚖ (weight/height → a BMI scale, smart-scale body-fat %/muscle-mass
  tiles, steps + workouts → active energy expenditure, and — with the day's food
  intake — an energy balance; the log form writes weight/height/body
  composition/steps, and these can equally arrive from a health feed — steps on
  `POST /api/body/steps` or `phone.steps`, weight + composition on `phone.weight`
  from a smart scale synced to Health Connect),
  and **Flashcards** 🃏 (spaced-repetition vocab, built first for Swedish — a deck
  picker with due/learned stats, a **study launcher** choosing direction
  (Swedish→English, English→Swedish, or mixed), mode (**flip** to self-grade,
  **write** to type the answer with a typo override, **match up** to pair prompts
  and answers a batch at a time) and session size, and a manage panel to add cards
  and browse/delete the deck; each review `POST`s back so the store's Leitner
  scheduler advances what's due — the same decks the `add_flashcard`/
  `remove_flashcard` tools write).

While a turn runs, its assistant bubble shows a **working indicator** — a
spinner, an elapsed clock that ticks up every second (`work-el`), and a
plain-language activity line (`work-act`, e.g. *editing app.js*, *running …*)
derived from the latest `agent.step` — so a long cloud turn visibly counts up
instead of looking frozen or vanished. Under it, the same steps stream into a
collapsible **steps** disclosure (each tool call, with a marker for mutations);
the final result rebuilds that from the authoritative `tools_used`. The live
turn is held as data in an `inflight` map keyed by `corr` (not just as a DOM
node), so switching tabs/threads rebuilds the working
bubble — clock and steps intact — instead of dropping it until the reply lands.
A single `setInterval` ticks every live bubble and runs the interrupted-turn
watchdog; `onResult` clears the entry on completion (see *Restart and reconnect*). Every message (yours and
Continuum's) carries a hover-revealed **copy** button — text pulled at click
time, so copying a still-streaming bubble grabs the finished answer, and a
**date+time stamp** under the bubble (`fmtStamp`) — from the message's stored
`ts` when a thread is reopened, or the wall clock for a live bubble (the
assistant's is restamped to when the reply lands). The Chat thread gets both,
built from the shared `assistantBubble`/`userBubble`.

When a turn calls `ask_user`, it **blocks**, and an **ask panel** (`#ask-panel`,
just above the composer) renders the question with tappable option buttons and a
free-text box — `showAsk()` on the `question.asked` event. Tapping or typing
`POST`s to `/api/answer` (`sendAnswer`), which unblocks the turn; the panel
clears on the paired `question.answered` event (`dismissAsk`), or if the turn
times out. The card guards against a double-send. Multiple open questions stack,
keyed by id. This is the only place the UI answers a turn *mid-flight* rather
than starting a new one.

Self-development is **not a separate tab or agent** — it happens in the ordinary
Chat thread. Tick the 🛠 self-dev toggle (on by default) so the turn gets the dev
tools, and leave complexity on `high → cloud`: the six *mutating* dev tools are
cloud-only, so a source edit has to route to the cloud tier. There is no longer a
dedicated Dev tab, a `dev_lock`, or a hardcoded `complexity: "high"` — a self-dev
turn is just a normal cloud turn with dev tools switched on.
