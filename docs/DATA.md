# What you remember

*Addressed to Continuum. Ten SQLite files, one JSON file, and an `uploads/`
directory in `data/` (gitignored). This is everything that survives a restart —
which is to say, everything that is actually you rather than the model answering
this turn.*

Every store opens `sqlite3.connect(path, check_same_thread=False)` and creates
its own tables. Most also migrate themselves with `PRAGMA table_info` +
`ALTER TABLE`; `Scheduler` and `MessageLog` don't. Paths come from `Config` and
each has a `CONTINUUM_*_DB` override.

| file | store | holds |
|---|---|---|
| `memory.db` | `MemoryStore` | facts, preferences, episodes, the core profile |
| `conversations.db` | `ConversationStore` | every thread and turn |
| `routines.db` | `RoutineStore` | the routine, subtasks, todos, completions |
| `scheduler.db` | `Scheduler` | reminders and timed prompts |
| `messages.db` | `MessageLog` | every message that arrived on this machine |
| `workouts.db` | `WorkoutStore` | training history — Speediance sessions + per-set detail, plus phone Health Connect sessions (e.g. DownDog yoga) |
| `activity.db` | `ActivityStore` | attention timeline — desktop focus + phone app/screen sessions |
| `nutrition.db` | `NutritionStore` | calorie/macro intake log — one row per food eaten, daily goals, plus saved reusable meals |
| `body.db` | `BodyStore` | body metrics — weight/height per day (→ BMI) and daily step counts (→ walking calorie burn) |
| `health.db` | `HealthStore` | recovery metrics — sleep (+stages), resting HR, HRV, SpO2, respiratory rate per day → derived readiness score |
| `flashcards.db` | `FlashcardStore` | Swedish vocab — word pairs with inflection tables, per-form audio and SM-2 review state |
| `ledger.db` | `ActionLedger` | append-only audit trail — every action performed OR refused |
| `proactive.json` | `ProactiveEngine` | what you've already nudged about today |
| `uploads/` (dir) | `AttachmentStore` | images Jake uploaded into the chat — the raw bytes + a JSON sidecar per file |

---

## routines.db

**`routines`** — the standing template. `id`, `title`, `time` (`HH:MM` or `''`
for untimed), `days` (`daily` / `weekdays` / `weekends` / CSV of Mon=0…Sun=6),
`kind` (`habit` | `block` | `reminder`), `note`, `section`, `active`, `sort`,
`created`, `repeat_days`, `offset_min` (NULL, or minutes-after-start for a
**time-relative** item — see *Time-relative anchoring* below).

**`anchors`** — `PRIMARY KEY(date, section)`, `ts`. The day's single *start* time,
stamped by `start_routine`. One start anchors the **whole day** — `start_routine`
clears any prior row for the date and writes exactly one, so there's only ever one
row per day (the `section` column is kept for the schema but no longer keys
behaviour). Its absence means the day hasn't been started; like completions it's
date-keyed, so it resets at local midnight.

**`completions`** — `PRIMARY KEY(routine_id, date)`, `status` = `done` or
`skipped`. **The absence of a row means pending.** Un-checking deletes the row
rather than writing a status. Because state is keyed by date, the whole routine
resets at local midnight with no job to run.

**Log steps auto-tick their routine step.** Logging food or a weigh-in completes
the matching "Log …" habit without a separate `complete_routine` call: `log_food`
/ `log_meal` tick `Log breakfast`/`Log lunch`/`Log dinner` (keyed on the entry's
stored meal slot — an empty or `snack` meal ticks nothing), and a weigh-in ticks
`Log weight` via `ToolLoop.autotick_weight()` — fired both by the `log_weight`
tool and by a scale reading synced from the phone's Health Connect feed
(`phone.weight` → `_phone_weight`), and only when a weight (not just
height/body-comp) was recorded.
A workout landing today ticks `Workout` via `ToolLoop.autotick_workout()`, which
fires from every ingest path — the `refresh_workouts` tool, the `/api/workouts/sync`
UI button (through the `workouts.synced` event), and a phone Health Connect session
(`phone.workout`) — each guarded by `WorkoutStore.has_session_on(today)`.
The seam is `ToolLoop._autocomplete_routine(title)` (`tools.py`), called from
those tool handlers: it matches an **active** routine by exact case-insensitive
title, marks it done for today via `set_done`, and emits an `action.performed`
(`complete_routine`, `auto: True`) for the ledger. It reads `status_of(rid)` first
and **never overrides a manual done or skip**, and is best-effort — any failure is
swallowed so it can't break the underlying log. Matching is by title, so renaming
a step away from `Log <meal>` / `Log weight` / `Workout` silently ends the auto-tick.

**`subtasks`** / **`subtask_completions`** — a per-day checklist under a routine,
same date-keyed reset. Presence of a completion row means done; there is no
skipped state for a subtask. **Ticking every subtask now auto-completes the
parent**, and un-ticking one pulls an all-done parent back to pending —
`set_subtask_done` calls `_sync_parent_from_subtasks(rid, date)` after every
write, which writes/clears the parent's `completions` row for that date. It only
fires for routines that *have* subtasks, and never overrides a `skipped` status.
(`sub_done`/`sub_total` are still the counters the UI shows.)

### Interval tasks — `repeat_days` and carry-over

`repeat_days` (default `0`) makes a routine recur **N days after it's checked
off**, not on a weekday pattern — e.g. "wash bedding" at `repeat_days=7`. When
`repeat_days>0`, `today()` **ignores `days`** and instead calls `_interval_due`:
the task becomes due `N` days after its last `done` completion (or from `created`
if never done), and then **carries over** — it shows every day from its due date
until it's marked off, at which point it hides for the next `N` days. It's also
shown on any day it was actioned (done *or* skipped) so it doesn't vanish
mid-tick; a **skip doesn't reset the interval**, so a skipped interval task
reappears the next day. ISO date strings compare lexicographically, which is why
the due-date maths stays string-based. Interval tasks are **excluded from
`performance()`** (a weekday denominator would be meaningless for them).

