"""The router — dynamic model allocation.

Given a task, decide which rung of the ladder handles it.

This used to score three axes: sensitivity, complexity and availability. Two of
them are gone, and it is worth writing down why so nobody helpfully adds them
back:

  * SENSITIVITY is gone because the local tier is gone. The old rule pinned
    anything flagged sensitive to a local model and *refused rather than leak*
    when that model was down. That was a real boundary, and it was removed
    deliberately by Jake (2026-09-15), not quietly worked around: every turn now
    goes to Claude. There is no "private" rung left to pin to, so a `sensitive`
    flag would be a lie — a control that looks like it does something and
    doesn't. The UI checkbox was removed in the same change.

  * AVAILABILITY is gone because both rungs are the same provider. When Claude
    is unreachable there is nothing to fall back *to*, so the honest response is
    to raise rather than to pretend a degraded answer is a normal one.

What is left is not really "how hard is this" either — it is "who is asking".
Jake's turns get the heavy model, full stop; Continuum's own internal machinery
(tagging, extraction, the proactive gate) asks for the cheap rung by name. A
`complexity` hint survives on the event for the UI's benefit, but it no longer
routes anything: there is no rung below `cloud` for an easy question to fall to,
and guessing "this one is simple" was never worth answering it worse.

A task is just a dict carried on a task.request event, e.g.:
    {"prompt": "...", "tier": "fast"}
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .config import Config, ModelTier
from .providers import AnthropicProvider, ClaudeCLIProvider

log = logging.getLogger("continuum.router")


@dataclass
class Decision:
    tier: ModelTier
    reason: str


class Router:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        # The cloud tier is reached by exactly one of these, chosen in config.
        # Both rungs use the same one; only the model name differs.
        self._providers = {
            "claude-cli": ClaudeCLIProvider(cfg),
            "anthropic": AnthropicProvider(cfg),
        }

    async def decide(self, task: dict[str, Any]) -> Decision:
        if not self._cfg.cloud_available:
            raise RuntimeError(
                f"no available model: the {self._cfg.cloud.provider} cloud tier is "
                "unreachable, and there is no local tier to fall back to"
            )

        # Internal machinery (tagger, extractor, proactive gate) asks for the
        # cheap rung by name. Jake's own turns never do, so anything he types
        # reaches the heavy model — which is the whole point of the change.
        if str(task.get("tier", "")).lower() == "fast":
            return Decision(self._cfg.cloud_fast, "caller asked for the fast rung")
        return Decision(self._cfg.cloud, f"default -> {self._cfg.cloud.model}")

    async def run(self, task: dict[str, Any]) -> tuple[str, Decision]:
        decision = await self.decide(task)
        log.info("route: %s (%s)", decision.tier.name, decision.reason)
        prompt = task["prompt"]
        provider = self._providers[decision.tier.provider]
        out = await provider.generate(decision.tier, prompt)
        return out, decision
