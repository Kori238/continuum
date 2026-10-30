"""Characterisation test for PhoneDevice — written BEFORE the RemoteDevice
refactor and expected to pass identically after it.

This is a safety net, not a spec. Its job is to notice if extracting the generic
socket machinery into a base class quietly changes any behaviour the phone
already depends on. The two that matter most, because both are silent when
broken and were each worth a paragraph of comment in phone.py:

  * detach from a *superseded* socket must be ignored — otherwise a phone that
    changed network buries the replacement socket on the old one's behalf and
    the device reads as absent and present at the same time;
  * unknown-signal eviction is ordered by dict insertion, not by `ts`, because
    time.time() on Windows has ~16ms resolution and a burst of reports all carry
    the same stamp.

It touches `_signals` directly in a few places to control timestamps. That is
deliberate white-box coupling: `_signals` is part of what moves to the base
class, so if the name changes this test should fail and be updated knowingly.

    python -u _phone_device_test.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from continuum.bus import Bus                       # noqa: E402
from continuum.devices.phone import (               # noqa: E402
    MAX_CAPABILITIES, MAX_KIND_LEN, MAX_UNKNOWN_SIGNALS, PhoneDevice,
)

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


class FakeSock:
    """Just enough WebSocket to be commanded."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, frame: dict) -> None:
        self.sent.append(frame)


def new_phone() -> tuple[PhoneDevice, Bus, list]:
    bus = Bus()
    seen: list = []

    async def record(ev) -> None:
        seen.append(ev)

    bus.subscribe("*", record)
    bus.subscribe("phone.*", record)
    bus.subscribe("device.*", record)
    return PhoneDevice(bus), bus, seen


HELLO = {"type": "hello", "name": "Pixel", "model": "Pixel 8",
         "capabilities": ["notify", "open_app"], "os": "Android"}