### Time-relative anchoring — `offset_min` and the start button

Jake's day doesn't start at a fixed clock time — he gets up when he gets up. So a
routine item can be **anchored**: `offset_min IS NOT NULL` means it has *no* fixed
`time`, and its due-time today is **the day's single start (`anchors`) +
`offset_min` minutes** — whatever section it's in. One "start routine" covers the
whole day: `start_routine` clears any prior anchor for the date and stamps `now`
(re-callable — a restart moves the *entire* day, morning through evening, not just
one section); `clear_anchor` removes it. `_day_anchor(date)` returns that one ts.
Reached by the `start_routine` tool, `POST /api/routines/start`, and a single "▶
start routine" button the UI renders on the first time-relative section.

`today()` resolves every anchored item against that one day anchor: **started** →
`time` is the computed `HH:MM` (so it sorts, goes overdue, and reads in
`context_line` exactly like any timed item); **not started** → `time` stays `''`
and it carries a `rel` label (`+15m`), sorting by `offset_min` so the sequence
still has an order. Each section group also exposes `anchored` / `started` /
`started_at` (all driven by the same day anchor). Because an un-started anchored
item has no time, it can **never be overdue** — no fixed clock time to be late
against before the day begins — and `context_line` flags it as "not started yet"
so a nudge can offer to start it. Setting an `offset_min` (via `update`/`edit_routine`) clears any stored
`time`; clearing it (`None`/blank) turns the item back into a fixed-time one.
`edit_routine`'s `unanchor` flag clears the offset *and* copies its old value into
`sort`, so the item keeps its place in the list as a plain timeless step (no
`time`, no `+Nm` label) instead of falling back to creation order.
Anchoring only touches *time*: `days`/`repeat_days` still decide whether the item
applies today, and `performance()` counts it as an ordinary daily/weekly item.

**`todos`** — one-off tasks that deliberately *don't* reset. `done` is a column,
not a per-day log, and `completed_at` is set when it flips.

### Sections (morning / afternoon / evening)

`SECTIONS = ("morning","afternoon","evening","night","anytime")` — that tuple's
order *is* the display order. An item's effective section is:

1. an explicit stored section, if it isn't `anytime` — always wins; else
2. derived from the time: **<12:00 morning, <17:00 afternoon, <21:00 evening,
   ≥21:00 night**; else
3. `anytime`.

`today()` returns both `section_raw` (as stored) and `section` (effective).
`all()` sorts on the raw value only — so a 07:00 item saved as `anytime` groups
under *morning* in today's view and under *anytime* in the full list. That's a
real inconsistency, not a subtlety to defend.

Within a section: timed items in clock order, then untimed in creation order —
because a sequenced morning has an order without having clock times.

### Addressing items

You work by **title**, not id. `resolve()` / `resolve_subtask()` /
`resolve_todo()` do case-insensitive exact match first, then substring. The
**"exactly one match" rule applies only to the substring stage** — an exact match
returns the first row it finds without checking for a second. So two routines
titled `Meds` resolve silently to one of them, while `Med` is refused as
ambiguous. Substring ambiguity returns `None`, which surfaces as
`no routine matching '<title>'`. That is a refusal: nothing changed.
`resolve_todo` only searches open todos.

### Streaks and performance

`_streak` counts backwards from today, and **starts from yesterday if today
isn't done yet**, so an unfinished morning doesn't look like a broken streak.
Only `kind='habit'` gets one.

`performance(window_days)` reconstructs, for each past day, which routines
actually applied (weekday match *and* `created_on > day` — note `>`, so a routine
counts as scheduled on the day it was created), then scores
`done / (scheduled - skipped)`. **Skips don't count against adherence** — a
deliberate skip is a decision, not a failure. Rate is `None`, not zero, when
nothing was scheduled.

Two things make it **not** a faithful record of the past: it filters `active=1`,
so deactivating a routine retroactively erases it from every historical day; and
it reads today's template, so retitling or rescheduling rewrites history too.
It answers "how am I doing on my current routine", not "what happened".

`context_line()` is what gets injected into every turn — today's counts, what's
still pending grouped by section, and the open todo titles.

---

## memory.db

One table, `memories`. The columns that matter: `text` (the only thing ever
injected), `kind`, `embedding` + `embed_model` + `dim`, `importance` (1–10),
`tags`, `conversation_id` (**NULL means global**, set means private to that
thread), and the supersession pair `superseded_by` / `superseded_at`.

Three kinds:
- **`fact`**, **`preference`** — durable; the only kinds recalled into an answer.
- **`core`** — the pinned profile. Injected unconditionally, so excluded from
  recall to avoid counting twice.

A fourth kind, **`episode`**, was **retired**. It used to hold a truncated copy
of every turn (`Jake asked… Continuum answered…`), embedded and 3-day-decaying —
a lossy second copy of conversation data living in the memory table. Turns are
now embedded and searched **at their source in `conversations.db`** (see below),
so `memory.db` is strictly durable knowledge. `MemoryStore._drop_episodes()`
deletes any leftover episode rows on construction — a one-time migration that
becomes a no-op once none remain. `DECAY_TAU` is now empty; nothing decays.

### Writing is a judgement, not an append

