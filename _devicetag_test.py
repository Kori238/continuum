"""Does the model know which machine it is touching?

Before the hub moved to a VPS this question had no content: one process, one
machine. Now a turn spans three, and the failure mode is not a broken call - it
is a confident answer about the wrong computer. `run_shell` runs in Germany,
`read_clipboard` reads a clipboard in England, and nothing in the schema told
them apart.

Two halves, matching the two ways the model learns the topology:

  A. The manifest in the system prompt - what exists and what is reachable
     right now. Checked in both real arrangements, because they contradict each
     other: on Jake's own PC the hub IS the desk, and a manifest that announces
     "this is Jake's desk" while also listing "desktop: NOT CONNECTED" is worse
     than one that says neither.

  B. The per-tool tag. Mostly that it is *correct* - a tool tagged [hub] that
     really runs on the desk is a worse lie than an untagged tool, since the
     model has no reason to doubt it.

The load-bearing check is the last one: the tag and the schema gate have to
agree. If a tool says [desktop] but survives the gate with no desk attached,
the model has been handed a tool it was told reaches a machine that cannot be
reached - which is exactly the confident-lie failure, reintroduced one level up.

    python -u _devicetag_test.py
"""
import copy
import sys

sys.path.insert(0, ".")

from continuum import deskops, tools
from continuum.devices import remote

fails = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}")
    if not ok:
        fails.append(f"{label}: got {got!r}, wanted {want!r}")


def truthy(label, got):
    print(f"  {'PASS' if got else 'FAIL'}  {label}")
    if not got:
        fails.append(label)


class FakeDevice:
    """Attached far enough to be described."""

    connected = True

    def __init__(self, model, os_name, caps, ago=3):
        self._snap = {"model": model, "os": os_name, "capabilities": caps,
                      "last_seen_ago": ago}

    def snapshot(self):
        return dict(self._snap)


# ---- A. the manifest --------------------------------------------------------

print("\nmanifest - headless hub, nothing attached")
remote._attached.clear()
_real_local_desk = deskops.local_desk
deskops.local_desk = lambda: False
m = remote.manifest()
truthy("names the hub as the machine it runs on", "- hub (" in m)
truthy("says the hub is not where Jake sits", "Jake is not sitting at it" in m)
truthy("warns a shell command here is not about his PC",
       "says nothing about his PC" in m)
truthy("reports the desktop absent", "desktop: NOT CONNECTED" in m)
truthy("reports the phone absent", "phone: NOT CONNECTED" in m)
# The instruction that stops the model substituting the nearest tool that
# happens to return something.
truthy("tells it to say offline rather than answer from hub state",
       "Say it is offline" in m)

print("\nmanifest - headless hub, both devices attached")
remote._attached.update({
    "desktop": FakeDevice("DESKTOP-HC8D6C6", "Windows-11-10.0.26200", ["x"] * 16),
    "phone": FakeDevice("Pixel 7 Pro", "Android-14", ["y"] * 9, ago=12),
})
m = remote.manifest()
truthy("names the desktop by model", "DESKTOP-HC8D6C6" in m)
truthy("names its OS", "Windows" in m)
truthy("names the phone", "Pixel 7 Pro" in m)
truthy("reports freshness", "last reported 12s ago" in m)
check("nothing is reported absent", "NOT CONNECTED" in m, False)

print("\nmanifest - all-local Windows, where the hub IS the desk")
remote._attached.clear()
deskops.local_desk = lambda: True
m = remote.manifest()
truthy("says so outright", "This IS Jake's desk" in m)
# The contradiction this case exists to prevent. Listing the desktop as absent
# while announcing that the hub is the desk describes two machines where there
# is one, and invites the model to report Jake's own PC as unreachable.
check("does NOT also list the desktop as absent",
      "desktop: NOT CONNECTED" in m, False)
truthy("still reports the phone absent", "phone: NOT CONNECTED" in m)
deskops.local_desk = _real_local_desk
remote._attached.clear()

# ---- B. the per-tool tag ----------------------------------------------------

print("\ntool -> device")
# Derived from OPS rather than relisted, so this asserts the derivation holds
# rather than restating a literal - the same reason the OPS table exists.
wrong = sorted(op for op in deskops.OPS if tools.tool_device(op) != "desktop")
check("every desk op is tagged [desktop]", wrong, [])
check("a desk-state reader is too", tools.tool_device("list_open_apps"), "desktop")
# notify is the one tool that answers to two machines: it walks notify._ROUTES,
# taking the desktop when it is attached and the phone when it is not. Either
# single label would be a lie in one of the two arrangements, and falling
# through to the default would name the hub - the one machine it never touches.
check("notify names both machines it reaches",
      tools.tool_device("notify"), "desktop+phone")
# Guards the ordering inside tool_device: the multi-device table has to be
# consulted before the single-device rules, or this falls through them all.
truthy("and is not filed under the hub by default",
       tools.tool_device("notify") != "hub")
check("phone tools are [phone]", tools.tool_device("phone_screen"), "phone")
# The one a prefix rule gets wrong: rung by AlarmManager on the phone, gated on
# self._phone, but with no `phone_` in the name.
check("alarms are [phone] despite the name",
      [tools.tool_device(n) for n in ("set_alarm", "list_alarms", "cancel_alarm")],
      ["phone", "phone", "phone"])
check("the shell runs on the hub", tools.tool_device("run_shell"), "hub")
check("so do the dev tools", tools.tool_device("dev_run"), "hub")
check("and the stores are hub-side", tools.tool_device("recall_memory"), "hub")

