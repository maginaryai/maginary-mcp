"""/.well-known/mcp/server-card.json — what URL-scanning directories read.

The card must mirror tools/list exactly (it is generated from it), name the
canonical resource URL, and report the auth gate truthfully.
"""
import json

import pytest
from starlette.testclient import TestClient

from maginary_mcp import http_app

TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
JSON = {"Content-Type": "application/json", "Accept": "application/json"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("MAGINARY_MCP_REQUIRE_AUTH", raising=False)
    monkeypatch.setenv("MAGINARY_MCP_RESOURCE_URL", "https://mcp.maginary.ai/mcp")
    return TestClient(http_app.build_app())


def test_card_is_served_and_open(client):
    resp = client.get("/.well-known/mcp/server-card.json")
    assert resp.status_code == 200
    card = resp.json()
    assert card["name"] == "maginary"
    assert card["url"] == "https://mcp.maginary.ai/mcp"
    assert card["transport"] == "streamable-http"
    assert card["authentication"]["required"] is False
    assert card["authentication"]["resource_metadata"].endswith("/.well-known/oauth-protected-resource")
    assert card["version"] and card["version"] == card["serverInfo"]["version"]


def test_card_tools_match_tools_list(client):
    card = client.get("/.well-known/mcp/server-card.json").json()
    listed = client.post("/mcp", json=TOOLS_LIST, headers=JSON).json()["result"]["tools"]
    assert [t["name"] for t in card["tools"]] == [t["name"] for t in listed]
    assert all(t["description"] and t["inputSchema"] for t in card["tools"])


def test_card_reports_gate_when_on(monkeypatch):
    monkeypatch.setenv("MAGINARY_MCP_REQUIRE_AUTH", "1")
    card = TestClient(http_app.build_app()).get("/.well-known/mcp/server-card.json").json()
    assert card["authentication"]["required"] is True
    assert "oauth2" in card["authentication"]["schemes"]


def test_card_sits_before_mcp_transport():
    paths = [getattr(r, "path", None) for r in http_app.build_app().routes]
    assert paths.index("/.well-known/mcp/server-card.json") < paths.index("/mcp")
    assert paths[0] == "/health"


def test_card_is_json_serialisable_and_cached(client):
    resp = client.get("/.well-known/mcp/server-card.json")
    json.dumps(resp.json())  # no non-serialisable leftovers from the SDK types
    assert "max-age" in resp.headers.get("cache-control", "")
