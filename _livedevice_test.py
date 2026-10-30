"""Ask the live hub a question only a device-aware model can get right.

Everything up to here was checked against fakes: the manifest with a stub
device, the gate with a patched property. This is the one that matters, because
it exercises the path the change was actually for - a real turn, on the real
hub, with the real desktop attached over the tailnet.

The question is chosen so a wrong answer is unmistakable rather than merely
unhelpful. "Which machine does run_shell run on, and is it the same one
read_clipboard reads?" has a single correct answer (no - hub vs desktop) and the
pre-change prompt actively pushed the other way, opening with "a local assistant
running on Jake's own machine". If the brief and the tags are landing, the model
should name both machines and keep them apart.

    python -u _livedevice_test.py
"""
import asyncio
import json
import sys

import websockets

# Windows consoles default to cp1252, and this is the one suite here whose output is a
# language model's own prose — arrows, dashes and quotation marks it chose for itself. The
# first `→` in a reply raised UnicodeEncodeError, and it raised at the *reporting* line: the
# turn had already been taken and paid for, the answer was sitting in a local variable, and
# not one of the three assertions below ever ran. The traceback named this file and a codec,
# so it read as a broken test rather than as an unread answer.
#
# Reconfigured rather than sanitised on the way out, because the assertions read `text` and
# the printed form is what a human compares against them; scrubbing the display would leave
# those two disagreeing about what the model said.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

HUB = "wss://continuum-hub.skate-wyvern.ts.net/ws"
QUESTION = (
    "Two quick questions about your own setup, no tool calls needed: "
    "(1) which machine does run_shell execute on, and (2) is that the same "
    "machine read_clipboard reads from? Name both machines explicitly."
)


async def main() -> int:
    # Unlike its neighbours this suite needs the hub up, the tailnet routing
    # and a real model turn, so it cannot simply fail when run offline. It also
    # must not quietly pass: a check that reports success when it never ran is
    # the same false-pass trap as a verification command that errors and prints
    # "clean". So an unreachable hub prints SKIPPED, distinctly, and the
    # connection attempt is the only thing this guard covers - once the socket
    # is open, every failure below is a real failure.
    try:
        ws = await asyncio.wait_for(
            websockets.connect(HUB, max_size=8 * 1024 * 1024), timeout=20)
    except Exception as exc:
        print(f"\nSKIPPED - hub unreachable ({type(exc).__name__}: {exc})")
        print("Nothing was verified. Bring the hub up and re-run.")
        return 0

    async with ws:
        # The hub opens with a hello listing attached devices. Worth printing:
        # if the desktop is missing here, a "they are different machines"
        # answer would be right for the wrong reason.
        hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
        print("hello devices:", json.dumps(hello.get("devices"), indent=2)[:600])

        await ws.send(json.dumps({"type": "task", "prompt": QUESTION}))

        # Everything the hub sends back rides as {"type":"event","event":{...}};
        # the answer is the `task.result` event's `output`. There is no separate
        # reply frame, which is what my first attempt guessed at and why it
        # collected nothing.
        text, deadline = "", asyncio.get_event_loop().time() + 240
        while asyncio.get_event_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=60)
            except asyncio.TimeoutError:
                break
            msg = json.loads(raw)
            if msg.get("type") != "event":
                continue
            ev = msg.get("event") or {}
            topic = ev.get("topic")
            if topic == "tool.called":
                print("  tool:", (ev.get("payload") or {}).get("name"))
            if topic == "task.result":
                payload = ev.get("payload") or {}
                # The error path publishes the same topic with `error` instead
                # of `output`. Surfacing it beats reporting an empty answer.
                if payload.get("error"):
                    print("\n[task failed]", payload["error"])
                    return 1
                text = payload.get("output") or ""
                break

        print("\n=== reply ===\n" + text[-2500:])

        low = text.lower()
        # Deliberately weak assertions. This is a language model answering in
        # prose; pinning it to phrasing would fail on a correct answer worded
        # differently. What it must do is name the two machines and not merge
        # them.
        checks = {
            "names the hub/server": any(w in low for w in ("hub", "server", "vps")),
            "names the desktop/PC": any(w in low for w in ("desktop", "your pc", "his pc")),
            "keeps them apart": any(w in low for w in ("no,", "not the same", "different machine",
                                                       "two different", "separate machine")),
        }
        print()
        for label, ok in checks.items():
            print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
