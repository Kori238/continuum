# What you are

*Addressed to Continuum. This is the map of your own body — which file owns
what, and what happens between Jake pressing enter and you answering.*

You are a Python process on Jake's Windows machine. One event bus, one agent,
two model tiers, one set of tools. The models are rented; everything that
persists — memory, routine, conversations, permissions — is yours and lives on
this disk.

## The layout

```
continuum/
  __main__.py     entry points: app | serve | run | demo | stop
  bus.py          in-process pub/sub. fnmatch patterns, async handlers
  events.py       Event(topic, payload, source, id, ts)
  config.py       Config.load() — every env var, frozen dataclass
  router.py       picks the tier
  providers.py    Ollama / claude CLI / Anthropic SDK
  agent.py        the turn: context in, reply out
  tools.py        91 tools, the ReAct loop, the permission gates
  devtools.py     the ten dev_* hands (see SELF-DEVELOPMENT.md)
  conversations.py  threads: main, task, dev — and the turn-level search index
  memory/         semantic store — embed.py, store.py, reason.py
  state/          the live machine: apps, focus, browser, messages, metrics
  routines.py     routine, subtasks, todos, performance
  activity.py     durable attention timeline: desktop focus + phone screen
  nutrition.py    calorie/macro intake log; integrations/foods.py = FatSecret+USDA food DBs
  body.py         weight/height→BMI, smart-scale body fat/muscle, steps→burn (data/body.db)
  health.py       sleep/HRV/resting-HR/SpO2 → derived readiness (data/health.db)
  flashcards.py   Swedish vocab — inflection tables, audio, SM-2 scheduling (data/flashcards.db)
  vocab.py        vocab import (OpenRouter) + Swedish text-to-speech (ElevenLabs); both optional
  attachments.py  images Jake uploads into the chat — bytes on disk (data/uploads/)
  proactive.py    unprompted thoughts, when they earn it
  significance.py the local gate: has anything changed enough to be worth a call
  scheduler.py    reminders and timed prompts
  messagelog.py   every message that arrives on this machine
  ledger.py       append-only audit trail: every action performed OR refused
  app/            server.py (HTTP+WS), mcp.py, security.py, window.py, static/
```

Data lives in `data/` (gitignored) — see `docs/DATA.md`.
The HTTP, WebSocket and MCP surface — see `docs/INTERFACES.md`.

## One turn, start to finish

Everything begins as an `Event(topic="task.request")` on the bus — from the
WebSocket, the scheduler, or the demo. `Agent._on_task`
(`agent.py:156`) is the whole turn:

1. **Resolve the thread** — `new_task` makes a `kind='task'` conversation; a
   given id resumes it; otherwise the main thread. Loads the last 8 turns.
2. **Record Jake's message** *before* the model runs, so a crash doesn't lose it.
3. **Assemble context** — pinned core profile; `memory.recall(k=5)` over
   `fact`/`preference` only (memory is strictly durable knowledge now; past turns
   live and are searched in `conversations.db`, not injected);
   `state.summary_text()`; `routines.context_line()`. `recall()` and
   `context_line()` are individually try/excepted. **`get_core()`
   (`agent.py:175`) and `state.summary_text()` (`agent.py:184`) are not** — a
   raise there escapes `_on_task`, so no `task.result` is published and the UI
   bubble hangs with no error. If you touch either, guard it.
4. **Gather** — `ToolLoop.gather()` runs the local model in a ReAct loop
   (`MAX_STEPS = 5`) to fetch what the answer needs. Not a read-only loop —
   it calls `_schemas(dev_writes=False)`, which withholds only the six mutating
   dev tools. Skipped when 🔧 tools are off.
5. **Compose** — one flat prompt: profile, facts, live state, "looked up just
   now (authoritative — prefer this)", conversation so far, then Jake's message.
   Every tier sees identical text.
6. **Route and generate** — `Router.decide()` then `provider.generate()`.
7. **Fold in MCP calls** — `mcp_calls_since(run_started)`, because cloud-tier
   tool calls bypass `gather()` entirely and would otherwise be invisible.
8. **Persist and publish** — append the assistant turn with its metadata, then
   `task.result` on the bus, then a desktop notification.
9. **After delivery** — index this turn for semantic search in `conversations.db`
   (`index_turn`, replacing the old memory.db `episode` copy); tag a brand-new
   task thread; run fact extraction if auto-learn is on.

