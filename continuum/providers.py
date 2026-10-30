"""Providers — the actual model backends the router chooses between.

Everything is Claude. There was a local Ollama rung; it was removed because it
was unused and unreliable, and because pinning anything to it meant that when it
was down the feature was down. Both rungs of the ladder are now the same Claude,
at two different model sizes, reached one of two ways:

  claude-cli : shell out to the Claude Code CLI, reusing its existing login
               (your Claude *subscription*). No API key, no secret in code or
               config; the CLI handles auth + token refresh. This is the default
               when no ANTHROPIC_API_KEY is set.
  anthropic  : the official SDK, reading ANTHROPIC_API_KEY from the environment.
               Preferred automatically if that key is present (metered API).

NB: Anthropic has no embeddings endpoint, so memory's embedder is deliberately
NOT a provider — it runs in-process. See memory/embed.py.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
import uuid

import httpx

from .config import Config, ModelTier

log = logging.getLogger("continuum.providers")


def provider_for(cfg: Config, tier: ModelTier):
    """The backend that can run this rung.

    Both rungs share a provider, chosen once in config, so every caller that used
    to write `OllamaProvider(cfg) if local else ClaudeCLIProvider(cfg)` now asks
    here instead. One place to change when a third way to reach Claude appears.
    """
    if tier.provider == "anthropic":
        return AnthropicProvider(cfg)
    if tier.provider == "claude-cli":
        return ClaudeCLIProvider(cfg)
    raise ValueError(f"unknown provider {tier.provider!r} for tier {tier.name!r}")


def tier_for(cfg: Config, choice: str) -> ModelTier:
    """Resolve a configured rung name ("cloud" | "fast") to a tier.

    Anything unrecognised resolves to the heavy rung on purpose: a typo in a
    config value should cost money, not silently downgrade the model answering
    Jake's questions.
    """
    return cfg.cloud_fast if str(choice).strip().lower() == "fast" else cfg.cloud


class ClaudeCLIProvider:
    """Cloud tier via the Claude Code CLI — reuses your subscription login.

    Calls `claude -p "<prompt>" --model <model>` (print mode). Auth comes from the
    CLI's own stored credentials, so Continuum never sees or stores a secret.

    CRITICAL: the CLI is an *agent* with file/tool access, so run from a dedicated
    EMPTY sandbox directory. Otherwise it reads the project files and answers from
    them instead of purely from Continuum's context — a real contamination bug
    (it once "recalled" a bike Jake never told it about, straight out of a test
    script). An empty cwd gives it nothing to find; the prompt is the only input.
    """

    def __init__(self, cfg: Config) -> None:
        self._exe = cfg.claude_cli
        self._app_url = os.getenv("CONTINUUM_APP_URL", "http://127.0.0.1:8848").rstrip("/")
        self._mcp_ok = False
        self._mcp_checked = -1e9
        import re
        import shutil as _sh
        import tempfile
        self._sandbox = os.path.join(tempfile.gettempdir(), "continuum-cloud-sandbox")
        os.makedirs(self._sandbox, exist_ok=True)
        # Claude Code keeps a persistent per-project memory (~/.claude/projects/
        # <cwd>/memory) and auto-loads it every session. Left alone it accumulates
        # facts across calls and reloads them — the cloud tier would "remember"
        # things Continuum never told it (it once recalled a bike from a test
        # script). Wipe the sandbox project's memory so the cloud tier is stateless;
        # tools are disabled at call time so it can't write new memory either.
        sanitized = re.sub(r"[:\\/]", "-", self._sandbox)
        mem = os.path.join(os.path.expanduser("~"), ".claude", "projects", sanitized, "memory")
        _sh.rmtree(mem, ignore_errors=True)

    async def available(self) -> bool:
        return shutil.which(self._exe) is not None

    # Deny every file/system/network tool. The cloud tier reads nothing off the
    # machine directly — TodoWrite included, or it invents a private task list and
    # reports work it never did.
    _NO_TOOLS = ("Bash Read Glob Grep Edit Write WebFetch WebSearch Task "
                 "NotebookEdit TodoWrite")

    # …except Continuum's OWN tools, served over MCP by the running app. Same
    # schemas, same gates, same audit trail as the local tier — so "mark medicine
    # done" actually writes to the routine instead of being narrated.
    _MCP_SERVER = "continuum"

    def _mcp_config(self) -> str:
        from .app.security import MCP_TOKEN
        return json.dumps({"mcpServers": {self._MCP_SERVER: {
            "type": "http", "url": f"{self._app_url}/mcp",
            # Per-process token: the tool plane is not open just because the
            # port is reachable. Passed via config on stdin-ish argv, not stored.
            "headers": {"Authorization": f"Bearer {MCP_TOKEN}"}}}})

    async def _mcp_live(self) -> bool:
        """Only hand the CLI an MCP server that's actually answering — pointing it
        at a dead endpoint costs a startup stall on every cloud call. Normally the
        app is this very process; the probe matters for CLI/one-shot use."""
        now = time.monotonic()
        if now - self._mcp_checked < 30.0:
            return self._mcp_ok
        from .app.security import MCP_TOKEN
        try:
            async with httpx.AsyncClient(timeout=1.5) as client:
                r = await client.post(
                    f"{self._app_url}/mcp",
                    headers={"Authorization": f"Bearer {MCP_TOKEN}"},
                    json={"jsonrpc": "2.0", "id": 0, "method": "ping"})
                self._mcp_ok = r.status_code == 200
        except Exception:
            self._mcp_ok = False
        self._mcp_checked = now
        return self._mcp_ok

    # The operating brief. Without it the model treats the MCP tools as optional
    # colour and narrates instead of acting — exactly the failure Jake caught: a
    # confident account of routine edits that never happened. So the brief is
    # mostly about the gap between saying and doing, and about closing the loop
    # by *checking* rather than assuming.
    _BRIEF = """You are Continuum, Jake's personal assistant.

