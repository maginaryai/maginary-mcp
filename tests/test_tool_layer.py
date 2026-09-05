"""Contracts at the FastMCP boundary — ``mcp.call_tool()``, not raw functions.

Every other test file calls the tool functions directly, which bypasses
FastMCP's output validation. That's exactly where get_products was broken for
its entire life (backend sends a bare array; a dict-annotated tool returning a
list fails pydantic validation on every successful call) while the raw-function
suite stayed green. These tests keep that layer covered.
"""
import asyncio

import httpx
import pytest

from maginary_mcp.server import mcp


def call_tool(name: str, args: dict | None = None):
    return asyncio.run(mcp.call_tool(name, args or {}))


@pytest.fixture(autouse=True)
def _no_ambient_key(monkeypatch):
    monkeypatch.delenv("MAGINARY_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def _offline_catalog(monkeypatch):
    # get_catalog()'s cold path does a live HTTPS fetch (5s timeout) — a unit
    # test must not depend on the docs site. Force the bundled snapshot.
    from maginary_mcp import params

    monkeypatch.setattr(params, "_CACHE", None)
    snapshot = params._load_bundled_snapshot()
    snapshot["_source"] = "bundled-snapshot"
    monkeypatch.setattr(params, "get_catalog", lambda: snapshot)
    monkeypatch.setattr("maginary_mcp.server.get_catalog", lambda: snapshot)


class TestToolLayer:

    def test_get_products_survives_output_validation(self, monkeypatch):
        def fake_get(self, url, **kw):
            return httpx.Response(200, json=[{"id": 1, "short_name": "novice_pack"}],
                                  request=httpx.Request("GET", url))

        monkeypatch.setattr(httpx.Client, "get", fake_get)
        content, structured = call_tool("get_products")
        assert structured["count"] == 1
        assert structured["products"][0]["short_name"] == "novice_pack"

    def test_get_parameter_not_found_is_error_through_protocol(self):
        result = asyncio.run(_call_raw("get_parameter", {"name": "--no-such-flag"}))
        assert result.isError is True
        assert result.structuredContent["error"] == "not_found"

    def test_generate_auth_error_through_protocol(self, monkeypatch):
        monkeypatch.setattr("maginary_mcp.api._read_config_key", lambda: None)
        result = asyncio.run(_call_raw("generate", {"prompt": "a fox"}))
        assert result.isError is True
        assert result.structuredContent["error"] == "auth"

    def test_catalog_success_keeps_structured_content(self):
        content, structured = call_tool("search_parameters", {"query": "aspect"})
        assert structured["count"] >= 1
        assert structured["source"] in ("live", "bundled-snapshot")


async def _call_raw(name: str, args: dict):
    """Call through the low-level server handler to see the real CallToolResult
    (FastMCP.call_tool unwraps to (content, structured) and raises on isError
    for exceptions, but passes returned CallToolResults through the handler)."""
    from mcp.types import CallToolRequest, CallToolRequestParams

    handler = mcp._mcp_server.request_handlers[CallToolRequest]
    req = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name=name, arguments=args),
    )
    server_result = await handler(req)
    return server_result.root
