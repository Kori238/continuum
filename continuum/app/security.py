"""Access control — what changes when Continuum stops being a localhost toy.

On loopback, "no auth" was defensible: reaching the socket already meant you were
on the machine. Serving it over the tailnet breaks that assumption, and Continuum
is not a read-only dashboard — it can edit the routine, read messages, and (with
dev tools on) rewrite its own source. So the door needs a lock before it moves.

Three ideas, in order of how much they're load-bearing:

1. **The socket never leaves loopback.** uvicorn still binds 127.0.0.1. Tailscale
   is what carries it, via `tailscale serve` terminating TLS on the tailnet and
   proxying in. There is no port open on a real interface to find.

2. **Identity, not a password.** `tailscale serve` stamps each proxied request
   with `Tailscale-User-Login` — the tailnet's own authenticated identity, which
   the client cannot set (tailscaled overwrites any client-supplied value). We
   allow-list logins and fail *closed*: an unrecognised identity is refused, and
   if we can't work out who owns this tailnet, nobody gets in from outside.

3. **The tool plane has its own key.** `/mcp` executes tools; it isn't a UI. It
   takes a per-process bearer token generated at startup, handed only to the
   model subprocess, never written to disk. Even a tailnet device that gets past
   (1) and (2) can't drive tools without it — and it still has to be someone on
   the allow-list, because a token is a thing that can leak.

4. **The browser is not a trusted client.** (1) says reaching the socket means
   you're on the machine. That is false for a *web page*: any site you visit can
   open `ws://127.0.0.1:8848/ws` or POST to a route, from your browser, on
   loopback, and the rules above would call it local and wave it through. The
   Same-Origin Policy does not stop this — websockets are exempt from it and
   from CORS preflight. What the browser does guarantee is the `Origin` header,
   which a page cannot forge. So: an `Origin` we don't recognise is refused, and
   no `Origin` at all means a non-browser client (the phone app, the CLI, curl)
   and is left alone.

Local requests (the native window, curl on this box) carry no identity header and
are allowed — a process already running as you can do anything you could do
anyway, so pretending otherwise would be theatre rather than security. A web page
running *inside* that browser is a different matter, and that's what (4) is for.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import secrets
import shutil
import subprocess
from urllib.parse import urlsplit

log = logging.getLogger("continuum.security")

# Per-process, never persisted: restarting Continuum invalidates every old token.
MCP_TOKEN = secrets.token_urlsafe(32)

_IDENTITY_HEADER = b"tailscale-user-login"
_LOOPBACK = (ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128"))

_STATUS_CACHE: dict | None = None


def _tailscale_exe() -> str | None:
    return (shutil.which("tailscale")
            or next((p for p in (r"C:\Program Files\Tailscale\tailscale.exe",
                                 "/usr/bin/tailscale", "/usr/local/bin/tailscale")
                     if os.path.exists(p)), None))


def _tailscale_status() -> dict:
    """`tailscale status --json`, read at most once per process.

    This shells out, and cold it takes a third of a second — longer if tailscaled
    is still waking up after a resume. It used to run inside the first request
    (Starlette builds middleware lazily), which froze the whole server on the
    `GET /` that loads the dashboard. Now it's read once, eagerly, while the app
    is still being constructed on the main thread — see `create_app`.
    """
    global _STATUS_CACHE
    if _STATUS_CACHE is not None:
        return _STATUS_CACHE
    _STATUS_CACHE = {}
    exe = _tailscale_exe()
    if not exe:
        return _STATUS_CACHE
    try:
        r = subprocess.run([exe, "status", "--json"], capture_output=True, text=True,
                           timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        _STATUS_CACHE = json.loads(r.stdout or "{}") or {}
    except Exception:
        log.debug("could not read tailscale status", exc_info=True)
    return _STATUS_CACHE


def tailnet_owner() -> str | None:
    """The login that owns this node — the sane default allow-list of one."""
    data = _tailscale_status()
    uid = str((data.get("Self") or {}).get("UserID", ""))
    user = (data.get("User") or {}).get(uid) or {}
    return (user.get("LoginName") or "").strip().lower() or None


def allowed_origin_hosts() -> set[str]:
    """Hostnames a browser may legitimately be pointed at to reach us.

    Always the loopback names (the native window and a local tab), plus this
    node's tailnet DNS name so the phone keeps working — `tailscale serve`
    terminates TLS under that name, and the browser stamps it into `Origin`
    whether or not the proxy preserves the original `Host`. `CONTINUUM_ALLOWED_ORIGINS`
    is the escape hatch for anything else (a custom proxy, a second hostname).
    """
    hosts = {"127.0.0.1", "localhost", "::1", "[::1]"}
    dns = str((_tailscale_status().get("Self") or {}).get("DNSName", "")).strip().lower()
    if dns:
        hosts.add(dns.rstrip("."))
    for extra in os.getenv("CONTINUUM_ALLOWED_ORIGINS", "").split(","):
        cleaned = extra.strip().lower()
        if cleaned:
            # Accept a bare host or a full URL, so either form works in the env var.
            hosts.add(urlsplit(cleaned).hostname or cleaned)
    return hosts


def allowed_logins() -> set[str]:
    """Who may reach Continuum over the tailnet. Explicit config wins; otherwise
    just this node's owner. Empty means nobody — fail closed, loudly."""
    env = os.getenv("CONTINUUM_ALLOWED_LOGINS", "").strip()
    if env:
        return {s.strip().lower() for s in env.split(",") if s.strip()}
    owner = tailnet_owner()
    if owner:
        return {owner}
    log.warning("no tailnet owner detected and CONTINUUM_ALLOWED_LOGINS unset — "
                "refusing all non-local requests")
    return set()


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(ip in net for net in _LOOPBACK)


