"""The desktop — Continuum's home base, from either side of the wire.

Two classes, because there are two situations and they are not symmetrical.

`DesktopDevice` is the desktop describing *itself*, in-process, the way it has
since slice one. It is what runs when Continuum and the desktop are the same
machine.

`DesktopLink` is the hub's *view of a desktop somewhere else* — the same shape
as `PhoneDevice`, because it is the same problem. When the referee moves to a
box in Germany it can no longer call into Windows to ask what's in front; the
desktop has to tell it, and take instructions back. `budget.evaluate()` runs on
the hub unchanged and the actions travel down this socket to `enforce`.

The one thing worth understanding here is `basic()`. The hub bills time against
whatever the desktop last said was in the foreground, so the honest handling of
silence is not "assume the last reading still holds" — it's "stop counting".
See the note on `basic()`; it's the only place in this file where the wrong
choice would quietly cost Jake real budget.
"""
from __future__ import annotations

import logging
import platform
from typing import Any

from .. import deskops
from ..bus import Bus
from ..events import Event
from ..deskops import OPS as DESK_OPS
from .remote import RemoteDevice

log = logging.getLogger("continuum.devices.desktop")

# What a *desk* can do for the fabric. "local-models" used to lead the list; it
# was removed with Ollama, and the distinction matters because these strings are
# how a hub decides what to ask of a device — advertising inference this box no
# longer serves would route work to a machine that would refuse it. "gpu-compute"
# stays: the card is still there, still idle, still available to anything Jake
# wires up next.
DESK_CAPABILITIES = [
    "system-state",     # live snapshot of open apps, focus, and metrics
    "ui-automation",    # drive web/desktop UIs — tabs, message windows
    "notifications",    # Windows toast
    "gpu-compute",      # RTX 4070 SUPER
]

# Kept for anything still importing the old name.
CAPABILITIES = DESK_CAPABILITIES

# Commands a remote desktop is expected to implement.
#
# The first three are the budget's: the hub decides, the desktop acts, and the
# only acts a verdict needs are "say something" and "close that". The rest are
# the agent's tools, and they are taken straight from `deskops.OPS` rather than
# listed again here. Two lists would drift, and the drift has a direction — the
# hub gates on this tuple *before* sending, so a command missing here is
# rejected locally with "unknown desktop command" and never reaches a client
# that implements it perfectly well. One list, one truth.
COMMANDS = (
    "notify",          # raise a desktop toast
    "close_process",   # terminate (never kill) a process by name
    "state",           # ask for an immediate state report rather than waiting
) + tuple(DESK_OPS)

# The client reports on the tracker's own cadence (~5s). Thirty seconds is
# several missed reports — comfortably past a slow poll, comfortably short of a
# nap.
STATE_FRESH = 30.0


class DesktopDevice:
    """This machine, announcing itself on the local bus.

    Announcing itself *as what it is*. Both the name and the capability list
    used to be constants written when this only ever ran on Jake's desktop, so
    after the migration the hub introduced itself to its own device registry as
    `desktop:continuum-hub` offering `notifications`, `ui-automation` and
    `gpu-compute` — a headless Debian VM with no screen, no UIAutomation and no
    GPU, claiming all three. Nothing dispatched on it yet, which is the only
    reason it never broke anything; it was a trap set for the first thing that
    did, and it put a second "desktop" in the registry beside the real one.
    """

    def __init__(self, bus: Bus) -> None:
        self._bus = bus
        # Called through the module rather than imported by name, so the one
        # question "is this host a desk?" has exactly one answerable definition
        # — and so a test can replace it without reaching into two namespaces.
        desk = deskops.local_desk()
        # A hub is not a desktop with missing features, it is a different kind
        # of machine, and the registry should be able to tell them apart at a
        # glance — including in the UI's device panel, where two rows both
        # called `desktop:…` is exactly the confusion to avoid.
        self.name = f"{'desktop' if desk else 'hub'}:{platform.node()}"
        self._caps = list(DESK_CAPABILITIES) if desk else ["hub"]

    async def register(self) -> None:
        await self._bus.publish(Event(
            topic="device.register",
            source=self.name,
            payload={"capabilities": self._caps, "os": platform.platform()},
        ))