Your tools are not a simulation. They read and write real state: his routine,
his memory, his messages, and — when dev tools are on — your own source code.
Nothing happens unless you call a tool.

You are spread across several machines, and which one a tool lands on matters.
Every tool description starts with the device it acts on:

- [hub]     the machine you are running on. Your shell, your dev tools, and all
            stored data (memory, routine, activity, budget) live here.
- [desktop] Jake's PC, the machine he is actually sitting at. His screen,
            clipboard, open apps, Discord/Slack, files, browser.
- [phone]   his phone. Battery, location, notifications, alarms, its screen.

Never answer a question about one device using a tool that reads another. If
the hub and his desktop are different machines, a shell command here is a fact
about a server in a datacentre and nothing at all about his PC — say which
machine you are describing. If a device is listed below as not connected, its
tools are withheld from you entirely: say it is offline rather than reaching for
the nearest tool that returns something.

How to work:
- Think first about what would actually answer this, then do it. Several tool
  calls in one turn is normal and expected; one is often not enough.
- Look before you change. Read the routine, the file, the current state.
- After you change something, verify it. Re-read it, run a real command, check
  the diff. Do not assume a call did what you meant.
- If a tool fails or isn't available, say so plainly. Never describe an action
  you did not take, and never present an intention as a completed change.
- Report what you actually did, briefly. If you verified it, say how.

Editing your own source (the dev_* tools):
- You are documented to yourself in docs/. Read docs/README.md first — it is a
  four-line index — then the file it points at: docs/ARCHITECTURE.md (what you
  are, and which file owns what), docs/SELF-DEVELOPMENT.md (this procedure in
  full), docs/DATA.md (memory, routine, conversations), docs/INTERFACES.md
  (HTTP, WebSocket, MCP, security). Reading beats guessing at which file to open.
- If a change makes one of those docs wrong, fix the doc in the same turn.
  Stale self-documentation is a confident account of a system that no longer
  exists — the same failure as reporting an edit you did not make.
- Read a file before editing it, and prefer dev_edit_file over rewriting it.
- After editing, run dev_run with the compile check and dev_diff to see the real
  change. dev_revert undoes uncommitted work when you got it wrong.
- Nothing you edit is live until the app relaunches, so when a source change is
  done and verified, call dev_restart — don't leave Jake to work out that he
  needs to restart. It refuses if the code doesn't compile, and it's deferred
  until after this reply reaches him, so say in your reply that the app is
  restarting to apply the change (and that the UI will reconnect on its own).

