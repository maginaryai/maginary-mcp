"""Hosted, multi-tenant Streamable-HTTP transport for the Maginary MCP server.

stdio mode (``__main__.main``) is single-tenant: one ``MAGINARY_API_KEY`` in
the env. This module serves the *same* tool set over Streamable HTTP so agents
can connect with **no install** (e.g. to ``https://mcp.maginary.ai/mcp``), each
authenticating with **their own** key via ``Authorization: Bearer <key>``.

Design:
- Reuse the ``mcp`` instance from :mod:`maginary_mcp.server` (all tools already
  registered) and build its Streamable-HTTP ASGI app.
- Run **stateless** with **JSON responses** so every request is independent —
  horizontally scalable behind the same reverse proxy as the engine, and the
  caller's key is reliably in-scope for the whole request (no long-lived SSE
  task that would break ContextVar propagation).
- A thin ASGI middleware lifts the Bearer key off each request and binds it via
  :func:`maginary_mcp.api.request_api_key`, so :mod:`maginary_mcp.api` forwards
  the caller's own key to the backend. Read-only catalog tools still work with
  no key.

Run: ``maginary-mcp-http`` (console script) or ``python -m maginary_mcp.http_app``.
Config via env: ``MAGINARY_MCP_HOST`` (default 0.0.0.0), ``MAGINARY_MCP_PORT``
(default 8642), ``MAGINARY_BASE_URL`` (backend REST base).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from .api import request_api_key

LOG = logging.getLogger(__name__)


def _bearer_from_headers(headers: list[tuple[bytes, bytes]]) -> str | None:
    """Extract the token from a raw ASGI header list' ``Authorization: Bearer <token>``.

    Returns None for a missing header, a non-Bearer scheme, or a malformed value
    — callers treat None the same as "no key" (read-only tools still work; the
    API-hitting tools raise AuthError).
    """
    for name, value in headers:
        if name.lower() != b"authorization":
            continue
        parts = value.decode("latin-1").split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip():
            return parts[1].strip()
        return None
    return None


class ApiKeyMiddleware:
    """Pure-ASGI middleware binding each request's Bearer key to the api ContextVar.

    Pure ASGI (not BaseHTTPMiddleware) so the ContextVar set here reliably
    propagates into the tool call, which runs in the same task/context for a
    stateless JSON request.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        key = _bearer_from_headers(scope.get("headers") or [])
        with request_api_key(key, forwarded=_forwarded_from_scope(scope)):
            await self.app(scope, receive, send)


# ── OAuth resource server (Phase 2c) ───────────────────────────────────────
#
# Per the MCP spec the hosted server is an OAuth 2.1 RESOURCE SERVER: it must
# (1) answer 401 with a WWW-Authenticate pointing at its protected-resource
# metadata when a request carries no valid token, (2) publish that metadata
# (RFC 9728) naming the authorization server, and (3) validate tokens before
# any tool runs. The authorization server is the backend (django-oauth-toolkit
# under /o/ on app.maginary.ai); validation is by USE against it — the backend
# is the AS and the API, so asking it is the exact check — cached briefly.

def auth_required() -> bool:
    return os.environ.get("MAGINARY_MCP_REQUIRE_AUTH", "").lower() in ("1", "true", "yes")


def resource_url() -> str:
    return os.environ.get("MAGINARY_MCP_RESOURCE_URL", "https://mcp.maginary.ai/mcp").rstrip("/")


def oauth_issuer() -> str:
    return os.environ.get("MAGINARY_OAUTH_ISSUER", "https://app.maginary.ai/o").rstrip("/")


def protected_resource_metadata() -> dict:
    return {
        "resource": resource_url(),
        "authorization_servers": [oauth_issuer()],
        "bearer_methods_supported": ["header"],
        # What the MCP tools need — not every scope the AS knows. Clients copy
        # this into their authorize request; `account` would over-ask.
        "scopes_supported": ["generate", "read"],
        "resource_name": "Maginary MCP",
        "resource_documentation": "https://maginary.ai/docs",
    }


def _resource_metadata_url() -> str:
    from urllib.parse import urlparse
    p = urlparse(resource_url())
    return f"{p.scheme}://{p.netloc}/.well-known/oauth-protected-resource"


CREDENTIAL_CACHE_SECONDS = 60
_credential_cache: dict[str, tuple[float, bool]] = {}