On any provider exception, step 8 becomes a `task.result` carrying `error` —
the UI always gets something back.

## Which tier answers

`Router.decide` (`router.py:40`) is a fixed ladder, no model, no scoring:

| condition | tier | note |
|---|---|---|
| `sensitive` | **local, always** | if Ollama is down it still routes local and errors — it will fail before it leaks |
| `complexity in ("high","hard")` and cloud reachable | **cloud** | the only complexity signal in the system |
| Ollama up | **local** | the default |
| cloud reachable | **cloud** | local-down fallback |
| neither | `RuntimeError` | |

- **local-fast** — `llama3.1:8b` at `CONTINUUM_OLLAMA_HOST`. Free, private,
  always the default. Liveness = `GET /api/tags` within 2 s.
- **cloud** — `claude-opus-4-8`, reached by running the `claude` CLI against
  Jake's subscription login. No API key, no metered spend. Set
  `ANTHROPIC_API_KEY` and `config.py` switches to the metered SDK path instead.

## How the cloud tier gets hands

This is the part worth understanding properly, because it is the design point.

The `claude` CLI is invoked as a **pure model pipe**, deliberately crippled:
`--disallowedTools "Bash Read Glob Grep Edit Write WebFetch WebSearch Task
NotebookEdit TodoWrite"`, and `cwd` set to an empty temp sandbox. Claude Code's
per-project memory directory for that sandbox is wiped **once, in
`ClaudeCLIProvider.__init__` (`providers.py:94-96`)** — not per call; the tool
denies are what stop it writing new memory during a turn. An ungoverned CLI agent
reads Jake's files and keeps its own hidden state; this one can't.

Its only capability comes back through **your own MCP server** (`app/mcp.py`),
served in-process at `/mcp`. This whole block is conditional on
`_mcp_live()` (`providers.py:186-197`) — a 1.5 s `ping` to `/mcp`, cached 30 s:

```
--mcp-config {"mcpServers":{"continuum":{"type":"http","url":".../mcp",
                                          "headers":{"Authorization":"Bearer <token>"}}}}
--strict-mcp-config          # never load Jake's global MCP config
--allowedTools mcp__continuum
--permission-mode acceptEdits   # gating already happened on our side
--append-system-prompt <the operating brief>
```

So both tiers call the *same* `ToolLoop._execute`, through the same permission
gates, producing the same `action.performed` audit events. One implementation,
two tiers. `TodoWrite` is in the deny list specifically because the model would
otherwise keep a private task list and report work it never did.

`--permission-mode acceptEdits` is safe only because of that: the CLI cannot
prompt for permission in print mode, so a second gate there would deadlock the
turn. The real gate is Continuum's.

**If the probe fails, none of those flags are passed** — including
`--append-system-prompt`. So a cloud turn with a dead `/mcp` is not just
tool-less: it has no operating brief either, and answers as a generic assistant.
If a cloud reply reads oddly unlike you and called nothing, suspect the probe
before suspecting the model.

The operating brief lives in `ClaudeCLIProvider._BRIEF` (`providers.py:145`).
Timeout is `CONTINUUM_CLOUD_TIMEOUT`, default 900 s — a read-edit-compile-verify
turn is not a 180 s turn.

## Tools — 91 of them, in twelve classes

Declared as OpenAI-style function schemas in `tools.py`; `mcp.py` translates
them to MCP shape. `ToolLoop._schemas()` is the single gate, and `_execute`
re-checks each gate independently.

