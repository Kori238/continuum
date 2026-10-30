"""Enforce — the actuator half of the budget.

`budget.evaluate()` decides and returns a list of actions; this carries them out.
The split is deliberate and structural: the referee is moving to a hub in Germany,
and when it does, `evaluate()` runs there untouched while *this* runs on whichever
machine is actually holding the app. Keeping it in its own module rather than
inside `server.py` means the desktop client can import the actuator without
dragging in FastAPI and thirteen stores.

Two things happen here, and only two: a notification goes out, or a process is
asked to close.

**Asked**, not killed. `terminate()` sends WM_CLOSE/SIGTERM — the polite signal a
game can catch to flush a save — and nothing here ever escalates to `kill()`. A
process that ignores it wins. That looks like a bug and is the entire point: Jake
asked for "a system put in place to keep me more in control", not a brick wall,
and the tool that corrupts a save file to prove it's serious is a tool he
uninstalls that evening. Friction with a bell on it beats a wall he has to defeat.

The denylist below is defence in depth. Actions only ever name a process that
`budget.categorise()` matched against a rule Jake wrote, so in principle nothing
dangerous can arrive — but "in principle" is doing a lot of work in a function
whose job is terminating processes by name, and a typo'd rule saying
`proc: explorer.exe -> game` should cost him a bad afternoon, not a login session.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Iterable, Optional

from . import deskops
from .notify import notify_async

log = logging.getLogger("continuum.enforce")

# Names that must never be terminated no matter what a rule says. Continuum's own
# interpreter is in here: a rule matching `pythonw.exe` would otherwise have the
# budget shut down the thing enforcing the budget, which is funny exactly once.
_NEVER_CLOSE = {
    "explorer.exe", "winlogon.exe", "csrss.exe", "services.exe", "lsass.exe",
    "svchost.exe", "dwm.exe", "smss.exe", "wininit.exe", "system", "systemd",
    "python.exe", "pythonw.exe", "python", "python3", "continuum.exe",
    "cmd.exe", "powershell.exe", "pwsh.exe", "bash", "sh",
}


def _psutil():
    try:
        import psutil
        return psutil
    except Exception:  # pragma: no cover - psutil is a hard dep, but don't crash the loop
        log.warning("enforce: psutil unavailable, cannot close processes")
        return None


def close_process(proc_name: Optional[str]) -> dict[str, Any]:
    """Ask every process with this name to close. Returns what actually happened.

    Matches on name alone, which is blunt — two windows of the same game are two
    processes and both get the signal. That's correct here: the budget is spent,
    not partially spent.
    """
    name = (proc_name or "").strip()
    if not name:
        return {"closed": 0, "skipped": "no process name"}
    if name.lower() in _NEVER_CLOSE:
        log.error("enforce: refusing to close protected process %r — check the rules", name)
        return {"closed": 0, "skipped": "protected process"}
    ps = _psutil()
    if ps is None:
        return {"closed": 0, "skipped": "psutil unavailable"}

    me = os.getpid()
    hit, failed = [], []
    for p in ps.process_iter(["pid", "name"]):
        try:
            if p.info["pid"] == me:
                continue
            if (p.info["name"] or "").lower() != name.lower():
                continue
            p.terminate()          # polite: the game gets to save
            hit.append(p.info["pid"])
        except (ps.NoSuchProcess, ps.AccessDenied) as exc:
            # AccessDenied means it's running elevated and Jake isn't. Logged, not
            # retried with force — see the module docstring.
            failed.append(f"{p.info.get('pid')}:{type(exc).__name__}")
        except Exception:
            log.exception("enforce: unexpected error terminating %s", name)
    if hit:
        log.info("enforce: asked %s to close (pids %s)", name, hit)
    if failed:
        log.warning("enforce: could not close %s (%s)", name, ", ".join(failed))
    return {"closed": len(hit), "pids": hit, "failed": failed}


async def apply(actions: Iterable[dict[str, Any]],
                phone: Any = None,
                desktop: Any = None) -> list[dict[str, Any]]:
    """Carry out a verdict's actions. Never raises — a failed toast must not take
    down the poll loop that produced it.

    `desktop` is the hub's link to a *remote* desktop. When one is connected,
    every act is routed down that socket instead of happening here, because
    "here" is then a VM in Germany: a toast would be raised for nobody and
    `close_process` would search a process table Jake's game was never in. When
    it's absent *and this host is itself a desk*, this is the machine in question
    and we act directly. Same verdict either way — `budget.evaluate()` never
    learns which.

    The third case is the one that needed saying out loud: a hub with no desktop
    attached. The old code treated "not remote" as "therefore local", so a
    verdict to close Jake's game went looking through the Linux VM's process
    table, found nothing, and reported `{"closed": 0}` — indistinguishable from
    "the game had already exited". The budget then recorded an enforcement that
    never happened. Nothing can be done about it from here, so the honest result
    is to say which machine was missing.
    """
    done: list[dict[str, Any]] = []
    remote = desktop is not None and getattr(desktop, "connected", False)
    desk = remote or deskops.local_desk()
    for a in actions or []:
        kind = a.get("kind")
        title = a.get("title") or "Continuum"
        body = a.get("body") or ""
        try:
            notified = False
            if kind in ("warn", "depleted", "close"):
                if remote:
                    try:
                        await desktop.command("notify", {"title": title, "body": body})
                        notified = True
                    except Exception:
                        # The desktop dropped between the verdict and the act.
                        # The phone mirror below is now the only path left, so
                        # this must not abort the iteration.
                        log.warning("enforce: remote notify failed", exc_info=True)
                elif desk:
                    await notify_async(title, body)
                    notified = True
                # Mirror to the phone when it's connected, because the whole point
                # of a 95% warning is that it reaches him — and at 95% of a gaming
                # budget he is, by definition, looking at a game in fullscreen,
                # which is where Windows toasts go to die. On a detached hub this
                # is not a mirror at all, it is the only surface left.
                if phone is not None and getattr(phone, "connected", False):
                    try:
                        await phone.command("notify", {"title": title, "body": body})
                        notified = True
                    except Exception:
                        log.debug("enforce: phone mirror failed", exc_info=True)
            if kind == "close":
                if remote:
                    res = await desktop.command("close_process", {"proc": a.get("proc")})
                elif desk:
                    res = close_process(a.get("proc"))
                else:
                    log.warning("enforce: cannot close %r — no desktop attached and "
                                "this host is not a desk", a.get("proc"))
                    res = {"closed": 0, "skipped": "no desktop attached"}
                done.append({**a, "result": res})
                continue
            done.append({**a, "result": {"notified": notified}})
        except Exception:
            log.exception("enforce: action %r failed", kind)
            done.append({**a, "result": {"error": True}})
    return done
