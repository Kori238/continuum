"""The consolidation reasoner — memory hygiene, borrowed from Mem0.

When a durable memory is written, we don't blindly append. We find the nearest
existing memories and ask a model whether the new note is genuinely new (ADD),
already known (NOOP), or a change to something we already believe (UPDATE). It
also rates the note's long-term importance (from the Generative Agents playbook)
for recall ranking.

The judgment used to run on the local model, on the grounds that memory writes
should be free and private. They are now neither: every consolidation is a paid
call, and every candidate memory — which is by definition the most personal text
in the system — is sent. That's why it runs on the FAST rung rather than the
expensive one (CONTINUUM_REASONER=cloud overrides), and why the fallback matters
more than it used to: if the call fails or returns garbage, the caller falls back
to a plain ADD, so a provider outage costs money and tidiness, never information.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from ..config import Config
from ..providers import provider_for, tier_for

log = logging.getLogger("continuum.memory.reason")

_JSON = re.compile(r"\{.*\}", re.DOTALL)
_JSON_ARRAY = re.compile(r"\[.*\]", re.DOTALL)

_PROMPT = """You maintain a user's long-term memory. Decide how a NEW note relates to the EXISTING notes.

EXISTING NOTES:
{neighbors}

NEW NOTE:
{candidate}

Choose exactly ONE operation:
- NOOP: the new note adds nothing beyond an existing note.
- UPDATE <id>: the new note corrects, changes, or replaces exactly one existing note (that note is now outdated).
- ADD: the new note is new information not covered above.

Also rate the new note's long-term importance from 1 (trivial) to 10 (defining fact about the user).

Also give 3-6 short lowercase tags that categorize the NEW note by topic and entity, so it can be found later. Use general categories AND specifics — e.g. for "drives a blue Subaru": ["car","vehicle","possessions","subaru"].

Reply with ONLY a JSON object and nothing else:
{{"op": "ADD" | "NOOP" | "UPDATE", "target": <existing id or null>, "importance": <1-10>, "tags": ["...", "..."]}}"""


@dataclass
class Judgment:
    op: str                 # "ADD" | "UPDATE" | "NOOP"
    target: Optional[int]   # id being updated/duplicated, when relevant
    importance: float
    tags: list[str]         # topic/entity tags for hybrid retrieval


class MemoryReasoner:
    """Memory-reasoning model (consolidation + tagging — i.e. how memories relate).
    Runs on the fast rung by default, or the heavy one when the memory
    relationships need a stronger model (CONTINUUM_REASONER=cloud). Both rungs
    expose the same generate(tier, prompt), so the choice is a one-line swap."""

    def __init__(self, cfg: Config, provider: Any = None, tier: Any = None) -> None:
        if provider is not None:
            self._provider, self._tier = provider, tier
        else:
            self._tier = tier_for(cfg, getattr(cfg, "reasoner_tier", "fast"))
            self._provider = provider_for(cfg, self._tier)

    async def available(self) -> bool:
        return await self._provider.available()

    async def tag(self, text: str, n: int = 6) -> list[str]:
        """Standalone tagger — normalized topic/entity tags for arbitrary text
        (e.g. a whole conversation). Reused so tags are consistent across the app."""
        prompt = (
            "Give 3-6 short lowercase tags (topics and entities) that categorize the "
            "following, so it can be found later. Reply with ONLY a JSON array.\n\n"
            f"TEXT:\n{text[:1200]}\n\nJSON:"
        )
        try:
            raw = await self._provider.generate(self._tier, prompt)
        except Exception:
            return []
        m = _JSON_ARRAY.search(raw)
        if not m:
            return []
        try:
            return normalize_tags(json.loads(m.group(0)), limit=n)
        except Exception:
            return []

    async def consolidate(self, candidate: str,
                          neighbors: list[tuple[int, str]]) -> Judgment:
        listing = "\n".join(f"[{i}] {t}" for i, t in neighbors) or "(none)"
        prompt = _PROMPT.format(neighbors=listing, candidate=candidate)
        raw = await self._provider.generate(self._tier, prompt)
        m = _JSON.search(raw)
        if not m:
            raise ValueError(f"no JSON in reasoner output: {raw[:120]!r}")
        data = json.loads(m.group(0))

        op = str(data.get("op", "ADD")).upper()
        if op not in ("ADD", "UPDATE", "NOOP"):
            op = "ADD"
        target = data.get("target")
        valid_ids = {i for i, _ in neighbors}
        if op in ("UPDATE", "NOOP"):
            try:
                target = int(target)
            except (TypeError, ValueError):
                target = None
            if target not in valid_ids:
                # Model referenced an id it wasn't given — safest is to keep both.
                op, target = "ADD", None
        else:
            target = None
        try:
            importance = max(1.0, min(10.0, float(data.get("importance", 5))))
        except (TypeError, ValueError):
            importance = 5.0
        return Judgment(op=op, target=target, importance=importance,
                        tags=normalize_tags(data.get("tags")))


def normalize_tags(raw: Any, limit: int = 6) -> list[str]:
    """Clean a model's tag output into short, lowercase, de-duplicated tags."""
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for t in raw:
        t = str(t).strip().lower()
        if 2 <= len(t) <= 24 and t not in out:
            out.append(t)
        if len(out) >= limit:
            break
    return out


