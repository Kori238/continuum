"""A device on the other side of a socket it opened itself.

The desktop's `state/` probes sense by *asking*: every one is a synchronous call
into Windows that answers on demand. A remote device can't work that way. It
lives across the tailnet, it may sleep, and it decides when it is awake enough
to speak — so the device is the one that talks, and it talks when it has
something to say.

That inverts the shape of a device, and the inversion is identical whether the
far end is an Android phone or a Windows desktop reporting to a hub in another
country. This class is that shape: it polls nothing, it holds the last thing the
device reported along with *when*, and it hands commands back down the socket the
device itself opened. Every reading is therefore a memory of a reading, and the
age matters as much as the value — a battery level from four hours ago is not a
battery level, it's a rumour, and a foreground window from four hours ago is not
what someone is looking at now. `get()` ages every signal so no surface can state
a stale one as fact.

Subclasses supply their vocabulary — which signals they know, how fast each one
decays, which commands the far end implements — and anything device-specific
built on top (the phone's `summary_text`, the desktop's `basic`). Everything
here is transport.

Authentication is not our problem: the socket arrives through Tailscale, which
stamps the identity before `Guard` ever lets the handler run (see
`app/security.py`). We never mint a token for a device to hold.
"""
from __future__ import annotations

import asyncio
import logging
import platform
import time
import uuid
from typing import Any, Optional

from ..bus import Bus
from ..events import Event

log = logging.getLogger("continuum.devices.remote")

# Past its window a reading is still kept — it's the best we have — but every
# surface must mark it as aged rather than assert it.
DEFAULT_FRESH = 15 * 60

# Every field a device sends arrives verbatim over the socket, and nothing in the
# protocol constrains it. `kind` keys `_signals` and `name` keys the server's
# device registry — both are dicts nothing ever prunes, both are serialised into
# an endpoint the UI polls. So a client that varies either per message (a bug as
# easily as an attack) grows memory, payload size and disk without limit. Bound
# them here, at the edge.
#
# `ingest` still *accepts* an unrecognised kind: a newer client build has to be
# able to report something this server predates, and silently dropping it would
# be a miserable thing to debug. It just doesn't get unlimited room to do it.
MAX_UNKNOWN_SIGNALS = 8
UNKNOWN_WARN_LIMIT = 5
MAX_KIND_LEN = 40
MAX_NAME_LEN = 64
MAX_CAPABILITIES = 32

COMMAND_TIMEOUT = 20.0

# ---- reaching a device from outside the app graph ---------------------------
#
# `create_app()` builds the DesktopLink and keeps it in a closure, which is fine
# for anything the server calls directly. It is no use to code far below the
# server that needs to reach a real machine — `notify()` being the case that
# forced this: it sits at the bottom of the import graph, is called from half a
# dozen places that have no device reference, and after the hub moved to Linux
# had no way to reach the desktop at all.
#
# Threading a link down through every caller would touch all of them and mean
# nothing could notify without being handed a device. So an attached device
# announces itself here instead. Keyed by label because exactly one device of
# each class is attached at a time — `attach()` treats the newest socket as the
# real one and replaces the previous.
_attached: dict[str, "RemoteDevice"] = {}


def attached(label: str) -> Optional["RemoteDevice"]:
    """The live device of this class, or None.

    Checks `connected` rather than trusting the dict: detach can be skipped for
    a superseded socket, so presence here is not proof of a usable socket.
    """
    dev = _attached.get(label)
    return dev if dev is not None and dev.connected else None


# The device classes worth naming even when they are absent. A missing desktop
# is not the same fact as a desktop nobody ever built: the tools that need one
# are withheld, so the model has to be told the machine exists and is currently
# unreachable, or it will read their absence as "Jake has no PC".
KNOWN_DEVICES: tuple[tuple[str, str], ...] = (
    ("desktop", "Jake's PC - where he actually sits. Screen, clipboard, open "
                "apps, Discord/Slack, files, browser history."),
    ("phone", "Jake's phone. Battery, location, notifications, its own screen."),
)


