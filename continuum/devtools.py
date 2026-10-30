"""Self-development tools — Continuum working on its own codebase.

The powerful, sensitive capability: Continuum can read, edit, run, and restart
its own source. Built to the design brief's autonomy model — scoped, gated,
revertible, everything logged:

  * Repo-jailed. Every path is resolved and must live inside the Continuum repo;
    anything escaping it (system files, the OpenClaw workspace, `..`) is refused.
    `.git` is never writable.
  * Off by default. The whole toolset only appears when dev tools are enabled
    (`CONTINUUM_DEV_TOOLS=1` or the UI toggle) — a deliberate switch, not ambient.
  * Revertible. The repo is under git; a broken change is `git reset`-able, and
    `restart` refuses to relaunch code that doesn't even import.
  * Audited. Each mutation/command is emitted as an `action.performed` bus event.

These primitives are the whole toolkit. There is deliberately no "delegate the
task to another coding agent" escape hatch: Continuum's own agent reads, edits,
runs and verifies through these tools, so every step lands on its own bus and in
its own conversation. Handing the job to a nested agent hid the work, split the
memory of it across two places, and made "it says it changed something" and "it
changed something" two different claims.
"""
from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("continuum.devtools")

_REPO = Path(__file__).resolve().parent.parent   # .../continuum (repo root)
_NO_WRITE = {".git"}                              # never writable subtrees

# The exit code a deliberate restart-by-dying uses. Anything non-zero satisfies
# systemd's `Restart=on-failure`; a distinctive number makes the journal readable
# afterwards, so "did it crash or did it restart itself?" has an answer.
EXIT_RESTART = 42


def _kill_tree(pid: int) -> None:
    """Kill a process *and its descendants*.

    Terminating just the pid is not enough here: `shell=True` gives us a cmd.exe
    whose children (gradlew → the Gradle daemon → the Kotlin daemon) survive it
    happily. Windows has no process groups worth the name, so shell out to
    taskkill /T; POSIX gets the process group we asked Popen to create.
    """
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception:
        log.warning("could not kill process tree %s", pid, exc_info=True)


