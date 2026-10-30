"""The bus — Continuum's spine. Everything publishes and subscribes here.

Slice one ships an in-process async implementation so the whole system boots
with zero infrastructure. `Bus` is the seam: when Continuum gains a second
device, a NATS-backed adapter implements the same three methods and nothing
above this layer changes. That is the entire point of paying the "bus tax"
up front — cross-device is a transport swap, not a rewrite.
"""
from __future__ import annotations

import asyncio
import fnmatch
import logging
from typing import Awaitable, Callable

from .events import Event

log = logging.getLogger("continuum.bus")

Handler = Callable[[Event], Awaitable[None]]


class Bus:
    """In-process publish/subscribe with glob topic matching."""

    def __init__(self) -> None:
        self._subs: list[tuple[str, Handler]] = []

    def subscribe(self, pattern: str, handler: Handler) -> None:
        self._subs.append((pattern, handler))
        log.debug("subscribe %s -> %s", pattern, getattr(handler, "__qualname__", handler))

    async def publish(self, event: Event) -> None:
        log.debug("publish %s", event)
        coros = [
            self._dispatch(handler, event)
            for pattern, handler in self._subs
            if fnmatch.fnmatch(event.topic, pattern)
        ]
        if coros:
            await asyncio.gather(*coros)

    @staticmethod
    async def _dispatch(handler: Handler, event: Event) -> None:
        try:
            await handler(event)
        except Exception:  # a bad handler must never take down the bus
            log.exception("handler failed for %s", event)