For durable kinds, `remember()` finds neighbours above cosine 0.55 and asks the
reasoner for `ADD` / `UPDATE` / `NOOP`. Above 0.95 similarity it short-circuits
to `NOOP` without calling a model at all. `UPDATE` inserts the new row and marks
the old one superseded — **nothing is deleted**, it just stops being current.
If the reasoner names a target id it wasn't given, the write downgrades to
`ADD`: keeping both is the safe failure.

`delete()` is the one place memory is genuinely destroyed.

### Recall is hybrid

`recall(query, k=5)` scores
`relevance + 0.50·tag_hits + 0.40·importance + 0.30·recency`, and applies a
scope filter first: global memories always visible, thread-scoped ones only
inside their own thread.

The interesting part: the semantic bar is **0.35 normally, but drops to 0.15
when a tag matches**. That's why *"what car do I drive?"* finds *"drives a blue
Subaru"* — the query word "car" hits the tag even though the memory never says
"car".

Tag matching (`store.py:75`) is **prefix containment, not a shared prefix**: a
query token of ≥4 characters matches a tag that *starts with* it, or a tag that
the token starts with. So `drive` ↔ `drives` matches — but `driving` and
`driven` do **not**, because neither is a prefix of the other. The ≥4 length
test applies to the query token only. Don't assume stemming; there is none.

The tag term is also **capped**: `W_TAG * min(hits, 3) / 3.0`. Three tag hits
score the same as ten, so piling tags onto a memory buys nothing past the third.
`jake` and `continuum` are stopwords, since they'd otherwise match everything.

### One embedder, always

`nomic-embed-text` on Ollama embeds every memory and every query, regardless of
which tier is answering. Using the chat tier to embed would create incompatible
vector spaces and silently break recall. Each row records its model and dim;
rows of a mismatched dim are skipped with a warning rather than compared.

**Consequence: if Ollama is down, memory is down.** `embed()` has no fallback,
so `recall` and `remember` raise — and every caller catches it, because memory
must never break a turn. You will answer without recall and not be told loudly.
Check `ollama_up` before concluding you have no memory of something.

That silence is why `OllamaSupervisor` exists (see `ARCHITECTURE.md` → *The
pulse*): the heartbeat restarts Ollama within ~25 s of finding it down. It
narrows the window but does not close it — a turn landing inside that window
still answers with no recall, and nothing in the reply says so. **Turns taken
while Ollama was down are not evidence that a memory doesn't exist.** If Jake
says you've forgotten something, check `ollama_up` before believing him.

### When it happens

Read: every turn — `get_core()` plus `recall(k=5, kinds={fact,preference})`.
Written: extracted facts after every answer if auto-learn is on (**global**, not
thread-scoped); and explicit saves over the bus. Extraction runs after the reply
is sent, so it costs no latency. (The per-turn record is no longer written here —
the turn is indexed for search in `conversations.db` instead; see below.)

---

## conversations.db

`conversations` (`title`, `tags`, `kind`, `updated`) and `messages`
(`conversation_id`, `role`, `content`, `meta`, `ts`, and — new in v2 —
`embedding`, `embed_model`, `dim`). Message `id` order is the canonical turn
order.

### Turn-level semantic search (the retired episode's new home)

Each message now carries a vector from the **same fixed local embedder memory
uses** (`nomic-embed-text` via `Embedder`), so a thread is semantically
searchable **at its source**, in full — this replaced the truncated `episode`
copies that used to live in `memory.db`. `ConversationStore` keeps an in-memory
normalized matrix of the embedded messages (the same fast-at-personal-scale trick
`MemoryStore` uses), built once in `_load_index()` and appended to per turn.

- `index_message(id)` / `index_turn(*ids)` — embed a message and add it to the
  index. The agent calls `index_turn(user_id, assistant_id)` **after** delivering
  each answer (best-effort, non-blocking — search never blocks a reply), exactly
  where the old episode write used to be.
- `backfill(limit=None)` — embed any messages with no vector yet (older threads,
  or turns written while Ollama was down). Guarded on the embedder being up, it
  **stops on the first embed error** so a mid-run Ollama drop just leaves the rest
  for next time — NULL embeddings are retried. Kicked off once on server startup.
- `search(query, k, conversation_id=None)` — cosine over the matrix; returns
  matching turns with their conversation `title`, `role`, snippet and score.
  Reachable via the `search_conversations` tool and `GET /api/conversations/search`.

Rows of a mismatched `dim` (a different embedder space) are skipped, never mixed —
the same rule `MemoryStore` applies. **Consequence: like memory, this search is
down when Ollama is down** — indexing and querying both embed, and both no-op
rather than raise, so a turn taken while Ollama was down simply isn't indexed
until `backfill` catches it.