def _credential_ok(bearer: str, forwarded: dict[str, str] | None = None) -> bool | None:
    """Cached validation-by-use. None = backend unreachable (caller decides).

    ``forwarded`` carries the agent's real IP/host to the backend: its
    account-status endpoint is rate-limited per IP, and without it every
    probe from every client would count against this container's address.
    """
    import hashlib
    import time

    from .api import credential_is_valid

    key = hashlib.sha256(bearer.encode()).hexdigest()
    now = time.monotonic()
    hit = _credential_cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    verdict = credential_is_valid(bearer, forwarded=forwarded)
    if verdict is not None:
        _credential_cache[key] = (now + CREDENTIAL_CACHE_SECONDS, verdict)
        if len(_credential_cache) > 5000:  # bounded; entries are tiny
            _credential_cache.clear()
    return verdict


class AuthGateMiddleware:
    """401 + WWW-Authenticate for the MCP endpoint when auth is required.

    Only the MCP transport paths are gated; /health, the human page and the
    metadata documents stay open (clients must be able to read them to find
    the login). Unreachable backend = fail open (log), so a backend blip
    degrades to the tool-level auth error instead of locking everyone out.
    """

    def __init__(self, app, mcp_path: str = "/mcp"):
        self.app = app
        self.gated = {mcp_path.rstrip("/") or "/", "/"}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not auth_required():
            await self.app(scope, receive, send)
            return
        path = (scope.get("path") or "/").rstrip("/") or "/"
        method = scope.get("method", "GET")
        is_mcp_call = path in self.gated and not (path == "/" and method in ("GET", "HEAD"))
        if not is_mcp_call:
            await self.app(scope, receive, send)
            return
        bearer = _bearer_from_headers(scope.get("headers") or [])
        if bearer is None:
            await _send_401(send, _no_bearer_description())
            return
        # Sync httpx call: keep it off the event loop or one slow probe
        # stalls every other connection on this worker.
        import anyio
        ok = await anyio.to_thread.run_sync(_credential_ok, bearer, _forwarded_from_scope(scope))
        if ok is False:
            await _send_401(send, "The access token is invalid or expired", error="invalid_token")
            return
        await self.app(scope, receive, send)


def _no_bearer_description() -> str:
    """The gate cannot see a wallet, so a keyless x402 agent is told the one
    HTTP call that turns its first payment into a key it can send as Bearer."""
    public_host = os.environ.get("MAGINARY_PUBLIC_HOST", "app.maginary.ai")
    return ("Authentication required: send an API key or OAuth access token as Bearer. "
            f"Wallet-only agents: one x402-paid POST to https://{public_host}/api/gens/ "
            "returns an API key in X-Maginary-Api-Key.")


async def _send_401(send, description: str, error: str | None = None) -> None:
    import json as _json
    params = [f'resource_metadata="{_resource_metadata_url()}"']
    if error:
        params.insert(0, f'error="{error}"')
        params.append(f'error_description="{description}"')
    body = _json.dumps({"error": error or "unauthorized", "error_description": description,
                        "resource_metadata": _resource_metadata_url()}).encode()
    await send({"type": "http.response.start", "status": 401, "headers": [
        (b"content-type", b"application/json"),
        (b"www-authenticate", ("Bearer " + ", ".join(params)).encode()),
    ]})
    await send({"type": "http.response.body", "body": body})


def _forwarded_from_scope(scope) -> dict[str, str]:
    """Present the original request to the backend as a reverse proxy would.

    uvicorn runs with proxy_headers=True, so ``scheme`` and ``client`` here are
    already the values traefik forwarded (https + the agent's real IP), not
    the docker-network hop. ``X-Forwarded-Host`` is the backend's PUBLIC host
    (``MAGINARY_PUBLIC_HOST``), not ours: the backend builds absolute URLs
    from it (Stripe return URLs, email links) and must never emit
    ``backend:8000``.
    """
    forwarded = {
        "X-Forwarded-Proto": scope.get("scheme") or "http",
        "X-Forwarded-Host": os.environ.get("MAGINARY_PUBLIC_HOST", "app.maginary.ai"),
    }
    client = scope.get("client")
    if client and client[0]:
        forwarded["X-Forwarded-For"] = str(client[0])
    return forwarded


