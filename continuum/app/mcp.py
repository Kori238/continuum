"""MCP endpoint — how the answering model reaches Continuum's own tools.

That model is the Claude Code CLI: a text-in/text-out subprocess with every file
and system tool denied — deliberately, so it can't read the machine behind your
back. The sandbox also left it unable to touch the routine: it could *read*
whatever context Continuum injected, but it had no handle to mark anything done,
and would happily claim it had.

MCP is the seam that fixes this without reopening the sandbox. Continuum serves
its tools over a tiny JSON-RPC endpoint; the CLI is launched with
`--mcp-config` + `--strict-mcp-config` + `--allowedTools mcp__continuum`, so the
*only* capability it gains is Continuum's own toolset — the same schemas, the
same gates, the same audit trail, all from the one `ToolLoop`.

This was one of two doors into that toolset; a local model held the other and
called the tools natively. That model is gone, which makes this file the only
way a tool ever gets called, and `ToolLoop._schemas()` the only gate.

Deliberately hand-rolled (no `mcp` SDK): the server half of streamable-HTTP is a
handful of methods, and running in-process means tools hit the live stores
directly rather than a second copy over the network.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response

log = logging.getLogger("continuum.mcp")

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "continuum"


def _to_mcp_tool(schema: dict) -> dict:
    """OpenAI-style function schema (what ToolLoop speaks) -> MCP tool."""
    fn = schema["function"]
    return {
        "name": fn["name"],
        "description": fn.get("description", ""),
        "inputSchema": fn.get("parameters") or {"type": "object", "properties": {}},
    }


def _result(rid: Any, payload: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": payload}


def _error(rid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


async def _dispatch(msg: dict, tool_loop: Any) -> dict | None:
    """Handle one JSON-RPC message. Returns None for notifications."""
    method = msg.get("method", "")
    rid = msg.get("id")

    if method == "initialize":
        # Echo the client's protocol version when we understand it; MCP clients
        # abort on a version they didn't ask for.
        asked = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL_VERSION
        return _result(rid, {
            "protocolVersion": asked,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": "1"},
        })

    if method.startswith("notifications/"):
        return None  # nothing to acknowledge

    if method == "ping":
        return _result(rid, {})

    if method == "tools/list":
        # _schemas() applies every permission gate (master switch, clipboard,
        # actions, dev, shell), and this is now the ONLY door tools are served
        # through — so that function is the whole security boundary, not one of
        # two copies of it.
        tools = [_to_mcp_tool(s) for s in tool_loop._schemas()]
        return _result(rid, {"tools": tools})

    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name", "")
        args = params.get("arguments") or {}
        known = {s["function"]["name"] for s in tool_loop._schemas()}
        if name not in known:
            return _result(rid, {"isError": True,
                                 "content": [{"type": "text", "text": f"unknown tool: {name}"}]})
        await tool_loop.note_step(name, args)  # stream it live as a 'thinking' step
        try:
            out = await tool_loop._execute(name, args)
        except Exception as exc:
            log.exception("mcp tool %s failed", name)
            return _result(rid, {"isError": True,
                                 "content": [{"type": "text", "text": f"error: {exc}"}]})
        tool_loop.record_mcp_call(name, args)  # so the turn's tool list is honest
        log.info("mcp %s(%s) -> %s", name, args, str(out)[:120])
        return _result(rid, {"content": [{"type": "text", "text": str(out)}]})

    return _error(rid, -32601, f"method not found: {method}")


def install(app: Any, tool_loop: Any) -> None:
    """Mount the MCP endpoint on the FastAPI app."""

    @app.post("/mcp")
    async def mcp_post(request: Request) -> Response:
        try:
            body = json.loads(await request.body() or b"{}")
        except Exception:
            return JSONResponse(_error(None, -32700, "parse error"), status_code=400)

        # A batch is a list; a single call is an object.
        batch = body if isinstance(body, list) else [body]
        replies = [r for r in
                   [await _dispatch(m, tool_loop) for m in batch if isinstance(m, dict)]
                   if r is not None]
        if not replies:
            return Response(status_code=202)  # notifications only
        return JSONResponse(replies if isinstance(body, list) else replies[0])

    @app.get("/mcp")
    async def mcp_get() -> Response:
        # No server-initiated SSE stream; 405 is the spec's "not offered".
        return Response(status_code=405)
