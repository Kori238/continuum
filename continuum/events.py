"""Events — the vocabulary every device, sensor, and agent speaks on the bus.

Topics are dotted strings so the bus can pattern-match (e.g. "device.*").
Conventions used in slice one:
  device.register   — a device announces itself and its capabilities
  task.request      — something wants the agent to do work
  task.result       — the agent finished
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Event:
    topic: str
    payload: dict[str, Any] = field(default_factory=dict)
    source: str = "unknown"
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    ts: float = field(default_factory=time.time)

    def __repr__(self) -> str:  # keep logs readable
        return f"<Event {self.topic} from={self.source} id={self.id}>"
