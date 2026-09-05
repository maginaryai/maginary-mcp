"""Hosted mode forwards the original request's scheme + client IP to the backend.

Regression for prod: once ``backend`` was an allowed host, Django's
``SECURE_SSL_REDIRECT`` answered the MCP's internal ``http://backend:8000``
call with a 301 to ``https://backend:8000`` (no TLS on that port ->
``[SSL: WRONG_VERSION_NUMBER]``). Traefik avoids it by sending
``X-Forwarded-Proto: https``; the MCP is a proxy too and must do the same.
``X-Forwarded-For`` additionally keeps the backend's per-IP rate limiting
per tenant instead of per container.
"""
import asyncio

import pytest

from maginary_mcp import api
from maginary_mcp.http_app import ApiKeyMiddleware, _forwarded_from_scope


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("MAGINARY_API_KEY", raising=False)
    monkeypatch.setenv("MAGINARY_BASE_URL", "http://backend:8000/api")


def test_scope_to_forwarded_headers(monkeypatch):
    monkeypatch.setenv("MAGINARY_PUBLIC_HOST", "app.example.test")
    scope = {"type": "http", "scheme": "https", "client": ("203.0.113.9", 51234), "headers": []}
    assert _forwarded_from_scope(scope) == {
        "X-Forwarded-Proto": "https",
        "X-Forwarded-Host": "app.example.test",
        "X-Forwarded-For": "203.0.113.9",
    }


def test_public_host_defaults_to_app_domain(monkeypatch):
    monkeypatch.delenv("MAGINARY_PUBLIC_HOST", raising=False)
    assert _forwarded_from_scope({"type": "http", "scheme": "https", "headers": []})["X-Forwarded-Host"] \
        == "app.maginary.ai"


def test_forwarded_host_is_never_our_own_host(monkeypatch):
    """The backend must see ITS public host, not mcp.maginary.ai."""
    monkeypatch.delenv("MAGINARY_PUBLIC_HOST", raising=False)
    scope = {"type": "http", "scheme": "https", "headers": [(b"host", b"mcp.maginary.ai")]}
    assert _forwarded_from_scope(scope)["X-Forwarded-Host"] != "mcp.maginary.ai"


def test_scope_without_client_still_sends_proto_and_host(monkeypatch):
    monkeypatch.delenv("MAGINARY_PUBLIC_HOST", raising=False)
    fwd = _forwarded_from_scope({"type": "http", "scheme": "http", "headers": []})
    assert fwd["X-Forwarded-Proto"] == "http"
    assert "X-Forwarded-Host" in fwd
    assert "X-Forwarded-For" not in fwd


def test_backend_headers_carry_forwarded_in_hosted_mode():
    with api.request_api_key("k", forwarded={"X-Forwarded-Proto": "https", "X-Forwarded-For": "1.2.3.4"}):
        headers = api._headers()
    assert headers["Authorization"] == "Bearer k"
    assert headers["X-Forwarded-Proto"] == "https"
    assert headers["X-Forwarded-For"] == "1.2.3.4"


def test_stdio_mode_sends_no_forwarded_headers(monkeypatch):
    monkeypatch.setenv("MAGINARY_API_KEY", "env-key")
    headers = api._headers()
    assert "X-Forwarded-Proto" not in headers
    assert "X-Forwarded-For" not in headers


def test_forwarded_resets_after_request():
    with api.request_api_key("k", forwarded={"X-Forwarded-Proto": "https"}):
        pass
    assert api._request_forwarded.get() is None


def test_middleware_binds_forwarded_for_the_inner_app():
    seen = {}

    async def inner(scope, receive, send):
        seen.update(api._headers())

    mw = ApiKeyMiddleware(inner)
    scope = {
        "type": "http", "scheme": "https", "client": ("198.51.100.7", 4000),
        "headers": [(b"authorization", b"Bearer abc")],
    }
    asyncio.run(mw(scope, None, None))
    assert seen["Authorization"] == "Bearer abc"
    assert seen["X-Forwarded-Proto"] == "https"
    assert seen["X-Forwarded-For"] == "198.51.100.7"