_EXTRACT_PROMPT = """You extract durable facts about the user (Jake) worth remembering long-term, from a message they sent to their assistant.

Extract:
- stable facts about the user: their life, people, pets, possessions, work, location, health
- lasting preferences (prefers/likes/dislikes/always/never)
- CHANGES: if the user stopped, sold, quit, left, ended, or no longer has/does something,
  record that as a fact so old memory can be corrected — e.g. "Jake no longer owns a Canyon bike",
  "Jake no longer works at Google". Do this IN ADDITION to any new fact that replaces it.

Do NOT extract:
- questions, requests, or task instructions
- transient one-off details, or anything not about the user
- anything not clearly stated by the user

Rewrite each as a short, self-contained sentence in the third person ("Jake ...").

EXAMPLES
Message: "what's the weather tomorrow?"
JSON: []
Message: "I just adopted a golden retriever named Rex"
JSON: [{{"text": "Jake has a golden retriever named Rex.", "kind": "fact"}}]
Message: "keep answers short, I really dislike long explanations"
JSON: [{{"text": "Jake prefers short answers and dislikes long explanations.", "kind": "preference"}}]
Message: "I sold my Canyon and now ride a Specialized Tarmac"
JSON: [{{"text": "Jake no longer owns a Canyon bike (sold it).", "kind": "fact"}}, {{"text": "Jake rides a Specialized Tarmac.", "kind": "fact"}}]

Now do the same. Reply with ONLY a JSON array (use [] if nothing qualifies).

Message: "{message}"
JSON:"""


class Extractor:
    """Auto-extraction (Mem0's first stage): pull durable facts from a user
    message so memory grows from normal conversation, not just explicit
    'remember X'. Extracted facts flow through the store's consolidation, so
    dedup and contradiction handling come for free. Runs on the fast rung;
    any failure yields no facts (never fabricates, never blocks the task).
    """

    def __init__(self, cfg: Config, provider: Any = None, tier: Any = None) -> None:
        if provider is not None:
            self._provider, self._tier = provider, tier
        else:
            self._tier = tier_for(cfg, getattr(cfg, "reasoner_tier", "fast"))
            self._provider = provider_for(cfg, self._tier)

    async def extract(self, message: str, limit: int = 5) -> list[dict]:
        message = (message or "").strip()
        if len(message) < 12:
            return []
        raw = await self._provider.generate(self._tier, _EXTRACT_PROMPT.format(message=message))
        m = _JSON_ARRAY.search(raw)
        if not m:
            return []
        try:
            items = json.loads(m.group(0))
        except Exception:
            return []
        out: list[dict] = []
        for it in items if isinstance(items, list) else []:
            if not isinstance(it, dict):
                continue
            text = str(it.get("text", "")).strip()
            kind = str(it.get("kind", "fact")).lower()
            if not text or kind not in ("fact", "preference"):
                continue
            out.append({"text": text, "kind": kind})
            if len(out) >= limit:
                break
        return out
