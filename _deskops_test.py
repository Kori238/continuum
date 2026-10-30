"""Does a desk question reach a desk — and say so plainly when it can't?

`deskops.run()` is a three-way switch, and every branch of it was a bug once:

  1. a desktop client is attached  -> send it down the socket   (didn't exist)
  2. otherwise, this host is a desk -> do it here               (the old default)
  3. otherwise                      -> refuse, and name the reason
                                       (previously: answer anyway, from Germany)

Branch 3 is the one that matters most, because its failure mode isn't an error.
`read_clipboard` said "clipboard is empty", `browsing_history` said "no browser
history available", `list_open_apps` said "no open apps" — every one a confident
claim about Jake's desktop, produced by a Linux VM that has never seen it.

Also checks the thing that makes the whole arrangement hold: both ends route
against the *same* table, so a capability cannot exist on one side and be
missing on the other.
"""
import asyncio
import logging
import sys

sys.path.insert(0, ".")

from continuum import deskops
from continuum.client.desktop import CAPABILITIES as CLIENT_CAPS
from continuum.devices.desktop import COMMANDS as HUB_COMMANDS

fails = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}")
    if not ok:
        fails.append(f"{label}: got {got!r}, wanted {want!r}")


class FakeDesktop:
    """A DesktopLink far enough along to answer a command."""

    name = "desktop:jakes-pc"

    def __init__(self, caps=None):
        self.sent = []
        self._caps = list(CLIENT_CAPS) if caps is None else list(caps)

    async def command(self, op, params=None, timeout=20.0):
        if op not in self._caps:
            # The shape RemoteDevice.command() raises in — an older client
            # build that doesn't implement this op yet.
            raise RuntimeError(f"desktop does not offer '{op}'")
        self.sent.append((op, params, timeout))
        return {"text": "whatever was on Jake's clipboard"}


_real_local = deskops.local_desk
_real_attached = deskops.attached_desk


def stage(*, is_desk=False, device=None):
    deskops.local_desk = lambda: is_desk
    deskops.attached_desk = lambda: device


try:
    print("\n=== 1. a desktop is attached: the question travels ===")
    dev = FakeDesktop()
    stage(is_desk=False, device=dev)
    out = asyncio.run(deskops.run("read_clipboard"))
    check("answered by the desk", out.get("text"), "whatever was on Jake's clipboard")
    check("and it really went over the wire", dev.sent[0][0], "read_clipboard")
    check("where() names the machine", deskops.where(), "desktop:jakes-pc")

    print("\n=== slow ops get the timeout they need, not the default 20s ===")
    asyncio.run(deskops.run("close_tab", {"match": "youtube"}))
    check("close_tab outlives its own 20s internal budget", dev.sent[-1][2], 45.0)
    asyncio.run(deskops.run("read_clipboard"))
    check("ordinary ops keep the default", dev.sent[-1][2], 20.0)

    print("\n=== an attached desktop wins even when this host is a desk too ===")
    stage(is_desk=True, device=dev)
    asyncio.run(deskops.run("see_screen", {"mode": "all"}))
    check("routed to the attached machine, not answered locally",
          dev.sent[-1][0], "see_screen")

    print("\n=== 2. no desktop, but this host is one: answered here ===")
    stage(is_desk=True, device=None)
    ran = []
    deskops.OPS["read_clipboard"] = lambda tracker, **kw: ran.append(kw) or {"text": "local"}
    try:
        out = asyncio.run(deskops.run("read_clipboard"))
    finally:
        deskops.OPS["read_clipboard"] = deskops._read_clipboard
    check("ran in-process", out.get("text"), "local")
    check("nothing was sent anywhere", len(dev.sent), 4)

    print("\n=== 3. a hub with nothing attached: refuses, and says why ===")
    stage(is_desk=False, device=None)
    for op in ("read_clipboard", "browsing_history", "recent_files",
               "see_screen", "open_url", "sleep_monitors"):
        out = asyncio.run(deskops.run(op, {"url": "https://example.com"}))
        if out.get("error") != deskops.NO_DESK:
            fails.append(f"{op} did not refuse: {out!r}")
    print(f"  {'PASS' if not fails else 'FAIL'}  every op refuses rather than answering locally")
    check("reachable() agrees", deskops.reachable(), False)
    check("where() has the honest answer", deskops.where(), "nowhere")

    print("\n=== an older client build: the refusal names the missing op ===")
    stage(is_desk=False, device=FakeDesktop(caps=["notify", "close_process", "state"]))
    out = asyncio.run(deskops.run("see_screen"))
    check("says which op and which machine",
          "see_screen" in (out.get("error") or ""), True)
    check("not silently answered from the hub instead",
          "text" in out or "png_b64" in out, False)

    print("\n=== an op that raises still answers ===")
    stage(is_desk=True, device=None)

    def _boom(tracker, **kw):
        raise OSError("the clipboard is held by another process")

    deskops.OPS["read_clipboard"] = _boom
    # `run_local` logs the traceback on purpose — it's the only record of a
    # failure that then travels back as a tidy string. Muted here so the
    # expected one doesn't read as a test blowing up.
    logging.getLogger("continuum.deskops").setLevel(logging.CRITICAL)
    try:
        out = asyncio.run(deskops.run("read_clipboard"))
    finally:
        deskops.OPS["read_clipboard"] = deskops._read_clipboard
        logging.getLogger("continuum.deskops").setLevel(logging.NOTSET)
    check("an exception becomes a diagnosable error, not a timeout",
          out.get("error"), "OSError: the clipboard is held by another process")

    out = asyncio.run(deskops.run("not_a_real_op"))
    check("unknown ops are named too", out.get("error"),
          "unknown desk operation: not_a_real_op")
