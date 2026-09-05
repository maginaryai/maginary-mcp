"""generate() failure contract: every failure is an isError result, never a raise.

The docstring promises three shapes (auth / payment_required / failed) and
that nothing propagates as an exception — agent frameworks branch on isError,
so a raised httpx error or a plain success-shaped dict would mislead them.
(The payment_required shape is covered in test_payment_required.py.)
"""
import httpx
import pytest
from mcp.types import CallToolResult

from maginary_mcp.server import generate, get_generation


@pytest.fixture(autouse=True)
def _no_ambient_key(monkeypatch):
    monkeypatch.delenv("MAGINARY_API_KEY", raising=False)


class TestGenerateErrorShapes:

    def test_auth_failure_is_error_result(self, monkeypatch):
        monkeypatch.setattr("maginary_mcp.api._read_config_key", lambda: None)
        result = generate("a fox")
        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent["error"] == "auth"

    def test_revoked_key_401_is_auth_not_failed(self, monkeypatch):
        # A rejected key must steer the agent to key setup, not a blind retry.
        monkeypatch.setenv("MAGINARY_API_KEY", "sk-mag-revoked")

        def fake_post(self, url, **kw):
            return httpx.Response(401, json={}, request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx.Client, "post", fake_post)
        result = generate("a fox")
        assert result.isError is True
        assert result.structuredContent["error"] == "auth"

    def test_backend_500_is_error_result_not_raise(self, monkeypatch):
        monkeypatch.setenv("MAGINARY_API_KEY", "sk-mag-test")

        def fake_post(self, url, **kw):
            return httpx.Response(500, text="boom", request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx.Client, "post", fake_post)
        result = generate("a fox")
        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent["error"] == "failed"

    def test_network_error_is_error_result_not_raise(self, monkeypatch):
        monkeypatch.setenv("MAGINARY_API_KEY", "sk-mag-test")

        def fake_post(self, url, **kw):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(httpx.Client, "post", fake_post)
        result = generate("a fox")
        assert result.isError is True
        assert result.structuredContent["error"] == "failed"


class TestGetGenerationErrorShapes:

    def test_backend_error_is_error_result_not_raise(self, monkeypatch):
        monkeypatch.setenv("MAGINARY_API_KEY", "sk-mag-test")

        def fake_get(self, url, **kw):
            return httpx.Response(500, text="boom", request=httpx.Request("GET", url))

        monkeypatch.setattr(httpx.Client, "get", fake_get)
        result = get_generation("some-uuid")
        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent["error"] == "failed"


class TestSoftErrorShapes:
    """api-layer soft errors (409/403 bodies) must surface as isError too."""

    def test_checkout_email_not_verified_is_error_result(self, monkeypatch):
        from maginary_mcp.server import checkout
        monkeypatch.setenv("MAGINARY_API_KEY", "sk-mag-test")

        def fake_post(self, url, **kw):
            return httpx.Response(403, json={}, request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx.Client, "post", fake_post)
        result = checkout(product_id=1)
        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert result.structuredContent["error"] == "email_not_verified"

    def test_create_account_conflict_is_error_result(self, monkeypatch):
        from maginary_mcp.server import create_account

        def fake_post(self, url, **kw):
            return httpx.Response(409, json={"error": "Account exists"},
                                  request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx.Client, "post", fake_post)
        result = create_account("a@b.c")
        assert result.isError is True
        assert result.structuredContent["error"] == "already_exists"

    def test_successful_payload_stays_unwrapped(self, monkeypatch):
        # A success dict must NOT be wrapped — structuredContent/outputSchema
        # for successes depend on the plain-dict return.
        from maginary_mcp.server import get_products

        def fake_get(self, url, **kw):
            return httpx.Response(200, json=[{"id": 1, "name": "novice_pack"}],
                                  request=httpx.Request("GET", url))

        monkeypatch.setattr(httpx.Client, "get", fake_get)
        result = get_products()
        assert not isinstance(result, CallToolResult)
        # The backend sends a bare array; the tool must wrap it in a dict or
        # FastMCP's output validation rejects the call (see test_tool_layer).
        assert result["products"][0]["name"] == "novice_pack"
        assert result["count"] == 1


