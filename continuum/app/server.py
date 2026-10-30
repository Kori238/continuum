"""The GUI server — FastAPI + WebSocket over the live bus.

Boots the fabric (bus, agent, desktop device) in-process, then:
  - subscribes a fan-out handler to every topic and pushes each event to all
    connected websocket clients (the live event stream), and
  - accepts task submissions from the UI and publishes them as task.request.

The UI matches a result back to its request via a client-supplied correlation
id, which we use as the task.request event id; the agent echoes it back as
`request_id` on task.result.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date as _date
from pathlib import Path
from typing import Any, Optional

from fastapi import Body, FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.websockets import WebSocketState   # not re-exported at the top level

from ..agent import Agent
from ..bus import Bus
from ..config import Config
from .. import deskops
from ..devices.desktop import (
    COMMANDS as DESKTOP_COMMANDS, DesktopDevice, DesktopLink, StateSource,
)
from ..devices.phone import COMMANDS as PHONE_COMMANDS, PhoneDevice
from ..events import Event
import asyncio

from ..memory import Extractor, MemoryReasoner, MemoryStore
from ..memory.embed import Embedder
from ..conversations import ConversationStore
from ..messagelog import MessageLog, MessageCollector, _key as _phone_key
from ..proactive import ProactiveEngine
from ..routines import RoutineStore
from ..workouts import WorkoutStore
from ..activity import ActivityStore
from ..budget import BudgetStore, CATEGORIES as BUDGET_CATEGORIES
from .. import enforce
from ..nutrition import NutritionStore
from ..body import BodyStore
from ..health import HealthStore
from ..flashcards import FlashcardStore
from ..vocab import VocabService, VocabError
from ..ledger import ActionLedger
from ..attachments import AttachmentStore, _MAX_BYTES as MAX_UPLOAD_BYTES
from ..integrations.foods import FoodClient
from ..scheduler import Scheduler
from ..state import StateTracker
from ..tools import ToolLoop
from . import mcp
from .security import Guard, allowed_logins, allowed_origin_hosts

log = logging.getLogger("continuum.app")
STATIC = Path(__file__).parent / "static"

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _day(value: Any, field: str = "date") -> Optional[str]:
    """Validate a caller-supplied calendar day, returning None for "today".

    These strings are stored verbatim and used as part of the primary key in
    completions/anchors/entries. Nothing downstream checks them — the stores do
    `date_str or _today_str()`, so any truthy string is accepted. An unvalidated
    value ('abc', '2026-13-45') becomes a permanent row on a day that no
    calendar view can reach and no date-nav can scroll to: invisible in the UI,
    but still counted by anything that aggregates across days.

    Raises ValueError so the endpoint can answer 400 instead of writing it.
    """
    if value is None or value == "":
        return None
    s = str(value).strip()
    if not _DAY_RE.match(s):
        raise ValueError(f"{field} must be YYYY-MM-DD, got {value!r}")
    try:
        _date.fromisoformat(s)  # rejects 2026-02-30, month 13, etc.
    except ValueError:
        raise ValueError(f"{field} is not a real date: {s}") from None
    return s


# Every asset ships under a fixed, unversioned name — index.html, app.js,
# app.css — so a client that is permitted to cache one has no way to discover
# that a newer build exists. With no Cache-Control at all, Starlette sends only
# ETag/Last-Modified and the heuristic freshness rules let Chrome and the phone
# WebView reuse a stored copy *without asking*, for hours. That is how a
# frontend fix could land, get deployed, and appear to have done nothing.
#
# `no-cache` is not `no-store`: the copy is still kept, but it must be
# revalidated every time, so the cheap 304 is preserved while a changed file is
# always picked up on the next load.
REVALIDATE = {"Cache-Control": "no-cache"}


class _RevalidatingStatic(StaticFiles):
    """StaticFiles, but every file must be revalidated. Starlette exposes no
    cache-control option, and its 304 path builds its own response, so the
    header is stamped on whatever `file_response` returns."""

    def file_response(self, *args: Any, **kwargs: Any) -> Any:
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp
# The phone is sideloaded — no store to push to — so it updates by pulling
# whatever APK build.sh last produced. build.sh writes version.json next to the
# APK so the manifest and the binary always describe the same build.
REPO_ROOT = Path(__file__).resolve().parents[2]
APK_PATH = REPO_ROOT / "android" / "dist" / "continuum-phone.apk"


class Hub:
    """Fans bus events out to every connected websocket client."""

    # How long one client gets to accept one event before we give up on it.
    # A socket that is *stalled* rather than closed — a phone that walked out of
    # signal, a laptop with the lid shut — still looks perfectly healthy from
    # here: its TCP send buffer fills and `send_json` then blocks on transport
    # drain until the OS finally gives up, which on Windows is tens of seconds.
    # Nothing about a few seconds of silence from a dashboard is worth waiting
    # for, so this is generous rather than precise.
    SEND_TIMEOUT = 3.0

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        # Strong refs to in-flight hang-up tasks. A bare `create_task` whose
        # result nobody holds can be garbage-collected mid-await.
        self._closing: set[asyncio.Task[None]] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    async def broadcast(self, message: dict[str, Any]) -> None:
        """Push one event to every client, concurrently and with a deadline.

        This used to be a plain sequential loop, which made one wedged client
        everybody's problem: `bus.publish` gathers over its subscribers and the
        fan-out handler is one of them, so a blocked `send_json` froze the
        agent's turn, the state loop and the phone handler alike — and then did
        it again on the next event. Fanning out concurrently means a slow client
        only costs us its own timeout, and the timeout means it doesn't even
        cost us that for long. A client that can't take an event inside the
        deadline is dropped: it's already missed the stream, and the browser
        reconnects on its own, whereas stalling every publisher to wait for it
        is the trade we can't afford.

        "The browser reconnects on its own" is only true if we actually hang up
        — see `_hang_up`.
        """
        clients = list(self._clients)
        if not clients:
            return

        async def _send(ws: WebSocket) -> None:
            await asyncio.wait_for(ws.send_json(message), timeout=self.SEND_TIMEOUT)

        results = await asyncio.gather(*(_send(ws) for ws in clients),
                                       return_exceptions=True)
        for ws, res in zip(clients, results):
            if isinstance(res, BaseException):
                self._clients.discard(ws)
                self._hang_up_soon(ws)
                # Not `res or "timed out"`: exceptions define no `__bool__`, so
                # they are always truthy and that fallback was unreachable —
                # while `str(TimeoutError())` is `""`, so the one case most
                # worth diagnosing logged nothing after the colon. `%r` always
                # says something.
                log.info("dropped websocket client (%s): %r",
                         type(res).__name__, res)

    def _hang_up_soon(self, ws: WebSocket) -> None:
        """Actually close a socket we've just evicted from the fan-out set.

        Discarding it from `_clients` alone left it open, and that was a bug
        with a very long tail: the route handler stays parked in
        `receive_json()`, so its `finally` never runs, and the *browser* never
        sees a close — `onclose` is the only thing that calls
        `scheduleReconnect()`. The tab sat there showing "connected" while
        receiving nothing. Worse, it could still send: the user submits a task,
        the agent runs the whole turn, and the `task.result` broadcast goes to
        every client except this one, so the bubble spins forever and
        `reconcileAfterReconnect()` — the only thing that clears it — can never
        run. Only a manual reload recovered. So hang up for real and let the
        client's own reconnect path do its job.

        Fire-and-forget with its own deadline, because a client that just blew
        a 3s send deadline is exactly the kind that will also stall the close
        handshake, and `broadcast` must not block on it.
        """
        async def _close() -> None:
            try:
                await asyncio.wait_for(ws.close(code=1011), timeout=self.SEND_TIMEOUT)
            except Exception:
                pass  # already gone, or wedged past saving — it's out of the set either way

        task = asyncio.create_task(_close())
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)


def _event_to_dict(ev: Event) -> dict[str, Any]:
    return {
        "topic": ev.topic,
        "source": ev.source,
        "id": ev.id,
        "ts": ev.ts,
        "payload": ev.payload,
    }


def _apk_version() -> dict[str, Any]:
    """What the served phone APK is. version.json (written by build.sh) is
    authoritative; fall back to the version declared in build.gradle.kts so this
    still answers sensibly before a build has written the manifest. Size and
    build time come from the APK on disk, so they always match the real file."""
    info: dict[str, Any] = {"available": APK_PATH.exists()}
    vjson = APK_PATH.with_name("version.json")
    if vjson.exists():
        try:
            info.update(json.loads(vjson.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            log.exception("unreadable version.json")
    else:
        gradle = REPO_ROOT / "android" / "app" / "build.gradle.kts"
        try:
            txt = gradle.read_text(encoding="utf-8")
        except OSError:
            txt = ""
        mc = re.search(r"versionCode\s*=\s*(\d+)", txt)
        mn = re.search(r'versionName\s*=\s*"([^"]+)"', txt)
        if mc:
            info["versionCode"] = int(mc.group(1))
        if mn:
            info["versionName"] = mn.group(1)
    if APK_PATH.exists():
        st = APK_PATH.stat()
        info["size"] = st.st_size
        info["builtAt"] = int(st.st_mtime)
    return info


def create_app() -> FastAPI:
    app = FastAPI(title="Continuum Control")
    cfg = Config.load()
    bus = Bus()
    hub = Hub()
    reasoner = MemoryReasoner(cfg)
    memory = MemoryStore(cfg, reasoner=reasoner)
    state = StateTracker()
    conversations = ConversationStore(cfg)
    scheduler = Scheduler(cfg)
    messagelog = MessageLog(cfg)
    collector = MessageCollector(messagelog)
    routines = RoutineStore(cfg)
    workouts = WorkoutStore(cfg)
    activity = ActivityStore(cfg)
    budget = BudgetStore(cfg)
    nutrition = NutritionStore(cfg)
    body = BodyStore(cfg)
    health = HealthStore(cfg)
    flashcards = FlashcardStore(cfg)
    # Import and text-to-speech. Both optional: with no keys the tab still
    # studies what is stored, and only those two buttons go dark.
    vocab = VocabService(cfg, flashcards)
    ledger = ActionLedger(cfg)
    foods = FoodClient(cfg)
    phone = PhoneDevice(bus)
    # The hub's view of a desktop reporting from elsewhere. Idle and harmless
    # until something dials `/desktop/ws`; while nothing has, this process polls
    # its own machine exactly as it always did.
    desktop_link = DesktopLink(bus)
    # What everything *except* `_state_loop` should read. The loop keeps the raw
    # tracker because it owns the polling (`state.poll`, `state.poll_interval`);
    # everyone below only ever asks questions, and must ask them of whichever
    # machine Jake is actually at. See StateSource for what went wrong without it.
    live = StateSource(state, desktop_link)
    proactive = ProactiveEngine(cfg, state=live, memory=memory,
                                scheduler=scheduler, conversations=conversations,
                                routines=routines, nutrition=nutrition,
                                body=body, workouts=workouts, phone=phone)
    attachments = AttachmentStore(cfg.uploads_dir)
    tool_loop = ToolLoop(cfg, state=live, memory=memory, bus=bus, scheduler=scheduler,
                         messagelog=messagelog, routines=routines, phone=phone,
                         workouts=workouts, activity=activity, nutrition=nutrition,
                         body=body, health=health, flashcards=flashcards,
                         conversations=conversations, ledger=ledger,
                         attachments=attachments)
    agent = Agent(bus, cfg, memory, extractor=Extractor(cfg), state=live,
                  tools=tool_loop, conversations=conversations, reasoner=reasoner,
                  routines=routines, phone=phone)
    agent.attach()

    # Serve Continuum's own tools over MCP. This is the cloud tier's only hand-
    # hold on the machine: the Claude CLI runs with every file/system tool denied,
    # so without this it can read injected context but never act on the routine.
    mcp.install(app, tool_loop)

    @app.post("/api/upload")
    async def upload(body: dict[str, Any] = Body(default={})) -> Any:
        """Store an image Jake attached in the chat. Takes base64 in JSON
        ({name, data}) so it needs no multipart dependency — the browser reads the
        file client-side and posts the bytes. Returns {id, name, mime, url}; the
        composer holds that until the task is sent, then references it by id."""
        import base64
        raw = body.get("data") or ""
        if not isinstance(raw, (str, bytes)):
            return JSONResponse({"error": "could not decode image data"}, status_code=400)
        if isinstance(raw, str) and raw[:5] == "data:" and "," in raw:
            raw = raw.split(",", 1)[1]  # tolerate a full data: URL
        # Size-check the *encoded* string, before decoding it. The store enforces
        # the real limit on the decoded bytes, but by then we've already paid for
        # the decode — a 200 MB paste cost a quarter-second of frozen event loop
        # and the better part of half a gigabyte of allocation just to be told no.
        # Base64 is 4 chars per 3 bytes, so the encoded length is an upper bound
        # on the decoded size; the slack absorbs padding and any line breaks a
        # non-browser client might have wrapped it with.
        if len(raw) > (MAX_UPLOAD_BYTES + 2) // 3 * 4 + 1024:
            return JSONResponse(
                {"error": f"image too large (max {MAX_UPLOAD_BYTES} bytes)"},
                status_code=400)
        # Both halves are CPU/disk work in an async handler, and a legitimate
        # phone photo is still a few MB of decode plus a write and an fsync, so
        # they go to a thread rather than holding the loop for everyone else.
        try:
            data = await asyncio.to_thread(base64.b64decode, raw or "", validate=False)
        except Exception:
            return JSONResponse({"error": "could not decode image data"}, status_code=400)
        try:
            meta = await asyncio.to_thread(attachments.save, data,
                                           name=str(body.get("name") or ""))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return {"id": meta["id"], "name": meta["name"], "mime": meta["mime"],
                "size": meta["size"], "url": f"/api/upload/{meta['id']}"}

    @app.get("/api/upload/{aid}")
    async def upload_file(aid: str) -> Any:
        """Serve a stored attachment back — the thumbnail in the composer, and the
        inline image when a thread is reopened. Jailed to the uploads dir by the
        store: a non-hex id resolves to nothing."""
        p = await asyncio.to_thread(attachments.path, aid)
        if not p:
            return JSONResponse({"error": "not found"}, status_code=404)
        meta = await asyncio.to_thread(attachments.meta, aid) or {}
        return FileResponse(p, media_type=meta.get("mime") or "application/octet-stream")

    # Remember registered devices so a late-connecting UI can populate its panel.
    # Bounded, because one of these keys isn't ours: a phone registers as
    # `phone:<name>` using the name from its own hello, so every distinct name it
    # ever sends would otherwise live here forever and ride out in every
    # /api/status poll.
    #
    # The cap is per device class (the part before the ':'), not global. A global
    # cap evicting the oldest entry does the exactly wrong thing: the desktop
    # registers once at startup and never again, so it is permanently the oldest,
    # and a burst of junk phone names would push the real device out of the panel
    # while the junk stayed. Per class, phone noise can only cost other phones.
    MAX_PER_CLASS = 8
    devices: dict[str, dict[str, Any]] = {}

    async def _record_device(ev: Event) -> None:
        devices[ev.source] = {
            "name": ev.source,
            "capabilities": ev.payload.get("capabilities", []),
            "os": ev.payload.get("os"),
            "ts": ev.ts,
        }
        cls = ev.source.split(":", 1)[0]
        siblings = [k for k in devices if k.split(":", 1)[0] == cls]
        for stale in siblings[:max(0, len(siblings) - MAX_PER_CLASS)]:
            log.info("device registry: forgetting %r to make room for %r", stale, ev.source)
            del devices[stale]

    async def _record_phone(ev: Event) -> None:
        """Persist phone reports into the activity timeline. `phone.screen`
        opens/closes a screen-on session; any other `phone.*` report extends an
        open one. Kept off the event loop — it's a small SQLite write."""
        kind = ev.topic.split(".", 1)[1] if "." in ev.topic else ev.topic
        await asyncio.to_thread(activity.on_phone_signal, kind, ev.payload or {}, ev.ts)

    async def _phone_offline(ev: Event) -> None:
        await asyncio.to_thread(activity.close_phone)

    async def _phone_steps(ev: Event) -> None:
        """A step count pushed from the phone's hardware step counter (the
        companion app reads TYPE_STEP_COUNTER and reports today's total) → the
        body store, tagged as a phone reading. The count is the running daily
        total, so set_steps replaces the day."""
        p = ev.payload or {}
        count = p.get("count", p.get("steps"))
        if count is None:
            return
        await asyncio.to_thread(
            body.set_steps, count, day=p.get("day") or None,
            source=str(p.get("source") or "phone"))

    async def _phone_weight(ev: Event) -> None:
        """A weigh-in pushed from the phone's Health Connect reader → the body
        store (the companion app reads WeightRecord, and from a smart scale like
        the Renpho the BodyFatRecord + LeanBodyMassRecord written alongside it).
        set_measurement COALESCE-preserves the day's other fields, so a scale
        report never nulls a height typed in earlier, and re-sending the same
        day's reading is idempotent (weight replaces, latest wins)."""
        p = ev.payload or {}
        if not any(k in p for k in ("weight_kg", "body_fat_pct", "muscle_mass_kg")):
            return
        await asyncio.to_thread(
            body.set_measurement, weight_kg=p.get("weight_kg"),
            body_fat_pct=p.get("body_fat_pct"), muscle_mass_kg=p.get("muscle_mass_kg"),
            day=p.get("day") or None)
        if p.get("weight_kg"):
            await tool_loop.autotick_weight()   # a synced weigh-in ticks 'Log weight' off

    async def _phone_workout(ev: Event) -> None:
        """An exercise session pushed from the phone's Health Connect reader → the
        workout store, deduped by training_id (the companion app reads
        ExerciseSessionRecord and reports each as a `workout`). HC sessions carry
        no per-set breakdown, so we mark detail fetched (empty): that keeps them
        out of the Speediance per-set detail probe and lets them sit in the same
        store alongside Speediance sessions — a real 'workout inbox', not a
        machine-only log."""
        p = ev.payload or {}
        tid = str(p.get("training_id") or "")
        if not tid:
            return

        def _store() -> None:
            workouts.upsert_session(p)
            workouts.store_sets(tid, [])   # no sets; also marks detail_synced

        await asyncio.to_thread(_store)
        await tool_loop.autotick_workout()   # a session today ticks 'Workout' off

    async def _phone_health(ev: Event) -> None:
        """A day's recovery metrics pushed from the phone's Health Connect reader
        → the health store (the companion app reads Sleep/RestingHeartRate/HRV/
        SpO2 records, aggregates them per day and reports each as `health`). The
        payload carries only the metrics it has; upsert_day COALESCE-preserves
        the rest, so a spot vitals report never wipes the night's sleep."""
        p = ev.payload or {}
        if not any(k in p for k in ("asleep_min", "resting_hr", "hrv_ms", "spo2",
                                    "respiratory_rate", "in_bed_min")):
            return
        await asyncio.to_thread(health.upsert_day, p)

    async def _workout_autotick(ev: Event) -> None:
        """A UI/tool Speediance sync (workouts.synced) ticks 'Workout' off the
        routine when a session landed today — the same auto-tick the phone HC
        path and the refresh tool apply, so it fires however a workout arrives."""
        try:
            await tool_loop.autotick_workout()
        except Exception:
            log.exception("workout autotick")

    async def _fanout(ev: Event) -> None:
        await hub.broadcast({"type": "event", "event": _event_to_dict(ev)})

    async def _deferred_restart(ev: Event) -> None:
        """A turn that edits the source usually wants the app relaunched — but the
        relaunch kills this process, so doing it inside the turn destroys the very
        reply that explains the change. The answer is already written to the
        conversation and on its way to the socket by now; give it a beat, then go."""
        if not tool_loop.take_restart():
            return

        async def _go() -> None:
            await asyncio.sleep(2.0)  # let the reply flush to every connected client
            await hub.broadcast({"type": "app_restarting"})
            await asyncio.sleep(0.3)
            out = await asyncio.to_thread(tool_loop.dev.restart)
            log.info("deferred restart: %s", out)

        asyncio.create_task(_go())

    async def _record_ledger(ev: Event) -> None:
        """Persist every action — performed or refused — into the durable ledger.
        Transient bus events fan out and vanish; this is the half that survives, so
        'what did Continuum actually do today?' is answerable after the fact, and
        the notification gate's refusals leave a trace too."""
        p = ev.payload or {}
        # To a thread: this is subscribed to both action topics, so it's on the
        # hot path, and a ledger write is a SQLite INSERT plus the commit's
        # fsync. Doing that inline stalls the loop — and because `bus.publish`
        # gathers over subscribers, it stalls whoever published the action too.
        await asyncio.to_thread(ledger.record, action=p.get("action", ev.topic),
                                args=p.get("args"), result=str(p.get("result", "")),
                                allowed=(ev.topic != "action.refused"),
                                reason=str(p.get("reason", "")),
                                request_id=str(p.get("request_id", "")),
                                source=ev.source or "")

    bus.subscribe("*", _fanout)
    bus.subscribe("device.register", _record_device)
    bus.subscribe("task.result", _deferred_restart)
    bus.subscribe("workouts.synced", _workout_autotick)  # UI/tool sync ticks 'Workout' off
    bus.subscribe("phone.*", _record_phone)      # phone screen sessions → activity timeline
    bus.subscribe("phone.steps", _phone_steps)   # phone health feed → body step count
    bus.subscribe("phone.weight", _phone_weight)    # phone health feed → weight + composition
    bus.subscribe("phone.workout", _phone_workout)  # phone health feed → workout store
    bus.subscribe("phone.health", _phone_health)    # phone health feed → recovery store
    bus.subscribe("device.offline", _phone_offline)
    bus.subscribe("action.performed", _record_ledger)  # durable audit trail
    bus.subscribe("action.refused", _record_ledger)    # + the notification gate's refusals

    conversations.main()  # ensure the day-to-day main thread exists

    @app.on_event("startup")
    async def _startup() -> None:
        await DesktopDevice(bus).register()
        asyncio.create_task(_state_loop())
        asyncio.create_task(_heartbeat_loop())
        # Only where there is something to collect. Both of the collector's
        # feeds are Windows-only — the toast listener and UIAutomation scraping
        # of the chat windows — so on the hub this was two empty scrapes every
        # 8 seconds, forever, filling nothing. The desktop client runs its own
        # collector and relays the rows here; see `_ingest_desktop_messages`.
        if deskops.local_desk():
            asyncio.create_task(collector.run())
        else:
            log.info("message collection deferred to the desktop client — "
                     "this host cannot see Jake's notifications")
        # Backfill embeddings for any turns not yet indexed for search — older
        # threads, or anything written while the embedder was unavailable.
        # Retried on the next boot for whatever is still missing.
        asyncio.create_task(conversations.backfill())

    async def _heartbeat_loop() -> None:
        """Continuum's pulse — each beat fires due jobs, and on its slower cadence
        lets Continuum consider speaking proactively."""
        while True:
            try:
                await scheduler.tick(bus)
                await proactive.maybe_check(bus)
            except Exception:
                log.exception("heartbeat tick failed")
            await asyncio.sleep(cfg.heartbeat_s)

    @app.get("/api/schedule")
    async def get_schedule() -> dict[str, Any]:
        return {"jobs": [j.as_dict() for j in scheduler.list()]}

    @app.delete("/api/schedule/{job_id}")
    async def cancel_schedule(job_id: int) -> dict[str, Any]:
        ok = scheduler.cancel(job_id)
        if ok:
            await bus.publish(Event(topic="schedule.changed", source="app", payload={}))
        return {"ok": ok}

    async def _state_loop() -> None:
        """Poll live machine state and push it to the UI (kept off the event
        stream — it's a live view, not a log)."""
        while True:
            try:
                if desktop_link.connected:
                    # A desktop is reporting from elsewhere, so this machine is
                    # the hub and has nothing worth polling — its own foreground
                    # window is a VM's, not Jake's. `basic()` returns {} when the
                    # reports have gone quiet, which stops the clock rather than
                    # billing him for silence; see DesktopLink.basic().
                    b = desktop_link.basic()
                else:
                    await asyncio.to_thread(state.poll)
                    b = state.basic()
                await hub.broadcast({"type": "state", "state": b})
                # Persist the foreground app into the durable timeline — but only
                # while Jake is present, so a window left in front while he's away
                # doesn't count as attention (same rule the tracker uses).
                present = (b.get("presence") or {}).get("present", True)
                fg = b.get("foreground") if present else None
                await asyncio.to_thread(activity.note_desktop, fg)
                # …then ask the budget what it makes of that. Deliberately after
                # the write, so the verdict sees the second that just elapsed
                # rather than trailing the timeline by one poll — at 95% of an
                # allowance, five seconds of drift is a warning that fires on the
                # wrong side of a threshold.
                verdict = await asyncio.to_thread(budget.evaluate, activity, fg)
                if verdict["actions"]:
                    await enforce.apply(verdict["actions"], phone=phone,
                                        desktop=desktop_link)
                    await hub.broadcast({"type": "budget", "budget": verdict})
            except Exception:
                log.exception("state poll failed")
            await asyncio.sleep(state.poll_interval)

    @app.get("/api/conversations")
    async def list_conversations() -> dict[str, Any]:
        return {"conversations": await asyncio.to_thread(conversations.list)}

    @app.get("/api/conversations/search")
    async def search_conversations(q: str = "", k: int = 8) -> dict[str, Any]:
        """Semantic search over past turns, indexed at their source in
        conversations.db (this replaced the old memory.db 'episode' copies)."""
        hits = await conversations.search(q, k=k) if q.strip() else []
        return {"query": q, "results": hits}

    @app.get("/api/conversations/{conv_id}")
    async def get_conversation(conv_id: int) -> Any:
        conv = await asyncio.to_thread(conversations.get, conv_id)
        if not conv:
            return JSONResponse({"error": "not found"}, status_code=404)
        return conv

    @app.delete("/api/conversations/{conv_id}")
    async def delete_conversation(conv_id: int) -> dict[str, Any]:
        return {"ok": await asyncio.to_thread(conversations.delete, conv_id)}  # False for the main thread

    @app.get("/api/whoami")
    async def whoami(request: Request) -> dict[str, Any]:
        """Who does Continuum think you are? Local sessions are anonymous; tailnet
        ones carry an identity Tailscale vouched for. Also the honest way to test
        that the identity header is real and not client-supplied."""
        login = request.headers.get("Tailscale-User-Login", "")
        return {
            "login": login,
            "name": request.headers.get("Tailscale-User-Name", ""),
            "via": "tailnet" if login else "local",
            "client": request.client.host if request.client else None,
        }

    @app.post("/api/answer")
    async def answer_question(request: Request) -> dict[str, Any]:
        """Jake's reply to an ask_user question. The turn that asked is parked on a
        Future (in the MCP handler for a cloud turn, or the local gather() loop);
        this resolves it so that same turn continues. It's a plain HTTP POST on
        purpose: the WS receive loop is blocked inside the running turn, so the
        answer can't come back over that socket — a separate request handler can.
        The question was rendered from the `question.asked` event the UI already
        got over the socket's fan-out (sending is independent of that block)."""
        try:
            body = await request.json()
        except Exception:
            body = {}
        qid = str(body.get("id") or "")
        choice = body.get("choice")
        if choice is None:
            choice = body.get("text") or ""
        matched = tool_loop.answer_question(qid, str(choice))
        return {"ok": matched}

    @app.get("/api/messages")
    async def get_messages(limit: int = 40, app: str = "", q: str = "") -> dict[str, Any]:
        if q:
            rows = await asyncio.to_thread(messagelog.search, q, limit)
        else:
            rows = await asyncio.to_thread(messagelog.recent, limit, app or None)
        return {"messages": rows}

    @app.get("/api/routines")
    async def get_routines(date: str = "") -> dict[str, Any]:
        # Two independent reads — gather so they share one round trip, not two.
        today, every = await asyncio.gather(
            asyncio.to_thread(routines.today, date or None),
            asyncio.to_thread(routines.all))
        return {"today": today, "all": every}

    @app.get("/api/routines/performance")
    async def get_routine_performance(days: int = 30) -> dict[str, Any]:
        return await asyncio.to_thread(routines.performance, days)

    @app.post("/api/routines")
    async def add_routine(body: dict[str, Any] = Body(...)) -> Any:
        try:
            rid = await asyncio.to_thread(
                routines.add,
                body.get("title", ""), body.get("time", ""), body.get("days", "daily"),
                body.get("kind", "habit"), body.get("note", ""), body.get("section", "anytime"),
                repeat_days=body.get("repeat_days", 0))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return {"ok": True, "id": rid}

    @app.patch("/api/routines/{rid}")
    async def patch_routine(rid: int, body: dict[str, Any] = Body(...)) -> Any:
        # The id is the path; drop it from the body before splatting. A client
        # that round-trips a routine object it just read — which is the obvious
        # way to write an edit form — sends the identity fields back too, and
        # `update(rid, rid=...)` is a TypeError, i.e. a 500 for a request that
        # was perfectly reasonable. update() already discards every other key it
        # doesn't recognise, so this just makes the id behave like the rest.
        fields = {k: v for k, v in body.items() if k not in ("rid", "id")}
        try:
            return {"ok": await asyncio.to_thread(routines.update, rid, **fields)}
        except ValueError as exc:
            # update() only started raising when `days` stopped falling back to
            # "daily", so this route answered 500 with an empty body for input
            # POST /api/routines had always answered 400 for. Every other route
            # that reaches a raiser already converts it here; add, add_subtask
            # and add_todo were simply the only raisers until now.
            return JSONResponse({"error": str(exc)}, status_code=400)

    @app.delete("/api/routines/{rid}")
    async def delete_routine(rid: int) -> dict[str, Any]:
        return {"ok": await asyncio.to_thread(routines.delete, rid)}

    @app.post("/api/routines/{rid}/done")
    async def done_routine(rid: int, body: dict[str, Any] = Body(default={})) -> Any:
        try:
            day = _day(body.get("date"))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return {"ok": await asyncio.to_thread(routines.set_done, rid, bool(body.get("done", True)), day)}

    @app.post("/api/routines/{rid}/status")
    async def status_routine(rid: int, body: dict[str, Any] = Body(default={})) -> Any:
        try:
            day = _day(body.get("date"))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return {"ok": await asyncio.to_thread(routines.set_status, rid, str(body.get("status", "none")), day)}

    @app.post("/api/routines/start")
    async def start_routine_ep(body: dict[str, Any] = Body(default={})) -> Any:
        """Stamp (or clear) the day's start for a section, so its time-relative
        items get concrete due-times counted from now."""
        section = str(body.get("section") or "morning")
        try:
            day = _day(body.get("date"))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if body.get("clear"):
            await asyncio.to_thread(routines.clear_anchor, section, day)
            return {"ok": True, "started": False, "today": await asyncio.to_thread(routines.today)}
        ts = await asyncio.to_thread(routines.start_routine, section, day)
        return {"ok": True, "started": True, "ts": ts, "today": await asyncio.to_thread(routines.today)}

    # ---- subtasks ----
    @app.post("/api/routines/{rid}/subtasks")
    async def add_subtask(rid: int, body: dict[str, Any] = Body(...)) -> Any:
        try:
            sid = await asyncio.to_thread(routines.add_subtask, rid, body.get("title", ""))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return {"ok": True, "id": sid}

    @app.post("/api/subtasks/{sid}/done")
    async def done_subtask(sid: int, body: dict[str, Any] = Body(default={})) -> Any:
        try:
            day = _day(body.get("date"))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return {"ok": await asyncio.to_thread(
            routines.set_subtask_done, sid, bool(body.get("done", True)), day)}

    @app.delete("/api/subtasks/{sid}")
    async def delete_subtask(sid: int) -> dict[str, Any]:
        return {"ok": await asyncio.to_thread(routines.delete_subtask, sid)}

    # ---- to-dos ----
    @app.get("/api/todos")
    async def get_todos() -> dict[str, Any]:
        return {"todos": await asyncio.to_thread(routines.todos)}

    @app.post("/api/todos")
    async def add_todo(body: dict[str, Any] = Body(...)) -> Any:
        try:
            tid = await asyncio.to_thread(routines.add_todo, body.get("title", ""), body.get("note", ""))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return {"ok": True, "id": tid}

    @app.post("/api/todos/{tid}/done")
    async def done_todo(tid: int, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        return {"ok": await asyncio.to_thread(routines.set_todo_done, tid, bool(body.get("done", True)))}

    @app.delete("/api/todos/{tid}")
    async def delete_todo(tid: int) -> dict[str, Any]:
        return {"ok": await asyncio.to_thread(routines.delete_todo, tid)}

    # ---- workouts (Speediance) ----
    # Non-AI explorer: the Workouts tab reads these directly. Specific paths are
    # declared before /{training_id} so they aren't captured by the path param.
    @app.get("/api/workouts")
    async def get_workouts(limit: int = 20) -> dict[str, Any]:
        sessions, count, synced = await asyncio.gather(
            asyncio.to_thread(workouts.recent, limit),
            asyncio.to_thread(workouts.count),
            asyncio.to_thread(workouts.last_synced))
        return {"sessions": sessions, "count": count, "last_synced": synced,
                "configured": bool(cfg.speediance_email and cfg.speediance_password)}

    @app.get("/api/workouts/stats")
    async def get_workout_stats(days: int = 90) -> dict[str, Any]:
        return await asyncio.to_thread(workouts.stats, days)

    @app.get("/api/workouts/exercises")
    async def get_workout_exercises() -> dict[str, Any]:
        return {"exercises": await asyncio.to_thread(workouts.exercises)}

    @app.get("/api/workouts/exercise")
    async def get_exercise_progress(name: str) -> dict[str, Any]:
        return await asyncio.to_thread(workouts.exercise_history, name)

    @app.post("/api/workouts/sync")
    async def sync_workouts(body: dict[str, Any] = Body(default={})) -> Any:
        """Pull the latest from Speediance into the store. Read-only against the
        account; blocking network work runs off the event loop.

        Three failures, three codes, on purpose: Speediance being down (502) is
        not Continuum being broken (500) and neither is Jake never having entered
        his credentials (400). All three used to be HTTP 200."""
        if not (cfg.speediance_email and cfg.speediance_password):
            return JSONResponse({"error": "Speediance credentials not configured"},
                                status_code=400)
        from ..integrations.speediance import SpeedianceClient, SpeedianceError
        client = SpeedianceClient(cfg.speediance_email, cfg.speediance_password,
                                  cfg.speediance_region)
        try:
            summary = await asyncio.to_thread(workouts.sync, client, int(body.get("days") or 365))
        except SpeedianceError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        except Exception as exc:
            log.exception("workout sync failed")
            return JSONResponse({"error": f"sync failed: {exc}"}, status_code=500)
        await bus.publish(Event(topic="workouts.synced", source="app", payload=summary))
        return {"ok": True, **summary}

    @app.get("/api/workouts/{training_id}")
    async def get_workout(training_id: str) -> Any:
        session = await asyncio.to_thread(workouts.session, training_id)
        if not session:
            return JSONResponse({"error": "not found"}, status_code=404)
        return session

    # ---- activity timeline ----
    # Non-AI explorer: the Activity tab reads these directly. Records both bodies
    # — desktop foreground (from the state loop) and phone screen sessions (off
    # the bus) — accumulated in activity.db so a restart doesn't lose the day.
    @app.get("/api/activity")
    async def get_activity(date: str = "") -> dict[str, Any]:
        return await asyncio.to_thread(activity.timeline, date or None)

    @app.get("/api/activity/summary")
    async def get_activity_summary(days: int = 7) -> dict[str, Any]:
        return await asyncio.to_thread(activity.summary, days)

    @app.get("/api/activity/app")
    async def get_activity_app(name: str, days: int = 14) -> dict[str, Any]:
        """One app's per-day usage across a window — backs the drill-down you get
        by clicking an app in the Activity tab."""
        return await asyncio.to_thread(activity.app_history, name, days)

    # ---- budget (daily allowances over the activity timeline) ----
    # Read-only here, with one exception: `evaluate` lives in the state loop, not
    # behind a route, so that nothing a browser does can start or spend a grace
    # window. Opening the tab must never cost Jake wrap-up time he hasn't used.
    @app.get("/api/budget")
    async def get_budget() -> dict[str, Any]:
        return await asyncio.to_thread(budget.status, activity)

    @app.get("/api/budget/events")
    async def get_budget_events(date: str = "") -> dict[str, Any]:
        """Today's audit trail: warnings fired, grace granted, closes, overrides.
        The overrides are the interesting column — the number he chose in the
        morning next to the number of times he went round it."""
        return {"events": await asyncio.to_thread(budget.events, date or None)}

    @app.get("/api/budget/rules")
    async def get_budget_rules() -> dict[str, Any]:
        return {"rules": await asyncio.to_thread(budget.rules),
                "categories": list(BUDGET_CATEGORIES)}

    @app.post("/api/budget/allowance")
    async def set_budget_allowance(payload: dict[str, Any] = Body(...)) -> Any:
        """Set a daily allowance. A cut lands now, a raise lands tomorrow — the
        asymmetry is enforced in the store, not here, so no caller can skip it."""
        # `minutes or 0` collapsed "not supplied" into a real zero, and zero is
        # the most restrictive value the field has: `0 <= current` is a CUT, so
        # the store applied it IMMEDIATELY and blocked the category outright. A
        # typo'd field name, an omitted key and a null all did that and answered
        # 200. Same shape as the `days` fallback to "daily", pointed the other
        # way: the fallback sat at one end of the range, so a failed request was
        # indistinguishable from a deliberate extreme one.
        #
        # An explicit 0 is still honoured - blocking a category on purpose is a
        # thing Jake can want, and refusing it would trade this bug for an
        # unreachable setting.
        #
        # `""` is refused alongside None because a blank form field sends it, and
        # it is the one value that clears an `is None` guard while still being
        # falsy - precisely the gap `or 0` fell through. Without it the empty
        # string only fails further down on int(""), and the caller gets
        # "invalid literal for int() with base 10" instead of the field name.
        minutes = payload.get("minutes")
        if minutes is None or minutes == "":
            return JSONResponse(
                {"error": "minutes is required (send 0 to block the category)"},
                status_code=400)
        try:
            return await asyncio.to_thread(
                budget.set_allowance, str(payload.get("category") or ""),
                int(minutes))
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @app.post("/api/budget/override")
    async def add_budget_override(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """The escape hatch, on purpose. Extra minutes for today only, logged."""
        try:
            return await asyncio.to_thread(
                budget.override, str(payload.get("category") or ""),
                int(payload.get("minutes") or 0), str(payload.get("reason") or ""))
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @app.post("/api/budget/rules")
    async def add_budget_rule(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                budget.add_rule, str(payload.get("field") or "proc"),
                str(payload.get("match") or ""), str(payload.get("category") or ""))
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @app.delete("/api/budget/rules/{rule_id}")
    async def delete_budget_rule(rule_id: int) -> dict[str, Any]:
        return {"ok": await asyncio.to_thread(budget.remove_rule, rule_id)}

    @app.post("/api/budget/enabled")
    async def set_budget_enabled(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        await asyncio.to_thread(budget.set_enabled,
                                str(payload.get("category") or ""),
                                bool(payload.get("enabled")))
        return {"ok": True}

    # ---- nutrition (calorie/macro intake) ----
    # Back the non-AI Nutrition tab. All read/write the local NutritionStore
    # except /search, which queries the USDA food database. Specific literal
    # paths are declared before /{entry_id} so the path param doesn't swallow
    # them (same ordering rule as the workouts routes).
    def _nutrition_day(date: str = "") -> dict[str, Any]:
        """A day's intake with the calorie goal adapted to activity when that's
        on — the same cross-store composition as _body_day (steps from the body
        store + workout calories), folded onto the goal so the Nutrition tab and
        the Life dashboard's dials both track intake against the adaptive target."""
        d = nutrition.day(date or None)
        b = body.day(d["date"])
        expenditure = int(b["steps_kcal"]) + round(workouts.calories_on(d["date"]))
        d["goals"] = nutrition.adjust_for_activity(d["goals"], expenditure)
        return d

    @app.get("/api/nutrition")
    async def get_nutrition(date: str = "") -> dict[str, Any]:
        return await asyncio.to_thread(_nutrition_day, date)

    @app.get("/api/nutrition/summary")
    async def get_nutrition_summary(days: int = 7) -> dict[str, Any]:
        return await asyncio.to_thread(nutrition.summary, days)

    @app.get("/api/nutrition/providers")
    async def get_nutrition_providers() -> dict[str, Any]:
        """Which food database is primary and which is fallback — the tab shows
        this so Jake can see what's being used (FatSecret, or USDA)."""
        return foods.status()

    @app.get("/api/nutrition/search")
    async def search_food(q: str, limit: int = 8) -> dict[str, Any]:
        """Look a food up — FatSecret first, USDA fallback. Returns the macros
        for the picker plus which provider actually answered (and any fallback
        note), so the tab can show what served the results."""
        if not q.strip():
            return {"foods": [], "provider": foods.primary}
        try:
            return await asyncio.to_thread(foods.search_result, q, limit)
        except Exception as exc:
            return {"foods": [], "provider": foods.primary, "error": str(exc)}

    @app.get("/api/nutrition/barcode")
    async def barcode_food(upc: str) -> dict[str, Any]:
        """Resolve a barcode (UPC/EAN) to a food via Open Food Facts — manual UPC
        entry in the tab today, and the seam a phone scan will post to next.
        Returns {barcode, food, provider}; `food` is null when the code isn't
        found, `error` if OFF was unreachable. Never raises."""
        if not (upc or "").strip():
            return {"barcode": "", "food": None, "provider": "openfoodfacts"}
        try:
            return await asyncio.to_thread(foods.by_barcode, upc)
        except Exception as exc:
            return {"barcode": upc, "food": None,
                    "provider": "openfoodfacts", "error": str(exc)}

    @app.get("/api/nutrition/goals")
    async def get_nutrition_goals() -> dict[str, Any]:
        return await asyncio.to_thread(nutrition.goals)

    @app.post("/api/nutrition/goals")
    async def set_nutrition_goals(body: dict[str, Any] = Body(default={})) -> Any:
        # Absent keys are forwarded as None so the store *merges* them. The old
        # `body.get(k) or 0` turned every unmentioned field into a hard zero, so
        # a partial POST wiped the targets it didn't mention.
        try:
            return await asyncio.to_thread(
                nutrition.set_goals,
                calories=body.get("calories"), protein_g=body.get("protein_g"),
                carbs_g=body.get("carbs_g"), fat_g=body.get("fat_g"),
                activity_adjust=body.get("activity_adjust"))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @app.post("/api/nutrition")
    async def log_nutrition(body: dict[str, Any] = Body(default={})) -> Any:
        """Log a food from the UI (Jake logging himself). Macros are the totals
        for the amount eaten, already scaled by the client."""
        name = str(body.get("name") or "").strip()
        if not name:
            return JSONResponse({"error": "name required"}, status_code=400)
        try:
            fdc_id = int(body["fdc_id"]) if body.get("fdc_id") else None
        except (TypeError, ValueError):
            return JSONResponse({"error": "fdc_id must be a number"}, status_code=400)
        entry = await asyncio.to_thread(
            nutrition.log, name, body.get("calories") or 0,
            protein_g=body.get("protein_g") or 0, carbs_g=body.get("carbs_g") or 0,
            fat_g=body.get("fat_g") or 0, qty=body.get("qty") or 0,
            unit=str(body.get("unit") or ""), meal=str(body.get("meal") or ""),
            source="manual", fdc_id=fdc_id)
        await bus.publish(Event(topic="action.performed", source="app",
                                payload={"action": "log_food", "args": {"name": name},
                                         "result": f"logged {name} ({round(entry['calories'])}kcal)"}))
        return entry

    @app.delete("/api/nutrition/{entry_id}")
    async def delete_nutrition(entry_id: int) -> dict[str, Any]:
        return {"ok": await asyncio.to_thread(nutrition.delete, entry_id)}

    # ---- saved meals (reusable definitions, logged by the portion) ----
    @app.get("/api/nutrition/meals")
    async def list_meals() -> dict[str, Any]:
        return {"meals": await asyncio.to_thread(nutrition.meals)}

    @app.post("/api/nutrition/meals")
    async def create_meal(body: dict[str, Any] = Body(default={})) -> Any:
        """Save (or update by name) a reusable meal. `ingredients` — each with
        its macros already scaled to its amount — are summed, or pass the
        macros directly; either way they are RECIPE TOTALS and `servings`
        splits them into one stored portion. (This said "per-portion macros
        directly", which the store has never done: it divides whatever it is
        given. Harmless while servings is 1, wrong by a factor of N otherwise.)

        Omitting a field leaves it unchanged on update — so a partial edit is
        a partial edit, not a blanking."""
        name = str(body.get("name") or "").strip()
        if not name:
            return JSONResponse({"error": "name required"}, status_code=400)
        m = await asyncio.to_thread(
            nutrition.add_meal, name,
            # No `or 0` / `or 1` / `or ""`: add_meal distinguishes "not supplied"
            # (None — leave it alone) from a real 0, and `or` collapses exactly
            # that distinction, which is how editing one field blanked the rest.
            calories=body.get("calories"), protein_g=body.get("protein_g"),
            carbs_g=body.get("carbs_g"), fat_g=body.get("fat_g"),
            ingredients=body.get("ingredients") or None,
            servings=body.get("servings"),
            meal=None if body.get("meal") is None else str(body.get("meal")))
        await bus.publish(Event(topic="action.performed", source="app",
                                payload={"action": "create_meal", "args": {"name": name},
                                         "result": f"saved meal {name}"}))
        return m

    @app.post("/api/nutrition/meals/log")
    async def log_saved_meal(body: dict[str, Any] = Body(default={})) -> Any:
        name = str(body.get("name") or "").strip()
        if not name:
            return JSONResponse({"error": "name required"}, status_code=400)
        logged = await asyncio.to_thread(
            nutrition.log_meal, name, body.get("portions") or 1,
            meal=str(body.get("meal") or ""), source="manual")
        if not logged:
            return JSONResponse({"error": f"no saved meal matching '{name}'"}, status_code=404)
        await bus.publish(Event(topic="action.performed", source="app",
                                payload={"action": "log_meal", "args": {"name": name},
                                         "result": f"logged {logged['portions']:g} portion(s) of {name}"}))
        return logged

    @app.delete("/api/nutrition/meals/{meal_id}")
    async def delete_saved_meal(meal_id: int) -> dict[str, Any]:
        return {"ok": await asyncio.to_thread(nutrition.delete_meal, meal_id)}

    # ---- flashcards (Swedish vocab, SM-2) ----
    # Backs the Flashcards tab. Cards carry a full inflection table and per-form
    # audio, so the routes are shaped around (source, set) buckets and tags
    # rather than one flat deck name. Scheduling lives in the store, so the tab
    # and the add_flashcard/remove_flashcard tools cannot disagree about what is
    # due. Literal segments are declared before /{card_id} so the path param
    # doesn't swallow them (same ordering rule as workouts/nutrition).
    def _fc_filters(source: str, set_name: str, tag: str) -> dict[str, str]:
        return {"source": source or "", "set_name": set_name or "", "tag": tag or ""}

    @app.get("/api/flashcards")
    async def get_flashcards(source: str = "", set_name: str = Query("", alias="set"),
                             tag: str = "", limit: int = 500) -> dict[str, Any]:
        f = _fc_filters(source, set_name, tag)
        return {"cards": await asyncio.to_thread(flashcards.cards, limit=limit, **f),
                "buckets": await asyncio.to_thread(flashcards.buckets),
                "tags": await asyncio.to_thread(flashcards.tags),
                "stats": await asyncio.to_thread(
                    flashcards.stats, source=f["source"], set_name=f["set_name"]),
                "filters": f}

    @app.get("/api/flashcards/buckets")
    async def get_flashcard_buckets() -> dict[str, Any]:
        return {"buckets": await asyncio.to_thread(flashcards.buckets),
                "tags": await asyncio.to_thread(flashcards.tags)}

    @app.get("/api/flashcards/study")
    async def get_flashcard_study(source: str = "", set_name: str = Query("", alias="set"),
                                  tag: str = "", limit: int = 20, due_only: bool = False) -> dict[str, Any]:
        f = _fc_filters(source, set_name, tag)
        cards = await asyncio.to_thread(
            flashcards.study, limit=limit, due_only=due_only, **f)
        return {"cards": cards, "filters": f,
                "stats": await asyncio.to_thread(
                    flashcards.stats, source=f["source"], set_name=f["set_name"])}

    @app.get("/api/flashcards/status")
    async def get_flashcard_status() -> dict[str, Any]:
        """What the tab can offer: whether import and audio have keys behind
        them. Booleans only - this renders in a browser."""
        return {**vocab.status(),
                "missing_audio": len(await asyncio.to_thread(flashcards.missing_audio))}

    @app.get("/api/flashcards/audio/{name:path}")
    async def get_flashcard_audio(name: str) -> Any:
        # `:path`, not the default converter, because put_audio() files each
        # card's mp3s under their own directory and returns "<id>/<form>.mp3" —
        # a plain {name} cannot match the slash, so every real audio name 404s
        # while a synthetic flat one passes. The store validates that `name`
        # resolves inside the audio directory; this route must not build the
        # path itself, or the guard moves here and the next caller
        # re-implements it wrong.
        try:
            data = await asyncio.to_thread(flashcards.audio_bytes, name)
        except ValueError:
            return JSONResponse({"error": "bad audio name"}, status_code=400)
        if data is None:
            return JSONResponse({"error": "no such audio"}, status_code=404)
        return Response(content=data, media_type="audio/mpeg",
                        headers={"Cache-Control": "private, max-age=3600"})

    @app.post("/api/flashcards")
    async def add_flashcard_route(body: dict[str, Any] = Body(default={})) -> Any:
        """Add one card by hand. Re-adding an identical pair updates its soft
        fields rather than duplicating."""
        sv = str(body.get("swedish") or "").strip()
        en = str(body.get("english") or "").strip()
        if not sv or not en:
            return JSONResponse({"error": "both faces required"}, status_code=400)

        def named(key: str) -> Any:
            # A key the body never sent means "no opinion" and must stay None;
            # a key sent empty is a deliberate clear. `str(... or "")` collapsed
            # the two, so a form that posts only the two faces used to blank the
            # examples and notes on a card the importer had filled in.
            v = body.get(key)
            return None if v is None else str(v)

        card = await asyncio.to_thread(
            flashcards.add, sv, en,
            source=str(body.get("source") or ""), set_name=str(body.get("set") or ""),
            pos=named("pos"), gender=named("gender"),
            tags=body.get("tags"), example_sv=named("example_sv"),
            example_en=named("example_en"), forms=body.get("forms"),
            speak=body.get("speak"), notes=named("notes"))
        await bus.publish(Event(topic="action.performed", source="app",
                                payload={"action": "add_flashcard",
                                         "args": {"swedish": sv, "english": en},
                                         "result": f"added '{sv} = {en}'"}))
        return card

    @app.post("/api/flashcards/import")
    async def import_flashcards_route(body: dict[str, Any] = Body(default={})) -> Any:
        """Turn a pasted word list into draft cards. Deliberately does NOT save:
        a mis-gendered noun is far cheaper to fix here than after it has become
        forty cards with audio attached."""
        try:
            cards = await vocab.build_cards(str(body.get("text") or ""))
        except VocabError as e:
            return JSONResponse({"error": str(e)}, status_code=502)
        return {"cards": cards, "count": len(cards)}

    @app.post("/api/flashcards/import/save")
    async def save_imported_flashcards(body: dict[str, Any] = Body(default={})) -> Any:
        """Commit reviewed draft cards. Goes through import_backup so there is
        exactly one mapping from a card dict to a row - the import path and any
        future restore cannot drift apart."""
        drafts = body.get("cards")
        if not isinstance(drafts, list) or not drafts:
            return JSONResponse({"error": "no cards"}, status_code=400)
        payload = {"cards": drafts,
                   "source": str(body.get("source") or ""),
                   "set": str(body.get("set") or "")}
        res = await asyncio.to_thread(flashcards.import_backup, payload)
        await bus.publish(Event(topic="action.performed", source="app",
                                payload={"action": "import_flashcards",
                                         "args": {"count": len(drafts)},
                                         "result": f"imported {res.get('added', 0)} card(s)"}))
        return res

    @app.post("/api/flashcards/voice")
    async def voice_flashcards_route(body: dict[str, Any] = Body(default={})) -> Any:
        """Generate missing audio, capped. The cap counts synthesis calls because
        that is what ElevenLabs bills; the response reports what is left so the
        tab can offer another batch instead of looking finished."""
        ids = body.get("card_ids")
        try:
            res = await vocab.voice_cards(
                ids if isinstance(ids, list) and ids else None,
                limit=int(body.get("limit") or 40))
        except VocabError as e:
            return JSONResponse({"error": str(e)}, status_code=502)
        return res

    @app.patch("/api/flashcards/{card_id}")
    async def edit_flashcard_route(card_id: str,
                                   body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        return await asyncio.to_thread(flashcards.update, card_id, **body)

    @app.post("/api/flashcards/{card_id}/review")
    async def review_flashcard_route(card_id: str,
                                     body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        # quality is SM-2's 0-5, not a boolean: "almost had it" and "no idea"
        # schedule differently, and collapsing them throws away the distinction
        # the whole algorithm turns on.
        return await asyncio.to_thread(
            flashcards.review, card_id, int(body.get("quality") or 0),
            mode=str(body.get("mode") or ""))

    @app.delete("/api/flashcards/{card_id}")
    async def delete_flashcard_route(card_id: str) -> dict[str, Any]:
        return {"ok": await asyncio.to_thread(flashcards.delete, card_id)}

    # ---- body (weight / height / steps → BMI + energy expenditure) ----
    # Back the non-AI Body tab. Weight/height/steps live in the BodyStore; the
    # day view COMPOSES the two cross-store pieces here (there's no cross-store
    # transaction): workout calories from the WorkoutStore and food intake from
    # the NutritionStore, so it can report active expenditure and energy balance.
    def _body_day(date: str = "") -> dict[str, Any]:
        d = body.day(date or None)
        workout_kcal = round(workouts.calories_on(d["date"]))
        step_kcal = int(d["steps_kcal"])
        expenditure = step_kcal + workout_kcal        # active burn (excludes BMR)
        intake = round((nutrition.day(d["date"])["totals"] or {}).get("calories") or 0)
        d["workout_kcal"] = workout_kcal
        d["expenditure_kcal"] = expenditure
        d["intake_kcal"] = intake
        # Net of active burn only — intake minus what steps+training burned. It
        # deliberately omits resting metabolism, so it's labelled "active" in the UI.
        d["balance_kcal"] = intake - expenditure if intake else None
        return d

    @app.get("/api/body")
    async def get_body(date: str = "") -> dict[str, Any]:
        return await asyncio.to_thread(_body_day, date)

    @app.get("/api/body/summary")
    async def get_body_summary(days: int = 30) -> dict[str, Any]:
        return await asyncio.to_thread(body.summary, days)

    @app.post("/api/body")
    async def set_body(body_in: dict[str, Any] = Body(default={})) -> Any:
        """Log a weight and/or height (and, from a smart scale, body-fat % and
        muscle mass) for a day (Jake, from the tab)."""
        comp = ("weight_kg", "height_cm", "body_fat_pct", "muscle_mass_kg")
        if all(body_in.get(k) in (None, "") for k in comp):
            return JSONResponse(
                {"error": "weight, height or body composition required"}, status_code=400)
        try:
            day = _day(body_in.get("day"), "day")
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        res = await asyncio.to_thread(
            body.set_measurement, weight_kg=body_in.get("weight_kg"),
            height_cm=body_in.get("height_cm"),
            body_fat_pct=body_in.get("body_fat_pct"),
            muscle_mass_kg=body_in.get("muscle_mass_kg"),
            day=day)
        bits = []
        if body_in.get("weight_kg"):
            bits.append(f"{res['weight_kg']}kg")
        if body_in.get("height_cm"):
            bits.append(f"{res['height_cm']}cm")
        if body_in.get("body_fat_pct"):
            bits.append(f"{res['body_fat_pct']}% fat")
        if body_in.get("muscle_mass_kg"):
            bits.append(f"{res['muscle_mass_kg']}kg muscle")
        await bus.publish(Event(topic="action.performed", source="app",
                                payload={"action": "log_body", "args": {"day": res["date"]},
                                         "result": f"recorded {', '.join(bits)} for {res['date']}"}))
        return await asyncio.to_thread(_body_day, res["date"])

    @app.post("/api/body/steps")
    async def set_body_steps(body_in: dict[str, Any] = Body(default={})) -> Any:
        """Set a day's step count. The ingestion seam for step data — the tab
        posts here, and so can any health feed (source names where it came from)."""
        if body_in.get("count") in (None, ""):
            return JSONResponse({"error": "count required"}, status_code=400)
        try:
            day = _day(body_in.get("day"), "day")
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        res = await asyncio.to_thread(
            body.set_steps, body_in.get("count"), day=day,
            source=str(body_in.get("source") or "manual"))
        await bus.publish(Event(topic="action.performed", source="app",
                                payload={"action": "log_steps", "args": {"day": res["date"]},
                                         "result": f"{res['steps']} steps for {res['date']}"}))
        return await asyncio.to_thread(_body_day, res["date"])

    # ---- health (sleep / HRV / resting HR / SpO2 → readiness) ----
    # Back the non-AI Health tab. All read/write the HealthStore. The day view is
    # store-only (readiness is derived inside the store from its own baselines),
    # so unlike _body_day it composes nothing across stores.
    @app.get("/api/health")
    async def get_health(date: str = "") -> dict[str, Any]:
        return await asyncio.to_thread(health.day, date or None)

    @app.get("/api/health/summary")
    async def get_health_summary(days: int = 30) -> dict[str, Any]:
        return await asyncio.to_thread(health.summary, days)

    @app.post("/api/health")
    async def set_health(body_in: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        """Log recovery metrics for a day (Jake, from the tab) — and the seam any
        health feed can post to. The body carries whatever metrics are known
        (asleep_min, resting_hr, hrv_ms, …); upsert_day preserves the rest."""
        res = await asyncio.to_thread(health.upsert_day,
                                      {**body_in, "source": body_in.get("source") or "manual"})
        r = res.get("readiness") or {}
        note = (f"readiness {r['score']}" if r.get("score") is not None
                else (res.get("asleep_label") or "recovery metrics"))
        await bus.publish(Event(topic="action.performed", source="app",
                                payload={"action": "log_health", "args": {"day": res["date"]},
                                         "result": f"recorded {note} for {res['date']}"}))
        return res

    @app.get("/api/state")
    async def get_state() -> dict[str, Any]:
        return state.basic()

    @app.get("/api/state/inspect")
    async def inspect_state(target: str) -> dict[str, Any]:
        return await asyncio.to_thread(state.inspect, target)

    @app.get("/")
    async def index() -> FileResponse:
        # The app shell above all: it is what names every other asset, so a
        # stale index.html pins the whole frontend to an old build.
        #
        # FileResponse, not HTMLResponse-of-read_text, for two reasons. First,
        # `no-cache` obliges the browser to revalidate, and revalidation needs a
        # validator to revalidate *with*: only FileResponse stamps ETag and
        # Last-Modified (via set_stat_headers), so the HTMLResponse version
        # forced an unconditional full re-download of ~43 KB on every single
        # load and made a 304 impossible — the opposite of what REVALIDATE is
        # for. Second, read_text() was a synchronous 43 KB disk read on the
        # event loop on every page load.
        return FileResponse(STATIC / "index.html", media_type="text/html",
                            headers=REVALIDATE)

    # Both files live under static/ on disk but are served from the root. A
    # service worker's scope is its own directory, so one fetched from /static/
    # could never control the app at /, and Chrome would not offer to install
    # it. The manifest gets an explicit media type because StaticFiles guesses
    # octet-stream for .webmanifest, which browsers ignore.
    # A cached service worker is the worst case of the three: it is the thing
    # that would otherwise be able to refresh the others, so once it goes stale
    # it keeps itself stale.
    @app.get("/sw.js")
    async def service_worker() -> FileResponse:
        return FileResponse(STATIC / "sw.js", media_type="application/javascript",
                            headers=REVALIDATE)

    @app.get("/manifest.webmanifest")
    async def manifest() -> FileResponse:
        return FileResponse(STATIC / "manifest.webmanifest",
                            media_type="application/manifest+json",
                            headers=REVALIDATE)

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        # The UI polls this constantly. Five independent reads — one embedder
        # probe and four SQLite hits — so run them concurrently instead of
        # paying for them end to end on every poll. (The embedder probe stopped
        # being a network call when it moved in-process; the first one loads the
        # model, the rest are a dict lookup.)
        (memory_ready, mem_count, mem_total,
         main_conv, core) = await asyncio.gather(
            Embedder(cfg).available(),
            asyncio.to_thread(memory.count),
            asyncio.to_thread(memory.count, active_only=False),
            asyncio.to_thread(conversations.main),
            asyncio.to_thread(memory.get_core))
        return {
            "app": "Continuum Control",
            "fast_model": cfg.cloud_fast.model,
            "cloud_model": cfg.cloud.model,
            "cloud_provider": cfg.cloud.provider,   # "claude-cli" | "anthropic"
            "cloud_available": cfg.cloud_available,
            "has_cloud_key": cfg.has_cloud_key,
            "embed_model": memory.embed_model,
            "memory_ready": memory_ready,
            "memory_count": mem_count,
            "memory_total": mem_total,
            "autoextract": agent.autoextract,
            "tools_enabled": agent.tools_enabled,
            "actions_enabled": agent.actions_enabled,
            "dev_enabled": tool_loop.dev_enabled,
            "proactive_enabled": proactive.enabled,
            "main_conversation_id": main_conv,
            "core": core,
            "devices": list(devices.values()),
        }

    @app.get("/api/memory")
    async def get_memory(limit: int = 30, q: str = "") -> dict[str, Any]:
        if q.strip():
            mems = await memory.recall(q, k=limit, min_score=0.25)
        else:
            mems = await asyncio.to_thread(memory.recent, limit)
        count, core = await asyncio.gather(
            asyncio.to_thread(memory.count),
            asyncio.to_thread(memory.get_core))
        return {"count": count, "query": q, "core": core,
                "memories": [m.as_dict() for m in mems]}

    @app.delete("/api/memory/{memory_id}")
    async def delete_memory(memory_id: int) -> dict[str, Any]:
        ok = await asyncio.to_thread(memory.delete, memory_id)
        if ok:
            await bus.publish(Event(topic="memory.deleted", source="app",
                                    payload={"id": memory_id}))
        return {"ok": ok}

    @app.get("/api/memory/{memory_id}/related")
    async def related_memory(memory_id: int) -> dict[str, Any]:
        return {"related": [m.as_dict() for m in await memory.related(memory_id)]}

    @app.get("/api/conversations/{conv_id}/memories")
    async def conversation_memories(conv_id: int) -> dict[str, Any]:
        return {"memories": [m.as_dict() for m in await asyncio.to_thread(memory.by_conversation, conv_id)]}

    @app.get("/api/phone")
    async def phone_state() -> dict[str, Any]:
        return phone.snapshot()

    @app.get("/api/desktop")
    async def desktop_state() -> dict[str, Any]:
        """The hub's view of a remote desktop. `connected: false` here is the
        normal answer while Continuum and the desktop are the same machine —
        nothing has dialled in because nothing needs to. `local` says which of
        those two worlds this process is living in, so a blank Budget tab can be
        explained rather than guessed at."""
        snap = desktop_link.snapshot()
        snap["local"] = not desktop_link.connected
        snap["counting"] = bool(desktop_link.basic()) or not desktop_link.connected
        return snap

    # ---- power (the Power controls) ----
    #
    # Three verbs, two transports, and the split is forced by physics rather than
    # taste. Sleep and restart are things the desk does to itself, so they ride
    # the socket the desk already holds to this hub — which means they work from
    # anywhere Jake can reach the hub, mobile data included. Waking cannot use
    # that socket, because a sleeping machine holds no socket: it needs a magic
    # packet, a magic packet is a layer-2 broadcast, and the only always-on
    # device ever present on the desk's LAN is the phone. So wake goes out over
    # the phone channel and is honestly limited to "phone is at home".
    #
    # Nothing here is exposed as a model tool. These are Jake's buttons, and an
    # agent that can reboot the machine it is thinking on is a bad trade for a
    # capability he asked for by name and can press himself.

    @app.get("/api/power")
    async def power_state() -> dict[str, Any]:
        """What each verb could do if pressed right now, so the UI can say why a
        button is disabled instead of just greying it out."""
        return {
            "desk_connected": deskops.reachable(),
            "desk_where": deskops.where(),
            "phone_connected": phone.connected,
            # Not "can wake" — the phone being on home Wi-Fi is only knowable at
            # send time, and claiming otherwise here would be the same synthesised
            # success the alarm route learned not to produce.
            "wake_configured": bool(cfg.desk_mac),
        }

    @app.post("/api/power/{verb}")
    async def power_action(verb: str) -> Any:
        if verb in ("sleep", "restart"):
            if not deskops.reachable():
                return JSONResponse({"error": "the desk is not connected"}, status_code=503)
            out = await deskops.run(f"{verb}_pc", {})
            if out.get("error") or not out.get("ok"):
                return JSONResponse(
                    {"error": str(out.get("error") or out.get("message")
                                  or f"the desk would not {verb}")}, status_code=502)
            await bus.publish(Event(topic="action.performed", source="app",
                                    payload={"action": f"{verb}_pc", "args": {},
                                             "result": out.get("message", "")}))
            return {"ok": True, "message": out.get("message", "")}

        if verb == "wake":
            if not cfg.desk_mac:
                return JSONResponse(
                    {"error": "no desk MAC configured — set CONTINUUM_DESK_MAC to the "
                              "wired NIC's address"}, status_code=503)
            if not phone.connected:
                return JSONResponse({"error": "phone not connected"}, status_code=503)
            try:
                out = await phone.command("wake_lan", {"mac": cfg.desk_mac})
            except Exception as exc:
                return JSONResponse({"error": str(exc)}, status_code=502)
            # The phone refuses in-band (`ok: false`) when it is on mobile data,
            # which is the common case and not a transport failure.
            if not out.get("ok"):
                return JSONResponse(
                    {"error": str(out.get("error") or "the phone could not send the packet")},
                    status_code=502)
            await bus.publish(Event(topic="action.performed", source="app",
                                    payload={"action": "wake_pc", "args": {},
                                             "result": out.get("message", "")}))
            # Deliberately not "the PC is awake". Nothing here can observe that —
            # the packet is fire-and-forget and the NIC never answers it.
            return {"ok": True, "message": out.get("message", "magic packet sent"),
                    "sent": out.get("sent") or []}

        return JSONResponse({"error": f"unknown power verb: {verb}"}, status_code=400)

    # ---- alarms (the Alarms tab) ----
    # Real wake-up alarms live on the phone — it rings them via AlarmManager even
    # while this desktop is asleep or the socket dropped overnight. These routes
    # are just the remote control: list reads the phone's set back, set/cancel
    # sync it down. Every call needs the phone connected; absent is an honest
    # answer, not an error to hide.
    @app.get("/api/alarms")
    async def get_alarms() -> dict[str, Any]:
        if not phone.connected:
            return {"alarms": [], "connected": False}
        try:
            out = await phone.command("list_alarms", {})
        except Exception as exc:
            # "We couldn't ask" is not "there are none". The phone can be
            # attached but dozing, so this times out after COMMAND_TIMEOUT
            # while `phone.connected` stays True (it only tracks whether a
            # socket exists) — and an empty list under `connected: true` made
            # the Alarms tab assert the phone had no alarms when it had
            # several, hiding the delete buttons for the real ones. `ok` is
            # explicit so the client cannot mistake the two.
            return {"ok": False, "alarms": [], "connected": phone.connected,
                    "error": str(exc) or "the phone did not answer"}
        return {"ok": True, "alarms": out.get("alarms") or [], "connected": True}

    @app.post("/api/alarms")
    async def set_alarm_route(body_in: dict[str, Any] = Body(default={})) -> Any:
        # 503, not 400: the phone reconnects on its own, so this is the one
        # failure here that's worth retrying unchanged. (The GET above keeps its
        # 200 on purpose — its body carries `connected`, which the tab reads.)
        if not phone.connected:
            return JSONResponse({"error": "phone not connected"}, status_code=503)
        try:
            hour = int(body_in.get("hour"))
            minute = int(body_in.get("minute"))
        except (TypeError, ValueError):
            return JSONResponse({"error": "hour and minute required"}, status_code=400)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return JSONResponse({"error": "invalid time of day"}, status_code=400)
        days = sorted({int(d) for d in (body_in.get("days") or [])
                       if isinstance(d, (int, float)) and 0 <= int(d) <= 6})
        payload = {"hour": hour, "minute": minute,
                   "label": str(body_in.get("label") or "").strip() or "Alarm",
                   "days": days,
                   "enabled": bool(body_in.get("enabled", True))}
        if body_in.get("id"):
            payload["id"] = str(body_in["id"])
        try:
            out = await phone.command("set_alarm", payload)
        except Exception as exc:
            # Transport or timeout talking to the handset — upstream, not us.
            return JSONResponse({"error": str(exc)}, status_code=502)
        # `phone.command` raises only on transport/timeout. A *phone-side*
        # refusal comes back as a perfectly successful command carrying
        # `{"ok": false, "error": ...}` — and `AlarmScheduler.upsert` throws
        # `SecurityException` whenever `SCHEDULE_EXACT_ALARM` isn't granted,
        # which Android revokes on app hibernation, so this is routine.
        #
        # This used to be `out.get("alarm") or payload`, which on that reply
        # (no "alarm" key) *synthesised the success response out of the request
        # body*: the tab said the alarm was saved, a false `action.performed`
        # went into the durable ledger as `allowed=True`, the next list showed
        # no such alarm, and Jake didn't wake up. `cancel_alarm_route` below
        # always got this right — it checks `out.get("removed")`.
        saved = out.get("alarm")
        if not out.get("ok") or not saved:
            return JSONResponse(
                {"error": str(out.get("error") or "the phone would not set that alarm")},
                status_code=502)
        await bus.publish(Event(topic="action.performed", source="app",
                                payload={"action": "set_alarm",
                                         "args": {"hour": hour, "minute": minute, "days": days},
                                         "result": f"alarm set for {hour:02d}:{minute:02d}"}))
        return {"ok": True, "alarm": saved}

    @app.delete("/api/alarms/{alarm_id}")
    async def cancel_alarm_route(alarm_id: str) -> Any:
        if not phone.connected:
            return JSONResponse({"error": "phone not connected"}, status_code=503)
        try:
            out = await phone.command("cancel_alarm", {"id": alarm_id})
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        removed = bool(out.get("removed"))
        if removed:
            await bus.publish(Event(topic="action.performed", source="app",
                                    payload={"action": "cancel_alarm", "args": {"id": alarm_id},
                                             "result": "alarm cancelled"}))
        return {"ok": removed}

    @app.get("/api/app/version")
    async def app_version() -> dict[str, Any]:
        """The version of the phone APK the desktop is serving, so the installed
        app can tell whether a newer build is waiting for it."""
        return _apk_version()

    def _serve_apk() -> Any:
        """The phone APK itself. Reached over the tailnet like everything else, so
        the Guard's identity check already gates who may pull a build."""
        if not APK_PATH.exists():
            return JSONResponse({"error": "no build available"}, status_code=404)
        return FileResponse(
            APK_PATH,
            media_type="application/vnd.android.package-archive",
            filename="continuum-phone.apk",
        )

    @app.get("/app/download")
    async def app_download() -> Any:
        return _serve_apk()

    # A human-friendly alias for the same build: typeable straight into a phone
    # browser to sideload, no /app/ prefix to remember. Same Guard, same file.
    @app.get("/continuum-phone.apk")
    async def app_download_named() -> Any:
        return _serve_apk()

    async def _ingest_desktop_messages(payload: dict[str, Any]) -> None:
        """Write a batch of messages the desktop collected into the store.

        Off the loop, like the phone's notification path directly below, and for
        the same reason: sqlite commits are blocking, and stalling the event loop
        here runs straight into the 3s timeout that evicts dashboards.

        Trusts the client's `key` rather than recomputing it. The key is only a
        dedup token, `record` inserts OR IGNORE on it, and the client has already
        used it to avoid re-sending — recomputing here from different inputs
        would just mean storing the same toast twice under two hashes.
        """
        rows = payload.get("rows")
        if not isinstance(rows, list):
            return
        stored = 0
        for row in rows[:500]:
            if not isinstance(row, dict) or not row.get("key"):
                continue
            if await asyncio.to_thread(
                    messagelog.record,
                    source=str(row.get("source") or "desktop"),
                    app=str(row.get("app") or ""), title=str(row.get("title") or ""),
                    body=str(row.get("body") or ""), key=str(row["key"]),
                    author=row.get("author"), ts=row.get("ts")):
                stored += 1
        if stored:
            log.debug("stored %d message(s) relayed by the desktop", stored)

    @app.websocket("/desktop/ws")
    async def desktop_ws(sock: WebSocket) -> None:
        """A desktop reporting in from elsewhere.

        The desktop dials *us* for the same reason the phone does: it sits behind
        a router with no open ports and no stable address, and the socket it
        opens then carries commands back without needing a second channel. That
        it used to be the machine we were running on is an accident of having
        started with one computer.

        Same authentication as `/phone/ws` — `Guard` runs as raw ASGI middleware
        over websockets, so Tailscale has already vouched for who this is before
        we accept. Nothing here mints or checks a token.
        """
        await sock.accept()
        attached = False
        try:
            hello = await sock.receive_json()
            if hello.get("type") != "hello":
                await sock.close(code=1002)
                return
            await desktop_link.attach(sock, hello)
            attached = True
            await sock.send_json({"type": "welcome",
                                  "commands": list(DESKTOP_COMMANDS)})

            while True:
                # Same guard as the phone socket, and for the same reason: one
                # malformed frame must not run `finally` → `detach`, which would
                # take the desktop offline and hand the budget an empty reading
                # until the client's reconnect backoff came round. Binary frames
                # raise KeyError from receive_json, bad JSON raises ValueError,
                # and a top-level array makes `.get` raise AttributeError. A real
                # disconnect still raises WebSocketDisconnect, so this can't spin.
                try:
                    msg = await sock.receive_json()
                    kind = msg.get("type")
                except WebSocketDisconnect:
                    raise
                except (ValueError, TypeError, KeyError, AttributeError) as exc:
                    log.warning("ignoring malformed desktop frame: %s", exc)
                    continue

                try:
                    if kind == "report":
                        signal = (msg.get("kind") or "").strip()
                        if signal == "messages":
                            # Not a signal — a batch of rows for the store. The
                            # collector runs on the desktop now (it drives two
                            # Windows-only feeds), while the database stays here.
                            # Routed before `ingest` deliberately: `messages` is
                            # not in `known_signals`, so ingesting it would file
                            # a whole batch as one aged "reading" and burn a slot
                            # in the unknown-signal pool every 2 seconds.
                            await _ingest_desktop_messages(msg.get("payload") or {})
                        elif signal:
                            await desktop_link.ingest(signal, msg.get("payload") or {})
                    elif kind == "result":
                        await desktop_link.resolve(msg.get("corr") or "",
                                                   msg.get("payload") or {})
                    elif kind == "ping":
                        await sock.send_json({"type": "pong"})
                except WebSocketDisconnect:
                    raise
                except Exception:
                    log.exception("desktop frame failed (type=%r)", kind)
                    continue
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("desktop socket failed")
        finally:
            if attached:
                # Say *which* socket — a reconnect that already landed must not
                # be buried by the old handler finally noticing it's dead.
                await desktop_link.detach(sock)

    @app.websocket("/phone/ws")
    async def phone_ws(sock: WebSocket) -> None:
        """The phone's socket. It connects to us — not the other way round —
        because a phone has no stable address and no open ports, and because the
        same socket then carries commands back without a second channel.

        Authentication already happened: `Guard` runs as raw ASGI middleware over
        websockets too, so by the time we accept, Tailscale has vouched for who
        this is. The app never holds a token of ours.
        """
        await sock.accept()
        attached = False
        try:
            hello = await sock.receive_json()
            if hello.get("type") != "hello":
                await sock.close(code=1002)
                return
            await phone.attach(sock, hello)
            attached = True
            await sock.send_json({"type": "welcome", "commands": list(PHONE_COMMANDS)})

            # Feed the home-screen routine widget. Driven off the socket rather
            # than the routine HTTP endpoints on purpose: most routine changes
            # come through the agent's MCP tools (start/complete/skip), which
            # never touch those endpoints — but every path is reflected the next
            # time we recompute today() and push it here. So we push once on
            # connect and again on each of the phone's ~4-minute pings, skipping
            # the send when nothing changed. Fire-and-forget: awaiting the
            # command result inline would deadlock, since only this same loop
            # reads the `result` that resolves it.
            last_widget = ""

            def _fire_widget_push() -> None:
                async def _go() -> None:
                    nonlocal last_widget
                    try:
                        t = await asyncio.to_thread(routines.today)
                    except Exception:
                        return
                    upcoming = []
                    for it in t["items"]:
                        if not it["done"] and not it["skipped"]:
                            upcoming.append({"title": it["title"],
                                             "when": it.get("time") or it.get("rel") or "",
                                             "section": it["section"]})
                            if len(upcoming) >= 5:
                                break
                    # `items` is the up-to-5 upcoming list the widget renders;
                    # `next` is kept as items[0] so an older (v11) widget that
                    # only reads a single item still paints correctly.
                    payload = {"date": t["date"], "done": t["done"],
                               "skipped": t["skipped"], "total": t["total"],
                               "next": upcoming[0] if upcoming else None,
                               "items": upcoming}
                    key = json.dumps(payload, sort_keys=True)
                    if key == last_widget:
                        return
                    try:
                        # Best-effort: an older APK without this capability (or an
                        # offline phone) just raises, and we retry next ping.
                        await phone.command("routine_widget", payload, timeout=8.0)
                        last_widget = key
                    except Exception:
                        last_widget = ""
                asyncio.create_task(_go())

            _fire_widget_push()

            while True:
                # The same guard the dashboard socket below carries — and it
                # matters more here, because this is a *device*, not a viewer.
                # With nothing between `receive_json()` and the handler's
                # `except Exception`, one malformed frame ran `finally` →
                # `phone.detach(sock)`, which clears `_sock`, fails every
                # in-flight command and publishes `device.offline`. So a single
                # bad frame took the phone offline until Android's reconnect
                # backoff came round, and until then every `phone.command`
                # raised and the alarms tab went dark. These are real inputs in
                # Starlette: a binary frame raises KeyError from receive_json,
                # malformed JSON raises JSONDecodeError (a ValueError), and a
                # top-level JSON array or null makes `msg.get` raise
                # AttributeError. A true disconnect still raises
                # WebSocketDisconnect, so the loop cannot spin.
                try:
                    msg = await sock.receive_json()
                    kind = msg.get("type")
                except WebSocketDisconnect:
                    raise
                except (ValueError, TypeError, KeyError, AttributeError) as exc:
                    log.warning("ignoring malformed phone frame: %s", exc)
                    continue

                try:
                    if kind == "report":
                        signal = (msg.get("kind") or "").strip()
                        if signal:
                            await phone.ingest(signal, msg.get("payload") or {})
                    elif kind == "notification":
                        # Straight into the same log the desktop's toasts land in.
                        # A message is a message; which body received it is metadata,
                        # not a reason for a second store to search.
                        n = msg.get("payload") or {}
                        app_name = str(n.get("app") or "phone")
                        title = str(n.get("title") or "")
                        body = str(n.get("body") or "")
                        # Off the loop: `record` is an INSERT plus a commit, and
                        # a commit is an fsync. The phone mirrors many
                        # notifications a minute, and every one of those fsyncs
                        # was delaying every `Hub.broadcast` send — feeding
                        # straight into the 3s timeout that evicts dashboards.
                        await asyncio.to_thread(
                            messagelog.record,
                            source="phone", app=app_name, title=title, body=body,
                            key=_phone_key(app_name, title, body),
                            author=n.get("author"), ts=n.get("ts"),
                        )
                        await phone.ingest("notification", n)
                    elif kind == "result":
                        await phone.resolve(msg.get("corr") or "", msg.get("payload") or {})
                    elif kind == "ping":
                        # The phone drives the heartbeat: it knows when its radio
                        # is already awake, so letting it choose the moment costs
                        # far less battery than us polling into a dozing device.
                        await sock.send_json({"type": "pong"})
                        # Piggyback the widget refresh on the heartbeat we already
                        # answer — keeps today's routine on the home screen current
                        # within a ping, with no timer of our own.
                        _fire_widget_push()
                except WebSocketDisconnect:
                    raise
                except Exception:
                    # One frame's handler blowing up is not a reason to drop the
                    # device either. Note it and take the next frame.
                    log.exception("phone frame failed (type=%r)", kind)
                    continue
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("phone socket failed")
        finally:
            if attached:
                # Say *which* socket. A phone that changed network leaves this
                # handler parked here for minutes after the replacement is
                # already live and working; an unqualified detach would bury the
                # new one on the old one's behalf.
                await phone.detach(sock)

    @app.websocket("/ws")
    async def ws(sock: WebSocket) -> None:
        await hub.connect(sock)
        try:
            await sock.send_json({"type": "hello", "devices": list(devices.values())})
            while True:
                # One bad frame is not a reason to hang up on the dashboard. A
                # JSONDecodeError (or a binary frame, which surfaces as a
                # TypeError/KeyError from receive_json) used to fall through to
                # the handler below and tear the whole control channel down;
                # now it's noted and skipped. A real disconnect still raises
                # WebSocketDisconnect from here, so the loop can't spin: every
                # pass through it blocks on the next frame or exits.
                try:
                    msg = await sock.receive_json()
                except WebSocketDisconnect:
                    raise
                except RuntimeError:
                    # `Hub._hang_up_soon` closed this socket out from under us
                    # while we were still handling the previous frame. That is a
                    # deliberate eviction — a client that missed a broadcast
                    # deadline gets hung up on with code 1011 and reconnects —
                    # but starlette reports a close *we* initiated as a bare
                    # RuntimeError, not WebSocketDisconnect, so it fell past both
                    # arms here and logged a full traceback for a routine
                    # hang-up. Five of those in one evening, and they were very
                    # nearly the thing that hid the notification bug underneath.
                    #
                    # Only when the socket is genuinely gone: a RuntimeError on a
                    # socket still CONNECTED is some other bug entirely, and that
                    # one should keep its traceback.
                    if sock.application_state is WebSocketState.CONNECTED:
                        raise
                    break
                except (ValueError, TypeError, KeyError) as exc:
                    log.warning("ignoring malformed websocket frame: %s", exc)
                    continue
                try:
                    if msg.get("type") == "task":
                        prompt = (msg.get("prompt") or "").strip()
                        atts = msg.get("attachments") or []
                        if not prompt and not atts:  # an image alone is a valid message
                            continue
                        corr = msg.get("corr")
                        await bus.publish(Event(
                            topic="task.request",
                            source="app",
                            id=corr or Event.__dataclass_fields__["id"].default_factory(),
                            payload={
                                "title": (msg.get("title") or prompt[:48] or "image"),
                                "prompt": prompt,
                                "complexity": msg.get("complexity", "high"),
                                "conversation_id": msg.get("conversation_id"),
                                "new_task": bool(msg.get("new_task")),
                                "attachments": msg.get("attachments") or [],
                            },
                        ))
                    elif msg.get("type") == "remember":
                        text = (msg.get("text") or "").strip()
                        if not text:
                            continue
                        await bus.publish(Event(
                            topic="memory.remember",
                            source="app",
                            payload={"text": text, "kind": msg.get("kind", "fact"),
                                     "conversation_id": msg.get("conversation_id")},
                        ))
                    elif msg.get("type") == "core":
                        await memory.set_core(msg.get("text", ""))
                        await bus.publish(Event(
                            topic="memory.core.set", source="app",
                            payload={"text": await asyncio.to_thread(memory.get_core)},
                        ))
                    elif msg.get("type") == "autoextract":
                        agent.set_autoextract(bool(msg.get("on")))
                        await bus.publish(Event(
                            topic="memory.autoextract", source="app",
                            payload={"on": agent.autoextract},
                        ))
                    elif msg.get("type") == "tools":
                        agent.set_tools(bool(msg.get("on")))
                        await bus.publish(Event(
                            topic="agent.tools", source="app",
                            payload={"on": agent.tools_enabled},
                        ))
                    elif msg.get("type") == "actions":
                        agent.set_actions(bool(msg.get("on")))
                        await bus.publish(Event(
                            topic="agent.actions", source="app",
                            payload={"on": agent.actions_enabled},
                        ))
                    elif msg.get("type") == "dev":
                        tool_loop.set_dev(bool(msg.get("on")))
                        await bus.publish(Event(
                            topic="agent.dev", source="app",
                            payload={"on": tool_loop.dev_enabled},
                        ))
                    elif msg.get("type") == "proactive":
                        proactive.set_enabled(bool(msg.get("on")))
                        await bus.publish(Event(
                            topic="agent.proactive", source="app",
                            payload={"on": proactive.enabled},
                        ))
                    elif msg.get("type") == "proactive_now":
                        # Manual "what's on your mind?" nudge (bypasses cadence/cooldown).
                        await proactive.check_now(bus)
                except WebSocketDisconnect:
                    raise
                except Exception:
                    # Same reasoning as above, for the handlers themselves: one
                    # branch blowing up on a payload it didn't expect shouldn't
                    # cost Jake the socket he'd have to reload the page to get back.
                    log.exception("websocket message failed (type=%r)",
                                  msg.get("type") if isinstance(msg, dict) else None)
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("websocket error")
        finally:
            # In a finally, not per-except, because CancelledError is a
            # BaseException on 3.10+: uvicorn cancelling handlers on shutdown or
            # reload missed both clauses above and left the socket in the hub's
            # client set forever, where every broadcast then tried to write to it.
            hub.disconnect(sock)

    app.mount("/static", _RevalidatingStatic(directory=str(STATIC)), name="static")
    # Outermost layer: nothing reaches a route until it proves who it is.
    # Both allow-lists are resolved *here*, on the main thread, because working
    # them out shells out to `tailscale`. Starlette builds middleware lazily, so
    # leaving it to the Guard constructor ran that subprocess inside the first
    # request and froze the whole server for as long as tailscaled took to answer
    # — worst right after a resume, which is exactly when you open the dashboard.
    app.add_middleware(Guard, allowed=allowed_logins(), origins=allowed_origin_hosts())
    return app


def serve(host: str = "127.0.0.1", port: int = 8848) -> None:
    """Headless: run the server without a native window (opens nothing)."""
    import uvicorn

    from .window import _port_open, _wait_port_free, stop_running

    # `log_level="info"` below only ever configured uvicorn's own loggers, never
    # the root one, so every `continuum.*` logger fell through to Python's
    # `logging.lastResort` handler — which starts at WARNING. The effect on the
    # hub was that ERROR tracebacks appeared in the journal with none of the
    # context that explains them: "dropped websocket client (...)" and every
    # other `log.info` in this app have zero hits across the whole journal.
    # Debugging the websocket noise meant reasoning backwards from a traceback
    # whose cause was being logged the whole time, into nowhere.
    #
    # Root stays at WARNING so this doesn't turn on INFO for httpx, asyncio and
    # every other library in the process; only Continuum's own tree is lowered.
    # The handler basicConfig installs is level 0, so records propagating up from
    # `continuum` pass through it regardless of root's own level.
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("continuum").setLevel(logging.INFO)

    if _port_open(host, port):
        log.info("Continuum already on %s:%d — replacing it", host, port)
        stop_running(port)
        _wait_port_free(host, port)

    uvicorn.run(create_app(), host=host, port=port, log_level="info")
