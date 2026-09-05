"""MCP generate tool returns structured error on 402, not a raw crash."""
import json

import httpx
import pytest

from maginary_mcp import api


@pytest.fixture(autouse=True)
def _set_key(monkeypatch):
    monkeypatch.setenv("MAGINARY_API_KEY", "test-key")
    monkeypatch.setenv("MAGINARY_BASE_URL", "http://fake.test/api")


class TestPaymentRequiredError:

    def test_api_raises_on_402(self, monkeypatch):
        challenge = {
            "x402Version": 2,
            "error": "Insufficient credits.",
            "accepts": [{"scheme": "exact", "amount": "10000000"}],
            "billing_url": "https://app.maginary.ai/dashboard",
        }

        def fake_post(self, url, **kw):
            return httpx.Response(402, json=challenge, request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx.Client, "post", fake_post)
        with pytest.raises(api.PaymentRequiredError) as exc_info:
            api.create_generation("a fox")
        assert exc_info.value.challenge == challenge
        assert exc_info.value.billing_url == "https://app.maginary.ai/dashboard"

    def test_api_raises_on_402_without_billing_url(self, monkeypatch):
        challenge = {"x402Version": 2, "error": "Insufficient credits.", "accepts": []}

        def fake_post(self, url, **kw):
            return httpx.Response(402, json=challenge, request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx.Client, "post", fake_post)
        with pytest.raises(api.PaymentRequiredError) as exc_info:
            api.create_generation("a fox")
        assert exc_info.value.billing_url is None


class TestGenerateToolPaymentRequired:

    def test_generate_returns_structured_error(self, monkeypatch):
        from mcp.types import CallToolResult
        from maginary_mcp.server import generate

        challenge = {
            "x402Version": 2,
            "error": "Insufficient credits.",
            "accepts": [{"scheme": "exact", "amount": "10000000"}],
            "billing_url": "https://app.maginary.ai/dashboard",
        }

        def fake_post(self, url, **kw):
            return httpx.Response(402, json=challenge, request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx.Client, "post", fake_post)
        result = generate("a fox")
        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent["error"] == "payment_required"
        assert result.structuredContent["billing_url"] == "https://app.maginary.ai/dashboard"
        assert result.structuredContent["challenge"] == challenge
        assert "x402" in result.structuredContent["message"]

    def test_error_message_not_duplicated(self, monkeypatch):
        from mcp.types import CallToolResult
        from maginary_mcp.server import generate

        challenge = {
            "x402Version": 2,
            "error": "5 credits short. Agents: pay $0.35 USDC on Base Sepolia (see accepts). Humans: top up at https://app.maginary.ai/dashboard",
            "accepts": [{"scheme": "exact", "amount": "350000"}],
            "billing_url": "https://app.maginary.ai/dashboard",
        }

        def fake_post(self, url, **kw):
            return httpx.Response(402, json=challenge, request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx.Client, "post", fake_post)
        result = generate("a fox")
        msg = result.structuredContent["message"]
        assert msg.count("top up at") <= 1, f"Duplicate billing URL in message: {msg}"