async def main() -> None:
    # ---- connection lifecycle -------------------------------------------
    phone, _bus, seen = new_phone()
    check("a fresh device is not connected", phone.connected, False)

    s1 = FakeSock()
    await phone.attach(s1, HELLO)
    check("attach connects", phone.connected, True)
    check("name is derived from hello", phone.name, "phone:Pixel")
    truthy("attach announces device.register",
           any(e.topic == "device.register" for e in seen))

    # Capabilities are sanitised, not trusted — they gate which commands we send.
    s2 = FakeSock()
    await phone.attach(s2, {**HELLO, "capabilities": ["notify"] * 100})
    check("capabilities are capped", len(phone._caps), MAX_CAPABILITIES)

    # The load-bearing one. s1 is superseded; its late funeral must be ignored.
    await phone.detach(s1)
    check("detach from a superseded socket is ignored", phone.connected, True)
    check("...and the live socket is still the new one", phone._sock is s2, True)

    await phone.detach(s2)
    check("detach from the live socket disconnects", phone.connected, False)
    truthy("detach announces device.offline",
           any(e.topic == "device.offline" for e in seen))

    # ---- inbound reports -------------------------------------------------
    phone, _bus, seen = new_phone()
    await phone.attach(FakeSock(), HELLO)
    seen.clear()
    await phone.ingest("battery", {"level": 82, "charging": False})
    truthy("a report publishes phone.<kind>",
           any(e.topic == "phone.battery" for e in seen))
    got = phone.get("battery")
    check("get() returns the value", got["level"], 82)
    check("get() folds in freshness", got["stale"], False)
    truthy("get() folds in age", "age_seconds" in got)
    check("get() of an unreported signal is None", phone.get("nonsense"), None)

    # A stale reading is still returned — it's the best we have — but marked.
    phone._signals["battery"]["ts"] = time.time() - 4 * 3600
    check("an old reading is marked stale", phone.get("battery")["stale"], True)

    # An over-long kind is truncated rather than rejected.
    await phone.ingest("x" * 200, {"v": 1})
    truthy("an over-long kind is truncated",
           any(len(k) == MAX_KIND_LEN for k in phone._signals))

    # Unknown signals share a bounded pool; known ones are never evicted.
    phone, _bus, _seen = new_phone()
    await phone.attach(FakeSock(), HELLO)
    await phone.ingest("battery", {"level": 50})
    for i in range(MAX_UNKNOWN_SIGNALS + 6):
        await phone.ingest(f"weird{i}", {"i": i})
    unknown = [k for k in phone._signals if k.startswith("weird")]
    check("unknown signals are bounded", len(unknown), MAX_UNKNOWN_SIGNALS)
    truthy("a known signal is never evicted", phone.get("battery") is not None)
    check("eviction drops the oldest first", unknown[0], f"weird{6}")

    # Re-reporting an unknown signal refreshes its place in the queue, so a
    # steadily-reported one doesn't age out behind a burst of new arrivals.
    await phone.ingest("weird6", {"i": "again"})
    unknown = [k for k in phone._signals if k.startswith("weird")]
    check("re-reporting moves a signal to newest", unknown[-1], "weird6")

    # ---- commands --------------------------------------------------------
    phone, _bus, _seen = new_phone()
    try:
        await phone.command("notify", {"title": "x"})
        FAILED.append("command with no socket should raise")
        print("FAIL  command with no socket raises")
    except RuntimeError as exc:
        check("command with no socket raises", "not connected" in str(exc), True)

    sock = FakeSock()
    await phone.attach(sock, HELLO)

    try:
        await phone.command("self_destruct")
        FAILED.append("an unknown command should raise")
        print("FAIL  an unknown command raises ValueError")
    except ValueError:
        print("PASS  an unknown command raises ValueError")

    # The phone advertised only notify/open_app.
    try:
        await phone.command("set_alarm", {})
        FAILED.append("an unoffered capability should raise")
        print("FAIL  an unoffered capability raises")
    except RuntimeError as exc:
        check("an unoffered capability raises", "does not offer" in str(exc), True)

    # A round trip: send, then answer the correlation id it minted.
    async def answer() -> None:
        await asyncio.sleep(0.01)
        corr = sock.sent[-1]["corr"]
        await phone.resolve(corr, {"ok": True, "echo": corr})

    task = asyncio.create_task(answer())
    result = await phone.command("notify", {"title": "hi"})
    await task
    check("a command resolves with its result", result["ok"], True)
    frame = sock.sent[-1]
    check("the frame is a command", frame["type"], "command")
    check("the frame carries the command name", frame["command"], "notify")
    check("the frame carries params", frame["params"], {"title": "hi"})
    check("the correlation id is 12 hex", len(frame["corr"]), 12)
    check("a resolved command leaves nothing pending", len(phone._pending), 0)

    # A silent phone must not hang the caller forever.
    t0 = time.time()
    try:
        await phone.command("notify", {"title": "no answer"}, timeout=0.05)
        FAILED.append("a silent phone should time out")
        print("FAIL  a silent phone times out")
    except RuntimeError as exc:
        check("a silent phone times out", "did not answer" in str(exc), True)
    truthy("the timeout is honoured promptly", time.time() - t0 < 1.0)
    check("a timed-out command leaves nothing pending", len(phone._pending), 0)

    # Losing the socket resolves in-flight waiters rather than stranding them.
    pending = asyncio.create_task(phone.command("notify", {"title": "inflight"}, timeout=5))
    await asyncio.sleep(0.02)
    await phone.detach(sock)
    try:
        await pending
        FAILED.append("losing the socket should fail in-flight commands")
        print("FAIL  losing the socket fails in-flight commands")
    except RuntimeError as exc:
        check("losing the socket fails in-flight commands",
              "disconnected" in str(exc), True)

    # ---- snapshot + summary ---------------------------------------------
    phone, _bus, _seen = new_phone()
    check("summary of an unconnected phone says so",
          phone.summary_text(), "Phone: not connected")

    await phone.attach(FakeSock(), HELLO)
    await phone.ingest("battery", {"level": 17, "charging": False})
    await phone.ingest("screen", {"on": True})
    await phone.ingest("location", {"place": "home"})
    snap = phone.snapshot()
    check("snapshot reports connected", snap["connected"], True)
    check("snapshot carries the model", snap["model"], "Pixel 8")
    truthy("snapshot lists signals", "battery" in snap["signals"])
    summary = phone.summary_text()
    truthy("summary mentions a low battery", "17%" in summary and "low" in summary)
    truthy("summary mentions location", "home" in summary)
    truthy("summary mentions the screen", "screen on" in summary)

    # An aged reading must never be stated as current.
    phone._signals["location"]["ts"] = time.time() - 2 * 3600
    truthy("an aged reading is marked in the summary",
           "as of 2h ago" in phone.summary_text())


asyncio.run(main())

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("all PhoneDevice characterisation checks passed")
