"""End-to-end proof that a desk question asked of the hub reaches this desk.

The probe is a file created in *this* machine's Downloads folder moments before
asking. It is unforgeable from Germany in the way that matters: `recent_files`
reads `~/Downloads`, which exists on Linux too, so before this change the hub
answered it happily with real data about the wrong machine. A token generated
per run means a stale answer cannot pass either.

Not the clipboard: `read_clipboard` sits behind CONTINUUM_CLIPBOARD, which is
off by default on the hub, so it proves nothing about routing.

Frames are accumulated into one buffer rather than searched individually - the
reply streams, so a token can straddle two frames.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HUB = "wss://continuum-hub.skate-wyvern.ts.net/ws"
TOKEN = f"routing-proof-{uuid.uuid4().hex[:8]}"


async def clipboard_probe() -> int:
    """The same proof via the clipboard, which is the stronger of the two.

    `~/Downloads` at least exists on the hub; a clipboard does not exist there
    at all, so a token coming back cannot have been answered locally under any
    circumstances. Requires CONTINUUM_CLIPBOARD=1 on the hub - without it the
    tool is withheld from the model and this fails for a reason that has nothing
    to do with routing, which is exactly the trap that cost me a run.
    """
    from continuum.state import probe
    ok, msg = probe.set_clipboard_text(TOKEN)
    if not ok:
        print(f"FAIL  could not set this machine's clipboard: {msg}")
        return 1
    print(f"clipboard on this desk := {TOKEN}")
    return await ask("Read my clipboard with your tools and reply with its "
                     "exact contents and nothing else.")


async def ask(prompt: str, show: bool = False) -> int:
    from websockets.asyncio.client import connect

    async with connect(HUB, max_size=16 * 1024 * 1024, open_timeout=20) as sock:
        await sock.send(json.dumps({
            "type": "task", "title": "routing probe", "prompt": prompt,
            "complexity": "low", "new_task": True,
        }))
        buf: list[str] = []
        deadline = asyncio.get_event_loop().time() + 150
        while asyncio.get_event_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(sock.recv(), timeout=25)
            except asyncio.TimeoutError:
                break
            buf.append(raw if isinstance(raw, str) else raw.decode("utf-8", "replace"))
            joined = "".join(buf)
            body = joined.split("}", 1)[-1] if joined.startswith('{"type":"hello"') else joined
            if not show and TOKEN in body:
                print("\nPASS  the hub reported something only this desk knows")
                return 0
        joined = "".join(buf)
        if show:
            print(f"\n--- reply ({len(buf)} frames) ---")
            print(joined[-1400:])
            return 0
        print(f"\nFAIL  token never came back ({len(buf)} frames)")
        print(f"last frames: {joined[-700:]}")
        return 1


async def screen_probe() -> int:
    """see_screen has no token to match on, so this prints the reply instead.

    The proof is the content: the capture runs on the desktop and the reading
    runs in the cloud eye, so a real description of Jake's actual screen means
    the whole chain worked. An error, or a description of nothing, means it did
    not. Needs CONTINUUM_SCREEN=1 on the hub.
    """
    return await ask("Look at my screen and tell me in one sentence what "
                     "application is in focus and what is on it.", show=True)


async def main() -> int:
    if "--screen" in sys.argv:
        return await screen_probe()
    if "--clipboard" in sys.argv:
        return await clipboard_probe()
    downloads = os.path.join(os.path.expanduser("~"), "Downloads")
    if not os.path.isdir(downloads):
        print(f"FAIL  no Downloads folder at {downloads}")
        return 1
    probe_file = os.path.join(downloads, f"{TOKEN}.txt")
    with open(probe_file, "w", encoding="utf-8") as fh:
        fh.write("continuum routing probe\n")
    print(f"created on this desk: {probe_file}")

    from websockets.asyncio.client import connect

    try:
        async with connect(HUB, max_size=16 * 1024 * 1024, open_timeout=20) as sock:
            await sock.send(json.dumps({
                "type": "task",
                "title": "routing probe",
                "prompt": ("List my most recent files using your tools, then tell "
                           "me the newest filename verbatim."),
                "complexity": "low",
                "new_task": True,
            }))

            buf: list[str] = []
            deadline = asyncio.get_event_loop().time() + 150
            while asyncio.get_event_loop().time() < deadline:
                try:
                    raw = await asyncio.wait_for(sock.recv(), timeout=25)
                except asyncio.TimeoutError:
                    break
                buf.append(raw if isinstance(raw, str) else raw.decode("utf-8", "replace"))
                joined = "".join(buf)
                # Skip the opening `hello` frame, which advertises every device
                # capability by name and would otherwise match anything.
                body = joined.split("}", 1)[-1] if joined.startswith('{"type":"hello"') else joined
                if TOKEN in body:
                    print("\nPASS  the hub reported a file that only exists on this desk")
                    return 0

            joined = "".join(buf)
            print(f"\nFAIL  token never came back ({len(buf)} frames, {len(joined)} chars)")
            tail = joined[-700:].replace("\\n", " ")
            print(f"last frames: {tail}")
            return 1
    finally:
        try:
            os.remove(probe_file)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
