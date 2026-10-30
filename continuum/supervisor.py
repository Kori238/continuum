"""The supervisor — boots the spine and keeps it alive.

Slice one runs the whole system in one process on the desktop. When Continuum
grows, this is where the NATS server launch, per-service supervision, and
restart-on-crash logic live. Autostart on login is handled by
scripts/continuum-autostart.ps1.

This is the headless boot path (`python -m continuum run`): the same fabric the
desktop app runs, without a window on it. continuum/app/server.py's create_app()
is the path Jake actually uses day to day, which makes it the reference — every
store and device wired there has to be wired here too, or this quietly becomes a
second, lesser program answering to the same name. It had already drifted into
exactly that: half the stores missing, a cloud tier holding no tools at all, and
no supervision of the local model. So when a dependency is added to create_app(),
add it here in the same breath. The handful of things that genuinely cannot exist
without the FastAPI `app` object are named in the comment at the end of build()
rather than stubbed into something that looks wired but isn't.
"""
from __future__ import annotations

import asyncio
import logging

from . import deskops
from .activity import ActivityStore
from .agent import Agent
from .attachments import AttachmentStore
from .body import BodyStore
from .bus import Bus
from .config import Config
from .devices.desktop import DesktopDevice
from .devices.phone import PhoneDevice
from .events import Event
from .conversations import ConversationStore
from .flashcards import FlashcardStore
from .health import HealthStore
from .ledger import ActionLedger
from .messagelog import MessageLog, MessageCollector
from .memory import Extractor, MemoryReasoner, MemoryStore
from .nutrition import NutritionStore
from .routines import RoutineStore
from .proactive import ProactiveEngine
from .scheduler import Scheduler
from .state import StateTracker
from .tools import ToolLoop
from .workouts import WorkoutStore

log = logging.getLogger("continuum.supervisor")


async def build() -> tuple[Bus, Config, Scheduler, ProactiveEngine]:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)-22s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    cfg = Config.load()
    bus = Bus()
    reasoner = MemoryReasoner(cfg)
    memory = MemoryStore(cfg, reasoner=reasoner)
    state = StateTracker()
    scheduler = Scheduler(cfg)
    conversations = ConversationStore(cfg)
    messagelog = MessageLog(cfg)
    routines = RoutineStore(cfg)
    workouts = WorkoutStore(cfg)
    activity = ActivityStore(cfg)
    nutrition = NutritionStore(cfg)
    body = BodyStore(cfg)
    health = HealthStore(cfg)
    flashcards = FlashcardStore(cfg)
    ledger = ActionLedger(cfg)
    attachments = AttachmentStore(cfg.uploads_dir)
    # The phone object exists here even though nothing can connect to it on this
    # path — its socket is a FastAPI websocket route. A PhoneDevice with no socket
    # attached is simply a phone that is offline, which is a state every caller
    # already handles (they all gate on .connected), so wiring it is honest and
    # keeps the construction identical to server.py. Passing None instead would
    # take a different branch and hide the drift again.
    phone = PhoneDevice(bus)
    proactive = ProactiveEngine(cfg, state=state, memory=memory,
                                scheduler=scheduler, conversations=conversations,
                                routines=routines, nutrition=nutrition,
                                body=body, workouts=workouts, phone=phone)
    # Every store the ToolLoop can expose, because the tool list it advertises is
    # derived from which of these are present: a ToolLoop built with eight of them
    # doesn't fail loudly, it just silently offers a smaller Continuum than the one
    # running in the window. Same argument list as create_app(), deliberately.
    tool_loop = ToolLoop(cfg, state=state, memory=memory, bus=bus, scheduler=scheduler,
                         messagelog=messagelog, routines=routines, phone=phone,
                         workouts=workouts, activity=activity, nutrition=nutrition,
                         body=body, health=health, flashcards=flashcards,
                         conversations=conversations, ledger=ledger,
                         attachments=attachments)
    await asyncio.to_thread(state.poll)  # prime a first snapshot
    Agent(bus, cfg, memory, extractor=Extractor(cfg), state=state,
          tools=tool_loop, conversations=conversations, reasoner=reasoner,
          routines=routines, phone=phone).attach()

    async def _record_ledger(ev: Event) -> None:
        """Persist every action — performed or refused — into the durable ledger.
        Bus events fan out and vanish; this is the half that survives. Without it
        the ledger is wired into the ToolLoop for reading and fed by nothing, so
        review_actions answers "nothing happened today" however busy the day was."""
        p = ev.payload or {}
        ledger.record(action=p.get("action", ev.topic),
                      args=p.get("args"), result=str(p.get("result", "")),
                      allowed=(ev.topic != "action.refused"),
                      reason=str(p.get("reason", "")),
                      request_id=str(p.get("request_id", "")),
                      source=ev.source or "")

    bus.subscribe("action.performed", _record_ledger)  # durable audit trail
    bus.subscribe("action.refused", _record_ledger)    # + the notification gate's refusals

    conversations.main()  # ensure the day-to-day main thread exists
    await DesktopDevice(bus).register()
    # Both of the collector's feeds — the Windows toast listener and UIAutomation
    # scraping of the Discord/Slack windows — are Win32. Off Windows it is two
    # empty scrapes every 8 seconds forever, which is how `messages.db` stopped
    # growing the day the hub moved countries. This path has no desktop socket to
    # relay from (that lives in `create_app`), so off Windows there is simply
    # nothing to collect, and saying so beats spinning.
    if deskops.local_desk():
        asyncio.create_task(MessageCollector(messagelog).run())  # capture incoming messages
    else:
        log.info("message collection skipped — this host cannot see Jake's notifications")
    # Index any turns that were never embedded — older threads, or anything
    # written while the embedder was unavailable. Retried on the next boot.
    asyncio.create_task(conversations.backfill())

    # Structurally absent on this path, all for the same reason — they hang off the
    # FastAPI `app` that only create_app() has, and faking them would be worse than
    # not having them:
    #   * mcp.install(app, tool_loop) — the /mcp mount. The cloud tier reaches its
    #     tools over MCP, so on this path the Claude CLI runs with no tools at all:
    #     it can read the context injected into the prompt but cannot act. That is
    #     the single biggest difference between `run` and `app`, and the reason
    #     scripts/continuum-autostart.ps1 now registers `app` instead.
    #   * the /phone/ws websocket — so `phone` above never connects, and with it
    #     the phone.* bus handlers (steps/weight/workout/health → their stores)
    #     would have nothing to fire on; they live in server.py with the route.
    #   * the /ws fan-out hub, the state-poll loop that pushes to it, the deferred
    #     dev-restart, and the whole HTTP API — all UI surface by definition.
    return bus, cfg, scheduler, proactive