class TestErrorFidelity:
    """Failures must carry the actionable reason, not just a status code."""

    def test_400_body_detail_reaches_the_agent(self, monkeypatch):
        monkeypatch.setenv("MAGINARY_API_KEY", "sk-mag-test")

        def fake_post(self, url, **kw):
            return httpx.Response(400, json={"error": "Unknown flag: --badflag"},
                                  request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx.Client, "post", fake_post)
        result = generate("a fox --badflag")
        assert result.isError is True
        assert "--badflag" in result.structuredContent["message"]

    def test_500_body_is_still_stripped(self, monkeypatch):
        monkeypatch.setenv("MAGINARY_API_KEY", "sk-mag-test")

        def fake_post(self, url, **kw):
            return httpx.Response(500, text="Traceback: secret internals",
                                  request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx.Client, "post", fake_post)
        result = generate("a fox")
        assert "secret" not in result.structuredContent["message"]

    def test_key_name_validation_is_not_limit_reached(self, monkeypatch):
        from maginary_mcp.server import manage_api_key
        monkeypatch.setenv("MAGINARY_API_KEY", "sk-mag-test")

        def fake_post(self, url, **kw):
            return httpx.Response(400, json={"name": ["Ensure this field has at most 64 characters."]},
                                  request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx.Client, "post", fake_post)
        result = manage_api_key(action="create", name="x" * 100)
        assert result.isError is True
        assert result.structuredContent["error"] == "validation"
        assert "64 characters" in result.structuredContent["message"]

    def test_key_limit_is_still_limit_reached(self, monkeypatch):
        from maginary_mcp.server import manage_api_key
        monkeypatch.setenv("MAGINARY_API_KEY", "sk-mag-test")

        def fake_post(self, url, **kw):
            return httpx.Response(400, json={"error": "Maximum 10 API keys per account. Delete one first."},
                                  request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx.Client, "post", fake_post)
        result = manage_api_key(action="create", name="ok")
        assert result.structuredContent["error"] == "limit_reached"

    def test_non_json_409_still_maps_already_exists(self, monkeypatch):
        from maginary_mcp.server import create_account

        def fake_post(self, url, **kw):
            return httpx.Response(409, text="<html>Conflict</html>",
                                  request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx.Client, "post", fake_post)
        result = create_account("a@b.c")
        assert result.structuredContent["error"] == "already_exists"


class TestStructuralClassification:

    def test_non_json_402_still_payment_required(self, monkeypatch):
        # A proxy error page on 402 must not lose the x402 recovery path.
        monkeypatch.setenv("MAGINARY_API_KEY", "sk-mag-test")

        def fake_post(self, url, **kw):
            return httpx.Response(402, text="<html>Payment Required</html>",
                                  request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx.Client, "post", fake_post)
        result = generate("a fox")
        assert result.isError is True
        assert result.structuredContent["error"] == "payment_required"

    def test_success_payload_with_error_field_stays_success(self, monkeypatch):
        # The sentinel makes classification structural: a backend success
        # body containing error+message strings must NOT be wrapped.
        from maginary_mcp.server import check_account_status
        monkeypatch.setenv("MAGINARY_API_KEY", "sk-mag-test")

        def fake_get(self, url, **kw):
            return httpx.Response(200, json={"verified": True, "error": "last gen failed",
                                             "message": "informational"},
                                  request=httpx.Request("GET", url))

        monkeypatch.setattr(httpx.Client, "get", fake_get)
        result = check_account_status()
        assert not isinstance(result, CallToolResult)
        assert result["verified"] is True

    def test_soft_error_body_is_clean(self, monkeypatch):
        # Classification is by type (SoftError), so the agent-facing body
        # carries exactly the documented shape — no marker keys.
        from maginary_mcp.server import create_account

        def fake_post(self, url, **kw):
            return httpx.Response(409, json={"error": "Account exists"},
                                  request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx.Client, "post", fake_post)
        result = create_account("a@b.c")
        assert result.isError is True
        assert set(result.structuredContent) == {"error", "message"}

    def test_non_json_402_has_no_phantom_challenge(self, monkeypatch):
        # An empty dict reads as a settleable x402 challenge; must be None.
        monkeypatch.setenv("MAGINARY_API_KEY", "sk-mag-test")

        def fake_post(self, url, **kw):
            return httpx.Response(402, text="<html>Payment Required</html>",
                                  request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx.Client, "post", fake_post)
        result = generate("a fox")
        assert result.structuredContent["challenge"] is None
