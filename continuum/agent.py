"""The agent — Continuum's brain.

It listens for task.request events, recalls relevant memory, sends the task
through the router, publishes a task.result, notifies you, and records the
interaction back into memory. Memory is injected as prompt *context* rather than
fine-tuned in or held in a provider's own store, so whichever rung answers sees
the same knowledge — and so the knowledge outlives any one model. That mattered
most when the rungs were different models on different machines; it still
matters, because the day a better model appears, Continuum keeps everything it
knows about Jake and simply changes who it asks.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from .bus import Bus
from .config import Config
from .conversations import ConversationStore
from .events import Event
from .memory import Extractor, MemoryReasoner, MemoryStore
from .notify import notify
from .router import Router
from .state import StateTracker
from .tools import ToolLoop

log = logging.getLogger("continuum.agent")


class Agent:
    def __init__(self, bus: Bus, cfg: Config, memory: Optional[MemoryStore] = None,
                 extractor: Optional[Extractor] = None,
                 state: Optional[StateTracker] = None,
                 tools: Optional[ToolLoop] = None,
                 conversations: Optional[ConversationStore] = None,
                 reasoner: Optional[MemoryReasoner] = None,
                 routines: Optional[object] = None,
                 phone: Optional[object] = None) -> None:
        self._bus = bus
        self._router = Router(cfg)
        self._memory = memory
        self._extractor = extractor
        self._state = state
        self._tools = tools
        self._routines = routines
        self._phone = phone
        self._conversations = conversations
        self._reasoner = reasoner
        self._autoextract = bool(extractor) and cfg.memory_autoextract

    # The tools switch is ToolLoop's state, not Agent's — Agent no longer calls a
    # tool itself, it just hands the turn to a model that reaches them over MCP.
    # Keeping a second copy of the flag here is how it went stale in the first
    # place, so these now delegate, exactly like `actions_enabled` below.
    @property
    def tools_enabled(self) -> bool:
        return bool(self._tools) and self._tools.tools_enabled

    def set_tools(self, on: bool) -> None:
        if self._tools:
            self._tools.set_tools(on)

    @property
    def actions_enabled(self) -> bool:
        return bool(self._tools) and self._tools.actions_enabled

    def set_actions(self, on: bool) -> None:
        if self._tools:
            self._tools.set_actions(on)

    @property
    def autoextract(self) -> bool:
        return self._autoextract

    def set_autoextract(self, on: bool) -> None:
        self._autoextract = bool(on) and self._extractor is not None

    def attach(self) -> None:
        self._bus.subscribe("task.request", self._on_task)
        self._bus.subscribe("device.register", self._on_device)
        self._bus.subscribe("memory.remember", self._on_remember)

    async def _on_device(self, event: Event) -> None:
        caps = ", ".join(event.payload.get("capabilities", [])) or "none"
        log.info("device online: %s [%s]", event.source, caps)

    async def _on_remember(self, event: Event) -> None:
        """Explicit 'remember this' — from the UI, a routine, anything on the bus."""
        if not self._memory:
            return
        kind = event.payload.get("kind", "fact")
        res = await self._memory.remember(
            event.payload.get("text", ""),
            kind=kind, source=event.source, meta=event.payload.get("meta"),
            conversation_id=event.payload.get("conversation_id"),
        )
        # Surface the consolidation outcome so the UI can dedup/replace, not just append.
        await self._bus.publish(Event(
            topic="memory.stored", source="memory",
            payload={"id": res.id, "op": res.op, "kind": kind,
                     "importance": res.importance, "superseded": res.superseded_id,
                     "text": event.payload.get("text", "")},
        ))

    @staticmethod
    def _attach_note(attachments: Optional[list]) -> str:
        """A one-line prompt cue telling the model an image is attached and that it
        must call view_image to see it — the id is the whole handle, and without
        this line the model has no way to know a picture arrived at all."""
        ids = [str(a) for a in (attachments or []) if a]
        if not ids:
            return ""
        n = len(ids)
        it = "it" if n == 1 else "them"
        return (f"Jake attached {n} image{'' if n == 1 else 's'} to this message. "
                f"Use the view_image tool to read {it} before answering — you cannot "
                f"see the picture otherwise. "
                f"Attachment id{'' if n == 1 else 's'}: {', '.join(ids)}.")

    @staticmethod
    def _compose(prompt: str, core: str, memories: list[str], state: str = "",
                 history: Optional[list[tuple[str, str]]] = None,
                 attachments_note: str = "") -> str:
        # Directive framing beats a hedged one: "facts you know — use them" gets
        # acted on where "context you may use…" gets skimmed. Core = pinned
        # profile; memories = retrieved long tail; state = live "right now"
        # snapshot; history = prior turns of this conversation. There was a
        # `gathered` section too, holding what the local triage loop had looked
        # up before the answering model ever saw the question; that model now
        # does its own looking, mid-thought, so the section had nothing to hold.
        if not (core or memories or state or history or attachments_note):
            return prompt
        parts = ["You know the following about the user (Jake). Use it when relevant."]
        if core:
            parts.append(f"Profile:\n{core}")
        if memories:
            parts.append("Facts:\n" + "\n".join(f"- {m}" for m in memories))
        if state:
            parts.append(state)
        if attachments_note:
            parts.append(attachments_note)
        if history:
            turns = "\n".join(
                f"{'Jake' if r == 'user' else 'Continuum'}: {c}" for r, c in history)
            parts.append("Conversation so far (for context; continue it naturally):\n" + turns)
        return "\n\n".join(parts) + f"\n\nJake now says: {prompt}"

    def _resolve_conversation(self, task: dict, prompt: str) -> tuple[Optional[int], list]:
        """Return (conversation_id, prior_turns), loaded BEFORE this message.
        - new_task=True → break off a fresh task conversation.
        - a valid conversation_id → that thread.
        - otherwise → the day-to-day main thread (the default home)."""
        if not self._conversations:
            return None, []
        conv_id = task.get("conversation_id")
        if task.get("new_task"):
            conv_id = self._conversations.create(title=prompt[:60], kind="task")
            prior = []
        elif conv_id and self._conversations.exists(int(conv_id)):
            conv_id = int(conv_id)
            prior = self._conversations.recent_turns(conv_id, n=8)
        else:
            conv_id = self._conversations.main()
            prior = self._conversations.recent_turns(conv_id, n=8)
        return conv_id, [(m.role, m.content) for m in prior]

    async def _on_task(self, event: Event) -> None:
        task = event.payload
        title = task.get("title", "task")
        request_id = event.id  # carry so interfaces can match result -> request
        prompt = task.get("prompt", "")
        # A turn Jake started himself comes off the WebSocket as source "app"; the
        # scheduler ("scheduler") and proactive engine run into an empty room. Only
        # a live turn may ask_user, because only then is there someone to answer.
        live = event.source == "app"
        log.info("task: %s", title)

        # 0. Resolve the conversation and load prior turns, then record this message.
        #    Attachments (image ids Jake uploaded with this message) ride in the
        #    task payload; persist them in the user message's meta so a reopened
        #    thread can re-render the pictures, and turn them into a prompt cue so
        #    the model knows to call view_image.
        attachments = [str(a) for a in (task.get("attachments") or []) if a]
        attach_note = self._attach_note(attachments)
        conv_id, history = self._resolve_conversation(task, prompt)
        user_msg_id = None
        if conv_id is not None:
            user_msg_id = self._conversations.append(
                conv_id, "user", prompt,
                {"attachments": attachments} if attachments else None)

        # 1. Recall — the same store, whatever model ends up answering. The core
        #    profile is always injected; the long tail is retrieved by relevance.
        used: list[str] = []
        core = ""
        if self._memory:
            core = self._memory.get_core()
            try:
                # Answer from durable knowledge; episodes are the activity log,
                # not answer material, so they don't add noise to factual queries.
                # Scope: global facts + anything scoped to THIS conversation.
                used = [m.text for m in
                        await self._memory.recall(prompt, k=5, kinds={"fact", "preference"},
                                                  conversation_id=conv_id)]
            except Exception:
                log.exception("recall failed")  # memory must never break a task

        state_basic = self._state.summary_text() if self._state else ""

        # Passive routine awareness — so Continuum knows Jake's routine and what's
        # left today without having to be asked. Woven in alongside live state.
        if self._routines:
            try:
                routine_ctx = self._routines.context_line()
                if routine_ctx:
                    state_basic = f"{state_basic}\n{routine_ctx}" if state_basic else routine_ctx
            except Exception:
                log.exception("routine context failed")  # never block a task

        # The phone is a second body, and where Jake's body is changes the right
        # answer — a nudge that makes sense at the desk is noise on a train. One
        # terse line, and only when the phone has actually said something.
        if self._phone:
            try:
                phone_ctx = self._phone.summary_text()
                if phone_ctx:
                    state_basic = f"{state_basic}\n{phone_ctx}" if state_basic else phone_ctx
            except Exception:
                log.exception("phone context failed")

        # There used to be a pre-pass here: a local tool loop that decided what to
        # inspect/recall for this exact question, and handed the findings to the
        # router as extra context. It existed because the local model was cheap
        # enough to spend a whole turn on triage. With one cloud tier answering
        # everything, that turn is neither cheap nor needed — the answering model
        # reaches the same tools itself over MCP, in the same loop it reasons in,
        # which is strictly better: it can ask a follow-up the pre-pass couldn't
        # anticipate. So nothing is gathered up front any more.
        tools_used: list[dict] = []
        routed = {**task, "prompt": self._compose(prompt, core, used, state_basic,
                                                  history, attach_note)}

        # 2. Route + run. Mark the turn in flight so the tool calls coming back
        #    over MCP stream as live steps to this bubble; the finally clears it
        #    even on the error path.
        run_started = time.time()
        if self._tools:
            self._tools.begin_turn(request_id, prompt, live=live)
        try:
            output, decision = await self._router.run(routed)
        except Exception as exc:
            log.exception("task failed")
            notify("Continuum — error", f"{title}: {exc}")
            await self._bus.publish(Event(
                topic="task.result", source="agent",
                payload={"title": title, "error": str(exc), "request_id": request_id,
                         "conversation_id": conv_id},
            ))
            return
        finally:
            if self._tools:
                self._tools.end_turn(request_id)

        # The cloud tier calls tools over MCP, outside the local gather() loop —
        # pick those up so the turn reports everything that actually ran.
        if self._tools:
            tools_used = tools_used + self._tools.mcp_calls_since(run_started)

        meta = {
            "tier": decision.tier.name, "reason": decision.reason,
            "model": decision.tier.model, "memories_used": len(used),
            "state_used": bool(state_basic), "tools_used": tools_used,
        }
        asst_msg_id = None
        if conv_id is not None:
            asst_msg_id = self._conversations.append(conv_id, "assistant", output, meta)

        await self._bus.publish(Event(
            topic="task.result",
            source="agent",
            payload={
                "title": title,
                "output": output,
                "request_id": request_id,
                "conversation_id": conv_id,
                **meta,
            },
        ))
        notify(f"Continuum ({decision.tier.name})", output)

        # 3. Index this turn for semantic search at its source. Past turns used to
        #    be copied into memory.db as truncated 'episode' rows; now the full
        #    messages in conversations.db carry their own embeddings, so a thread is
        #    searchable where it lives, linked by conversation_id — and memory.db
        #    stays strictly durable knowledge (facts / preferences / core).
        if self._conversations and conv_id is not None:
            try:
                await self._conversations.index_turn(user_msg_id, asst_msg_id)
            except Exception:
                log.exception("index turn failed")  # search is best-effort, never blocks

        # 3b. Tag a brand-new *task* conversation from its opening message (once).
        #     The main thread is day-to-day, not one topic, so it isn't tagged.
        if (conv_id is not None and not history and self._reasoner and self._conversations
                and not self._conversations.is_main(conv_id)):
            try:
                tags = await self._reasoner.tag(prompt)
                if tags:
                    self._conversations.set_tags(conv_id, tags)
                    await self._bus.publish(Event(
                        topic="conversation.tagged", source="agent",
                        payload={"conversation_id": conv_id, "tags": tags}))
            except Exception:
                log.exception("conversation tagging failed")

        # 4. Learn durable facts from what Jake said (Mem0-style extraction).
        #    Runs after the answer is already delivered, so it adds no latency to
        #    the response, and never breaks the task if it fails.
        if self._autoextract and self._memory and self._extractor:
            try:
                await self._extract_facts(prompt, request_id)
            except Exception:
                log.exception("auto-extract failed")

    async def _extract_facts(self, message: str, request_id: str) -> None:
        facts = await self._extractor.extract(message)
        for f in facts:
            res = await self._memory.remember(
                f["text"], kind=f["kind"], source="extractor",
                meta={"auto": True, "from": request_id},
            )
            if res.op in ("ADD", "UPDATE"):
                log.info("auto-learned [%s] %s", res.op, f["text"][:60])
                await self._bus.publish(Event(
                    topic="memory.stored", source="extractor",
                    payload={"id": res.id, "op": res.op, "kind": f["kind"],
                             "importance": res.importance, "superseded": res.superseded_id,
                             "source": "extractor", "text": f["text"]},
                ))
