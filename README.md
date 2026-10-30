# Continuum

A personal operating system — an event-driven fabric that weaves your devices,
your context, and Claude into one assistant you fully control. Not "an AI agent";
the AI is just the brain inside it.

> **`docs/` is Continuum explaining itself.** This README is written for you;
> `docs/` is written *to* the agent, in second person, and is what it reads
> before changing its own source — architecture, data model, interfaces, and the
> self-development procedure. Start at [`docs/README.md`](docs/README.md).

## The idea

Everything is an **event** on a **bus**. Devices, sensors, integrations, and the
agent all publish and subscribe. That one decision is what makes *collation* and
*cross-device* fall out naturally instead of being bolted on.

```
 devices ──▶ ┌───────────────────────────┐ ◀── integrations (calendar, email, HA…)
             │            BUS             │
 sensors ──▶ │   (in-process → NATS)     │ ──▶ context store (the digital twin)
             └─────────────┬─────────────┘
                           ▼
                        agent  ──▶  router  ──▶  fast / cloud  (both Claude)
                           │
                           ▼
                       you (toast, later: phone push, voice…)
```

## Layers

| Layer | File | Status |
|---|---|---|
| Events | `continuum/events.py` | ✅ |
| Bus (spine) | `continuum/bus.py` | ✅ in-process; NATS adapter is the next step |
| Router (model allocation) | `continuum/router.py` | ✅ two rungs; picks on who's asking |
| Providers (Claude, two ways) | `continuum/providers.py` | ✅ Claude Code CLI + Anthropic SDK |
| Agent (brain) | `continuum/agent.py` | ✅ thin; grows planning/tools/routines |
| Devices (fabric) | `continuum/devices/` | ✅ desktop; phone/Pi next |
| Desktop app (interface) | `continuum/app/` | ✅ native window, live event stream, task console |
| Memory (twin: the past) | `continuum/memory/` | ✅ cross-model semantic memory, injected as context |
| Live state (twin: the now) | `continuum/state/` | ✅ open apps + durations + focus + metrics; basic() always, inspect() on demand |
| Conversations | `continuum/conversations.py` | ✅ persisted multi-turn threads; prior turns injected as context |
| Agentic tools | `continuum/tools.py` | ✅ definitions, permission gates, audit trail; the model calls them over MCP |

## The ladder

Everything is Claude. There was a local Ollama rung; it was removed (2026-09-15)
because it was unused and unreliable, and because pinning anything to it meant
that when it was down, the feature was down. Both rungs are now the same Claude
at two different model sizes:

1. **fast** — `claude-sonnet-5`. Continuum's own internal machinery asks for this
   rung *by name*: tagging, fact extraction, "is this worth interrupting him
   for?" Small, frequent, never read directly — sending those to Opus is waste.
2. **cloud** — `claude-opus-4-8`. Everything you type. Your turns get the heavy
   model, full stop.

Either rung is reached one of two ways, chosen once in `continuum/config.py`:

- **Claude Code CLI** (the default) — shell out to `claude`, reusing its existing
  login, i.e. your Claude *subscription*. No API key to manage, no secret in code
  or config; the CLI handles auth and token refresh.
- **Anthropic SDK** — set `ANTHROPIC_API_KEY` and Continuum auto-switches to the
  metered API path.

The router no longer scores privacy, complexity or availability, and
`continuum/router.py` records why so nobody helpfully adds them back. **Privacy**
is gone because there is no local rung left to pin `sensitive` work to — a flag
that looks like a control and isn't is worse than no flag. **Availability** is
gone because both rungs are the same provider: when Claude is unreachable there
is nothing to fall back *to*, so the honest response is to raise rather than
pretend a degraded answer is a normal one.

## Run it

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -e ".[app]"          # include the desktop app extras
# No API key needed: the cloud tier uses your existing `claude` CLI login.
# (Optional) copy .env.example .env   # only to override defaults or force an API key

