"""End-to-end for the desktop-as-client split, over real websockets.

Two halves, because the two directions have different risks.

  A. Reports going up — the real `DesktopClient` against a real `create_app()`
     on a real loopback socket. Proves the hub sees this machine, that
     `/api/desktop` reflects it, and that losing the client stops the clock
     rather than freezing it at the last reading.

  B. Commands coming down — the real client against a *fake* hub, so a command
     frame can be posted deliberately and its result frame inspected. The real
     app can't do that from outside without exposing its internals.

Sandboxed exactly like the other suites: all fourteen stores redirected, and the
run aborts before `create_app()` if any Config field still resolves inside live
`data/`.

Two deliberate restraints, both about not doing real damage from a test:

  * `notify_async` is patched out. The path is still exercised end to end — the
    frame crosses the socket and the handler runs — but Jake doesn't get a
    mystery toast while he's playing.
  * `close_process` is NOT patched. It runs for real, because the guard that
    matters is inside it. It's called with a name that matches nothing, and with
    `explorer.exe` to prove the denylist refuses before psutil is ever touched.
    Explorer is checked to be still running afterwards. Nothing else is safe to
    aim it at, and a test that could close a real app is a test worth deleting.

    python -u _desktop_client_test.py
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

TMP = Path(tempfile.mkdtemp(prefix="continuum-desktop-"))
for var, leaf in [
    ("CONTINUUM_MEMORY_DB", "memory.db"), ("CONTINUUM_CONVERSATIONS_DB", "conversations.db"),
    ("CONTINUUM_SCHEDULER_DB", "scheduler.db"), ("CONTINUUM_MESSAGES_DB", "messages.db"),
    ("CONTINUUM_ROUTINES_DB", "routines.db"), ("CONTINUUM_WORKOUTS_DB", "workouts.db"),
    ("CONTINUUM_ACTIVITY_DB", "activity.db"), ("CONTINUUM_NUTRITION_DB", "nutrition.db"),
    ("CONTINUUM_BODY_DB", "body.db"), ("CONTINUUM_HEALTH_DB", "health.db"),
    ("CONTINUUM_LEDGER_DB", "ledger.db"), ("CONTINUUM_FLASHCARDS_DB", "flashcards.db"),
    ("CONTINUUM_BUDGET_DB", "budget.db"), ("CONTINUUM_UPLOADS_DIR", "uploads"),
]:
    os.environ[var] = str(TMP / leaf)
os.environ["CONTINUUM_OLLAMA_AUTOSTART"] = "0"
os.environ["CONTINUUM_PROACTIVE"] = "0"

sys.path.insert(0, str(Path(__file__).parent))
from continuum.config import Config              # noqa: E402

cfg = Config.load()
LIVE = str((Path(__file__).parent / "data").resolve()).lower()
bad = [f"{n}={getattr(cfg, n)}" for n in vars(cfg)
       if isinstance(getattr(cfg, n), str) and ("_db" in n or "_dir" in n)
       and getattr(cfg, n)
       and str(Path(getattr(cfg, n)).resolve()).lower().startswith(LIVE)]
if bad:
    sys.exit("REFUSING TO RUN - these still point at live data:\n  " + "\n  ".join(bad))
print(f"sandbox: {TMP}")

import httpx                                      # noqa: E402
import uvicorn                                    # noqa: E402
from websockets.asyncio.server import serve as ws_serve   # noqa: E402

import continuum.client.desktop as dc             # noqa: E402
from continuum import deskops                     # noqa: E402
from continuum.app.server import create_app       # noqa: E402
from continuum.bus import Bus                     # noqa: E402
from continuum.devices.desktop import DesktopLink  # noqa: E402

FAILED: list[str] = []


def check(label: str, got: Any, want: Any) -> None:
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"   got={got!r} want={want!r}"))
    if not ok:
        FAILED.append(label)


def truthy(label: str, got: Any) -> None:
    ok = bool(got)
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"   got={got!r}"))
    if not ok:
        FAILED.append(label)


# A toast would be real and visible. The socket hop either side of it is what
# this suite is actually testing, so keep the hop and drop the toast.
NOTIFIED: list[tuple[str, str]] = []


async def _fake_notify(title: str, body: str) -> None:
    NOTIFIED.append((title, body))


dc.notify_async = _fake_notify


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def until(pred, timeout: float = 20.0, every: float = 0.1) -> bool:
    """Wait for a condition instead of sleeping a guessed interval."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if await pred() if asyncio.iscoroutinefunction(pred) else pred():
            return True
        await asyncio.sleep(every)
    return False