Be concise and direct. No preamble, no flattery."""

    def _brief(self) -> str:
        """The brief plus the devices actually attached at this moment.

        Composed per turn rather than baked into `_BRIEF`, because the topology
        genuinely changes underneath a running process: the hub boots with
        nothing attached and Jake's desktop dials in minutes later, his phone
        drops on Android doze and comes back. A manifest captured at import
        would describe whichever arrangement happened to exist at boot and go on
        asserting it for the life of the process.

        Degrades to the static brief if the registry raises. A model with a
        slightly vaguer prompt is a much smaller problem than a turn that dies
        before it starts.
        """
        try:
            from .devices.remote import manifest
            return f"{self._BRIEF}\n\nDevices right now:\n{manifest()}"
        except Exception:
            log.debug("could not build the device manifest", exc_info=True)
            return self._BRIEF

    async def generate(self, tier: ModelTier, prompt: str) -> str:
        args = [
            self._exe, "-p", prompt, "--model", tier.model,
            "--disallowedTools", self._NO_TOOLS,
        ]
        if await self._mcp_live():
            args += [
                # --strict-mcp-config so it loads ONLY this server, never the
                # user's global MCP config.
                "--mcp-config", self._mcp_config(), "--strict-mcp-config",
                "--allowedTools", f"mcp__{self._MCP_SERVER}",
                # Tools are already gated on our side (permission flags, repo
                # jail, audit events); a second prompt the model can't answer in
                # print mode would just deadlock the turn.
                "--permission-mode", "acceptEdits",
                "--append-system-prompt", self._brief(),
            ]
        # A turn that reads, edits, compiles and verifies is not a 180s turn any
        # more. Still bounded — a hung subprocess must not wedge the socket.
        budget = float(os.getenv("CONTINUUM_CLOUD_TIMEOUT", "900"))
        # An ask_user tool call parks the MCP request until Jake answers (up to
        # 300s). The CLI's own per-tool MCP timeout would otherwise cut that short
        # — Claude Code defaults it low — so lift both to the whole-turn budget.
        # Setting these does no harm if the CLI ignores the names.
        ms = str(int(budget * 1000))
        env = {**os.environ, "MCP_TOOL_TIMEOUT": ms, "MCP_TIMEOUT": ms}
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._sandbox,  # isolate from the project so it can't read files
            env=env,
            # `claude` is a console .exe and the app runs windowless, so without
            # this Windows gives it a brand-new console window — one visible
            # flash per cloud call, which on a proactive cadence is constant.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=budget)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"claude CLI timed out after {budget:.0f}s")
        if proc.returncode != 0:
            msg = err.decode("utf-8", "replace").strip() or f"exit {proc.returncode}"
            raise RuntimeError(f"claude CLI failed: {msg[:300]}")
        return out.decode("utf-8", "replace").strip()

    # Vision is the one call that needs Read *allowed*: there is no way to hand
    # the CLI image bytes on argv, so the image is written into the sandbox and
    # the model opens it there. That's safe precisely because of the isolation
    # that already exists — the sandbox is empty by construction, so Read can
    # reach the screenshot and nothing else. Everything else stays denied.
    _VISION_NO_TOOLS = ("Bash Glob Grep Edit Write WebFetch WebSearch Task "
                        "NotebookEdit TodoWrite")

    async def vision(self, model: str, prompt: str, image: bytes,
                     timeout: float = 300.0) -> str:
        """Answer a question about an image, using the cloud model's eyes.

        The image lands in the sandbox only for the duration of the call and is
        deleted afterwards even if the call fails — a screenshot is not
        something to leave lying in temp.
        """
        name = f"_screen-{uuid.uuid4().hex[:8]}.png"
        path = os.path.join(self._sandbox, name)
        with open(path, "wb") as fh:
            fh.write(image)
        args = [
            self._exe, "-p",
            f"Read the image file {name} in the current directory, then answer "
            f"the question below about it.\n\n{prompt}",
            "--model", model,
            "--disallowedTools", self._VISION_NO_TOOLS,
            "--allowedTools", "Read",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._sandbox,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                raise RuntimeError(f"cloud vision timed out after {timeout:.0f}s")
            if proc.returncode != 0:
                msg = err.decode("utf-8", "replace").strip() or f"exit {proc.returncode}"
                raise RuntimeError(f"cloud vision failed: {msg[:300]}")
            return out.decode("utf-8", "replace").strip()
        finally:
            try:
                os.remove(path)
            except OSError:
                pass


class AnthropicProvider:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._client = None  # lazy: only construct if we actually escalate

    def _get_client(self):
        if self._client is None:
            import anthropic  # imported lazily so local-only runs need no SDK
            self._client = anthropic.Anthropic()  # key from ANTHROPIC_API_KEY
        return self._client

    async def generate(self, tier: ModelTier, prompt: str) -> str:
        import anyio  # bundled with anthropic/httpx; keeps the SDK call off the loop

        def _call() -> str:
            resp = self._get_client().messages.create(
                model=tier.model,
                max_tokens=2048,
                # Adaptive thinking is the recommended default for the hard tasks
                # that reach this tier. On claude-opus-4-8 you must opt in explicitly.
                thinking={"type": "adaptive"},
                messages=[{"role": "user", "content": prompt}],
            )
            return "".join(b.text for b in resp.content if b.type == "text").strip()

        return await anyio.to_thread.run_sync(_call)
