"""The hosted MCP as an OAuth 2.1 resource server (Phase 2c).

With MAGINARY_MCP_REQUIRE_AUTH on: no bearer -> 401 + WWW-Authenticate naming
the protected-resource metadata; invalid bearer -> 401 invalid_token; valid
bearer (API key or OAuth token, validated by USE against the backend, cached)
-> the call proceeds. Metadata documents, /health and the human page stay open.
Off: nothing changes (the rest of the suite runs that way).
"""
import httpx
import pytest
from starlette.testclient import TestClient

from maginary_mcp import http_app

TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
JSON = {"Content-Type": "application/json", "Accept": "application/json"}


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setenv("MAGINARY_MCP_REQUIRE_AUTH", "1")
    monkeypatch.setenv("MAGINARY_MCP_RESOURCE_URL", "https://mcp.maginary.ai/mcp")
    monkeypatch.setenv("MAGINARY_OAUTH_ISSUER", "https://app.maginary.ai/o")
    monkeypatch.setenv("MAGINARY_BASE_URL", "http://backend:8000/api")
    http_app._credential_cache.clear()
    yield
    http_app._credential_cache.clear()


# `client` comes from tests/conftest.py (session-wide hosted-app client).


@pytest.fixture
def backend(monkeypatch):
    """Fake the backend's account-status answer; count the calls."""
    state = {"status": 200, "calls": 0}

    def fake_get(self, url, **kw):
        state["calls"] += 1
        assert url.endswith("/auth/account-status/")
        state["headers"] = kw.get("headers") or {}
        state["auth"] = state["headers"].get("Authorization")
        return httpx.Response(state["status"], json={}, request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx.Client, "get", fake_get)
    return state


def test_no_bearer_is_401_with_resource_metadata_pointer(gated, client):
    resp = client.post("/mcp", json=TOOLS_LIST, headers=JSON)
    assert resp.status_code == 401
    assert "/api/gens/" in resp.json()["error_description"]  # keyless x402 agents are told the way in
    www = resp.headers["www-authenticate"]
    assert www.startswith("Bearer ")
    assert 'resource_metadata="https://mcp.maginary.ai/.well-known/oauth-protected-resource"' in www
    assert resp.json()["resource_metadata"].endswith("/.well-known/oauth-protected-resource")


def test_root_alias_is_gated_too_but_human_page_is_not(gated, client):
    assert client.post("/", json=TOOLS_LIST, headers=JSON).status_code == 401
    assert client.get("/").status_code == 200
    assert client.get("/health").status_code == 200


def test_protected_resource_metadata_document(gated, client):
    for path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"):
        resp = client.get(path)
        assert resp.status_code == 200
        d = resp.json()
        assert d["resource"] == "https://mcp.maginary.ai/mcp"
        assert d["authorization_servers"] == ["https://app.maginary.ai/o"]
        assert d["bearer_methods_supported"] == ["header"]
        assert d["scopes_supported"] == ["generate", "read"]  # what the tools need, not every AS scope


def test_valid_bearer_passes_and_is_cached(gated, client, backend):
    h = {**JSON, "Authorization": "Bearer tok-1"}
    r1 = client.post("/mcp", json=TOOLS_LIST, headers=h)
    assert r1.status_code == 200 and "tools" in r1.json()["result"]
    assert backend["auth"] == "Bearer tok-1"
    assert backend["headers"]["X-Forwarded-Host"] == "app.maginary.ai"  # probe counts against the agent's IP, not ours
    assert "X-Forwarded-For" in backend["headers"]
    client.post("/mcp", json=TOOLS_LIST, headers=h)
    assert backend["calls"] == 1  # second call served from the 60s cache


def test_invalid_bearer_is_401_invalid_token(gated, client, backend):
    backend["status"] = 401
    resp = client.post("/mcp", json=TOOLS_LIST, headers={**JSON, "Authorization": "Bearer bad"})
    assert resp.status_code == 401
    assert 'error="invalid_token"' in resp.headers["www-authenticate"]
    assert "resource_metadata=" in resp.headers["www-authenticate"]


def test_recognised_but_forbidden_credential_passes_the_gate(gated, client, backend):
    """403 from account-status = the backend knows this credential (unverified
    email, token without `read`). Not a login problem: let the tool call
    report the real reason instead of bouncing the client back to OAuth."""
    backend["status"] = 403
    resp = client.post("/mcp", json=TOOLS_LIST, headers={**JSON, "Authorization": "Bearer tok-3"})
    assert resp.status_code == 200


def test_credential_probe_runs_off_the_event_loop(gated, client, backend, monkeypatch):
    """The probe is a blocking httpx call; it must go through a worker thread."""
    import anyio.to_thread
    seen = {}
    real = anyio.to_thread.run_sync

    async def spy(fn, *args, **kw):
        seen["fn"] = getattr(fn, "__name__", None)
        return await real(fn, *args, **kw)
    monkeypatch.setattr(anyio.to_thread, "run_sync", spy)
    client.post("/mcp", json=TOOLS_LIST, headers={**JSON, "Authorization": "Bearer tok-thread"})
    assert seen.get("fn") == "_credential_ok"


def test_backend_unreachable_fails_open(gated, client, monkeypatch):
    def boom(self, url, **kw):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(httpx.Client, "get", boom)
    resp = client.post("/mcp", json=TOOLS_LIST, headers={**JSON, "Authorization": "Bearer tok-2"})
    assert resp.status_code == 200  # tool-level errors surface later instead of a lockout


def test_gate_off_leaves_anonymous_access_alone(monkeypatch, client):
    monkeypatch.delenv("MAGINARY_MCP_REQUIRE_AUTH", raising=False)
    assert client.post("/mcp", json=TOOLS_LIST, headers=JSON).status_code == 200