class DevTools:
    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = (root or _REPO).resolve()

    # ---- path jail ----------------------------------------------------------
    def _resolve(self, rel: str, *, write: bool = False) -> Path:
        rel = (rel or "").strip().strip('"').replace("\\", "/")
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise ValueError(f"path escapes the repo: {rel!r}")
        top = p.relative_to(self.root).parts[:1]
        if write and top and top[0] in _NO_WRITE:
            raise ValueError(f"{top[0]} is protected from writes")
        return p

    def _rel(self, p: Path) -> str:
        return str(p.relative_to(self.root)).replace("\\", "/")

    # ---- read ---------------------------------------------------------------
    def read_file(self, path: str, max_bytes: int = 60_000) -> str:
        p = self._resolve(path)
        if not p.is_file():
            return f"not a file: {path}"
        data = p.read_text(encoding="utf-8", errors="replace")
        if len(data) > max_bytes:
            return data[:max_bytes] + f"\n… [truncated, {len(data)} chars total]"
        return data

    def list_files(self, pattern: str = "**/*.py", limit: int = 200) -> list[str]:
        pattern = (pattern or "**/*.py").strip()
        out = []
        for p in sorted(self.root.glob(pattern)):
            if not p.is_file():
                continue
            rel = self._rel(p)
            if any(seg in rel for seg in (".git/", ".venv/", "__pycache__/", ".egg-info/")):
                continue
            out.append(rel)
            if len(out) >= limit:
                break
        return out

    def grep(self, pattern: str, glob: str = "**/*.py", *,
             ignore_case: bool = False, max_results: int = 200) -> str:
        """Regex-search the source tree, returning `path:line: text` per hit.

        The read-only counterpart to shelling out to grep/sed for inspection:
        same glob-walk and skip list as list_files, but it looks *inside* files.
        Read with replacement so a stray byte in one file can't abort the search;
        capped at max_results with a note, so a broad pattern can't flood."""
        pattern = (pattern or "").strip()
        if not pattern:
            return "no pattern"
        try:
            rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as exc:
            return f"bad regex: {exc}"
        glob = (glob or "**/*.py").strip()
        hits: list[str] = []
        truncated = False
        for p in sorted(self.root.glob(glob)):
            if not p.is_file():
                continue
            rel = self._rel(p)
            if any(seg in rel for seg in (".git/", ".venv/", "__pycache__/", ".egg-info/")):
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{rel}:{i}: {line.strip()[:200]}")
                    if len(hits) >= max_results:
                        truncated = True
                        break
            if truncated:
                break
        if not hits:
            return "(no matches)"
        out = "\n".join(hits)
        if truncated:
            out += f"\n… [truncated at {max_results} matches]"
        return out

    # ---- write --------------------------------------------------------------
    def write_file(self, path: str, content: str) -> str:
        p = self._resolve(path, write=True)
        p.parent.mkdir(parents=True, exist_ok=True)
        existed = p.exists()
        p.write_text(content, encoding="utf-8")
        return f"{'updated' if existed else 'created'} {self._rel(p)} ({len(content)} chars)"

    def edit_file(self, path: str, old: str, new: str) -> str:
        p = self._resolve(path, write=True)
        if not p.is_file():
            return f"not a file: {path}"
        text = p.read_text(encoding="utf-8")
        n = text.count(old)
        if n == 0:
            return "old_string not found — no change made"
        if n > 1:
            return f"old_string appears {n} times — make it unique; no change made"
        p.write_text(text.replace(old, new, 1), encoding="utf-8")
        return f"edited {self._rel(p)}"

    def replace(self, pattern: str, replacement: str, *,
                paths: Optional[list] = None, glob: str = "",
                count: int = 0, ignore_case: bool = False) -> str:
        """Regex find-and-replace across one or more files — the multi-file /
        regex counterpart to edit_file's single literal swap, and what kills the
        remaining reasons to shell out to `sed -i`.

        Targets are an explicit `paths` list, a `glob`, or both. Each file is
        jailed through `_resolve(write=True)`, so `.git` and anything outside the
        repo are refused here exactly as they are for every other write. Only
        valid UTF-8 text is touched: a file that doesn't decode cleanly is a
        binary and is skipped, so a broad glob can't corrupt a `.db` or an image.
        `count` caps replacements per file (0 = all). Reports per-file counts;
        pair it with `dev_diff` to see the actual hunks before trusting it."""
        if not pattern:
            return "no pattern"
        try:
            rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as exc:
            return f"bad regex: {exc}"
        targets: list[Path] = []
        for rel in (paths or []):
            p = self._resolve(rel, write=True)
            if not p.is_file():
                return f"not a file: {rel}"
            targets.append(p)
        if glob:
            for p in sorted(self.root.glob(glob.strip())):
                if not p.is_file():
                    continue
                rel = self._rel(p)
                if any(seg in rel for seg in (".git/", ".venv/", "__pycache__/", ".egg-info/")):
                    continue
                targets.append(self._resolve(rel, write=True))
        if not targets:
            return "no target files — pass paths or a glob that matches something"
        seen: set[Path] = set()
        ordered = [p for p in targets if not (p in seen or seen.add(p))]
        total, changed, skipped = 0, [], 0
        for p in ordered:
            try:
                text = p.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                skipped += 1              # binary — never rewrite it
                continue
            new, n = rx.subn(replacement, text, count=count)
            if n:
                p.write_text(new, encoding="utf-8")
                total += n
                changed.append(f"{self._rel(p)}: {n} replacement(s)")
        if not total:
            tail = f" ({skipped} binary file(s) skipped)" if skipped else ""
            return f"no matches — nothing changed{tail}"
        out = f"{total} replacement(s) across {len(changed)} file(s):\n" + "\n".join(changed)
        if skipped:
            out += f"\n({skipped} binary file(s) skipped)"
        return out

    # ---- run ----------------------------------------------------------------
    def _capture(self, argv: Any, *, shell: bool,
                 timeout: Optional[float],
                 cwd: Optional[Path] = None) -> tuple[Optional[int], str, str]:
        """Run a command to completion and return (exit code, stdout, stderr).

        Output goes to temp *files*, not pipes, and that is the whole point.
        `subprocess.run(capture_output=True, timeout=…)` starts a reader thread
        per pipe, and on Windows — CPython's subprocess.py, in `run`'s except
        branch — a TimeoutExpired is followed by a *second* `communicate()` with
        no timeout, to drain what the child already wrote. So the timeout you
        asked for is discarded and the call joins those threads forever if
        anything still holds the write end of the pipe. A grandchild does: kill
        `gradlew` and the Gradle daemon it spawned inherits the handle and keeps
        it open for hours. That is precisely how a `dev_run` build froze the
        whole app on 2026-08-21.

        Files have no reader threads to join, so `wait(timeout=…)` means what it
        says, an inherited handle costs nothing, and on a timeout we still get
        the output the command managed to produce before it was killed.
        """
        # ignore_cleanup_errors: a just-killed grandchild may still hold these
        # files open for a moment, and failing to tidy a temp dir must not turn
        # into the error the caller sees.
        with tempfile.TemporaryDirectory(prefix="continuum-run-",
                                         ignore_cleanup_errors=True) as tmp:
            op, ep = Path(tmp) / "out", Path(tmp) / "err"
            code: Optional[int]
            with open(op, "wb") as of, open(ep, "wb") as ef:
                proc = subprocess.Popen(
                    argv, shell=shell, cwd=str(cwd or self.root),
                    stdin=subprocess.DEVNULL, stdout=of, stderr=ef,
                    # Its own process group, so _kill_tree's POSIX branch kills
                    # the child's group and not the one Continuum is sitting in.
                    # Windows accepts and ignores this; it uses taskkill /T.
                    start_new_session=(os.name != "nt"),
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                try:
                    code = proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    # /T, not just this pid: the shell we launched is usually a
                    # cmd.exe wrapper, so killing it alone orphans the actual
                    # work and leaves it running after we've reported a timeout.
                    _kill_tree(proc.pid)
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        pass
                    code = None
            # Build output is whatever encoding the tool felt like; utf-8 with
            # replacement never raises. `text=True` used to decode as cp1252 and
            # a stray 0x8f killed the reader thread mid-command.
            out = op.read_bytes().decode("utf-8", "replace")
            err = ep.read_bytes().decode("utf-8", "replace")
        return code, out, err

    def run(self, command: str, timeout: float = 120.0) -> str:
        command = (command or "").strip()
        if not command:
            return "no command"
        code, stdout, stderr = self._capture(command, shell=True, timeout=timeout)
        out = stdout + (("\n[stderr]\n" + stderr) if stderr else "")
        out = out.strip() or "(no output)"
        if len(out) > 6000:
            out = out[:6000] + "\n… [truncated]"
        if code is None:
            return f"timed out after {timeout}s — process tree killed\n{out}"
        return f"exit {code}\n{out}"

    def shell(self, command: str, timeout: float = 120.0) -> str:
        """Run an arbitrary shell command on the machine — the general system
        access `run()` deliberately is *not*. Same file-captured, timeout-safe,
        kill-the-whole-tree machinery, but cwd is Jake's home folder, not the
        repo: this is for `netsh`/`powercfg`/`winget`-style work anywhere on the
        box, not for editing Continuum's own source (that stays `run()`, at the
        repo root). Still un-jailed and `shell=True` — the caller governs what it
        runs; the exposure gate lives in tools.py, not here."""
        command = (command or "").strip()
        if not command:
            return "no command"
        code, stdout, stderr = self._capture(command, shell=True, timeout=timeout,
                                             cwd=Path.home())
        out = stdout + (("\n[stderr]\n" + stderr) if stderr else "")
        out = out.strip() or "(no output)"
        if len(out) > 6000:
            out = out[:6000] + "\n… [truncated]"
        if code is None:
            return f"timed out after {timeout}s — process tree killed\n{out}"
        return f"exit {code}\n{out}"

    def compiles(self) -> tuple[bool, str]:
        """Does the package still import/byte-compile? The pre-restart safety gate."""
        code, stdout, stderr = self._capture(
            [sys.executable, "-m", "compileall", "-q", "continuum"],
            shell=False, timeout=120.0)
        return code == 0, (stdout + stderr).strip()

    # ---- restart ------------------------------------------------------------
    def restart(self, port: int = 8848) -> str:
        """Relaunch so code changes take effect — but only if it still compiles,
        so a broken edit can't take the app down.

        Two machines, two mechanisms. On the desktop, Continuum owns its own
        lifetime: it spawns a replacement and the newcomer's single-instance
        guard retires this one. On the hub it does not — systemd started it and
        systemd will start it again, so the way back up is to die on purpose.
        The old code did the desktop thing everywhere, which on the hub meant
        spawning `pythonw.exe` (absent), or failing that `python -m continuum
        app` — the pywebview viewer, on a box with no display — while the unit's
        real process carried on running the old code. `dev_restart` reported
        success and nothing restarted.
        """
        ok, msg = self.compiles()
        if not ok:
            return f"refused to restart — code does not compile:\n{msg[:800]}"
        if sys.platform != "win32":
            return self._restart_by_exit()
        # Relaunch with *this* interpreter when it's the windowless one. Going via
        # .venv\Scripts\pythonw.exe leaves a launcher-stub parent behind, and the
        # single-instance guard only kills whatever holds the port — so every
        # restart orphaned another stub. sys.executable is already venv-resolved.
        exe = sys.executable
        if not os.path.basename(exe).lower().startswith("pythonw"):
            venv = os.path.join(self.root, ".venv", "Scripts", "pythonw.exe")
            if os.path.exists(venv):
                exe = venv
        # Relaunch; the new instance's single-instance guard stops this one. The
        # child outlives us without DETACHED_PROCESS — Windows doesn't kill
        # children with their parent — and DETACHED_PROCESS would risk a visible
        # console window, which is exactly how the old Ollama supervisor used to
        # flash a terminal on Jake's screen every boot. pythonw has no console,
        # so a child inherits *no* valid stdio handles and the first write to
        # stdout would kill it silently. Give it real files.
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        out = open(self.root / "_app.log", "ab", buffering=0)
        err = open(self.root / "_app.err.log", "ab", buffering=0)
        subprocess.Popen([exe, "-m", "continuum", "app"], cwd=str(self.root),
                         stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                         creationflags=flags, close_fds=True)
        return "restart launched — a fresh instance is starting and will replace this one"

    def _restart_by_exit(self) -> str:
        """Restart by dying, and letting whatever started us notice.

        systemd sets `INVOCATION_ID` in the environment of every unit it starts,
        so its presence is the honest test for "something is watching this
        process and will bring it back". Without it, exiting is not a restart —
        it is just stopping, and a tool that stops the hub while reporting a
        restart is worse than one that refuses.

        The exit is deferred to a thread so this answer reaches the log and the
        UI's `app_restarting` broadcast first. `os._exit` rather than
        `sys.exit`: this runs on a worker thread, where SystemExit only unwinds
        that thread and the process would sail on having told Jake it was
        leaving.
        """
        if not os.environ.get("INVOCATION_ID"):
            return ("refused to restart — nothing is supervising this process, so "
                    "exiting would stop Continuum rather than restart it, and there "
                    "is no desktop session here to relaunch into. Restart it by hand "
                    "(`systemctl restart continuum`, or however it was started).")

        def _die() -> None:
            time.sleep(1.5)
            log.warning("restart: exiting %d so the service manager relaunches us",
                        EXIT_RESTART)
            os._exit(EXIT_RESTART)

        threading.Thread(target=_die, name="continuum-restart", daemon=True).start()
        return ("restart requested — exiting in a moment so the service manager "
                "starts a fresh process; it should be back within a few seconds")

    def diff(self, max_chars: int = 6000) -> str:
        """Uncommitted changes — how the agent checks what it actually did,
        rather than what it believes it did."""
        out = self.run("git --no-pager diff --stat && git --no-pager diff", timeout=30)
        return out[:max_chars]

    def revert(self, path: str = "") -> str:
        """Undo uncommitted changes — the whole tree, or one file. The escape
        hatch that makes editing its own source a recoverable mistake."""
        if path:
            rel = self._rel(self._resolve(path, write=True))
            return self.run(f'git checkout -- "{rel}"', timeout=30)
        return self.run("git checkout -- .", timeout=30)
