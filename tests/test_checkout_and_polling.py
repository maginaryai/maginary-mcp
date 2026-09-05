"""Checkout URL contract + poll-loop resilience.

Both bugs these tests pin were found in production-shaped review:
- checkout silently 404ing because the MCP hit ``create_session`` (underscore)
  while the backend's action2 router serves ``create-session`` (dash);
- wait_for_generation reporting a still-running generation as failed after a
  single transient poll error, inviting an agent to retry and double-spend.
"""
import httpx
import pytest

from maginary_mcp import api


@pytest.fixture(autouse=True)
def _test_key(monkeypatch):
    monkeypatch.setenv("MAGINARY_API_KEY", "sk-mag-test")


class TestCheckoutPath:

    def test_posts_kebab_case_route(self, monkeypatch):
        # Must match the backend route frozen in
        # backend/tests/test_public_openapi_surface.py: /checkout-stripe/create-session/
        seen = {}

        def fake_post(self, url, **kw):
            seen["url"] = url
            return httpx.Response(200, json={"checkout_url": "https://stripe/x"},
                                  request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx.Client, "post", fake_post)
        api.create_checkout(product_id=1)
        assert seen["url"].endswith("/checkout-stripe/create-session/")
        assert "create_session" not in seen["url"]


class TestPollResilience:

    def _states(self, monkeypatch, responses):
        """Feed a scripted sequence of poll responses; no real sleeping."""
        it = iter(responses)

        def fake_get(self, url, **kw):
            item = next(it)
            if isinstance(item, Exception):
                raise item
            return httpx.Response(item[0], json=item[1], request=httpx.Request("GET", url))

        monkeypatch.setattr(httpx.Client, "get", fake_get)
        monkeypatch.setattr(api.time, "sleep", lambda s: None)

    def test_single_transient_502_does_not_abort(self, monkeypatch):
        self._states(monkeypatch, [
            (200, {"processing_state": "running"}),
            (502, {}),
            (200, {"processing_state": "done", "image_urls": ["u"]}),
        ])
        gen = api.wait_for_generation("uuid", timeout_s=60)
        assert gen["processing_state"] == "done"

    def test_network_blip_does_not_abort(self, monkeypatch):
        self._states(monkeypatch, [
            httpx.ConnectError("blip"),
            (200, {"processing_state": "done"}),
        ])
        assert api.wait_for_generation("uuid", timeout_s=60)["processing_state"] == "done"

    def test_persistent_errors_still_abort(self, monkeypatch):
        self._states(monkeypatch, [(502, {})] * api._MAX_CONSECUTIVE_POLL_ERRORS)
        with pytest.raises(httpx.HTTPStatusError):
            api.wait_for_generation("uuid", timeout_s=60)

    def test_deterministic_4xx_aborts_immediately(self, monkeypatch):
        # A 404 (bad uuid) or 401 (revoked key) never heals — no retries.
        self._states(monkeypatch, [(404, {})])
        with pytest.raises(httpx.HTTPStatusError):
            api.wait_for_generation("bad-uuid", timeout_s=60)

    def test_success_resets_error_streak(self, monkeypatch):
        blips = [(502, {}), (200, {"processing_state": "running"})]
        self._states(monkeypatch,
                     blips * api._MAX_CONSECUTIVE_POLL_ERRORS
                     + [(200, {"processing_state": "done"})])
        assert api.wait_for_generation("uuid", timeout_s=60)["processing_state"] == "done"
