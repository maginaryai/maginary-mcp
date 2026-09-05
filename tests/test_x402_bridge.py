"""x402 over MCP (plan: Phase 2b-mcp).

The x402 SDK's MCP client (`x402/mcp/client.py`) detects a payable result by
``isError`` + a top-level ``accepts`` in the result JSON, signs, and re-calls
the SAME tool with the payment in ``_meta["x402/payment"]``; the receipt is
read from ``_meta["x402/payment-response"]``. The MCP holds no payment logic:
it forwards the payment as PAYMENT-SIGNATURE and echoes the backend's
PAYMENT-RESPONSE. ``_meta["maginary/api_key"]`` is our carrier for a key
obtained mid-session (keyless x402 returns one on the first settlement).
"""
import asyncio
import base64
import json
import types

import httpx
import pytest
from mcp.types import CallToolResult

from maginary_mcp import api, server
from maginary_mcp.server import generate

CHALLENGE = {
    "x402Version": 2,
    "error": "No account, no credits. Agents: pay $0.07 USDC on Base (see accepts).",
    "accepts": [{"scheme": "exact", "network": "eip155:8453", "amount": "70000",
                 "asset": "0x8335", "payTo": "0x945f", "maxTimeoutSeconds": 60,
                 "extra": {"name": "USD Coin", "version": "2"}}],
    "resource": {"url": "https://app.maginary.ai/api/gens/", "description": "d", "mimeType": "application/json"},
    "extensions": {"bazaar": {"info": {}, "schema": {}}},
    "billing_url": "https://app.maginary.ai/dashboard",
}
PAYMENT = {"x402Version": 2, "scheme": "exact", "network": "eip155:8453",
           "payload": {"signature": "0xsig", "authorization": {"from": "0xd802", "value": "70000", "nonce": "n1"}}}
RECEIPT = {"success": True, "transaction": "0xtx1", "network": "eip155:8453", "payer": "0xd802"}
GEN = {"uuid": "c04dccce0517c46d68b46d2f0a1a5670", "processing_state": "queued",
       "x402_account": {"api_key": "k-new", "wallet": "0xd802"}}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("MAGINARY_BASE_URL", "http://backend:8000/api")
    monkeypatch.delenv("MAGINARY_API_KEY", raising=False)


@pytest.fixture
def posts(monkeypatch):
    """Capture every backend POST; the response is chosen by a per-test hook."""
    calls: list[dict] = []
    state = {"respond": lambda: httpx.Response(402, json=CHALLENGE)}

    def fake_post(self, url, **kw):
        calls.append({"url": url, "headers": dict(kw.get("headers") or {}), "json": kw.get("json")})
        resp = state["respond"]()
        resp.request = httpx.Request("POST", url)
        return resp
    monkeypatch.setattr(httpx.Client, "post", fake_post)
    calls_obj = types.SimpleNamespace(calls=calls, respond=lambda fn: state.__setitem__("respond", fn))
    return calls_obj


def _ctx(meta: dict | None):
    """A FastMCP Context stand-in exposing request_context.meta.model_extra."""
    m = types.SimpleNamespace(model_extra=meta) if meta is not None else None
    return types.SimpleNamespace(request_context=types.SimpleNamespace(meta=m))


def _settled():
    return httpx.Response(201, json=GEN,
                          headers={"PAYMENT-RESPONSE": base64.b64encode(json.dumps(RECEIPT).encode()).decode()})


# ── shape: detectable by the SDK client ─────────────────────────────────────

class TestPaymentRequiredShape:
    def test_x402_fields_are_top_level_and_contract_fields_survive(self, posts):
        with api.request_api_key("k"):
            result = generate("a fox")
        assert isinstance(result, CallToolResult) and result.isError
        body = result.structuredContent
        assert body["error"] == "payment_required"          # our discriminator wins
        assert body["accepts"] == CHALLENGE["accepts"]        # what the SDK client keys on
        assert body["resource"]["url"].endswith("/api/gens/")
        assert body["x402Version"] == 2
        assert "No account" in body["message"]                # the x402 human text moved here
        assert body["challenge"] == CHALLENGE                 # deprecated duplicate, one release
        text = json.loads(result.content[0].text)
        assert "accepts" in text                              # the text form the SDK parses

    def test_sdk_client_detector_accepts_our_result(self, posts):
        client_mod = pytest.importorskip("x402.mcp.client")
        with api.request_api_key("k"):
            result = generate("a fox")
        found = client_mod._try_extract_payment_json(result.content[0].text)
        assert found is not None and found["accepts"] == CHALLENGE["accepts"]

    def test_through_fastmcp_handler(self, posts):
        """The real boundary: FastMCP injects ctx and passes our CallToolResult through."""
        from tests.test_tool_layer import _call_raw
        with api.request_api_key("k"):
            result = asyncio.run(_call_raw("generate", {"prompt": "a fox"}))
        assert result.isError
        assert result.structuredContent["accepts"] == CHALLENGE["accepts"]


