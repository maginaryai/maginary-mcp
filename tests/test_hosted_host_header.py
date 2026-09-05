"""Hosted transport must accept the public hostname on /mcp.

Regression for the first production deploy: FastMCP's default DNS-rebinding
protection allow-lists only localhost, so every request that reached the
container through traefik with ``Host: mcp.maginary.ai`` got ``421 Invalid
Host header`` while ``/health`` (mounted outside the MCP transport) stayed
green — the deploy looked healthy and the MCP was unusable.
"""
import pytest
from starlette.testclient import TestClient

TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
JSON_HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}


# `client` comes from tests/conftest.py: one session-wide hosted-app client,
# because the streamable-HTTP session manager starts once per FastMCP instance.


@pytest.mark.parametrize("host", ["mcp.maginary.ai", "testserver", "some-other.example:8642"])
def test_mcp_accepts_public_host_header(client, host):
    resp = client.post("/mcp", json=TOOLS_LIST, headers={**JSON_HEADERS, "Host": host})
    assert resp.status_code == 200, resp.text
    tools = resp.json()["result"]["tools"]
    assert any(t["name"] == "generate" for t in tools)


def test_mcp_is_served_at_root_as_alias(client):
    """`https://mcp.maginary.ai` bare and `/mcp` must be the same endpoint."""
    root = client.post("/", json=TOOLS_LIST, headers={**JSON_HEADERS, "Host": "mcp.maginary.ai"})
    at_mcp = client.post("/mcp", json=TOOLS_LIST, headers={**JSON_HEADERS, "Host": "mcp.maginary.ai"})
    assert root.status_code == 200, root.text
    assert [t["name"] for t in root.json()["result"]["tools"]] == \
           [t["name"] for t in at_mcp.json()["result"]["tools"]]


def test_root_get_is_a_human_page(client):
    """Same path, split by method: GET / is HTML for people, POST / is the protocol."""
    resp = client.get("/", headers={"Host": "mcp.maginary.ai"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text
    assert "noindex" in body                          # not an SEO surface
    assert 'rel="canonical" href="https://maginary.ai/mcp"' in body
    assert "http://mcp.maginary.ai/mcp" in body       # endpoint derived from the request host
    assert "<code>generate</code>" in body            # tools rendered live from the server


def test_canonical_mcp_path_serves_no_html(client):
    resp = client.get("/mcp", headers={"Host": "mcp.maginary.ai"})
    assert not resp.headers.get("content-type", "").startswith("text/html")


def test_mcp_accepts_browser_origin(client):
    """Browser-based MCP clients (claude.ai, Cursor web) send an Origin we don't control."""
    resp = client.post("/mcp", json=TOOLS_LIST,
                       headers={**JSON_HEADERS, "Host": "mcp.maginary.ai", "Origin": "https://claude.ai"})
    assert resp.status_code == 200, resp.text