async def heartbeat_loop(scheduler: Scheduler, proactive: ProactiveEngine,
                         bus: Bus, interval_s: float) -> None:
    """Continuum's pulse — each beat fires due jobs and, on its slower cadence,
    lets Continuum consider whether to say something proactively.

    This used to open with a supervisor tick that kept the local model alive,
    because when it died every turn silently escalated to the cloud and memory
    stopped entirely. There is no daemon to keep alive now: the cloud IS the
    tier, and the embedder loads in-process."""
    while True:
        try:
            fired = await scheduler.tick(bus)
            if fired:
                log.info("heartbeat fired %d job(s)", fired)
            await proactive.maybe_check(bus)
        except Exception:
            log.exception("heartbeat tick failed")
        await asyncio.sleep(interval_s)


async def run_demo() -> None:
    """The first vertical slice, end to end:
    trigger -> bus -> agent -> router (picks local/cloud) -> notification.
    """
    bus, cfg, _sched, _pro = await build()
    log.info("ladder: %s / %s via %s (reachable=%s)",
             cfg.cloud_fast.model, cfg.cloud.model, cfg.cloud.provider, cfg.cloud_available)

    # Two triggers that exercise both rungs of the ladder.
    await bus.publish(Event(
        topic="task.request", source="demo",
        payload={
            "title": "afternoon focus",
            "prompt": "In one sentence, suggest a focus for my afternoon.",
            "tier": "fast",   # -> the cheap rung
        },
    ))
    await bus.publish(Event(
        topic="task.request", source="demo",
        payload={
            "title": "plan a system",
            "prompt": "In 3 bullet points, outline how to add a phone as a "
                      "second device to an event-bus-based personal assistant.",
            "complexity": "high",  # -> the heavy rung (the default for a real turn)
        },
    ))
    log.info("demo complete.")


async def run_forever() -> None:
    """Boot the fabric and beat the heartbeat, ready for events. Ctrl+C to stop."""
    bus, cfg, scheduler, proactive = await build()
    log.info("Continuum is up. Heartbeat every %.0fs. (Ctrl+C to stop.)", cfg.heartbeat_s)
    try:
        await heartbeat_loop(scheduler, proactive, bus, cfg.heartbeat_s)
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Continuum shutting down.")