Three `kind`s:
- **`main`** — the singleton home thread. Cannot be deleted, pinned first in the
  list, never auto-tagged (it isn't about one thing), and the fallback for any
  message with no conversation, including scheduler prompts and proactive nudges.
- **`dev`** — legacy self-development threads from the removed Dev tab.
  `ConversationStore.dev()` still exists but nothing calls it now; self-development
  happens in the **`main`** thread. Old dev threads remain browsable.
- **`task`** — a topic broken off from main, titled from its first message.

`meta` on each assistant turn stores `tier`, `reason`, `model`,
`memories_used`, `state_used`, `tools_used` — which is why reopening a thread
renders exactly as it happened, tool chips and all.

**The context window is a flat last-8 messages.** No token budgeting, no
summarisation. A long thread silently loses its early turns from the prompt. What
still finds them is the turn-level semantic index above (the `search_conversations`
tool / `search()`) — so if Jake refers to something from forty turns ago, the
model has to *choose* to search for it; it isn't in the window automatically.

`related()` is computed on the fly from shared tags. There's no graph database
anywhere in this system; every edge is derived from tags, embeddings, or origin
links.

---

## scheduler.db

`jobs` — `kind` (`reminder` | `prompt`), `next_fire` (absolute epoch),
`interval_s` (NULL for one-shots), `payload` JSON, `fire_count`, `last_fired`.

`tick()` fires everything due, then **unconditionally reschedules** — a job that
raises still advances, so a broken job can't hot-loop. One-shots are deleted
after firing. Recurring jobs are rescheduled as `now + interval_s` measured at
tick time, so they drift by up to one heartbeat (15 s) per fire.

`prompt` jobs publish a `task.request` with no conversation id, which lands them
in the main thread.

⚠️ `Scheduler._row` decodes **by tuple position** (`r[0]`…`r[10]`), and neither
`Scheduler` nor `MessageLog` has a `PRAGMA table_info` migration path — they are
the two exceptions to the self-migrating rule at the top of this file. In
`scheduler.py` the positional decode makes that sharper: inserting a column
mid-table breaks it silently. Append, or rewrite `_row` to use names.

---

## messages.db

Every message that arrives on this machine, from two feeds: Windows toast
**notifications** (system-wide, works when the app is unfocused) and on-screen
**window scraping**. Deduped by a `UNIQUE` content hash + `INSERT OR IGNORE`;
`record()` returns whether the row was new. The collector runs notifications
every 8 s and window scrapes every ~24 s.

---

## workouts.db

Jake's training history — a general workout inbox, not a machine-only log. The
main feed is Speediance (Gym Monster), pulled read-only from the unofficial cloud
API by `integrations/speediance.py` and accumulated here so insights outlive any
single pull (and survive the integration breaking). The **second feed is the
phone**: its Health Connect reader (`android/…/HealthConnect.kt`) reports each
on-device `ExerciseSessionRecord` — DownDog yoga and the like — as a `workout`
over the phone link, which `_phone_workout` in `app/server.py` upserts here. Two
tables:

**`sessions`** — one row per completed workout, keyed by `training_id`
(Speediance's `trainingId`, **not** the record `id`). List-level totals:
`title`, `start_ts`/`end_ts`, `duration_s`, `calories`, `total_volume`,
`total_energy`, `device_type`, `session_type`, plus `set_count` +
`detail_synced` (0 until per-set detail is fetched) and `synced_at`.

**`sets`** — per-set rows for program sessions: `exercise_name`, `set_index`,
`reps`, `weight_kg`. A "Free Lift" freestyle session reports **totals only**, so
it has zero `sets` rows but is still marked `detail_synced=1` so it isn't
re-requested forever.

**Health Connect sessions** land the same way but from the phone, not the cloud.
`_phone_workout` upserts them under a `hc:<uuid>` `training_id` (namespaced so it
can never collide with a Speediance `trainingId`) and immediately calls
`store_sets(tid, [])` — they have no per-set breakdown, and the empty store marks
`detail_synced=1` so the Speediance detail probe skips them. `device_type` /
`session_type` stay `0`, which is what keeps the render tools showing them by
title alone (e.g. "Yoga") rather than as a machine session; `calories` carries
the active-calories the phone aggregated over the session, `total_volume` is 0.

`sync()` is idempotent: sessions upsert by `training_id` (preserving already-
fetched detail on a list-level re-pull), and re-fetching a session's detail
replaces its `sets` wholesale. It passes each session's authoritative list
`type` to `session_detail` because a `trainingId` can resolve to **two unrelated
sessions** across the program (`cttTrainingInfo*`) and free (`freeTraining*`)
namespaces — type 5 probes program-first, everything else free-first, each with
the other as fallback. The detail endpoints live under **`/app/trainingInfo/`**,
not `/mobile/v2/` (that prefix 404s). Analytics (`stats`, `exercise_history`,
`exercises`, estimated 1RM via Epley) are computed here so the five workout tools
and the non-AI Workouts tab agree. GM2 note: Jake's sessions report
`device_type=1`, the tested path, so the "GM2 ids differ" hedge doesn't bite.

---

## activity.db

Your durable attention timeline — the half of "what did the day look like" that
`StateTracker` can't keep, because its focus history is an in-memory ~2h deque
that dies with the process. `ActivityStore` turns the live foreground signal
into **coalesced segments** and accumulates them, so "where did my day go" spans
restarts and days.

One table, **`segments`**: `device` (`desktop` | `phone`), `app`, `proc`,
`title`, `start_ts`, `end_ts`, `seconds`. One row per continuous run of an app
in front. An *open* segment is a real row whose `end_ts` is extended on every
poll — so it's crash-safe (already on disk) and a live read already includes the
in-progress run. The in-memory `_cur[device]` is just a cursor onto that open
row, not the source of truth.

Two feeds, wired in `app/server.py`:

- **Desktop** — the state poll loop calls `note_desktop(foreground)` every
  `poll_interval` (~5 s). Same `(app, proc)` as the open segment → extend it; a
  different app → close and open a fresh one. Crucially it passes the foreground
  app **only while Jake is present** and `None` when he's away, which closes the
  open segment — so "VS Code 40m" is 40m of real attention, not a window left in
  front over lunch. Same rule the tracker uses for its own focus counters. A
  **browser** segment is relabelled by the site in front (`_SOCIAL_SITES`, matched
  on the active tab's URL host the tracker now threads through the foreground
  view) — so YouTube, Instagram, Reddit and the like split out of the generic
  `Firefox` bucket as their own apps, while ordinary browsing keeps the browser's
  name. It's a *write-time* label, so only segments recorded after it shipped are
  split; older `Firefox` rows stay generic.
- **Phone** — `bus.subscribe("phone.*")` feeds `on_phone_signal`. A
  `phone.screen` report toggles a session open/closed and tracks whether the
  screen is believed on. If the phone's **accessibility service** is enabled it
  also sends `phone.foreground` (which app is in front), and the phone track
  becomes real **per-app segments** — same app → extend, different → close and
  open a fresh one — gated on the screen being on so a stray window event can't
  invent activity. Without that service the session falls back to a generic
  `Phone` segment, so a phone that only reports screen state still shows honest
  screen-on time; the first `foreground` after a screen-on refines that generic
  segment into the real app. Any other `phone.*` report *extends* an open
  session but does nothing while the screen is off — a background notification
  doesn't invent activity. `device.offline` ends the open session at the last
  thing we heard, not at "now" (a silent phone's time stopped when it went quiet).

Analytics live in the store so the non-AI Activity tab and the `activity_summary`
/ `activity_timeline` tools agree: `timeline(date)` clips segments to a local
calendar day (a run spanning midnight is split, counted into each day it
touches) and aggregates per-app desktop **and** phone totals (`apps` /
`phone_apps`) + desktop/phone screen time; `summary(days)` buckets seconds into
each day for the range strip; `app_history(name, days)` returns one app's
per-day usage across a window (the tab's click-through drill-down). Segments
under `MIN_SEGMENT_S` (5 s) are dropped from the *timeline view* as alt-tab
noise, but still counted in totals.

A small **`_EXCLUDED_APPS`** set is filtered out by *every* reader — Continuum's
own window (`Pythonw`), the phone's launcher / keyboard / settings chrome
(`Pixel launcher`, `Gboard`, `Settings`), and the generic `Phone`/`screen`
screen-on fallback that a real per-app foreground report supersedes. It's a
*view filter*: the rows stay on disk (so the list is cheap to change and nothing
is lost), and the match is case-insensitive on the app name, with the generic
phone session disambiguated from a real dialer named "Phone" by its `proc`.

Like the rest of `data/`, this is **gitignored and has no undo** — but unlike
memory it's pure observation, so a bad write is at worst a wrong bar, not a lost
fact. Nothing here is ever injected into a turn; it's browsed and queried, never
recalled.

---

## nutrition.db

Jake's calorie and macro intake log — the energy-*in* counterpart to
`workouts.db`'s energy-*out*. Three tables:

**`entries`** — one row per logged food. Each carries the macros already computed
**for the amount eaten** (`calories`, `protein_g`, `carbs_g`, `fat_g`) — the store
does no portion maths, the caller (a tool, or the tab's picker) scales
per-100 g values before logging. So the log is self-contained: totalling a day
never re-queries the food database, and an entry stays correct even if that
database later changes. Also `qty`/`unit`, an optional `meal`
(breakfast/lunch/dinner/snack, blank → "other"), `source` (`manual` from the tab,
`continuum` from a tool), and an optional `fdc_id` (the source food's id — a USDA
FDC id or a FatSecret food id, kept only as provenance; the log never re-queries it).
`day` is the **local** calendar day (`YYYY-MM-DD`) derived from `ts`, so day
totals reset at local midnight with no job to run — the same date-keyed trick the
routine store uses.

**`goals`** — a single row (`id=1` CHECK-constrained) of daily targets. Optional;
absent/zero targets read as `set: False`, which the UI renders as "no goal" rather
than a 0-kcal cap. `set_goals` upserts it.

**`meals`** — saved, reusable meals: a `name` (unique case-insensitively) plus
**per-portion** macros, a `servings` count, an optional `meal`-type default, and
an optional `ingredients` JSON blob. Two ways to define one, both via `add_meal`:
give the per-portion macros directly, or pass `ingredients` — each carrying its
own macros already scaled to its amount (the same self-contained rule as
`entries`) — which the store **sums to the recipe total and divides by
`servings`** for one portion. `log_meal(name, portions)` resolves the meal
(exact-then-unique-substring, like the routine store's `resolve`), scales the
per-portion macros by a possibly-fractional `portions`, and writes an ordinary
`entries` row — so a logged meal is just food in the diary, and day totals need
no knowledge of meals. Re-saving under an existing name updates it in place.

Analytics live in the store so the `nutrition_*` tools and the non-AI Nutrition
tab agree: `day(date)` returns a day's entries grouped by meal (canonical order,
"other" last) with totals + goals; `summary(days)` builds a **dense** per-day
series over the window (empty days show as gaps, not missing) plus averages over
only the logged days. The food database itself is **not** in this store — it's a
live lookup in `integrations/foods.py`, which dispatches across three providers
behind one `FoodClient` facade — two for text search (FatSecret/USDA) and one
for barcode:

  * **FatSecret** is the primary when `CONTINUUM_FATSECRET_ID` + `_SECRET` are
    set — OAuth 2.0 (cached bearer token), a large branded/restaurant catalogue.
    Its `foods.search` returns each food's macros as a `food_description` string
    ("Per 100g - Calories: … | Fat: … | Carbs: … | Protein: …"); we parse the
    basis and normalise gram-basis foods to per-100 g, keeping non-gram servings
    ("1 cup") as-is with `per_100g: false`. Its free tier requires this machine's
    IP whitelisted, so it may error (code 21) until that takes — hence the fallback.
  * **USDA FoodData Central** is the fallback (and the default when FatSecret
    isn't configured), keyed by `CONTINUUM_FDC_KEY`, normalised to per-100 g
    macros by nutrient *number* (203 protein, 204 fat, 205 carbs, 208 energy-kcal)
    because the display names/units drift.
  * **Open Food Facts** handles **barcode** lookup only (`FoodClient.by_barcode`
    → the `food_by_barcode` tool and `GET /api/nutrition/barcode`). Free, keyless,
    heavy on branded/packaged products; a UPC/EAN resolves straight to per-100 g
    macros in the same normalised shape. It's independent of the two text
    providers — neither's free tier does barcode — so it's used regardless of
    which of them is primary. OFF ids *are* barcodes, so the result carries the
    code in `barcode` and leaves `fdc_id` NULL.

`FoodClient.search_result()` reports which provider actually answered (and any
fallback note), which the tab's source widget and the `food_search` tool surface.
Every provider yields the SAME normalised shape (`…, per_100g, basis, ref_qty,
ref_unit`); rows with no usable numbers are dropped so an empty result can't
surface a 0-kcal food.

Like the rest of `data/` this is gitignored with no undo, but it's Jake's own
data (not pure observation like activity) — a bad `log_food` is a wrong diary
entry he can delete, not a lost fact. Nothing here is ever injected into a turn;
it's browsed and queried, never recalled.

---

## body.db

Jake's physical metrics — the third leg of the fitness picture. `nutrition.db`
is energy *in*, `workouts.db` is the energy *out* of training; `body.db` holds
the body those numbers describe (weight/height → **BMI**) and the *other* half of
energy-out — everyday movement (**steps** → walking calories). Two tables, both
keyed on the **local** calendar day, so like nutrition/routines they reset at
local midnight with no job.

**`measurements`** — `PRIMARY KEY(day)`, `weight_kg`, `height_cm`, and the
smart-scale composition columns `body_fat_pct`, `muscle_mass_kg` (added by a
`PRAGMA table_info` + `ALTER` self-migration, like the rest of the stores). One
row per day Jake weighed/measured. `set_measurement` upserts with
`COALESCE(excluded, old)`, so **logging just a weight preserves the day's height
and any composition already recorded** (a partial update never nulls a field you
didn't send). Height barely changes, so reads **forward-fill** it: `_height_on(day)`
takes the most recent height recorded *on or before* that day, and
`day()`/`summary()` fall back to the latest height overall when even that misses —
so a day before you first typed your height still gets a BMI. **Body composition
does NOT forward-fill** — it's a per-day scale reading (`_composition_on(day)`
returns that day's own value only), but `day()` also exposes a `*_effective`
last-known fallback for display (mirroring `weight_effective_kg`). BMI **and fat
mass** are **derived, never stored** (`bmi()`: `<18.5` underweight, `<25` normal,
`<30` overweight, else obese; `None` if either input is missing — a 0/blank
measurement reads as "not set", not a real zero; `fat_mass_kg` = effective weight
× effective body-fat %).

**`steps`** — `PRIMARY KEY(day)`, `count`, `source`. A health feed reports the
*running daily total*, so `set_steps` **replaces** the day (latest wins), never
adds. `source` records where it came from (`manual` from the tab, `continuum`
from the `log_steps` tool, `phone`/`google`/… once a feed pushes them). Walking
calories are derived (`steps_kcal`: ~0.04 kcal/step at a 70 kg reference, scaled
linearly by weight — a deliberately rough everyday-movement estimate, labelled as
such in the UI).

`day(date)` returns weight (measured + effective/last-known), forward-filled
height, BMI, steps, source and `steps_kcal`. It deliberately does **not** reach
into other stores — there's no cross-store transaction here (see *Sharp edges*).
**Active expenditure** (steps + workouts) and **energy balance** (vs food intake)
are composed by the *caller*: `_body_day()` in `app/server.py` and the
`body_today` tool add `WorkoutStore.calories_on(day)` and `NutritionStore.day()`
totals on top. That balance omits resting metabolism (no age/sex is asked for),
so it's the net of *active* burn only and is labelled "active" everywhere.

Fed four ways, all writing the same day rows: the Body tab
(`POST /api/body` for weight/height/composition, `/api/body/steps`), the
`log_weight`/`log_steps` tools, a `phone.steps` subscriber (`_phone_steps`) for a
daily step total, and a `phone.weight` subscriber (`_phone_weight`) that ingests
the phone's Health Connect reader (`android/…/HealthConnect.kt` → `weight()`:
`WeightRecord` + a smart scale's `BodyFatRecord`/`LeanBodyMassRecord`). Like the rest
of `data/` it's gitignored with no undo, but it's Jake's own data — a bad write
is a wrong entry he can overwrite, not a lost fact. Never injected into a turn;
browsed and queried only.

---

## health.db

Jake's recovery picture — the metrics a wrist wearable produces that no other
store holds. `nutrition.db` is energy *in*, `workouts.db` + the steps half of
`body.db` are energy *out*, `body.db` is the body itself (weight → BMI); this is
how that body is *doing* day to day. Built for the Fitbit Air (arriving
2026-08-25) but wearable-agnostic — the feed is whatever wrote to Health Connect
(Fitbit, Garmin, Samsung). One table, **`days`**, keyed on the **local** calendar
day so it resets at midnight with no job, like nutrition/body/routines.

**`days`** — one row per day, every metric column optional: sleep (`asleep_min`,
`in_bed_min`, `deep_min`, `rem_min`, `light_min`, `awake_min`, `sleep_start_ts`,
`sleep_end_ts`), and the spot vitals (`resting_hr`, `hrv_ms` = HRV RMSSD, `spo2`,
`respiratory_rate`, `skin_temp_delta`), plus `source` and `ts`. A night that
spans midnight is attributed to the **wake** day — the day you get up is the day
the sleep counts for, matching how every wearable reports it.

`upsert_day(payload)` writes whatever metrics the payload carries and
**COALESCE-preserves the rest** — the same partial-update rule `body.py`'s
`set_measurement` uses, so a spot-vitals report never wipes the night's sleep and
vice-versa. That matters because sleep and the spot readings arrive from
different Health Connect record types at different times of day.

**Readiness is derived, never stored** — the same rule `body.py` follows for BMI.
Fitbit's own "Daily Readiness" is a Premium, in-app score that is **never written
to Health Connect**, so it can't be ingested; instead `readiness()` computes its
own 0–100 recovery score from the raw signals it *does* get: sleep vs an 8h
target, and HRV and resting HR each vs their own **trailing 14-day baseline**
(`_baseline()`, computed from this store, excluding the day itself; `None` under
3 data points so a cold start doesn't invent a baseline). The three components
are weighted 0.5 / 0.3 / 0.2 over whichever are present. It's a deliberately
simple **heuristic**, labelled as such wherever it surfaces — exactly like the
walking-calorie estimate in `body.py`, not a clinical figure.

Analytics live in the store so the `health_*` tools, the `/api/health` routes and
the Health tab agree: `day(date)` returns the day's metrics, sleep
efficiency, baselines and readiness; `summary(days)` builds a dense per-day series
(gaps for empty days) with averages over only the logged days — for trends and
weekly reports.

Fed three ways, all writing the same day rows: the `log_sleep`/`log_vitals` tools,
`POST /api/health` (the tab, and the seam any feed can post to), and the
`phone.health` bus subscriber (`_phone_health` in `server.py`) that ingests the
phone's Health Connect reader (`android/…/HealthConnect.kt` → `vitals()`, one
aggregate per day). Like the rest of `data/` it's gitignored with no undo, but
it's Jake's own data — a bad write is a wrong entry he can overwrite, not a lost
fact. Never injected into a turn; browsed and queried only.

---

## flashcards.db

Jake's learning store — the one store that isn't a tracker of what happened but a
tool for what he's trying to *remember*. Swedish specifically: the schema knows
about `en`/`ett` and inflection tables, which is not language-agnostic and is not
pretending to be. Two tables, **`cards`** and **`reviews`**, and an SM-2
scheduler that lives in the store so the `flashcard` tools, the `/api/flashcards`
routes and the Flashcards tab all agree on what's due — the same
single-source-of-analytics rule the other stores follow.

This replaced a Leitner box ladder in September 2026, when the phone app Jake
actually studied from (Glosor, a PWA) moved in here. Both databases held zero
rows, which is the only reason the old scheduler could simply go rather than
being migrated.

**`cards`** — one row per pair. `swedish` + `english` are the two faces; the UI
decides which is prompt and which is answer, so **one row studies in either
direction**. Around them sits what makes a card teachable rather than just
testable: `pos` and `gender`, a `tags` array (always carrying the pos, and the
gender for nouns — tags are what the study filter reads), an `example_sv` /
`example_en` sentence pair, a `forms` object holding the full inflection table,
and `speak`, the list of surface forms worth hearing aloud. `audio` maps
`form → voice → filename`; the mp3s live beside the db, not in it, because a
card's audio is ~15 KB and a row should not be.

Cards are grouped by **`source` + `set_name`** — "Rivstart" / "Kap 3",
"Duolingo" / "Unit 1" — rather than one flat deck name, because that is how the
material Jake learns from is actually shaped. The **`UNIQUE(source, set_name,
swedish, english)` COLLATE NOCASE** index means re-adding an identical pair
updates its soft fields rather than duplicating, while the same word genuinely
appearing in two chapters stays two cards.

On the wire the field is **`set`**; in Python it is **`set_name`**, because `set`
is a builtin and cannot be a keyword argument. That line is drawn once, in the
store, and the routes follow it — two names for one field is the drift worth
spending a paragraph to prevent.

**Scheduling is SM-2**, ported verbatim from Glosor's `srs.js`. Each card carries
an `ease` (starting 2.5, floored at 1.3), an `interval` in days, `reps`, `lapses`
and a `due` timestamp in **epoch seconds** — the one conversion `import_backup`
performs, since Glosor stores milliseconds and an unconverted value would put
every card's due date roughly 50,000 years out and render the queue empty
forever rather than error. `review(id, quality)` takes SM-2's **0–5**, not a
boolean: a lapse (q<3) resets `reps`, zeroes the interval and brings the card
back in ten minutes, while Hard/Good/Easy (3/4/5) all pass but schedule apart via
Glosor's own ×0.7 and ×1.3 multipliers. Those multipliers are tuning on top of
textbook SM-2, not errors in it, and must not be "corrected" away.

**`reviews`** is append-only — one row per answer with the quality, the mode it
was studied in, and the resulting interval and ease. The cards table holds only
the current state, so without this there would be no way to ask whether a card is
getting easier or Jake is just seeing it less often.

Reads: `cards(source, set_name, tag, limit)` is the manager list (newest first —
tag filtering happens in Python, not SQL, because `LIKE '%verb%'` also matches
*adverb*); `buckets()` is every source/set with its total, due and new counts,
all measured against one `now` so a slow query can't report totals from two
different instants; `tags()` is the filter vocabulary; `study(...)` is a session —
everything due soonest-first, topped up with never-seen cards before not-yet-due
ones, since a card never seen is worth more than an early re-run; `stats(...)` is
the header counts; `resolve(text)` finds one card by either face (exact, then
*unique* substring — ambiguous returns nothing rather than guessing, which for a
tool marked SENSITIVE is the point); `missing_audio()` lists every (card, form)
that should be speakable and isn't.

Fed three ways, all writing the same rows: the Flashcards tab, the
`add_flashcard`/`remove_flashcard` tools, and `import_backup` — which handles
both a Glosor phone backup and a freshly built set from the import screen, one
mapping for both so a restore and an import cannot drift into disagreeing about a
field. A backup names `source`/`set` on every card and an import names them once
at the top; a per-card value always wins, the payload-level one being a default
for cards that don't say rather than an override of one that does.

Like the rest of `data/` it's gitignored with no undo, but it's Jake's own data —
a bad card is one he can edit or delete, not a lost fact. Never injected into a
turn; browsed and studied only.

---

## ledger.db

The durable half of Continuum's agency trail. Every state-changing tool already
announces itself on the bus as `action.performed`, and — new with the
notification gate — a refused attempt as `action.refused`. Those events fan out
to the live UI and then vanish. `ActionLedger` is a bus subscriber (wired in
`app/server.py`) that persists each one, so **"what did Continuum actually do to
my machine today?" is answerable after the fact** instead of only reconstructable
by grepping `tools_used` out of the conversation log. That blind spot is not
hypothetical — it's exactly what let a wrongful "I didn't send that notification"
stand for a whole turn.

One table, **`actions`**: `id`, `ts`, `action`, `args` (JSON, capped at 4 KB so a
huge clipboard write can't bloat the trail), `result` (capped 1 KB), `allowed`
(1 performed / 0 refused), `reason` (why a refusal), `request_id` (the turn that
caused it — tool-path actions carry it; app-originated ones like a tab write are
NULL), and `source`.

**It is append-only by design — there is no update and no delete.** An audit
trail you can quietly rewrite is not an audit trail; that is the whole point, and
it's why the store exposes only `record()`, `recent()` and `count()`. Writes are
best-effort (a failed audit write is logged and swallowed, never breaking a turn,
like the bus's own transparency emitters). Read back via the `review_actions`
tool. Like the rest of `data/` it's gitignored with no undo — but unlike memory
it's pure record, so a bad row is at worst a confusing line, never a lost fact.

Note the ledger is the **witness**, not the gate. The enforcement lives in
`tools.py` (`_act` → `_asked_for_notification()`); the ledger is where the
refusal it produces is written down. See `docs/ARCHITECTURE.md` → the tools
section for the gate itself.

---

## uploads/ (attachments)

Not SQLite either — the one store that holds **bytes, not rows**. When Jake
attaches an image in the chat (`POST /api/upload`), `AttachmentStore`
(`continuum/attachments.py`) writes it under `data/uploads/` as two files keyed by
a random 16-hex id: `<id>.<ext>` (the raw image) and `<id>.json` (a sidecar with
`name`, `mime`, `ext`, `size`, `ts`). No database — the sidecar is enough to serve
the file back with the right content-type and remember its original name.

Trust boundary: ids are validated to **bare hex** before any read, so a fetch or
`view_image` call can never escape the uploads directory, and only real image
bytes (PNG / JPEG / GIF / WEBP, checked by **magic number**, not the client's
declared type) are ever written, capped at 20 MB.

The id is the whole handle: it's stashed in the user message's
`meta.attachments` (so a reopened thread re-renders the picture via
`GET /api/upload/{id}`) and injected into the turn as a prompt cue, so the model
knows to call the `view_image` tool — which reads the bytes through the same
local-VLM / cloud-eye vision path as `see_screen`. Like the rest of `data/` it's
gitignored with no undo; a stray upload is a leftover file, not a lost fact.

---

## proactive.json

Not SQLite — a small JSON file beside `routines.db`:

```json
{"reminded_date": "YYYY-MM-DD", "reminded": [9,10,11],
 "last_routine_surface": 0.0, "last_surface": 0.0, "last_cloud": 0.0,
 "wake_ts": 0.0, "wake_date": "YYYY-MM-DD", "autostart_date": "YYYY-MM-DD"}
```

The last three drive **routine auto-start** (see `docs/ARCHITECTURE.md` → the
pulse): `wake_ts` is when Jake first touched a device today (0 = not seen yet),
`wake_date` scopes that timestamp to a day (a new day zeroes the wake clock), and
`autostart_date` is the day the engine already started-or-stood-down — set
*before* the start fires so a crash or restart in the delay window can never
double-start. Their persistence is load-bearing for the same reason the rest of
this file's is: the auto-start must survive the restarts Continuum does to itself
without re-anchoring a day it already handled.

`last_cloud` is the significance gate's floor clock. It has to survive a restart
for the same reason the rest of this file does, only sharper: **you restart
yourself to apply your own edits.** Were it in memory only, every relaunch would
read as "never called the cloud", the floor would fire immediately, and a dev
session — which is a series of restarts — would become a burst of Opus calls.

This exists because **you restart yourself to apply your own code edits.**
`_last_check` starts at zero, so the first heartbeat after a relaunch always
evaluates — and with dedup held only in memory, a relaunched process had never
said anything and re-nudged the same overdue steps seconds after coming back.
That's the bug Jake reported as "the good morning greeting came up again".

It is written *before* the nudge is phrased, so a crash during phrasing can't
cause a repeat.

What survives a restart: routine-nudge dedup, both cooldowns, and the gate's
floor clock. What doesn't: `_last_check`, the general-message similarity deque
(so the 0.6-overlap filter is blank after a relaunch), and the significance
**baseline snapshot** — which is deliberate. A rebuilt baseline scores 0 against
nothing, so the first check after a restart waits for the floor rather than
treating the entire world as new.

---

## Sharp edges

- **`RoutineStore` sets `row_factory = sqlite3.Row` as a side effect** inside
  `todos()`, `all()`, `today()`, `performance()` and `resolve()`, and never
  resets it. Every later read on that connection sees `Row` objects. Harmless
  today; any *new* positional-tuple read in that file becomes dependent on call
  order. Use named columns.
- **No cross-store transaction exists.** Six independent connections, no WAL,
  no pooling. Don't design anything that needs two stores to commit atomically.
- **`data/` is gitignored**, so `dev_revert` cannot undo a bad write to it.
  Source is revertible; Jake's routine and memory are not.