# ── retry with payment, receipt back ────────────────────────────────────────

class TestPaymentRetry:
    def test_meta_payment_is_forwarded_as_payment_signature(self, posts):
        posts.respond(_settled)
        with api.request_api_key("k"):
            result = generate("a fox", ctx=_ctx({server.MCP_PAYMENT_META_KEY: PAYMENT}))
        sent = posts.calls[-1]["headers"]
        assert sent["PAYMENT-SIGNATURE"] == base64.b64encode(json.dumps(PAYMENT).encode()).decode()
        assert isinstance(result, CallToolResult) and not result.isError
        assert result.meta[server.MCP_PAYMENT_RESPONSE_META_KEY] == RECEIPT
        assert result.structuredContent["x402_receipt"] == RECEIPT
        assert result.structuredContent["uuid"] == GEN["uuid"]
        assert result.structuredContent["x402_account"]["api_key"] == "k-new"

    def test_no_meta_means_no_payment_header_and_plain_record(self, posts):
        posts.respond(lambda: httpx.Response(201, json={"uuid": "u", "processing_state": "queued"}))
        with api.request_api_key("k"):
            result = generate("a fox", ctx=_ctx({}))
        assert "PAYMENT-SIGNATURE" not in posts.calls[-1]["headers"]
        assert isinstance(result, dict) and result["uuid"] == "u"

    def test_non_dict_meta_payment_is_ignored(self, posts):
        posts.respond(lambda: httpx.Response(201, json={"uuid": "u", "processing_state": "queued"}))
        with api.request_api_key("k"):
            generate("a fox", ctx=_ctx({server.MCP_PAYMENT_META_KEY: "garbage"}))
        assert "PAYMENT-SIGNATURE" not in posts.calls[-1]["headers"]

    def test_receipt_without_meta_when_backend_sends_none(self, posts):
        posts.respond(lambda: httpx.Response(201, json={"uuid": "u", "processing_state": "queued"}))
        with api.request_api_key("k"):
            result = generate("a fox", ctx=_ctx({server.MCP_PAYMENT_META_KEY: PAYMENT}))
        assert isinstance(result, dict)
        assert "x402_receipt" not in result


# ── identity continuity: the _meta api-key carrier ──────────────────────────

class TestMetaApiKeyCarrier:
    def test_used_in_hosted_mode_when_request_bound_no_key(self, posts):
        posts.respond(lambda: httpx.Response(201, json={"uuid": "u"}))
        with api.request_api_key(None):  # hosted, anonymous connection
            generate("a fox", ctx=_ctx({server.MCP_API_KEY_META_KEY: "k-meta"}))
        assert posts.calls[-1]["headers"]["Authorization"] == "Bearer k-meta"

    def test_header_key_wins_over_meta(self, posts):
        posts.respond(lambda: httpx.Response(201, json={"uuid": "u"}))
        with api.request_api_key("k-header"):
            generate("a fox", ctx=_ctx({server.MCP_API_KEY_META_KEY: "k-meta"}))
        assert posts.calls[-1]["headers"]["Authorization"] == "Bearer k-header"

    def test_stdio_mode_ignores_meta_key(self, posts, monkeypatch):
        monkeypatch.setenv("MAGINARY_API_KEY", "env-key")
        posts.respond(lambda: httpx.Response(201, json={"uuid": "u"}))
        generate("a fox", ctx=_ctx({server.MCP_API_KEY_META_KEY: "k-meta"}))  # no middleware binding
        assert posts.calls[-1]["headers"]["Authorization"] == "Bearer env-key"

    def test_meta_key_does_not_leak_past_the_call(self, posts):
        posts.respond(lambda: httpx.Response(201, json={"uuid": "u"}))
        with api.request_api_key(None):
            generate("a fox", ctx=_ctx({server.MCP_API_KEY_META_KEY: "k-meta"}))
            assert api._request_api_key.get() is None


# ── the input schema must not grow a `ctx` argument ─────────────────────────

def test_ctx_is_hidden_from_the_tool_schema():
    from maginary_mcp.server import mcp
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    assert set(tools["generate"].inputSchema["properties"]) == {"prompt", "callback_url"}
    assert set(tools["get_generation"].inputSchema["properties"]) == {"uuid"}
    assert set(tools["wait_for_generation"].inputSchema["properties"]) == {"uuid", "timeout_s"}