def all_attached() -> dict[str, "RemoteDevice"]:
    """Every live device, keyed by label.

    Same `connected` test as `attached()` rather than the raw dict, for the same
    reason: a superseded socket can outlive its detach.
    """
    return {k: d for k, d in _attached.items() if d is not None and d.connected}


def manifest() -> str:
    """What the model is attached to right now, as prose for the system prompt.

    Continuum used to be one process on the machine Jake sat at, so "this
    machine" was unambiguous and nothing had to say which one it meant. After
    the hub moved to a VPS that phrase became a lie in the most expensive
    direction: the model is in Germany, the screen it is asked about is in
    England, and nothing in its context said so. It would answer questions about
    "your machine" with facts about the hub and present them as facts about his
    desk.

    Rebuilt per turn, never cached. Devices attach and drop mid-session, and a
    manifest written at boot describes a topology that has since changed.
    """
    from .. import deskops  # late: deskops reaches back into this module

    live = all_attached()
    # Everything in one process on Jake's own PC - the pre-migration shape, and
    # still what a fresh `python -m continuum app` gives you. The desk is then
    # this very host, so it must NOT also be listed below as an absent device:
    # saying "this is Jake's desk" and "desktop: NOT CONNECTED" in the same
    # breath is worse than saying neither.
    hub_is_desk = deskops.local_desk() and "desktop" not in live

    host = platform.node() or "hub"
    lines = [f"- hub ({host}, {platform.system() or 'unknown OS'}): the machine "
             "you are running on."]
    if hub_is_desk:
        lines[0] += (" This IS Jake's desk: it has his screen, clipboard and "
                     "apps, and the desk tools act on it directly. Your shell "
                     "and dev tools run here too, on that same machine.")
    else:
        lines[0] += (" Headless and remote - no screen, no clipboard, no apps, "
                     "and Jake is not sitting at it. Your shell and dev tools "
                     "run HERE, as does all stored data (memory, routine, "
                     "activity, budget). A command you run here says nothing "
                     "about his PC.")

    for label, what in KNOWN_DEVICES:
        if label == "desktop" and hub_is_desk:
            continue  # already described above; it is this host
        dev = live.get(label)
        if dev is None:
            lines.append(f"- {label}: NOT CONNECTED. {what} Nothing can reach it "
                         "right now, so the tools that need it are withheld this "
                         "turn. Say it is offline rather than answering from the "
                         "hub's own state.")
            continue
        snap = dev.snapshot()
        who = snap.get("model") or label
        os_name = (snap.get("os") or "").split("-")[0] or "unknown OS"
        ago = snap.get("last_seen_ago")
        seen = f", last reported {ago}s ago" if isinstance(ago, int) else ""
        lines.append(f"- {label} ({who}, {os_name}): connected{seen}. {what}")
    return "\n".join(lines)