print("\ntagging is safe to apply")
before = copy.deepcopy(tools.READ_TOOLS)
sample = next(t for t in tools.READ_TOOLS if t["function"]["name"] == "read_clipboard")
once = tools.tagged(sample)
truthy("the tag leads the description",
       once["function"]["description"].startswith("[desktop] "))
# These schemas are module-level constants shared by every ToolLoop and re-read
# every turn. Decorating in place would edit them for everyone. Compared with
# `truthy`, not `check`: the failure is what matters, and printing the diff on
# success dumps every schema in the file.
truthy("the constant is not mutated", tools.READ_TOOLS == before)
check("applying it twice does not stack tags",
      tools.tagged(once)["function"]["description"],
      once["function"]["description"])

# ---- the invariant: tag and gate must agree ---------------------------------

print("\nthe menu the model actually gets")


def menu(desk_reachable, notify_reachable=None):
    """The real `_schemas()`, with only the desk gate and the stores faked.

    `notify_reachable` defaults to the desk, which is the arrangement that held
    while notify was gated as a desk tool. It is passed explicitly for the case
    that broke that assumption: PC asleep, phone attached, a notification that
    can still land even though nothing about the desk is reachable.

    Built with `__new__` rather than a real Config: `_schemas()` reads about
    twenty `self._x` attributes and nothing else, so standing up fourteen stores
    to reach it would test the constructor instead of the gate. Every store is
    present and truthy here, so no subsystem gate can withhold a tool and
    quietly make the desk assertion below vacuous.
    """
    loop = tools.ToolLoop.__new__(tools.ToolLoop)
    for attr in ("_state", "_memory", "_conversations", "_messagelog",
                 "_routines", "_scheduler", "_ledger", "_workouts", "_activity",
                 "_phone", "_nutrition", "_body", "_health", "_flashcards",
                 "_attachments", "_calendar_ics", "_bus"):
        setattr(loop, attr, object())
    for flag in ("_tools_enabled", "_actions_enabled", "_clipboard_enabled",
                 "_screen_enabled", "_web_enabled", "_dev_enabled",
                 "_shell_enabled", "_turn_live"):
        setattr(loop, flag, True)
    loop._desk = desk_reachable
    loop._notify = desk_reachable if notify_reachable is None else notify_reachable
    return loop._schemas()


# Both are properties on the class, so they are patched on the class - setting
# them on the instance would be shadowed by the descriptor.
_real_prop = tools.ToolLoop._desk_reachable
_real_notify = tools.ToolLoop._notify_reachable
tools.ToolLoop._desk_reachable = property(lambda self: self._desk)
tools.ToolLoop._notify_reachable = property(lambda self: self._notify)
try:
    with_desk = menu(True)
    without_desk = menu(False)
    # The arrangement the phone rung exists for, and the one where nothing can
    # reach him at all - the hub, at rest, with the PC asleep and no phone.
    phone_only = menu(False, notify_reachable=True)
    nothing = menu(False, notify_reachable=False)
finally:
    tools.ToolLoop._desk_reachable = _real_prop
    tools.ToolLoop._notify_reachable = _real_notify


def offers(schemas, name):
    return any(t["function"]["name"] == name for t in schemas)

untagged = sorted(t["function"]["name"] for t in with_desk
                  if not t["function"]["description"].startswith("["))
check("every tool offered carries a tag", untagged, [])
truthy("and there are plenty of them", len(with_desk) > 50)

# The real point. A [desktop] tag on a tool offered while no desk is reachable
# is the confident lie moved up a level: the model is told the tool reaches his
# PC, and it does not.
#
# This caught a live one. The action gate used to read `deskops.ACTION_OPS`,
# which holds every action that *travels* to the desk - and so missed `notify`,
# the one that doesn't. `notify` forwards to an attached desktop, and with none
# attached falls back to a local toast: on the headless hub, a line in a journal
# nobody reads and a PowerShell call that cannot run, returning None exactly
# like a success. The model was handed a tool labelled as reaching Jake's
# screen, called it, saw no error, and told him it had notified him. Both gates
# now read the tag, so a tool cannot claim a device the gate does not require.
orphans = sorted(t["function"]["name"] for t in without_desk
                 if t["function"]["description"].startswith("[desktop]"))
check("no [desktop] tool survives with no desk attached", orphans, [])
truthy("the hub tools are still there",
       any(t["function"]["description"].startswith("[hub]") for t in without_desk))
truthy("withholding the desk removed something",
       len(without_desk) < len(with_desk))

# `notify` is the exception to the rule above, and the reason it needed its own
# gate. It is not a desk tool any more, so the [desktop] sweep cannot speak for
# it, and gating it on the desk would withhold the one tool that still works
# with the PC asleep - the case the phone rung was added for.
print("\nnotify follows the router, not the desk")
truthy("offered with the desk up", offers(with_desk, "notify"))
truthy("still offered with only the phone attached", offers(phone_only, "notify"))
# The failure that started all of this: on a headless hub with nothing attached,
# notify prints a console line, finds no device, shells out to a PowerShell that
# does not exist, swallows the error and returns None - indistinguishable from
# success. The model then reports it notified him. Withheld is the only honest
# answer, because there is nothing left for it to reach.
truthy("withheld when every rung is down", not offers(nothing, "notify"))

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURE(S)"))
for f in fails:
    print("  - " + f)
sys.exit(1 if fails else 0)