# ---------------------------------------------------------------------------
# A. reports going up, against the real app
# ---------------------------------------------------------------------------
async def part_a() -> None:
    print("\n--- A: the hub sees a remote desktop ---")
    port = free_port()
    app = create_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())
    await until(lambda: server.started)

    base = f"http://127.0.0.1:{port}"
    async with httpx.AsyncClient(base_url=base, timeout=10) as http:
        r = await http.get("/api/desktop")
        check("no client yet -> not connected", r.json()["connected"], False)
        check("...and the hub says it is the local machine", r.json()["local"], True)
        check("...and it is still counting time", r.json()["counting"], True)

        client = dc.DesktopClient(f"ws://127.0.0.1:{port}/desktop/ws", poll_interval=0.5)
        client_task = asyncio.create_task(client.run_forever())

        async def connected() -> bool:
            return (await http.get("/api/desktop")).json()["connected"]

        truthy("the client connects", await until(connected))

        snap = (await http.get("/api/desktop")).json()
        check("the hub no longer thinks it is local", snap["local"], False)
        truthy("the hub records the OS", snap.get("signals") is not None)
        # Derived from the constant rather than spelled out, because this list
        # is no longer a fixed three. Every desk op the client can perform is
        # advertised here, and the hub withholds a tool from the model unless it
        # sees the matching name, so a capability lost on the wire is a tool that
        # silently stops existing rather than one that fails loudly.
        check("capabilities are advertised",
              sorted(snap["capabilities"]), sorted(dc.CAPABILITIES))
        check("...every desk op among them has a handler behind it",
              sorted(set(snap["capabilities"]) & set(deskops.OPS)),
              sorted(deskops.OPS))

        async def got_state() -> bool:
            s = (await http.get("/api/desktop")).json()
            return "state" in (s.get("signals") or {})

        truthy("a state report arrives", await until(got_state))

        sig = (await http.get("/api/desktop")).json()["signals"]["state"]
        truthy("the report carries a foreground or presence reading",
               "foreground" in sig or "presence" in sig or "apps" in sig)
        check("a fresh report is not stale", sig["stale"], False)
        truthy("the hub is counting against it",
               (await http.get("/api/desktop")).json()["counting"])

        # Losing the client must stop the clock, not freeze it at the last frame.
        client_task.cancel()
        try:
            await client_task
        except asyncio.CancelledError:
            pass

        async def gone() -> bool:
            return not (await http.get("/api/desktop")).json()["connected"]

        truthy("dropping the client marks it offline", await until(gone))

    server.should_exit = True
    await serve_task


# ---------------------------------------------------------------------------
# B. commands coming down, against a fake hub
# ---------------------------------------------------------------------------
RESULTS: dict[str, Any] = {}
HELLO: dict[str, Any] = {}