class DesktopLink(RemoteDevice):
    """A desktop on the far end of a socket, as the hub sees it."""

    label = "desktop"
    topic_prefix = "desktop"
    default_os = "Windows"
    commands = COMMANDS
    fresh_seconds = {"state": STATE_FRESH}
    known_signals = frozenset({"state"})

    def basic(self) -> dict[str, Any]:
        """The desktop's last state report, in the shape `tracker.basic()` returns
        — or `{}` when we don't currently know.

        `{}` is load-bearing. The caller feeds this straight into
        `activity.note_desktop()` and then `budget.evaluate()`, and an empty dict
        yields no foreground, which closes the open activity segment and stops
        the clock. That is the correct direction to fail in, and the asymmetry is
        the whole argument: under-counting costs Jake a few unbilled minutes,
        while over-counting spends an allowance he wasn't using and eventually
        terminates a game he only just opened. A dropped Wi-Fi link, a laptop
        asleep, a client killed mid-session — all of them look like silence here,
        and none of them are evidence that he is still playing.

        So a stale reading is not "the best we have" for this signal, the way an
        old battery level is. It is simply not an answer, and it says so.
        """
        rec = self.get("state")
        if not rec or rec.get("stale"):
            return {}
        # `get()` merges its own bookkeeping into the value; strip it so callers
        # see exactly what the tracker produced and nothing they didn't expect.
        return {k: v for k, v in rec.items() if k not in ("age_seconds", "stale")}

    async def refresh(self, timeout: float = 6.0) -> bool:
        """Ask the desktop for a state report now, rather than waiting for its
        next scheduled one. Returns whether a fresh reading arrived.

        The client has answered `state` since it was written and nothing ever
        sent it — the same receiver-with-no-sender shape that left `notify`
        looking implemented for a month. It earns its keep in one specific
        moment: the socket is open but the last reading has aged past
        `STATE_FRESH`, so `basic()` is returning `{}` and every surface is
        correctly refusing to guess. That means reports stopped, not that the
        desktop went away, and the difference is worth one round trip before
        telling Jake we don't know what's on his screen.

        No polling afterwards, because the ordering is already guaranteed: the
        client sends its report *before* the result frame, over the same socket,
        and this end processes frames in order — so by the time the command
        resolves, `ingest` has already run. Checking `basic()` is therefore the
        honest test of whether it worked, including when the client answered
        with an error rather than a reading.
        """
        if not self.connected:
            return False
        try:
            await self.command("state", {}, timeout=timeout)
        except Exception as exc:
            log.debug("desktop state refresh failed: %s", exc)
            return False
        return bool(self.basic())

    def summary_text(self) -> str:
        """One line for the agent, mirroring PhoneDevice.summary_text()."""
        if not self.connected and not self._signals:
            return "Desktop: not connected"
        b = self.basic()
        if not b:
            rec = self.get("state")
            if rec and rec.get("age_seconds") is not None:
                mins = max(1, int(rec["age_seconds"]) // 60)
                return f"Desktop: silent for {mins}m — not counting time"
            return "Desktop: connected, no readings yet"
        fg = b.get("foreground") or {}
        pres = b.get("presence") or {}
        bits = []
        if fg.get("name"):
            bits.append(f"in {fg['name']}")
        if pres.get("state") == "away":
            bits.append(f"away (idle {pres.get('away_for', 'a while')})")
        if not self.connected:
            bits.append("offline")
        return "Desktop: " + ", ".join(bits) if bits else ""


class StateSource:
    """Which machine `basic()` describes, decided per call.

    `_state_loop` has always known that an attached desktop supersedes this
    host's own probes. Nothing else did: the proactive engine, the significance
    gate and the agent's context were each handed the local
    `StateTracker` at startup and went on reading it after the hub moved to
    Linux — where `probe.idle_seconds()` returns None and `tracker` reads that
    as present, pinning presence to "active" for the life of the process.

    That is worse than a missing signal, because two features branch on it and
    each took the wrong branch. Proactive nudges suppress only while he is away,
    so they started firing precisely when he cannot see them. Routine auto-start
    believed he had been at his desk since the hub booted.

    Resolved per call rather than chosen once in `create_app()`, because the
    desktop detaches — a closed lid, a reboot, a dropped tunnel — and a source
    picked at startup would outlive the socket it was picked for.
    """

    def __init__(self, local: Any, link: DesktopLink) -> None:
        self._local = local
        self._link = link

    @property
    def local(self) -> bool:
        """True when this host is describing itself rather than relaying a
        desktop's report.

        Note what this does *not* mean: "this host is a desk". On a hub with
        nothing attached it is still True, because the hub has no better answer
        than its own probes — but its own probes are meaningless, and a tool
        that read this as permission to go and look would be back to inventing
        facts about a machine in another country. That question is
        `deskops.local_desk()`, and it is a different one.
        """
        return not self._link.connected

    @property
    def tracker(self) -> Any:
        """The local `StateTracker`, for the few things that need the object
        rather than its readings — `deskops` resolving an app name against the
        apps this host has been watching, when this host is the desk."""
        return self._local

    async def refresh(self) -> bool:
        """Ask the desk for a reading, when the one we hold has expired.

        Only ever does anything for a linked desktop: the local tracker has its
        own poll loop and `basic()` reads whatever it last wrote, so there is
        nothing to ask it for. Callers use this before reporting an empty
        reading as a fact about Jake's machine.
        """
        if self.local:
            return bool(self._local.basic())
        return await self._link.refresh()

    def basic(self) -> dict[str, Any]:
        # Deliberately not "prefer the link, fall back to local". A connected
        # desktop that has gone quiet returns `{}`, and falling back there would
        # answer with this VM's idle timer — which is the exact reading that
        # caused the bug. Silence from the real machine has to stay silence.
        return self._local.basic() if self.local else self._link.basic()

    def summary_text(self) -> str:
        return self._local.summary_text() if self.local else self._link.summary_text()