finally:
    deskops.local_desk = _real_local
    deskops.attached_desk = _real_attached

# ---- the structural guarantee ----------------------------------------------
print("\n=== one table, two ends ===")
check("the hub will send every op the client implements",
      sorted(set(deskops.OPS) - set(HUB_COMMANDS)), [])
check("the client implements every op the hub will send",
      sorted(set(deskops.OPS) - set(CLIENT_CAPS)), [])
check("budget commands survive alongside them",
      {"notify", "close_process", "state"} <= set(HUB_COMMANDS) & set(CLIENT_CAPS),
      True)
check("read/action split covers the table",
      sorted(deskops.READ_OPS | deskops.ACTION_OPS), sorted(deskops.OPS))
check("nothing is in both halves", sorted(deskops.READ_OPS & deskops.ACTION_OPS), [])

# ---- machine power ----------------------------------------------------------
#
# Two properties here are invisible in normal use and expensive when wrong, so
# they get assertions rather than trust.
print("\n=== machine power ===")

import ctypes as _ctypes
import subprocess as _subprocess
import time as _time

from continuum.state import probe as _probe

check("sleep and restart are actions, not reads",
      sorted(deskops.ACTION_OPS & {"sleep_pc", "restart_pc"}),
      ["restart_pc", "sleep_pc"])

# `SetSuspendState(bHibernate, bForce, bWakeupEventsDisabled)`. The third
# argument reads backwards, and the recipe everyone copies — `rundll32
# powrprof.dll,SetSuspendState 0,1,0` — is one careless edit away from putting a
# 1 in the wrong slot. A 1 in the third suspends with wake events DISABLED: the
# machine sleeps perfectly, never answers a magic packet again, and nothing
# anywhere reports an error. Pinned, because only a test can see this.
_calls = []


class _FakeSuspendFn:
    argtypes = None
    restype = None

    def __call__(self, *a):
        _calls.append(a)
        return 1


class _FakePowrprof:
    SetSuspendState = _FakeSuspendFn()


class _FakeWinDLL:
    powrprof = _FakePowrprof()


if sys.platform == "win32":
    _real_windll = _ctypes.windll
    _ctypes.windll = _FakeWinDLL()
    try:
        _ok, _msg = _probe.suspend()
    finally:
        _ctypes.windll = _real_windll
    check("suspend reports success", _ok, True)
    check("suspend asks for sleep, not hibernate (arg 1 = 0)",
          _calls[0][0] if _calls else None, 0)
    check("suspend leaves wake events ENABLED (arg 3 = 0) — a 1 here kills WoL",
          _calls[0][2] if _calls else None, 0)
else:
    # The hub is Linux and runs this suite too. A skip that reads like a pass is
    # how a check stops being able to fail, so assert the refusal instead.
    _ok, _msg = _probe.suspend()
    check("suspend refuses off-Windows", _ok, False)
    check("…and says why", "Windows-only" in _msg, True)

# The whole point of the op's shape is that it answers BEFORE the machine goes
# down, so the reply can cross the socket the hub is waiting on. If it ever
# became a blocking call again the phone would hang for as long as the PC slept,
# and that would surface as "the button is broken", never as an error.
#
# Split by platform rather than run everywhere, because on Linux both ops refuse
# before reaching any of this — and a refusal makes "it had not slept yet when it
# answered" pass for the wrong reason. A check with nothing to observe reports
# exactly like one that observed the right thing, so the Linux side asserts the
# refusal it actually produces instead of coasting on an empty list.
if sys.platform == "win32":
    _slept = []
    _real_suspend = _probe.suspend
    _probe.suspend = lambda: (_slept.append(1), (True, "slept"))[1]
    try:
        _t0 = _time.monotonic()
        _out = deskops.run_local("sleep_pc", {"delay": 0.05})
        _elapsed = _time.monotonic() - _t0
        check("sleep_pc answers without waiting for the resume", _elapsed < 0.5, True)
        check("…and says it is about to sleep", _out.get("ok"), True)
        check("…and has NOT slept yet when it answers", _slept, [])
        _time.sleep(0.5)
        check("…then actually suspends, on its own timer", _slept, [1])
    finally:
        _probe.suspend = _real_suspend

    # A refusal from shutdown.exe must surface as a failure, not a cheerful
    # message. Same lesson the alarm route learned one layer up: never
    # synthesise success out of a request that was turned down.
    _real_run = _subprocess.run

    class _Refused:
        returncode = 1
        stdout = ""
        stderr = "Access is denied.(5)"

    _subprocess.run = lambda *a, **k: _Refused()
    try:
        _out = deskops.run_local("restart_pc", {"delay": 5})
        check("a refused restart is reported as a failure", _out.get("ok"), False)
        check("…carrying the reason", "Access is denied" in _out.get("message", ""), True)
    finally:
        _subprocess.run = _real_run
else:
    # The hub. It is not a desk and must never act like one — this is the same
    # bug the whole module exists to prevent, in its most literal form: a VM in
    # Germany rebooting itself because Jake asked his PC to restart.
    _out = deskops.run_local("sleep_pc", {"delay": 0.05})
    check("the hub refuses to sleep itself", bool(_out.get("error")), True)
    check("…naming the reason", "Windows-only" in _out.get("error", ""), True)
    _out = deskops.run_local("restart_pc", {"delay": 5})
    check("the hub refuses to restart itself", _out.get("ok"), False)
    check("…naming the reason", "Windows-only" in _out.get("message", ""), True)

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURE(S)"))
for f in fails:
    print("  -", f)
sys.exit(1 if fails else 0)
