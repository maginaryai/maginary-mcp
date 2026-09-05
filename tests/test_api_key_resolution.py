"""Per-request API-key resolution for multi-tenant (hosted HTTP) mode.

stdio mode uses a single MAGINARY_API_KEY from the env. The hosted HTTP server
is multi-tenant: each request carries the *caller's* own key, injected via a
ContextVar by the ASGI auth middleware. Resolution order: per-request key
(ContextVar) wins, else env fallback, else AuthError.

Also tests ``is_hosted_mode()`` and mode-aware ``persist_api_key()`` behavior.
"""
import pytest

from maginary_mcp import api


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("MAGINARY_API_KEY", raising=False)
    monkeypatch.setattr(api, "_session_api_key", None)
    monkeypatch.setattr(api, "_read_config_key", lambda: None)


class TestResolveApiKey:

    def test_per_request_key_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("MAGINARY_API_KEY", "env-key")
        with api.request_api_key("caller-key"):
            assert api._resolve_api_key() == "caller-key"

    def test_falls_back_to_env_when_no_request_key(self, monkeypatch):
        monkeypatch.setenv("MAGINARY_API_KEY", "env-key")
        assert api._resolve_api_key() == "env-key"

    def test_raises_when_neither_set(self):
        with pytest.raises(api.AuthError):
            api._resolve_api_key()

    def test_context_resets_after_block(self, monkeypatch):
        monkeypatch.setenv("MAGINARY_API_KEY", "env-key")
        with api.request_api_key("caller-key"):
            pass
        # After the block the per-request key must be gone (no leakage across requests).
        assert api._resolve_api_key() == "env-key"

    def test_headers_use_resolved_key(self):
        with api.request_api_key("caller-key"):
            headers = api._headers()
        assert headers["Authorization"] == "Bearer caller-key"


class TestHostedModeNeverLeaksEnvKey:
    """HTTP mode binds the request's key (possibly None). A request WITHOUT a
    Bearer header must get AuthError — never the box's env key, which would
    silently bill one account for every anonymous tenant."""

    def test_bound_none_with_env_key_raises(self, monkeypatch):
        monkeypatch.setenv("MAGINARY_API_KEY", "ops-leftover-key")
        with api.request_api_key(None):
            with pytest.raises(api.AuthError):
                api._resolve_api_key()


class TestIsHostedMode:
    """is_hosted_mode() reflects whether a ContextVar has been bound."""

    def test_false_in_stdio_mode(self):
        assert api.is_hosted_mode() is False

    def test_true_inside_request_api_key(self):
        with api.request_api_key("key"):
            assert api.is_hosted_mode() is True

    def test_true_even_with_none_key(self):
        with api.request_api_key(None):
            assert api.is_hosted_mode() is True

    def test_resets_after_block(self):
        with api.request_api_key("key"):
            pass
        assert api.is_hosted_mode() is False


class TestPersistApiKeyModeAware:
    """persist_api_key must skip disk writes in hosted mode."""

    def test_stdio_mode_writes_to_disk(self, tmp_path, monkeypatch):
        key_file = tmp_path / "api_key"
        monkeypatch.setattr(api, "_CONFIG_DIR", tmp_path)
        monkeypatch.setattr(api, "_CONFIG_KEY_FILE", key_file)

        api.persist_api_key("test-key-stdio")

        assert key_file.read_text() == "test-key-stdio"
        assert api._session_api_key == "test-key-stdio"
        # cleanup
        monkeypatch.setattr(api, "_session_api_key", None)

    def test_hosted_mode_skips_entirely(self, tmp_path, monkeypatch):
        key_file = tmp_path / "api_key"
        monkeypatch.setattr(api, "_CONFIG_DIR", tmp_path)
        monkeypatch.setattr(api, "_CONFIG_KEY_FILE", key_file)

        with api.request_api_key("caller-key"):
            api.persist_api_key("should-not-persist")

        assert not key_file.exists()
        assert api._session_api_key is None
