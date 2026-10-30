"""Entry point.

    python -m continuum app      # open the desktop control app (native window)
    python -m continuum app --hub https://hub.example.ts.net
                                 # ...as a window onto a hub elsewhere (no local server)
    python -m continuum stop     # stop a running server/app instance
    python -m continuum demo     # run the first vertical slice once
    python -m continuum run      # boot the fabric and stay up (autostart target)
    python -m continuum serve    # run the app server headless (no window)
    python -m continuum desktop  # report this desktop to a hub elsewhere

`desktop` is the other side of the migration: the hub runs `serve`, this machine
runs `desktop`, and they meet over the tailnet. Takes its own flags —
`python -m continuum desktop --help`.
"""
from __future__ import annotations

import asyncio
import sys

from . import supervisor


def _flag(argv: list[str], name: str) -> str | None:
    """Read `--flag value` or `--flag=value` out of argv. Deliberately not
    argparse: `desktop` owns its own parser and anything richer here would
    start competing with it."""
    for i, arg in enumerate(argv):
        if arg == name:
            return argv[i + 1] if i + 1 < len(argv) else None
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1] or None
    return None


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "app"
    if cmd == "app":
        hub = _flag(sys.argv[2:], "--hub")
        if hub:
            # A viewer for a hub elsewhere — no local server comes up.
            from .app.window import open_remote
            open_remote(hub)
        else:
            from .app.window import launch
            launch()
    elif cmd == "stop":
        from .app.window import stop_running
        print("stopped" if stop_running() else "nothing running")
    elif cmd == "serve":
        from .app.server import serve
        serve()
    elif cmd == "demo":
        asyncio.run(supervisor.run_demo())
    elif cmd == "run":
        asyncio.run(supervisor.run_forever())
    elif cmd == "desktop":
        # Its own argv, so --hub/--interval don't collide with anything above.
        from .client.desktop import main as desktop_main
        desktop_main(sys.argv[2:])
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