| class | count | gate | names |
|---|---|---|---|
| **read** | 26 | always on; *most* are dropped when their store is missing (see below); `recall_memory` needs a memory store and `search_conversations` a conversation store; `review_actions` needs the ledger; `read_clipboard` needs `CONTINUUM_CLIPBOARD`, `see_screen` the vision toggle, `view_image` an attachment store (it reads an image Jake uploaded into the chat, routed through the same local-VLM/cloud-eye path as `see_screen` but NOT behind `CONTINUUM_SCREEN` — an attachment is something Jake chose to send, not his live screen), `web_search`/`fetch_url` web access, `upcoming_events` a calendar feed, `list_scheduled_tasks` a scheduler, `activity_summary`/`activity_timeline` an activity store, `phone_status`/`phone_screen` a connected phone (`phone_screen` reads the phone's on-screen accessibility tree via its a11y service — read-only, privacy-sensitive) | `inspect_app`, `list_open_apps`, `recall_memory`, `search_conversations`, `browsing_history`, `project_state`, `recent_files`, `read_messages`, `recent_messages`, `search_messages`, `todays_routine`, `routine_performance`, `read_clipboard`, `see_screen`, `view_image`, `web_search`, `fetch_url`, `upcoming_events`, `list_scheduled_tasks`, `activity_summary`, `activity_timeline`, `phone_status`, `phone_screen`, `review_actions`, `briefing`, `correlations` — `review_actions` reads the append-only action ledger (what Continuum actually did, performed or refused); `briefing` synthesises a pulled digest of the day from the other stores (a pull, never a push); `correlations` joins routine adherence against activity time to surface patterns with their sample size |
| **ask** | 1 | **live turns only** — offered by `_schemas()` only when `self._turn_live` (a turn Jake started himself, WS source `"app"`); absent on proactive/scheduled turns, which face an empty room | `ask_user` — put a question to Jake mid-turn and **block** until he answers or it times out (default 60s, max 300), then continue the *same* turn with his reply as the tool result. Not an action on the machine and pushes nothing — it publishes `question.asked` to the already-open UI (tappable options + free-text) and awaits a Future the `POST /api/answer` handler resolves; on timeout returns `no_answer`. Because it's live-turn-only and in-band, it's also the notification gate's **recovery path**: when `_asked_for_notification()` would refuse a `notify`/`phone_notify`/`schedule_reminder` on a live turn, `_act` asks permission via `_ask_user_raw` instead of dropping silently — a tapped "Yes" is the only way a gate-missed notification still fires |
| **memory** | 2 | on whenever a memory store exists — remembering on request is core, not a privileged action | `remember`, `forget` |
| **routine** | 10 | on whenever the routine store exists — a granted capability, not an action | `add_routine`, `edit_routine`, `complete_routine`, `skip_routine`, `remove_routine`, `add_subtask`, `complete_subtask`, `add_todo`, `complete_todo`, `remove_todo` |
| **action** | 16 | ⚡ `CONTINUUM_ACTIONS`, **on** by default; the three `phone_*` actions and the three alarm tools also need a connected phone | `notify`, `open_url`, `open_app`, `schedule_reminder`, `schedule_task`, `cancel_scheduled_task`, `write_clipboard`, `media_control`, `close_tab`, `sleep_monitors`, `phone_open_app`, `phone_notify`, `phone_global_action` — five are **sensitive** (their schemas say "explicitly asked for", never a helpful extra): `close_tab` closes Firefox tabs by title via UIA (`state/tabs.py`) and destroys state; `cancel_scheduled_task` deletes a pending scheduler job (see `list_scheduled_tasks` in the read row to find its id); `schedule_task` runs a prompt unattended later and takes a `complexity` — `"low"` fires on the local 8B (simple checks only), `"high"` routes to cloud for real reasoning/fix work, so its schema forbids scheduling fix/reasoning work at `"low"` rather than banning it outright; `sleep_monitors` blanks Jake's displays (screen power-save via `SC_MONITORPOWER`, `state/probe.py`) mid-task — the machine keeps running and any input wakes them, but it interrupts what he's looking at, so only on an explicit ask; `phone_global_action` presses a system button on the phone via its accessibility service (back/home/recents/notification-shade/screenshot/lock/power) and acts *as* Jake — global actions only, no tapping or typing inside apps. Three **alarm** tools were added 2026-08-26 — `set_alarm`, `list_alarms`, `cancel_alarm` — real wake-up alarms that ring on the *phone* (Android `AlarmManager` + a foreground `AlarmService`), so they fire even while this desktop is asleep and the socket dropped overnight; the desktop is just the remote control (`phone.command("set_alarm"/"list_alarms"/"cancel_alarm")`) and the phone owns scheduling, ringing and the volume rule (play at the device's alarm volume or 50%, whichever is higher, restored after). All three are withheld in `_schemas()` when no phone is attached (via `_ALARM_TOOLS`, same as the `phone_*` gate), `list_alarms` returns read-only (no `action.performed`), and `set_alarm` is **notification-gated** like the three pushes below because it arms a future signal at Jake |
| **dev** | 10 | 🛠 `CONTINUUM_DEV_TOOLS`, **on** by default; local tier gets only the read-only four (`dev_read_file`, `dev_list_files`, `dev_grep`, `dev_diff`) | `dev_read_file`, `dev_list_files`, `dev_grep`, `dev_write_file`, `dev_edit_file`, `dev_replace`, `dev_run`, `dev_diff`, `dev_revert`, `dev_restart` — `dev_grep` regex-searches the source tree returning `file:line: text` (read-only, the first-class version of grepping the repo before an edit); `dev_replace` is **sensitive** — regex find-and-replace across a `paths` list and/or `glob`, the multi-file/regex counterpart to `dev_edit_file`'s single literal swap, and only touches UTF-8 text (binaries skipped) |
| **shell** | 1 | `CONTINUUM_SHELL`, **on** by default, but **cloud-tier only** — added to `_schemas()` only when `dev_writes=True`, the same seam that keeps `dev_run` off the 8B, so the local model never sees it | `run_shell` — **sensitive** (its schema says "explicitly asked for", never a helpful extra): arbitrary, un-sandboxed machine shell (PowerShell/cmd, `netsh`, `powercfg`, `winget`, anything on PATH), cwd = Jake's home. The general-machine counterpart to `dev_run`, which stays framed for and cwd'd to the repo. Reuses `DevTools._capture` (file-captured, timeout-safe, kills the whole process tree) and is audited on the bus like any action |
| **workouts** | 5 | on whenever the `WorkoutStore` exists (all five dropped in `_schemas()` when it doesn't) — read-only against Jake's Speediance history, except `refresh_workouts` which pulls the account into the store | `workout_summary`, `recent_workouts`, `workout_detail`, `exercise_progress`, `refresh_workouts` |
| **nutrition** | 11 | on whenever the `NutritionStore` exists (added as a block like the routine tools, not behind ⚡ — logging food when Jake asks is core to the tracker) — Jake's calorie/macro intake diary, saved meals and daily targets. `food_search` reads the food database (`integrations/foods.py` — **FatSecret** when its credentials are set, **USDA FoodData Central** otherwise or as automatic fallback); `food_by_barcode` resolves a UPC/EAN via **Open Food Facts** (free, keyless — the one provider that does barcode) and is a read like `food_search`, not an action; `nutrition_today`/`nutrition_summary`/`list_meals` read; `log_food`/`remove_food` write the diary; `create_meal`/`log_meal`/`remove_meal` manage reusable meals (a name + per-portion macros, optionally summed from an ingredient recipe and split by `servings`, logged by the — possibly fractional — portion); `set_nutrition_goals` writes the single-row daily calorie/macro targets the Nutrition tab and the Life dashboard's macro dials track intake against — it merges over the current goals so an omitted field is left unchanged, and carries an `activity_adjust` flag: when on, the stored `calories` is a *sedentary base* and the effective target = base + the day's active burn (steps + workouts), so only calories flex with movement (macros never do). That burn is cross-store, so the store only holds the flag/base — `NutritionStore.adjust_for_activity(goals, expenditure)` folds it in, called by the `/api/nutrition` route (`_nutrition_day`) and — via the tool loop's shared `_goals_with_activity` helper — by every read that quotes a budget (`nutrition_today` *and* `log_food`'s running-total tail), so the number Continuum reports right after logging matches the tab rather than the sedentary base; all supply expenditure the same way `_body_day`/`body_today` compute it. All writes are audited as `action.performed`, like routine edits; `remove_meal` is sensitive | `food_search`, `food_by_barcode`, `log_food`, `nutrition_today`, `nutrition_summary`, `remove_food`, `create_meal`, `log_meal`, `list_meals`, `remove_meal`, `set_nutrition_goals` |
| **body** | 3 | on whenever the `BodyStore` exists (a block like nutrition, not behind ⚡ — logging a weight/steps when Jake asks is core to the tracker) — Jake's weight/height/steps, plus smart-scale body composition. `log_weight` takes weight, optional height, and optional `body_fat_pct`/`muscle_mass_kg` (from a Renpho-class scale); `log_steps` writes steps — both audited as `action.performed`; `body_today` reads, reporting weight/BMI, body-fat % (+ derived fat mass) and muscle mass, and an expenditure figure that folds in `WorkoutStore.calories_on(day)` and the day's food intake for active burn + energy balance | `log_weight`, `log_steps`, `body_today` |
| **health** | 3 | on whenever the `HealthStore` exists (a block like body/nutrition, not behind ⚡ — logging how Jake slept or a resting-HR reading when he tells you is core to the tracker, not an action on the machine) — Jake's recovery picture: sleep (+stages), resting HR, HRV, SpO2, respiratory rate per day, and a **derived readiness/recovery score** (0–100, a heuristic from sleep-vs-8h + HRV/resting-HR vs their trailing baseline — Fitbit's own readiness is Premium-only and never reaches Health Connect, so Continuum computes its own from the raw signals, the way `BodyStore` derives BMI). `log_sleep`/`log_vitals` write and are audited as `action.performed`; `health_today` reads. Fed by the phone's Health Connect reader (`_phone_health` on `phone.health`) and manual tab/tool entries | `health_today`, `log_sleep`, `log_vitals` |
| **flashcards** | 3 | on whenever the `FlashcardStore` exists (a block like body/nutrition/health) — Jake's Swedish vocab. Each card holds two faces (`swedish` + `english`) so it studies in either direction, plus `pos`/`gender`, tags, an example sentence, an inflection table and per-form audio; cards group by `source` + `set` (Rivstart / Kap 3), and SM-2 scheduling lives in the store so the tools, the `/api/flashcards` routes and the Flashcards tab agree on what's due. `add_flashcard` writes one card, filling `pos`/`gender` into the tags the study filter reads (for a long word list the tab's import box is the right answer — it builds full inflection tables, which the tool does not); `list_flashcards` reads by source/set/tag with due counts; `remove_flashcard` is **sensitive**, deletes by id or by either face's text, and refuses an ambiguous match rather than guessing. add/remove are audited as `action.performed` like diary edits, and are in `FLASHCARD_WRITE` so they carry the ⚡ action flag | `add_flashcard`, `list_flashcards`, `remove_flashcard` |

The drop list in `_schemas()` (`tools.py:383-396`) is explicit, not derived, and
it is incomplete: `recent_files`, `read_messages` and `routine_performance` are
**never** dropped. So `routine_performance` is offered even with no routine store
and then falls through `_execute` to `unknown tool: routine_performance` — a
confusing failure rather than an absent capability. If you add a store-dependent
read tool, add it to that list too.

Everything that changes state publishes `action.performed`. The read-only dev
tools don't — reading a file is not an action. A **subscriber in `server.py`
persists both `action.performed` and `action.refused` into `ledger.py`** — the
durable, append-only audit trail (see `docs/DATA.md` → `ledger.db`). That is the
half that survives the fan-out, and what `review_actions` reads back.

**The notification gate is real enforcement, not just a brief.** `notify`,
`phone_notify`, `schedule_reminder` and `set_alarm` push an unsolicited signal at Jake, and
his rule is they must be explicitly asked for. `_act` checks
`_asked_for_notification()` — a strict, default-deny regex (`_NOTIFY_INTENT`)
over **this turn's own user text** — before firing any of the four. No ask this
turn → the call is refused *before* it fires and an `action.refused` lands in the
ledger. The turn's text is stashed on the loop by `gather()` (local tier) and
`begin_turn()` (cloud tier), cleared by `end_turn()`. This is a heuristic — it
reads intent words, not meaning — so it errs toward refusing; it is the runtime
backstop the four turns of SENSITIVE briefs never had, not a replacement for the
model's own judgement.

Agency lives here, not in the provider: the model *decides* which of your tools
to call; you execute, gate, and log them.

## The bus

`bus.publish(event)` fnmatch-matches subscriber patterns and awaits them all;
one handler's exception is caught and logged rather than killing the bus.

Topics you emit: `task.request`, `task.result`, `device.register`,
`memory.remember`, `memory.stored`, `memory.deleted`, `memory.core.set`,
`memory.autoextract`, `agent.tools`, `agent.actions`, `agent.dev`,
`agent.proactive`, `agent.step`, `action.performed`, `action.refused`, `conversation.tagged`, `schedule.changed`,
`reminder.fired`, `proactive.surfaced`, `ollama.state`, `question.asked`,
`question.answered`.

`question.asked`/`question.answered` bracket a blocking `ask_user` call: the
first carries `{id, request_id, question, options, timeout}` and rides the `*`
fan-out so the open UI renders a question panel; Jake's reply comes back **not**
over the socket (its receive loop is parked inside the running turn) but via
`POST /api/answer`, which resolves the awaiting Future. `question.answered`
(`{id, answer, timed_out}`) fires when the turn unblocks, so the panel dismisses
even on timeout. Both are attributed to the turn's `request_id`.

`agent.step` is the live "thinking" trail: one event per tool call as it runs,
carrying `{request_id, tool, args, action}`. It rides the `*` fan-out like any
event, and the UI attaches it to the pending bubble whose `request_id` matches —
turning the spinner into a visible, collapsible list of what you're doing. The
local `gather()` loop stamps each step with the turn's id directly; cloud (MCP)
tool calls are stamped via `ToolLoop.begin_turn`/`end_turn`, which the agent
brackets around the cloud run so a call arriving over `/mcp` can be attributed to
the single in-flight turn (ambiguous when two turns overlap, so it's skipped
live then — the final `tools_used` list still carries it).

Four subscribers exist server-side: the agent (`task.request`, `device.register`,
`memory.remember`), the UI fan-out (`*`), the deferred restart (`task.result`),
and `_record_device` on `device.register`. Everything else
reaches the browser purely through the `*` fan-out — which is why the event
stream in the State tab is a genuine view of the spine, not a curated log.

## The pulse

A heartbeat beats every `CONTINUUM_HEARTBEAT_SECONDS` (15 s). Each beat
supervises **Ollama**, ticks the **scheduler** (fires due reminders and timed
prompts), and asks the **proactive engine** whether anything is worth saying.
The Ollama pass is guarded separately, and runs first: everything after it
routes better with the local tier up.

`OllamaSupervisor` (`providers.py`) exists because Ollama being down is the
quiet, expensive failure — every turn silently escalates to cloud *and* memory
stops entirely, with nothing said out loud. Each beat it probes `/api/tags`; if
that fails and `CONTINUUM_OLLAMA_AUTOSTART` is on it spawns `ollama serve`,
backing off `15→30→60→120→300 s` over consecutive failures with a 25 s boot
grace so it doesn't stack spawns while one is still binding the port. It
publishes `ollama.state` on every up/down transition, so recovery is visible in
the State tab rather than silent.

The spawn is **detached** (`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`) for a
specific reason: `dev_restart` force-kills this process, and a child in the same
process group would die with it — you'd kill your own local tier every time you
edited yourself. If the binary isn't on `PATH` it logs once and disables
autostart; the probe still recovers if Jake starts Ollama by hand.

Two model-driven paths live in that engine, with different defaults, and
conflating them will confuse you: **general proactive messages** are gated by
`CONTINUUM_PROACTIVE` (**off**), while **routine-miss nudges** are gated by
`CONTINUUM_ROUTINE_REMINDERS` (**on**). So "proactive is off by default" is only
half true — out of the box you say nothing unprompted *except* nudging overdue
routine steps, which is exactly the path that produced the repeating greeting.


A third, **deterministic** path rides the same heartbeat: **routine auto-start**
(`CONTINUUM_ROUTINE_AUTOSTART`, **on**). It watches for Jake's first device touch
of the day — desktop presence going `active`, or the phone reporting `screen`
on and fresh — and `CONTINUUM_ROUTINE_AUTOSTART_DELAY_MIN` minutes later (default
15) calls `routines.start_routine()` so his anchored morning gets concrete times
without him pressing "start". `_autostart_check` runs at the very *top* of
`maybe_check`, before the away/live-conversation guards, because a phone-only
wake (PC still "away") must count and the delayed start must fire whether or not
he's at the desk. It fires at most once per day and is idempotent across
restarts — `wake_ts`/`wake_date`/`autostart_date` persist in `proactive.json`
(see `docs/DATA.md`). It stands down without starting if the day is already
anchored (a manual "start routine" wins), and ignores touches during the routine
quiet window so a 3am phone glance isn't mistaken for waking up. Needs no model —
it's a timer, not a judgement.

Both respect quiet hours and dedupe against what they already said — persisted
to `data/proactive.json`, because a relaunched process has never said anything
and would otherwise repeat itself seconds after coming back.

Three things shape *what* and *whether* the engine says, and all three are recent:

- **It won't barge into a live exchange.** Before either path runs, `maybe_check`
  reads the tail of the main thread and holds if Jake messaged within
  `_LIVE_WINDOW_S` (240 s) — a timer-driven "log dinner?" landing mid-discussion
  reads as a non-sequitur. Held routine steps aren't marked reminded, so they
  still nudge once things go quiet. `check_now()` bypasses this: asking directly
  is not barging in.
- **The general judgement runs on its own tier.** `CONTINUUM_PROACTIVE_TIER`
  (default `cloud`) decides which model answers "is anything worth saying?",
  decoupled from `CONTINUUM_REASONER`. The cloud provider is built once in
  `__init__` and shared with the routine-nudge phrasing; the general path reuses
  it (falling back to local only if cloud is chosen but unreachable). The
  significance gate keeps the call rare, so the cost that once forced this local
  is controlled.
- **The general evaluator sees the life-streams, not just the desk.** `_evaluate`
  now feeds the prompt a compact read of nutrition/body/training (`_streams_text`)
  and the recent conversation (`_conversation_text`) alongside state, memory and
  the schedule — so an observation can be about the day Jake actually had, and it
  won't re-open something just discussed. This is why messages stopped being only
  about missed routine.

### The significance gate

General proactivity used to call its model on a fixed cadence — every 300 s,
whether or not anything had moved. With `CONTINUUM_REASONER=cloud` that is ~288
Opus calls a day, nearly all of them looking at an unchanged desk and correctly
answering `NOTHING`. The expensive question ("is this worth saying?") was being
asked before the cheap one ("is this even different?").

`significance.py` asks the cheap one first, locally and for free. Three clocks
now govern a check, and confusing them is the main hazard:

| clock | default | means |
|---|---|---|
| `CONTINUUM_PROACTIVE_SECONDS` | 300 s | **earliest** a change may buy a call |
| `CONTINUUM_PROACTIVE_FLOOR` | 1800 s | **latest** — call anyway, even if nothing changed |
| `CONTINUUM_PROACTIVE_COOLDOWN` | 1800 s | minimum gap between messages actually *sent* |

Snapshotting runs every beat because it's free; that's what makes a change
detectable within 15 s instead of five minutes. `Significance.compare()` diffs
against the last baseline and returns a score plus reasons, and the call happens
when the score clears `CONTINUUM_PROACTIVE_SIGNIFICANCE` (1.0) or the floor
expires.

Three properties are load-bearing:

- **Rising edges, not levels.** Battery crossing below 25%, focus crossing the
  50-minute mark, a step count passing 5k/10k/15k, a workout landing today, or
  food newly logged. A condition that stays true doesn't re-fire every beat and
  burn the budget the gate exists to protect. The gate now takes the
  nutrition/body/workout stores too, so these life-stream signals can *trigger* a
  check, not just decorate one — `workout_done` (1.1) clears the bar alone; a
  single food entry (`nutrition_logged`, 0.6) deliberately does not, so there's
  no nudge per snack.
- **The baseline commits only when a call actually happens.** Re-baselining on
  every look would let a slow drift accumulate unseen, each step too small to
  score — the standard way a change detector goes silent forever.
- **The reasons go into the prompt.** `- Changed since you last looked: just
  came back to the desk; 6 new unread message(s)`. This is most of what makes
  the judgement good; without it the model re-derives the world every time and
  has no idea what is new.

Presence is directional — only *coming back* scores, since going away means
nobody is there to read the reply. While away no call is made, but the baseline's
presence field is marked so the return is a rising edge, and the rest of the
baseline is left intact so drift that happened during the break (mail piling up,
a step going overdue) still scores on the way back.

`check_now()` bypasses the gate entirely: Jake asking *is* the significance.

## Config

`Config.load()` reads `.env` with `setdefault`, so real environment variables
always win. One asymmetry to know: default-ON flags treat *any* unrecognised
value as on (`not in ("0","false","no","off")`), default-OFF flags treat any
unrecognised value as off (`in ("1","true","yes","on")`).

| variable | default | effect |
|---|---|---|
| `CONTINUUM_OLLAMA_HOST` | `http://localhost:11434` | local tier |
| `CONTINUUM_OLLAMA_AUTOSTART` | on | heartbeat restarts Ollama when it's down |
| `CONTINUUM_OLLAMA_BIN` | `ollama` | executable used to start it |
| `CONTINUUM_LOCAL_MODEL` | `llama3.1:8b` | local tier |
| `CONTINUUM_CLOUD_MODEL` | `claude-opus-4-8` | cloud tier |
| `CONTINUUM_CLOUD_PROVIDER` | auto | `claude-cli` unless `ANTHROPIC_API_KEY` is set |
| `CONTINUUM_CLAUDE_CLI` | `claude` | the executable |
| `CONTINUUM_CLOUD_TIMEOUT` | `900` | seconds before the CLI subprocess is killed |
| `CONTINUUM_APP_URL` | `http://127.0.0.1:8848` | where the CLI reaches `/mcp` |
| `CONTINUUM_EMBED_MODEL` | `nomic-embed-text` | one fixed embedder, always local |
| `CONTINUUM_TOOLS` | on | the `gather()` step |
| `CONTINUUM_ACTIONS` | **on** | action tools |
| `CONTINUUM_CLIPBOARD` | **off** | `read_clipboard` |
| `CONTINUUM_DEV_TOOLS` | **on** | dev tools |
| `CONTINUUM_SHELL` | **on** | `run_shell` — arbitrary machine shell, **cloud-tier only**, sensitive |
| `CONTINUUM_MEMORY_AUTOEXTRACT` | on | learn facts from conversation |
| `CONTINUUM_REASONER` | `local` | which tier tags/extracts/consolidates |
| `CONTINUUM_FDC_KEY` | `DEMO_KEY` | USDA FoodData Central key for `food_search` (the fallback food DB) — `DEMO_KEY` works but is rate-limited; a free key lifts it |
| `CONTINUUM_FATSECRET_ID` / `_SECRET` | *(unset)* | FatSecret OAuth client id + secret. Both set → FatSecret becomes the **primary** food DB (richer branded/serving data), USDA the fallback. `_SECRET` is a credential — never logged. Free tier also needs this machine's IP whitelisted in the FatSecret console, so calls must run locally |
| `CONTINUUM_BODY_DB` | `data/body.db` | body metrics store (weight/height/steps) |
| `CONTINUUM_HEALTH_DB` | `data/health.db` | recovery store (sleep/HRV/resting-HR/SpO2 → readiness) — the newest store, built for the Fitbit Air feed; covered by the `CONTINUUM_*_DB` row below |
| `CONTINUUM_HEARTBEAT_SECONDS` | `15` | the pulse |
| `CONTINUUM_PROACTIVE` | **off** | unprompted messages |
| `CONTINUUM_PROACTIVE_SECONDS` / `_COOLDOWN` | `300` / `1800` | earliest call, gap between messages |
| `CONTINUUM_PROACTIVE_FLOOR` | `1800` | call at least this often, even if nothing changed |
| `CONTINUUM_PROACTIVE_SIGNIFICANCE` | `1.0` | score the local gate must clear to call early |
| `CONTINUUM_PROACTIVE_TIER` | `cloud` | which tier makes the general "worth saying?" judgement — its OWN setting, no longer tied to `CONTINUUM_REASONER` (falls back to local if cloud is chosen but unreachable) |
| `CONTINUUM_ROUTINE_REMINDERS` | on | nudge overdue routine steps |
| `CONTINUUM_ROUTINE_GRACE_MIN` | `45` | how late counts as overdue |
| `CONTINUUM_ROUTINE_QUIET_START` / `_END` | `23` / `6.5` | quiet hours (also bound the auto-start wake window) |
| `CONTINUUM_ROUTINE_AUTOSTART` | on | auto-start the routine a delay after Jake's first device touch of the day |
| `CONTINUUM_ROUTINE_AUTOSTART_DELAY_MIN` | `15` | minutes after first touch (PC input or phone screen-on) before auto-starting |
| `CONTINUUM_ALLOWED_LOGINS` | tailnet owner | who may reach the app |
| `CONTINUUM_*_DB` | `data/*.db` | store locations |
| `CONTINUUM_UPLOADS_DIR` | `data/uploads` | where uploaded chat images (attachments) are stored |

## Things that are true and easy to forget

- **You are running the old code.** Any edit needs a restart to be live.
- **Nothing happens unless you call a tool.** Describing an action is not doing
  it, and the tool chips in the UI report what actually ran.
- **"Local is only triage" is wrong twice.** Local is the *default answering
  tier* — cloud is reached only on `complexity in ("high","hard")` or when
  Ollama is down. And `gather()`'s `dev_writes=False` withholds only the
  six mutating dev tools (`tools.py:397-403`); the 8B still gets every routine tool,
  and every action tool when ⚡ is on. It is not a read-only loop.
- **The window title snapshot goes to the cloud tier too.** Same trust boundary
  as Jake chatting with Claude directly — but worth remembering it's there.