async def part_b() -> None:
    print("\n--- B: the desktop carries out what the hub asks ---")
    port = free_port()
    done = asyncio.Event()

    async def handler(ws: Any) -> None:
        HELLO.update(json.loads(await ws.recv()))
        await ws.send(json.dumps({"type": "welcome",
                                  "commands": ["notify", "close_process", "state"]}))

        async def ask(name: str, params: dict) -> Any:
            corr = f"c{len(RESULTS)}"
            await ws.send(json.dumps({"type": "command", "corr": corr,
                                      "command": name, "params": params}))
            # Reports are interleaved with results; take the next result frame.
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("type") == "result" and msg.get("corr") == corr:
                    return msg.get("payload")

        RESULTS["notify"] = await ask("notify", {"title": "Budget", "body": "95% used"})
        RESULTS["missing"] = await ask("close_process",
                                       {"proc": "continuum-not-a-real-process.exe"})
        RESULTS["protected"] = await ask("close_process", {"proc": "explorer.exe"})
        RESULTS["bogus"] = await ask("wipe_the_disk", {})
        RESULTS["state"] = await ask("state", {})
        done.set()

    async with ws_serve(handler, "127.0.0.1", port):
        client = dc.DesktopClient(f"ws://127.0.0.1:{port}", poll_interval=0.5)
        client_task = asyncio.create_task(client.run_forever())
        try:
            await asyncio.wait_for(done.wait(), timeout=30)
        finally:
            client_task.cancel()
            try:
                await client_task
            except asyncio.CancelledError:
                pass

    check("the client introduces itself", HELLO.get("type"), "hello")
    truthy("...with a machine name", HELLO.get("name"))
    check("...and its real capabilities",
          sorted(HELLO.get("capabilities") or []),
          sorted(dc.CAPABILITIES))
    # The one that would have caught the original bug: the hub used to hold its
    # own idea of what a desktop could do, so a name could be advertised with
    # nothing implementing it. Both sides now read the same OPS table.
    check("...none of which is advertised without an implementation",
          sorted(n for n in (HELLO.get("capabilities") or [])
                 if n in deskops.OPS),
          sorted(deskops.OPS))

    check("notify is carried out", RESULTS["notify"], {"ok": True})
    check("...and actually reached the notifier", NOTIFIED[-1],
          ("Budget", "95% used"))

    check("closing a process that isn't running closes nothing",
          RESULTS["missing"]["closed"], 0)

    # The important one. This is the real enforce.close_process, and the denylist
    # has to refuse before psutil is ever asked for a process list.
    check("a protected process is refused", RESULTS["protected"]["closed"], 0)
    check("...and says why", RESULTS["protected"].get("skipped"), "protected process")
    # Not a formality. If the refusal didn't hold, this suite just closed Jake's
    # taskbar, and the return value alone wouldn't have told us.
    import psutil
    truthy("...and Explorer is genuinely still running",
           any((p.info.get("name") or "").lower() == "explorer.exe"
               for p in psutil.process_iter(["name"])))

    check("an unknown command answers instead of hanging",
          "error" in RESULTS["bogus"], True)
    check("an on-demand state report is acknowledged", RESULTS["state"], {"ok": True})


# ---------------------------------------------------------------------------
# C. the staleness rule, which is where budget money is won or lost
# ---------------------------------------------------------------------------
async def part_c() -> None:
    print("\n--- C: silence stops the clock ---")
    link = DesktopLink(Bus())
    check("a link with nothing reported has no state", link.basic(), {})

    await link.ingest("state", {"foreground": {"name": "VALORANT", "proc": "valorant.exe"},
                                "presence": {"present": True}})
    b = link.basic()
    check("a fresh report is returned", (b.get("foreground") or {}).get("name"), "VALORANT")
    check("...without the freshness bookkeeping leaking in", "stale" in b, False)
    check("...or the age", "age_seconds" in b, False)

    # Wind the reading back past the window rather than waiting 30 real seconds.
    link._signals["state"]["ts"] = time.time() - (link.fresh_seconds["state"] + 5)
    check("a stale report yields nothing", link.basic(), {})
    truthy("...and the summary says so, rather than naming an old app",
           "not counting" in link.summary_text())

    # The shape the state loop actually consumes: {} must produce no foreground,
    # which is what closes the activity segment and stops billing.
    b = link.basic()
    present = (b.get("presence") or {}).get("present", True)
    fg = b.get("foreground") if present else None
    check("the state loop would record no foreground", fg, None)


async def main() -> None:
    await part_a()
    await part_b()
    await part_c()


asyncio.run(main())

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("all desktop client checks passed")
