"""The desktop, as a client of a hub somewhere else.

Run this on the Windows box when the referee has moved:

    python -m continuum desktop --hub wss://hub.tail1234.ts.net/desktop/ws

It does two things and nothing else. It reports what this machine is doing, on
the tracker's own cadence, and it carries out what the hub asks — a toast, or a
process asked to close. The hub holds the stores, runs `budget.evaluate()`, and
decides; this end only senses and acts. That split is why `evaluate()` needed no
changes to move countries.

Deliberately thin on imports. `enforce` and `state` come along; FastAPI, the
fourteen stores and the agent do not, which is what makes this runnable on a
machine that is emphatically not the hub.

Four behaviours worth knowing, because each one is a decision rather than an
oversight:

*It dials out, and keeps dialling.* The desktop has no stable address and no
open ports, exactly like the phone. Reconnect backoff is capped low (30s) —
every second disconnected is a second the hub is not counting, and the honest
failure of this system is to under-measure rather than to guess.

*A command that fails answers anyway.* Handlers return `{"error": ...}` instead
of raising, because the hub is sitting in `RemoteDevice.command()` with a 20s
timeout. A refusal that arrives is diagnosable; a silence that times out looks
identical to a dead network.

*It never escalates.* Closing goes through `enforce.close_process`, which sends
the polite signal, honours the protected-name denylist, and gives up on
AccessDenied. Being the far end of a socket doesn't earn it more authority than
it had when it ran in-process.

*It senses more than it used to.* The agent's desk-bound tools — the clipboard,
the screen, Firefox history, open apps, media keys — now route here rather than
being answered by a Linux VM that cannot see any of them. It does not implement
them twice: the table is `deskops.OPS`, the same object the hub routes against,
so a capability cannot exist on one side and be missing on the other. That is
the bug that let `notify` look implemented for a month with only a receiver.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import platform
import sys
import time
from collections import deque
from typing import Any, Optional

from websockets.asyncio.client import connect

from .. import deskops, enforce
from ..messagelog import MessageCollector
from ..notify import notify_async
from ..state import StateTracker

log = logging.getLogger("continuum.client.desktop")

DEFAULT_HUB = os.getenv("CONTINUUM_HUB_URL", "ws://127.0.0.1:8848/desktop/ws")

# The command names this client implements. The hub gates on these — see
# `RemoteDevice.command()` — so anything advertised here must really work. The
# desk operations are spliced in from `deskops` rather than retyped: this list
# is what the hub trusts, and a hand-maintained copy that fell behind would have
# the hub refusing commands this client handles perfectly well.
CAPABILITIES = ("notify", "close_process", "state") + tuple(deskops.OPS)

MAX_BACKOFF = 30.0
PING_EVERY = 120.0

# How often to ask whether A1111 has gone idle. Only ever one small request to
# loopback, and when A1111 is off — its usual state — it is a refused connect
# that costs nothing. A minute is fine against a 15-minute threshold.
VRAM_CHECK_EVERY = 60.0

# How often collected messages are pushed to the hub. Well under the collector's
# 8s cycle, so a batch never waits on the next scrape.
RELAY_EVERY = 2.0
# Keys remembered to avoid re-sending the same toast every cycle. The Windows
# notification centre keeps showing a message until Jake clears it, so without
# this the same rows would cross the wire every 8s for hours — the hub would
# dedup them on `key` and store nothing, but we'd have paid for the traffic.
RELAY_SEEN = 4000


class _MessageRelay:
    """A `MessageLog`-shaped sink that ships rows to the hub instead of writing
    a database.

    `MessageCollector` drives two Windows-only feeds — the toast listener and
    UIAutomation scraping of the Discord/Slack windows — and on the hub both
    return nothing, every 8 seconds, forever. `data/messages.db` stopped growing
    the day Continuum moved countries, and `recent_messages` went on querying a
    frozen table and reporting "no messages" as a fact about Jake's day.

    So the collector moves to the machine with the messages on it, and the store
    stays on the machine with the database. The seam is `record()`: the
    collector never knew whether it was talking to SQLite, and still doesn't.

    Not thread-safe in general, and doesn't need to be. `record()` is only ever
    reached from the collector's worker thread, which awaits its two scrapes one
    after the other, so there is exactly one writer. `drain()` runs on the event
    loop and only pops — `deque` append/popleft are atomic under the GIL.
    """

    def __init__(self) -> None:
        # Bounded so a wedged drain can't grow without limit. Only ever holds
        # one or two collection cycles in practice: the relay lives inside a
        # session, so nothing collects while the hub is unreachable.
        self._queue: deque[dict[str, Any]] = deque(maxlen=2000)
        self._seen: dict[str, None] = {}

    def record(self, source: str, app: str, title: str, body: str, key: str,
               author: Optional[str] = None, ts: Optional[float] = None) -> bool:
        """Queue one message. Returns whether it was new, which is the contract
        `MessageCollector` counts on — same as `MessageLog.record`."""
        if key in self._seen:
            return False
        self._seen[key] = None
        while len(self._seen) > RELAY_SEEN:
            self._seen.pop(next(iter(self._seen)))
        self._queue.append({"source": source, "app": app, "title": title,
                            "body": body, "key": key, "author": author, "ts": ts})
        return True

    def drain(self, limit: int = 200) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        while self._queue and len(out) < limit:
            out.append(self._queue.popleft())
        return out


class DesktopClient:
    """Reports this machine to a hub; carries out what it's told."""

    def __init__(self, hub_url: str = DEFAULT_HUB,
                 poll_interval: Optional[float] = None) -> None:
        self._url = hub_url
        self._tracker = StateTracker()
        self._interval = poll_interval or self._tracker.poll_interval
        self._sock: Any = None
        self._relay = _MessageRelay()
        self._collector = MessageCollector(self._relay)

    # ---- connection ---------------------------------------------------------

    async def run_forever(self) -> None:
        backoff = 1.0
        # Deliberately outside the session below, and not cancelled between
        # reconnects: A1111 idles whether or not the hub is reachable, and an
        # outage is precisely when nobody is asking for images. Scoping this to
        # a live websocket would hold 6.6GB of VRAM through every disconnect.
        vram = asyncio.create_task(self._vram_loop(), name="vram")
        try:
            while True:
                try:
                    log.info("connecting to hub %s", self._url)
                    async with connect(self._url, max_queue=32) as ws:
                        backoff = 1.0
                        log.info("connected to hub")
                        await self._session(ws)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("hub connection failed (%s): %s", type(exc).__name__, exc)
                log.info("reconnecting in %.0fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
        finally:
            vram.cancel()

    async def _vram_loop(self) -> None:
        """Hand A1111's VRAM back once nothing has rendered for a while.

        Jake's 4070 has 12GB and a loaded checkpoint holds 6.6GB of it, which is
        why the renderer used to be started by hand and closed again. Unloading
        on idle lets it simply stay up: the memory comes back when he is not
        using it, and `_txt2img` pulls the weights in again on the next render
        for about 14s extra.

        `floor` covers an A1111 that has been started but never asked to render:
        it has no job timestamp to age against, so it ages against the moment
        this client first started watching instead of never unloading at all.
        """
        floor = time.time()
        while True:
            await asyncio.sleep(VRAM_CHECK_EVERY)
            try:
                # Blocking urllib, same as every other desk op — keep it off the
                # loop or a slow renderer stalls the hub's commands.
                freed = await asyncio.to_thread(
                    deskops.a1111_idle_unload, deskops.A1111_IDLE_UNLOAD_S, floor)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug("VRAM watcher failed", exc_info=True)
                continue
            if freed:
                log.info("%s", freed)

    async def _session(self, ws: Any) -> None:
        self._sock = ws
        await self._send(ws, {
            "type": "hello",
            "name": platform.node(),
            "model": platform.node(),
            "os": platform.platform(),
            "capabilities": list(CAPABILITIES),
        })
        # The welcome tells us which commands this hub knows about. We don't gate
        # on it — the hub gates on our capabilities, not the other way round — but
        # a mismatch is worth seeing in the log when something doesn't fire.
        try:
            welcome = json.loads(await ws.recv())
            log.info("hub welcomed us: %s", ",".join(welcome.get("commands") or []))
        except Exception:
            log.debug("no welcome frame", exc_info=True)

        # Collection lives inside the session, not beside it: with no hub to
        # send to there is nothing useful to do with a scraped message except
        # queue it, and a queue that fills while the network is down is just a
        # slower way to lose messages. The Windows notification centre holds
        # toasts until Jake clears them, so a reconnect picks up what is still
        # showing rather than starting blind.
        tasks = [asyncio.create_task(self._report_loop(ws), name="report"),
                 asyncio.create_task(self._receive_loop(ws), name="receive"),
                 asyncio.create_task(self._collector.run(), name="collect"),
                 asyncio.create_task(self._relay_loop(ws), name="relay")]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            # Re-raise whichever half died, so run_forever reconnects rather than
            # spinning on a session where only one direction still works.
            for t in done:
                t.result()
        finally:
            for t in tasks:
                t.cancel()
            self._sock = None

    @staticmethod
    async def _send(ws: Any, frame: dict[str, Any]) -> None:
        await ws.send(json.dumps(frame))

    # ---- outbound: reporting ------------------------------------------------

    async def _report_loop(self, ws: Any) -> None:
        since_ping = 0.0
        while True:
            await self._report_state(ws)
            await asyncio.sleep(self._interval)
            since_ping += self._interval
            if since_ping >= PING_EVERY:
                since_ping = 0.0
                await self._send(ws, {"type": "ping"})

    async def _report_state(self, ws: Any) -> None:
        # `poll` is a pile of synchronous Windows calls; keep it off the loop or
        # it stalls the receive side and the hub's commands sit unanswered.
        await asyncio.to_thread(self._tracker.poll)
        await self._send(ws, {"type": "report", "kind": "state",
                              "payload": self._tracker.basic()})

    async def _relay_loop(self, ws: Any) -> None:
        """Push collected messages to the hub, which owns the database."""
        while True:
            await asyncio.sleep(RELAY_EVERY)
            rows = self._relay.drain()
            if rows:
                await self._send(ws, {"type": "report", "kind": "messages",
                                      "payload": {"rows": rows}})
                log.debug("relayed %d message(s) to the hub", len(rows))

    # ---- inbound: commands --------------------------------------------------

    async def _receive_loop(self, ws: Any) -> None:
        while True:
            raw = await ws.recv()
            try:
                msg = json.loads(raw)
                kind = msg.get("type")
            except (ValueError, TypeError, AttributeError) as exc:
                log.warning("ignoring malformed hub frame: %s", exc)
                continue
            if kind != "command":
                continue
            corr = msg.get("corr") or ""
            name = msg.get("command") or ""
            params = msg.get("params") or {}
            payload = await self._dispatch(name, params, ws)
            if corr:
                await self._send(ws, {"type": "result", "corr": corr,
                                      "payload": payload})

    async def _dispatch(self, name: str, params: dict[str, Any],
                        ws: Any) -> dict[str, Any]:
        """Run one command. Returns a payload — never raises — so the hub always
        gets an answer inside its timeout."""
        try:
            if name == "notify":
                title = str(params.get("title") or "Continuum")
                body = str(params.get("body") or "")
                await notify_async(title, body)
                log.info("notified: %s — %s", title, body)
                return {"ok": True}

            if name == "close_process":
                proc = params.get("proc")
                log.info("hub asked to close %r", proc)
                res = await asyncio.to_thread(enforce.close_process, proc)
                log.info("close %r -> %s", proc, res)
                return res

            if name == "state":
                await self._report_state(ws)
                return {"ok": True}

            if name in deskops.OPS:
                # Off the loop without exception: every one of these bottoms out
                # in a blocking Win32 call, and the worst are far from quick —
                # `close_tab` drives UIAutomation for up to 20s and `see_screen`
                # captures every monitor and PNG-encodes the result. Run inline,
                # they would stall the receive loop, so the hub's *next* command
                # would sit unread until this one finished and then be reported
                # as a timeout against the wrong operation.
                res = await asyncio.to_thread(
                    deskops.run_local, name, params, self._tracker)
                log.info("desk op %s -> %s", name,
                         "error: " + res["error"] if "error" in res
                         else ", ".join(sorted(res)) or "ok")
                return res

            return {"error": f"unknown command: {name}"}
        except Exception as exc:
            log.exception("command %r failed", name)
            return {"error": f"{type(exc).__name__}: {exc}"}


def main(argv: Optional[list[str]] = None) -> None:
    ap = argparse.ArgumentParser(
        prog="python -m continuum desktop",
        description="Report this desktop to a Continuum hub and act on its commands.")
    ap.add_argument("--hub", default=DEFAULT_HUB,
                    help=f"hub websocket URL (default: {DEFAULT_HUB})")
    ap.add_argument("--interval", type=float, default=None,
                    help="seconds between state reports (default: the tracker's own)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
    )
    client = DesktopClient(args.hub, args.interval)
    try:
        asyncio.run(client.run_forever())
    except KeyboardInterrupt:
        print("\nstopped")
        sys.exit(0)
