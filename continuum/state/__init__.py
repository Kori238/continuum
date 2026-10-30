"""Live state — the "right now" half of the digital twin.

Continuum should know what you're doing this moment: which apps are open, how
long they've been open, what's in focus, how the machine is doing. Two tiers,
on purpose:

  * basic()   — a compact, always-available snapshot, cheap to gather and small
                enough to inject into the agent's context every turn.
  * inspect() — deep detail about one thing (a process/app), fetched on demand.
                This is the "request more" primitive: the app drills down when
                you click, and the agent can ask for it when a question needs it.
"""
from .tracker import StateTracker

__all__ = ["StateTracker"]