class RemoteDevice:
    """One connected device. Holds its last known state; relays commands to it."""

    # ---- subclass vocabulary -------------------------------------------------
    label = "device"            # how this device is named in errors and logs
    topic_prefix = "device"     # reports publish as "<topic_prefix>.<kind>"
    default_os = ""             # assumed OS when the hello doesn't say
    commands: tuple[str, ...] = ()
    fresh_seconds: dict[str, float] = {}
    known_signals: frozenset[str] = frozenset()

    def __init__(self, bus: Bus, name: Optional[str] = None) -> None:
        self._bus = bus
        self.name = name or self.label
        self._sock: Any = None
        self._caps: list[str] = []
        self._model: str = ""
        self._os: str = self.default_os
        self._connected_at: float = 0.0
        self._last_seen: float = 0.0
        # signal -> {"value": ..., "ts": float}. One dict per signal rather than
        # one flat snapshot, because signals arrive independently and a fresh
        # battery report must not make a stale location look fresh.
        self._signals: dict[str, dict[str, Any]] = {}
        self._pending: dict[str, asyncio.Future] = {}
        # An unknown signal is worth saying out loud once — it's how a new client
        # build announces itself. Saying it on every frame would just move the
        # unbounded growth from memory into the log file.
        self._unknown_warned = 0

    # ---- connection lifecycle ------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._sock is not None

    async def attach(self, sock: Any, hello: dict[str, Any]) -> None:
        """The client connected and introduced itself."""
        # A second connection replaces the first. A device on a real network
        # reconnects far more often than it disconnects cleanly — Android doze,
        # network handoff, process death, a laptop lid — so the newest socket is
        # always the real one.
        if self._sock is not None:
            log.info("%s reconnected — replacing previous socket", self.label)
            self._fail_pending(f"{self.label} reconnected")

        self._sock = sock
        # Sanitised, not trusted: `name` becomes a permanent key in the server's
        # device registry and `capabilities` gates which commands we'll send.
        self._caps = [str(c)[:MAX_NAME_LEN]
                      for c in list(hello.get("capabilities") or [])[:MAX_CAPABILITIES]]
        self._model = str(hello.get("model") or self.label)[:MAX_NAME_LEN]
        # Kept, not just announced. It was published to the bus and dropped,
        # which left nothing able to answer "which OS is the desk?" after the
        # fact — and that is the difference between a PowerShell command being
        # worth trying and being certain to fail.
        self._os = str(hello.get("os") or self.default_os)[:MAX_NAME_LEN]
        label = str(hello.get("name") or "").strip()[:MAX_NAME_LEN] or self._model
        self.name = f"{self.label}:{label}"
        self._connected_at = self._last_seen = time.time()
        _attached[self.label] = self

        await self._bus.publish(Event(
            topic="device.register",
            source=self.name,
            payload={"capabilities": self._caps,
                     "os": hello.get("os", self.default_os),
                     "model": self._model},
        ))
        log.info("%s attached: %s caps=%s", self.label, self.name, ",".join(self._caps))

    async def detach(self, sock: Any = None) -> None:
        """One socket's handler is cleaning up. `sock` is *which* socket, and it
        matters, because these arrive out of order.

        `attach` is built on the premise that the newest socket is the real one.
        But the old socket's handler doesn't learn it's dead until TCP gives up,
        which on a device that changed network can be minutes after the
        replacement is already live and working. Detaching unconditionally then
        is a late funeral for a socket nobody is using: it clears `_sock`,
        cancels the *new* socket's in-flight commands, and announces the device
        offline — while `ingest()`, which never looks at `_sock`, carries on
        recording its reports. The device reads as absent and present at the
        same time, and every command raises "not connected" until the next
        reconnect happens to land.

        So a detach from a socket we no longer hold is ignored. Passing nothing
        still means "tear down whatever is there", for callers that can't say.
        """
        if sock is not None and sock is not self._sock:
            log.debug("ignoring detach from a superseded socket — a newer one is live")
            return
        self._sock = None
        # Only reached for a socket we still hold — a superseded one returns
        # above — so this never unpublishes a live replacement.
        if _attached.get(self.label) is self:
            _attached.pop(self.label, None)
        self._fail_pending(f"{self.label} disconnected")
        await self._bus.publish(Event(
            topic="device.offline", source=self.name, payload={},
        ))
        log.info("%s detached: %s", self.label, self.name)

    def _fail_pending(self, reason: str) -> None:
        """Nothing in flight can survive losing the socket — resolve the waiters
        rather than leaving agent turns hanging until their timeout."""
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError(reason))
        self._pending.clear()

    # ---- inbound: the device reporting ---------------------------------------

    def _admit_unknown(self, kind: str) -> None:
        """Make room for an unrecognised signal, evicting least-recently reported
        first. Known kinds are never evicted — they're the ones the agent and the
        UI actually read, and there are only a dozen of them.

        Recency comes from dict insertion order, not from the stored `ts`:
        `time.time()` on Windows has ~16ms resolution, so a burst of reports all
        carry the same timestamp and sorting by it silently degrades to sorting
        by key name. `ingest` re-inserts on update to keep that order honest."""
        unknown = [k for k in self._signals if k not in self.known_signals]
        for oldest in unknown[:max(0, len(unknown) - MAX_UNKNOWN_SIGNALS + 1)]:
            log.info("dropping unknown %s signal %r to make room for %r",
                     self.label, oldest, kind)
            del self._signals[oldest]

    async def ingest(self, kind: str, payload: dict[str, Any]) -> None:
        """One report from the device."""
        kind = str(kind)[:MAX_KIND_LEN]
        if kind not in self.known_signals:
            if kind not in self._signals:
                if self._unknown_warned < UNKNOWN_WARN_LIMIT:
                    log.warning("%s reported unknown signal %r — kept, but it "
                                "shares a %d-slot pool", self.label, kind,
                                MAX_UNKNOWN_SIGNALS)
                elif self._unknown_warned == UNKNOWN_WARN_LIMIT:
                    log.warning("further unknown %s signals will not be logged", self.label)
                self._unknown_warned += 1
                self._admit_unknown(kind)
            else:
                # Move to the end so it reads as the most recent, per _admit_unknown.
                del self._signals[kind]
        now = time.time()
        self._last_seen = now
        self._signals[kind] = {"value": payload, "ts": now}
        await self._bus.publish(Event(
            topic=f"{self.topic_prefix}.{kind}", source=self.name, payload=payload,
        ))

    async def resolve(self, corr: str, payload: dict[str, Any]) -> None:
        """A command result came back."""
        fut = self._pending.pop(corr, None)
        if fut and not fut.done():
            fut.set_result(payload)

    # ---- outbound: commanding the device -------------------------------------

    async def command(self, name: str, params: Optional[dict] = None,
                      timeout: float = COMMAND_TIMEOUT) -> dict[str, Any]:
        """Send a command and wait for its result.

        Raises rather than returning an error dict: the callers are tool
        handlers that already translate exceptions into readable text, and a
        silent {"ok": false} is too easy for a model to narrate as success.
        """
        if self._sock is None:
            raise RuntimeError(f"{self.label} is not connected")
        if name not in self.commands:
            raise ValueError(f"unknown {self.label} command: {name}")
        if self._caps and name not in self._caps:
            raise RuntimeError(
                f"{self.label} does not offer '{name}' "
                f"(it reports: {', '.join(self._caps) or 'nothing'})")

        corr = uuid.uuid4().hex[:12]
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[corr] = fut
        try:
            await self._sock.send_json({"type": "command", "corr": corr,
                                        "command": name, "params": params or {}})
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"{self.label} did not answer '{name}' within {timeout:.0f}s")
        finally:
            self._pending.pop(corr, None)

    # ---- reading the state ---------------------------------------------------

    def get(self, kind: str) -> Optional[dict[str, Any]]:
        """Latest value for one signal, with `age_seconds` and `stale` folded in."""
        rec = self._signals.get(kind)
        if not rec:
            return None
        age = time.time() - rec["ts"]
        return {**rec["value"], "age_seconds": round(age),
                "stale": age > self.fresh_seconds.get(kind, DEFAULT_FRESH)}

    def snapshot(self) -> dict[str, Any]:
        """Everything known, for the API and the UI panel."""
        return {
            "connected": self.connected,
            "name": self.name,
            "model": self._model,
            "os": self._os,
            "capabilities": self._caps,
            "last_seen": self._last_seen or None,
            "last_seen_ago": round(time.time() - self._last_seen) if self._last_seen else None,
            "signals": {k: self.get(k) for k in self._signals},
        }


def aged(text: str, sig: dict[str, Any]) -> str:
    """Mark a reading the device hasn't refreshed lately. The agent will happily
    state a four-hour-old battery level as current unless the context says
    otherwise, so the context says otherwise."""
    if not sig.get("stale"):
        return text
    age = sig.get("age_seconds") or 0
    if age >= 3600:
        return f"{text} (as of {age // 3600}h ago)"
    return f"{text} (as of {max(1, age // 60)}m ago)"