def build_app():
    """Build the Streamable-HTTP ASGI app (stateless JSON) with auth middleware."""
    # Imported here (not at module top) so the pure helpers above stay importable
    # without the mcp SDK — keeps unit tests light.
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from .server import mcp

    mcp.settings.stateless_http = True
    mcp.settings.json_response = True
    # FastMCP defaults to DNS-rebinding protection with an allow-list of
    # localhost hosts/origins (its threat model is a server on a developer's
    # machine being hit by a malicious web page). Hosted, we sit behind traefik
    # which routes only Host: mcp.<apex> to us, and browser-based MCP clients
    # arrive with arbitrary Origins that traefik's CORS already admits — so the
    # SDK check would 421 every real request (it did, on first deploy).
    from mcp.server.transport_security import TransportSecuritySettings
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False)
    app = mcp.streamable_http_app()
    # LOAD-BEARING: the ApiKeyMiddleware ContextVar only scopes the tool call
    # because stateless JSON keeps request handling in the middleware's task.
    # SSE/stateful mode would detach the tool call from this context — the key
    # would silently vanish and authed tools would fail (or, worse, fall back).
    # Fail loudly if anything ever flips these.
    if not (mcp.settings.stateless_http and mcp.settings.json_response):
        raise RuntimeError(
            "maginary-mcp http transport requires stateless_http=True and "
            "json_response=True; per-request auth is not safe otherwise")
    app.add_middleware(ApiKeyMiddleware)
    # Outermost: refuses unauthenticated / invalid credentials before the MCP
    # transport when MAGINARY_MCP_REQUIRE_AUTH is on (OAuth resource server).
    app.add_middleware(AuthGateMiddleware, mcp_path=mcp.settings.streamable_http_path)

    async def _health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def _protected_resource_metadata(request: Request) -> JSONResponse:
        # RFC 9728. The client that got our 401 fetches this to learn WHERE to
        # log in; it then reads RFC 8414 metadata at that issuer.
        return JSONResponse(protected_resource_metadata())

    # /health stays route 0 (pinned by a test: it must never sit behind the
    # MCP transport); the metadata documents go right after it.
    app.routes.insert(0, Route("/.well-known/oauth-protected-resource/{rest:path}",
                               _protected_resource_metadata))
    app.routes.insert(0, Route("/.well-known/oauth-protected-resource", _protected_resource_metadata))
    app.routes.insert(0, Route("/health", _health))

    # The root serves two audiences by HTTP method:
    #   GET  /  -> a small human page (people paste the host into a browser)
    #   POST /  -> the MCP endpoint, aliasing /mcp so the bare host works too.
    # `/mcp` (the SDK default every client and directory pattern-matches on)
    # is the canonical, protocol-only URL and is untouched.
    mcp_route = next(r for r in app.routes if getattr(r, "path", None) == mcp.settings.streamable_http_path)
    app.routes.append(Route("/", endpoint=_index_page, methods=["GET", "HEAD"]))
    app.routes.append(Route("/", endpoint=mcp_route.endpoint, methods=["POST", "DELETE"]))
    return app


_INDEX_TEMPLATE = Path(__file__).with_name("index.html")


async def _index_page(request):
    """Human landing for the MCP host. Tools are listed live so it can't drift."""
    from html import escape
    from string import Template
    from starlette.responses import HTMLResponse

    from . import __version__
    from .server import mcp

    endpoint = str(request.url.replace(path=mcp.settings.streamable_http_path, query="", fragment=""))
    tools = await mcp.list_tools()
    def _summary(description: str | None) -> str:
        # First sentence of the docstring, backticks dropped, first letter
        # lowercased to match the page's voice (acronyms mid-sentence stay).
        s = (description or "").split(".")[0].replace("`", "").strip()
        return s[:1].lower() + s[1:]

    tools_html = "".join(
        f"<li><code>{escape(t.name)}</code><span>{escape(_summary(t.description))}</span></li>"
        for t in tools
    )
    html = Template(_INDEX_TEMPLATE.read_text(encoding="utf-8")).safe_substitute(
        endpoint=escape(endpoint),
        tools=tools_html,
        tool_count=str(len(tools)),
        version=escape(__version__),
    )
    return HTMLResponse(html, headers={"Cache-Control": "public, max-age=300"})


def main() -> None:
    """Run the hosted Streamable-HTTP server via uvicorn."""
    import uvicorn

    from . import __version__

    logging.basicConfig(
        level=os.environ.get("MAGINARY_MCP_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    host = os.environ.get("MAGINARY_MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MAGINARY_MCP_PORT", "8642"))
    LOG.info("maginary-mcp %s starting streamable-http on %s:%s/mcp", __version__, host, port)
    # Behind traefik on a private docker network (port never published), so
    # X-Forwarded-For is trustworthy from any peer: log/limit by real client IP.
    uvicorn.run(build_app(), host=host, port=port,
                proxy_headers=True, forwarded_allow_ips='*')


if __name__ == "__main__":
    main()