python -m continuum app    # open the desktop control app (native window)  ← default
python -m continuum stop   # stop a running instance
python -m continuum demo   # run the first vertical slice once
python -m continuum run    # boot the fabric and stay up (headless)
python -m continuum serve  # run the app server headless (open a browser to :8848)
```

Launching is **single-instance**: `app`/`serve` replace any server already on the
port, so you can't end up with two instances serving different code.

`demo` fires two tasks — one on each rung of the ladder — through the full path
and pops a notification for each.

## The desktop app

`python -m continuum app` opens a native window (WebView2; falls back to your
browser) that *is* a live view of the fabric:

- **Status bar** — the websocket connection, the **fast** rung's model, the
  **cloud** rung's model and how it's authenticated (*subscription* or *api key*),
  and **memory** (which embedder, how many memories — or "embedder down"). The two
  model pills share one reachability dot, because they're the same connection and
  a separate dot would imply a separate failure.
- **Device fabric** — every device and its capabilities as they register on the bus.
- **Task console** — type a prompt (attach an image if you like), toggle what
  Continuum may do (🔧 tools, ⚡ actions, 🌱 proactive, 🛠 self-dev) → a result card
  shows the rung that answered **and why**, plus the model's output.
- **Event stream** — every event crossing the bus, live. The spine, on screen.

Architecturally the app is just another interface consuming the bus; the core
knows nothing about it.

## Memory (cross-model)

Continuum's memory lives in *Continuum*, not in any model's head — so it's
**model-agnostic**. Whichever tier the router picks reads and writes the same
store, and memory is injected into the prompt as plain context, so every model
consumes it identically.

- **Provider-neutral storage** — text + metadata in SQLite (`data/memory.db`,
  gitignored). No chat model's private state is ever the source of truth.
- **One fixed embedder, in-process** — `BAAI/bge-small-en-v1.5` (384-d, via
  fastembed: ONNX runtime, no torch, no daemon, no separate install) embeds
  *every* memory and query, regardless of which rung is answering. Using the chat
  tier to embed would create incompatible vector spaces and silently break
  recall; one fixed embedder keeps the space consistent and always-on. It can't
  be a provider: Anthropic has no embeddings endpoint, so "route everything to
  Claude" was never going to cover this. It replaced `nomic-embed-text` on Ollama,
  which made memory fail *silently* every time the daemon was down. The model was
  picked on measurement, not reputation — against the incumbent on the real store
  it agreed on 0.71 of top-3 hits and 0.88 of top-1, at 2.8 ms an embed. Each row
  records the embed model + dim, so a future change is detected rather than
  silently mixed — and `store.py` skips rows from a different model rather than
  mixing incomparable vectors, so swapping the embedder means re-embedding the
  whole store.
- **Tagged, hybrid retrieval** — each durable memory is tagged on write (the
  fast rung assigns normalized topic/entity tags, e.g. *"drives a blue Subaru"*
  → `car, vehicle, transportation, subaru`). Recall then scores **semantic
  similarity + tag/keyword overlap + importance + recency**, and a tag match can
  *rescue* a memory the embedding ranked low. So *"what car do I drive?"* filters
  straight to the Subaru fact — the query word "car" matches the tag, even though
  the memory never says "car."
- **Recall → inject** — before routing, the agent injects the pinned core
  profile plus the top *durable* memories as "facts you know about Jake." After
  answering, it records the exchange as an `episode` (the activity log).
- **Auto-learn (opt-out)** — after answering, the agent extracts durable facts
  from what you said and feeds them through consolidation, so memory grows from
  normal conversation, not just explicit "remember X."
- **Scope: global vs conversation** — a memory is either **global** (a fact about
  Jake, in play everywhere) or **scoped** to one conversation (private to that
  thread). Recall = global + the active thread's memories. Extracted facts default
  global; episodes are scoped to their thread; you can store "this chat only" from
  the panel.
- **Delete + relationships** — memories can be deleted, and each one has *related*
  memories (derived from shared tags + embedding similarity — click a memory to
  see them). No graph DB: relationships are computed from the tags/vectors that
  already exist.
- **In the app** — the Memory panel shows the store live, has a pinned **core
  profile** editor, an **auto-learn** toggle, per-memory scope (🌐/💬) and delete,
  lets you add facts, and does semantic search; result cards show 🧠 N when memory
  shaped the answer.

Verified end-to-end: a fact stored once is used by both rungs, survives a
restart, and migrates a v1 store without loss.

### What we took from the state of the art

The store is single-user and on your own disk, so it takes the *ideas* from the
leading memory systems without their infrastructure (no graph DB, no extra
services). One honest note since Ollama went: memory writes used to be free and
private because a local model judged them. They are neither now — consolidation
is a paid call, and the candidate memory it sends is by definition the most
personal text in the system. That's why it runs on the fast rung, not the
expensive one (`CONTINUUM_REASONER=cloud` overrides).

| Idea | From | How Continuum does it |
|---|---|---|
| **Extraction** — learn facts from conversation, not just explicit saves | Mem0 | After answering, the **fast rung** pulls durable facts from what you said; they flow through consolidation. Opt-out toggle. |
| **Consolidation** — writes are `ADD`/`UPDATE`/`NOOP`, not blind appends | Mem0 | On each durable write, the **fast rung** judges the note against its nearest neighbours; near-identical notes short-circuit to `NOOP`. Dedups and resolves change. If the call fails, the caller falls back to a plain `ADD` — an outage costs tidiness, never information. |
| **Supersession** — outdated facts are retired, not deleted | Zep / Graphiti | `UPDATE` marks the old row `superseded_at`; recall sees only what's current, history is kept. |
| **Importance + recency ranking** | Generative Agents | Recall scores `relevance + importance + kind-aware recency decay`, so a defining fact outranks trivia and stale episodes sink. |
| **Tagged hybrid retrieval** | classic IR (hybrid dense+sparse) | Model-assigned tags on write; recall blends semantic + tag/keyword overlap, and a tag hit can rescue a low-embedding match. |
| **Pinned core memory** | Letta / MemGPT | A small `core` profile block, always injected, separate from the retrieved long tail. |
| **Episodic vs. semantic split** | classic cog-arch | Answers recall `fact`/`preference` only; `episode`s are the browsable log, never injected — so they don't add noise. |

Deliberately **not** built (yet): a knowledge graph and agent self-editing
memory tools. Overkill for one user today; the seams are in place to add them.

## Live state (the "now" half of the twin)

Memory is what Continuum knows *over time*; live state is what's true *this
second*. `continuum/state/` polls the machine every few seconds and models it in
two tiers — the same summary-plus-drill-down shape that keeps context windows sane:

- **`basic()`** — a compact snapshot injected into the agent every turn: which
  apps are open, how long each has been open, what's in focus (and for how long),
  plus CPU/RAM/battery. Cheap and small.
- **`inspect(target)`** — deep detail about one app on demand: exe, command line,
  memory, threads, open files, windows. This is the "request more" primitive.

Who calls `inspect`:
- **You**, by clicking an app in the Live State panel (drill-down).
- **Continuum**, autonomously — via a real **tool-use loop**. The model is given
  the question plus the basics and a set of tools — `inspect_app`,
  `list_open_apps`, `recall_memory` — and calls them as needed, so drill-down is
  the *model's* decision. `continuum/tools.py` used to host a ReAct loop driving
  those tools from a local llama3.1; it no longer does. The cloud tier reaches the
  same tools over **MCP** (served by the running app, same schemas, same gates,
  same audit trail) and runs its own loop, so what `tools.py` keeps is the half
  that was always the point: the definitions, the permission gates, and the
  logging.

  Verified: *"how much memory is Firefox using?"* → the agent calls
  `inspect_app(Firefox)` → *"490 MB."* With tools off, it only has the basic
  snapshot and correctly can't drill down. Toggle in the task console (`🔧`).

It's a desktop-device capability (`system-state`), so when other devices join the
fabric they can advertise and report their own state the same way.

### Agency lives in Continuum, not the provider

The tool loop is where Continuum *acts*, and the design point is deliberate: the
model only *decides* which of **Continuum's** tools to call; Continuum executes,
gates, and logs them. The provider gets exactly one tool plane and it's ours: the
CLI's own file and system tools are all denied, it runs from an empty sandbox
directory, and its per-project memory is wiped on startup (see `providers.py`).
A CLI agent left ungoverned reads your files and keeps its own hidden memory —
it once "recalled" a bike Jake never told it about, straight out of a test
script — so it's boxed in and handed Continuum's tools over MCP instead.

Tools split into two classes:
- **READ** (`inspect_app`, `list_open_apps`, `recall_memory`) — safe, always on.
- **ACTION** (`notify`, `open_url`, `open_app`, `schedule_reminder`,
  `schedule_task`) — *do* things, so they're gated behind an explicit **⚡ actions**
  toggle (on by default) and every one is logged as an `action.performed` event.

`run_shell` — arbitrary, un-sandboxed machine shell — is the sharp one, and it's
worth being precise about what guards it. It used to be fenced three ways; one of
those was "cloud tier only, so the easily-confused 8B never sees it," and with
the local tier gone every turn is a cloud turn, so that fence is gone with it.
What remains is two: its own gate (`CONTINUUM_SHELL`) and a SENSITIVE brief that
forbids running it as a helpful extra. There is no per-action confirmation dialog
— there's nowhere to prompt on a headless or cloud turn. Every call lands on the
bus as `action.performed`.

That's the shape of a trustworthy agent: Continuum owns the hands, and you hold
the permission switch.

> Note: the basic snapshot includes window titles, and every turn is a cloud turn
> now — so it goes to Claude on every turn, the same trust boundary as chatting
> with Claude directly. There is no local rung left to gate it behind.

## Conversations

The task console is a real chat now, not one-shots. Each thread has a stable id;
the last several turns are injected as another context block, so follow-ups work
("what's my parrot called?" → "Kiwi." from earlier in the thread). Threads are
persisted (`data/conversations.db`) and browsable — the 🕘 history list reopens
any past conversation with its full transcript, and each assistant turn stores
its metadata (tier, tools, 🧠/🖥️) so a reopened thread renders exactly as it
happened.

**The main thread vs. task break-offs.** There's one pinned, permanent
**🏠 main thread** — your day-to-day home. The app lands there by default, and
any message with no conversation goes there. **`＋ New`** breaks off a focused
**task conversation** for a specific job; it gets its own id, tags, and scoped
memory. The main thread is the ongoing spine; tasks are offshoots you can close.
Main is a singleton (`kind='main'`), pinned to the top of history, and can't be
deleted or topic-tagged (it's not about one thing).

Every conversation is saved and **tagged** (the fast rung tags it from your
opening message), which powers a derived graph: **related conversations** (shared
tags), the **memories that originated in / are scoped to** a conversation, and a
memory's **related memories**. No graph database — the edges are computed from
tags, embeddings, and origin links.

Conversations are the verbatim *short-term* memory of one thread; `memory/`
remains the distilled, cross-thread semantic layer. A message flows through the
full stack: conversation history + core profile + recalled facts (global + this
thread's) + live state + tool lookups all compose into the prompt the router
answers.

## Scheduling & the heartbeat

Continuum has a **pulse**: a background heartbeat that beats every ~15s. Each beat
it checks the **scheduler** (`continuum/scheduler.py`, persisted in
`data/scheduler.db`) and fires anything due. Two job kinds:

- **reminder** — pop a notification at a time (once or recurring).
- **prompt** — run a prompt through the agent at a time (e.g. *"every morning,
  suggest my focus"*), which lands in the main thread and notifies you.

You schedule things just by asking — with ⚡ actions on, *"remind me in 30 min to
stretch"* makes the agent call `schedule_reminder`, and the heartbeat fires it
later. The **⏰ Scheduled** panel lists upcoming jobs with a live "fires in"
countdown and a cancel button.

### Proactivity

On its slower cadence, the heartbeat also asks the **proactive engine**
(`continuum/proactive.py`) *"anything worth saying right now?"* — and it usually
answers no. When it does speak, it's grounded strictly in what Continuum can see
(live state + what it knows about you + your schedule); it never invents. A
surfaced thought arrives three ways: a desktop notification, a message in your
main thread (tagged 🌱), and a `proactive.surfaced` event.

It's built to *not* be annoying: **off by default** (🌱 toggle / `CONTINUUM_PROACTIVE`),
evaluates only every few minutes, has a 30-minute cooldown, respects quiet hours
(23:00–08:00), and de-dupes against what it just said. The 💭 button forces a
"what's on your mind?" check on demand.

Example — with one file focused for hours it offered: *"Nearly three hours deep
in that proactive.py file — a good moment to stand up and stretch."* With nothing
notable, it stays silent. Deterministic routines (battery low, calendar) drop in
beside the model check the same way.

## Autostart on login

```powershell
powershell -ExecutionPolicy Bypass -File scripts\continuum-autostart.ps1
```

## Roadmap (near-term)

- **More tools** — the tool loop is a clean seam; add `open_app`, `focus_window`,
  `read_file`, calendar/email lookups, etc. Focus-history and active-file/URL
  awareness would make the state itself richer.
- **Memory depth** — recall-weight tuning, extraction quality (sharper reasoner
  model on writes), optional relational/graph layer if it earns its keep.
- **Conversation depth** — model-generated titles, streaming replies, search
  across threads, and summarizing long threads into memory.
- **NATS transport** — swap the in-process bus; add a phone device.
- **Home Assistant** — consume its event stream, call its services.
- **UI automation** — computer-use with cached, self-healing action trajectories.
- **Routines** — proactive behaviors triggered by context.