def _origin_ok(origin: str, host_header: str, allowed_hosts: set[str]) -> bool:
    """Is this `Origin` one of ours?

    Absent means not a browser — curl, the phone's websocket client, the Claude
    CLI on /mcp — and those are governed by the identity rules instead. Present
    means a page, and a page only gets in if it is the page we served: same
    host:port as this request, or one of the names we answer to. `Origin: null`
    (a sandboxed iframe, a file:// document) parses to no netloc and is refused,
    because "null" is not a name we serve.
    """
    if not origin:
        return True
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    if (parsed.netloc or "").lower() == host_header:
        return True
    return (parsed.hostname or "").lower() in allowed_hosts


class Guard:
    """Raw ASGI middleware — Starlette's BaseHTTPMiddleware skips websockets, and
    the websocket is where every task and dev command is submitted."""

    def __init__(self, app, allowed: set[str] | None = None,
                 origins: set[str] | None = None) -> None:
        self.app = app
        self.allowed = allowed if allowed is not None else allowed_logins()
        self.origins = origins if origins is not None else allowed_origin_hosts()
        log.info("guard: tailnet logins allowed = %s",
                 ", ".join(sorted(self.allowed)) or "(none — local only)")
        log.info("guard: browser origins allowed = %s", ", ".join(sorted(self.origins)))

    def _verdict(self, scope) -> str | None:
        """None to allow, else a short reason to refuse."""
        headers = dict(scope.get("headers") or [])
        path = scope.get("path", "")

        # Before anything else: is a *web page* doing this? Every rule below is
        # about which machine or which person is calling, and a malicious page
        # borrows both from you. It runs in your browser, so it comes from your
        # machine; it rides your session, so it looks like you. Only `Origin`
        # distinguishes it, and only the browser can set that.
        origin = headers.get(b"origin", b"").decode("latin-1").strip()
        if not _origin_ok(origin, headers.get(b"host", b"").decode("latin-1").strip().lower(),
                          self.origins):
            return f"cross-origin request from {origin}"

        if path == "/mcp" or path.startswith("/mcp/"):
            auth = headers.get(b"authorization", b"").decode("latin-1")
            expected = f"Bearer {MCP_TOKEN}"
            # compare_digest: token check shouldn't leak length/prefix by timing.
            if not (auth and secrets.compare_digest(auth, expected)):
                return "mcp token required"
            # A correct token is necessary, not sufficient: fall through to the
            # identity rules. The token is held by a subprocess and can leak the
            # way any secret leaks, and /mcp is the one path that runs tools.

        login = headers.get(_IDENTITY_HEADER, b"").decode("latin-1").strip().lower()
        if login:
            return None if login in self.allowed else f"identity not allowed: {login}"

        # Proxied but anonymous. `tailscale serve` always stamps an identity, so
        # this means either a different proxy or a Tailscale that didn't — either
        # way we can't say who it is, and the loopback rule below would wave
        # through every tailnet device. Refuse instead of guessing.
        if b"x-forwarded-for" in headers:
            return "proxied request without tailnet identity"

        # No identity, not proxied → a local client on the loopback socket.
        client = (scope.get("client") or (None, None))[0]
        return None if _is_loopback(client) else "no tailnet identity"

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        reason = self._verdict(scope)
        if reason is None:
            await self.app(scope, receive, send)
            return

        log.warning("refused %s %s: %s", scope["type"], scope.get("path"), reason)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = json.dumps({"error": "forbidden", "reason": reason}).encode()
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
