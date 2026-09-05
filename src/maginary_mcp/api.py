"""Thin HTTP client for the Maginary REST API.

Bearer-token auth via ``MAGINARY_API_KEY``. Base URL overridable via
``MAGINARY_BASE_URL`` for staging / self-hosted deployments. All calls are
synchronous ``httpx.Client`` — the MCP server itself is single-threaded and
tools are called one at a time, so async buys us nothing here.

Public functions raise ``AuthError`` when there's no API key set or the
backend rejects the credentials (401), and ``httpx.HTTPStatusError`` for other
non-2xx responses (the message includes the response body so LLM callers can
surface the real reason).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator

import httpx

LOG = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://app.maginary.ai/api"
DEFAULT_TIMEOUT = 30.0
# wait_for_generation keeps polling through this many failed polls in a row
# before giving up — one transient 502 must not report a live gen as failed.
_MAX_CONSECUTIVE_POLL_ERRORS = 3
_CONFIG_DIR = Path.home() / ".config" / "maginary"
_CONFIG_KEY_FILE = _CONFIG_DIR / "api_key"

# Per-request caller API key for hosted (multi-tenant HTTP) mode. The ASGI auth
# middleware sets this from each request's `Authorization: Bearer` header; it is
# unset in stdio mode, where the single env key is used instead. A ContextVar
# (not a global) so concurrent requests never see each other's key.
#
# _UNBOUND (the default) means "no HTTP middleware in play" = stdio mode → env
# fallback applies. A bound value — INCLUDING None for a request with no Bearer
# header — means hosted mode, where the env key must NEVER apply: otherwise an
# anonymous request on a box with a leftover MAGINARY_API_KEY would silently
# bill that key's account for every tenant.
_UNBOUND = object()
_request_api_key: ContextVar[Any] = ContextVar("maginary_request_api_key", default=_UNBOUND)

# Hosted mode only: headers that make the backend see the ORIGINAL request the
# way a reverse proxy would present it. Without X-Forwarded-Proto the backend's
# SECURE_SSL_REDIRECT 301s our plain-http internal call to https://backend:8000
# (no TLS there -> "WRONG_VERSION_NUMBER"); without X-Forwarded-For every MCP
# tenant shares this container's IP for the backend's rate limiting.
_request_forwarded: ContextVar[dict[str, str] | None] = ContextVar("maginary_request_forwarded", default=None)

# Session-level API key set by configure_api_key (stdio mode only). Overrides
# env but not the per-request ContextVar (hosted mode). Enables the agent
# onboarding flow: create account → create API key → configure → generate.
_session_api_key: str | None = None


def is_hosted_mode() -> bool:
    """True when running inside the multi-tenant HTTP server.

    In hosted mode the ASGI middleware binds a ContextVar per request (even if
    the bound value is None for an anonymous request). In stdio mode the
    ContextVar is never bound and stays at the _UNBOUND sentinel.

    Use this to gate behavior that only makes sense in one mode — e.g. disk
    persistence is a stdio concern and must not run in hosted mode.
    """
    return _request_api_key.get() is not _UNBOUND


class AuthError(RuntimeError):
    """No API key available, or the backend rejected the credentials (401)."""


class PaymentRequiredError(Exception):
    """Backend returned 402 — caller needs to top up credits or pay via x402."""

    def __init__(self, message: str, challenge: dict | None = None, billing_url: str | None = None):
        super().__init__(message)
        self.challenge = challenge
        self.billing_url = billing_url


class SoftError(dict):
    """An expected failure returned as data (409 already_exists, 403
    email_not_verified, ...).

    A ``dict`` subclass: annotations, JSON serialization, and direct callers
    all keep working, while server._as_result classifies by *type* — no
    in-band marker key, so no backend payload can ever collide with it and
    nothing has to be stripped before the body reaches the agent.
    """


def soft_error(code: str, message: str) -> SoftError:
    return SoftError({"error": code, "message": message})


def _parsed_body(resp: httpx.Response) -> dict[str, Any] | None:
    """The response body as a dict, or None (non-JSON / non-dict). Never raises."""
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _detail_from_data(data: dict[str, Any] | None) -> str | None:
    """Best-effort human reason from a parsed error body.

    Understands ``{"error": ...}`` / ``{"detail": ...}`` / ``{"message": ...}``
    and DRF serializer errors (``{"field": ["msg", ...]}``).
    """
    if not data:
        return None
    for key in ("error", "detail", "message"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value[:300]
    for field, msgs in data.items():
        if isinstance(msgs, list) and msgs and isinstance(msgs[0], str):
            return f"{field}: {msgs[0]}"[:300]
    return None


def _body_detail(resp: httpx.Response) -> str | None:
    """Best-effort human reason from an error-response body. Never raises.

    Falls back to the first line of a non-JSON body (tags stripped): a bare
    Django ``DisallowedHost`` 400 or a proxy error page is HTML, and without
    this the agent only ever saw "HTTP 400" with no reason.
    """
    detail = _detail_from_data(_parsed_body(resp))
    if detail:
        return detail
    text = re.sub(r"<[^>]+>", " ", resp.text or "")
    text = " ".join(text.split())
    return text[:160] or None


def safe_error_message(exc: Exception) -> str:
    """Return a user-safe error string for surfacing to the LLM.

    4xx bodies are client-facing validation messages — the agent needs them
    to fix its call (e.g. which ``--flag`` was invalid), so their detail is
    included. 5xx bodies can leak backend internals (stack traces, DB
    errors) — especially dangerous in hosted multi-tenant mode — so those
    are reduced to status + path.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        base = f"HTTP {exc.response.status_code} from {exc.request.url.path}"
        if 400 <= exc.response.status_code < 500:
            detail = _body_detail(exc.response)
            if detail:
                return f"{base}: {detail}"
        return base
    return str(exc)


@contextmanager
def request_api_key(key: str | None, forwarded: dict[str, str] | None = None) -> Iterator[None]:
    """Bind ``key`` as the caller's API key for the duration of the block.

    Used by the hosted HTTP server's auth middleware to scope each request to
    the key it arrived with. ``forwarded`` (``X-Forwarded-Proto`` /
    ``X-Forwarded-For`` of the original request) is attached to every backend
    call made inside the block. Resets on exit so nothing leaks across requests.
    """
    token = _request_api_key.set(key)
    fwd_token = _request_forwarded.set(dict(forwarded) if forwarded else None)
    try:
        yield
    finally:
        _request_forwarded.reset(fwd_token)
        _request_api_key.reset(token)


def _read_config_key() -> str | None:
    """Read persisted API key from ~/.config/maginary/api_key."""
    try:
        key = _CONFIG_KEY_FILE.read_text().strip()
        return key or None
    except (OSError, FileNotFoundError):
        return None


def persist_api_key(key: str) -> None:
    """Save API key for this session and (in stdio mode) to disk.

    In hosted HTTP mode, each request carries its own key via ContextVar, so
    neither the session global nor disk persistence should be touched — the
    global would leak a tenant's key into non-request contexts, and the file
    would leak it to the server's filesystem.
    """
    if is_hosted_mode():
        LOG.debug("Hosted mode — skipping persist entirely")
        return
    global _session_api_key
    _session_api_key = key
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _CONFIG_KEY_FILE.write_text(key)
    _CONFIG_KEY_FILE.chmod(0o600)
    LOG.info("API key persisted to %s", _CONFIG_KEY_FILE)


def _resolve_api_key() -> str:
    """Hosted mode: the request's own key, or AuthError. stdio mode: env key.

    Resolution order (stdio): session var → env → config file.
    The env key is consulted ONLY when no middleware bound a value (stdio) —
    a hosted request without a Bearer header must fail, not fall back.
    """
    if is_hosted_mode():
        bound = _request_api_key.get()
        if not bound:
            raise AuthError(
                "No API key in request. Send it as `Authorization: Bearer <key>`."
            )
        return bound

    key = _session_api_key or os.environ.get("MAGINARY_API_KEY") or _read_config_key()
    if not key:
        raise AuthError(
            "No Maginary API key. Set MAGINARY_API_KEY, use `configure_api_key`, "
            "or create an account with `create_account` first. Grab a key at "
            "https://app.maginary.ai/dashboard#api-keys"
        )
    return key


def _base_url() -> str:
    return (os.environ.get("MAGINARY_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def _bearer_headers(api_key: str | None = None) -> dict[str, str]:
    """Build headers with Bearer auth. Uses resolved key if none provided."""
    key = api_key or _resolve_api_key()
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _basic_headers(email: str, password: str) -> dict[str, str]:
    """Build headers with HTTP Basic auth."""
    creds = base64.b64encode(f"{email}:{password}".encode()).decode()
    return {
        "Authorization": f"Basic {creds}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _headers() -> dict[str, str]:
    return {**_bearer_headers(), **(_request_forwarded.get() or {})}


def _raise_for_status(resp: httpx.Response) -> None:
    """Like ``resp.raise_for_status`` but maps 401 to :class:`AuthError`.

    A 401 means the backend rejected the key/credentials — the caller's
    recovery is to fix auth (new key, correct password), not to retry.
    Without this mapping a revoked key surfaced as a generic "failed".
    """
    if resp.status_code == 401:
        raise AuthError(
            "The Maginary API rejected the credentials (HTTP 401) — the API "
            "key or email/password is invalid or revoked. Create a new key "
            "with manage_api_key, or re-check the password."
        )
    resp.raise_for_status()


def _client(timeout: float = DEFAULT_TIMEOUT) -> httpx.Client:
    return httpx.Client(timeout=timeout, follow_redirects=True)


# ── Unauthenticated endpoints ───────────────────────────────────────────


def register_account(email: str) -> dict[str, Any]:
    """POST /auth/register/ — create a new account (no auth required)."""
    with _client() as client:
        resp = client.post(
            f"{_base_url()}/auth/register/",
            json={"email": email},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        if resp.status_code == 409:
            return soft_error("already_exists", _body_detail(resp) or "Account exists")
        if resp.status_code == 429:
            return soft_error("rate_limited", "Too many registration attempts. Try again later.")
        if resp.status_code == 400:
            return soft_error("validation", _body_detail(resp) or "Invalid request")
        _raise_for_status(resp)
        return resp.json()


# ── Basic-auth endpoints (email:password) ────────────────────────────────


def get_account_status(email: str | None = None, password: str | None = None) -> dict[str, Any]:
    """GET /auth/account-status/ — check verification, credits, key count."""
    headers = _basic_headers(email, password) if email and password else _headers()
    with _client() as client:
        resp = client.get(f"{_base_url()}/auth/account-status/", headers=headers)
        _raise_for_status(resp)
        return resp.json()


def create_api_key(name: str, email: str | None = None, password: str | None = None) -> dict[str, Any]:
    """POST /api-keys/ — create a new API key (Basic or Bearer auth).

    Requires a verified email address (returns 403 otherwise).
    """
    headers = _basic_headers(email, password) if email and password else _headers()
    with _client() as client:
        resp = client.post(f"{_base_url()}/api-keys/", json={"name": name}, headers=headers)
        if resp.status_code == 400:
            # The backend uses 400 for both the key cap ({"error": "Maximum
            # N API keys..."}) and serializer validation ({"name": [...]}) —
            # only the former means "delete a key first".
            data = _parsed_body(resp)
            if data and isinstance(data.get("error"), str):
                return soft_error("limit_reached", data["error"])
            return soft_error("validation", _detail_from_data(data) or "Invalid key name")
        if resp.status_code == 403:
            return soft_error("email_not_verified", "Email verification required. The user must click the verification link in their inbox first.")
        _raise_for_status(resp)
        return resp.json()


def list_api_keys(email: str | None = None, password: str | None = None) -> dict[str, Any]:
    """GET /api-keys/ — list all API keys for the authenticated user."""
    headers = _basic_headers(email, password) if email and password else _headers()
    with _client() as client:
        resp = client.get(f"{_base_url()}/api-keys/", headers=headers)
        _raise_for_status(resp)
        return resp.json()


def revoke_api_key(key_prefix: str, email: str | None = None, password: str | None = None) -> dict[str, Any]:
    """DELETE /api-keys/{prefix}/ — revoke a specific API key."""
    if not key_prefix or not key_prefix.isalnum():
        return soft_error("validation", "key_prefix must be alphanumeric")
    headers = _basic_headers(email, password) if email and password else _headers()
    with _client() as client:
        resp = client.delete(f"{_base_url()}/api-keys/{key_prefix}/", headers=headers)
        if resp.status_code == 404:
            return soft_error("not_found", f"No API key with prefix {key_prefix!r}")
        if resp.status_code == 204:
            return {"success": True, "message": f"API key {key_prefix}... revoked"}
        _raise_for_status(resp)
        return resp.json()


def list_products() -> list[dict[str, Any]]:
    """GET /products/ — list available products (public, no auth)."""
    with _client() as client:
        resp = client.get(
            f"{_base_url()}/products/",
            headers={"Accept": "application/json"},
        )
        _raise_for_status(resp)
        return resp.json()


def create_checkout(product_id: int, email: str | None = None, password: str | None = None) -> dict[str, Any]:
    """POST /checkout-stripe/create-session/ — create a Stripe checkout session.

    Requires a verified email address (returns 403 otherwise).
    """
    headers = _basic_headers(email, password) if email and password else _headers()
    with _client() as client:
        resp = client.post(
            # Dash, not underscore: the backend's action2 router rewrites
            # url_path to kebab-case (see the OpenAPI-surface guard test).
            f"{_base_url()}/checkout-stripe/create-session/",
            json={"product_id": product_id},
            headers=headers,
        )
        if resp.status_code == 403:
            return soft_error("email_not_verified", "Email verification required before purchasing.")
        _raise_for_status(resp)
        return resp.json()


# ── Bearer-auth endpoints (API key) ─────────────────────────────────────


PAYMENT_SIGNATURE_HEADER = "PAYMENT-SIGNATURE"   # x402 v2 request header
PAYMENT_RESPONSE_HEADER = "PAYMENT-RESPONSE"     # x402 v2 settlement receipt


def create_generation(
    prompt: str,
    callback_url: str | None = None,
    payment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """POST /gens/ — kick off a generation and return the created record.

    ``payment`` is an x402 PaymentPayload (the dict an x402 client puts in
    the MCP call's ``_meta["x402/payment"]``); it is forwarded verbatim as
    the PAYMENT-SIGNATURE header, and the backend does everything else
    (verify, settle, mint, keyless identity). The MCP holds no payment logic.
    On success, a PAYMENT-RESPONSE receipt header, if present, is decoded
    into ``x402_receipt`` on the returned record.
    """
    body: dict[str, Any] = {"prompt": prompt}
    if callback_url:
        body["callback_url"] = callback_url
    headers = _headers()
    if payment:
        headers[PAYMENT_SIGNATURE_HEADER] = base64.b64encode(json.dumps(payment).encode()).decode()

    with _client() as client:
        resp = client.post(f"{_base_url()}/gens/", headers=headers, json=body)
        if resp.status_code == 402:
            # Guarded like every other error body: a proxy's non-JSON 402
            # must still classify as payment_required, not generic "failed".
            data = _parsed_body(resp)
            raise PaymentRequiredError(
                message=_detail_from_data(data) or "Insufficient credits",
                # None, not {} — the docstring tells agents to settle the
                # challenge, and an empty dict reads as a settleable one.
                challenge=data or None,
                billing_url=(data or {}).get("billing_url"),
            )
        _raise_for_status(resp)
        record = resp.json()
        receipt = _decode_receipt(resp.headers.get(PAYMENT_RESPONSE_HEADER))
        if receipt is not None:
            record["x402_receipt"] = receipt
        return record


def credential_is_valid(bearer: str, timeout: float = 5.0,
                        forwarded: dict[str, str] | None = None) -> bool | None:
    """Validate a Bearer (API key or OAuth access token) by USE against the backend.

    The hosted MCP is an OAuth resource server and must reject invalid or
    expired tokens with 401 before any tool runs. The backend is the
    authorization server AND the API, so the cheapest exact check is asking
    it: GET /auth/account-status/ with the credential. 2xx -> True, 401 ->
    False, 403 -> True (the backend RECOGNISED the credential and refused
    this one endpoint — unverified email, missing scope; the tool call will
    say so precisely, a 401 here would send the client into a re-login
    loop), anything else (backend down) -> None so the gate can fail open.
    """
    try:
        with _client(timeout=timeout) as client:
            resp = client.get(f"{_base_url()}/auth/account-status/",
                              headers={**_bearer_headers(bearer),
                                       **(forwarded or _request_forwarded.get() or {})})
    except httpx.HTTPError as exc:
        LOG.warning("credential check unavailable: %s", exc)
        return None
    if 200 <= resp.status_code < 300 or resp.status_code == 403:
        return True
    if resp.status_code == 401:
        return False
    LOG.warning("credential check unexpected status %s", resp.status_code)
    return None


def _decode_receipt(header: str | None) -> dict[str, Any] | None:
    """PAYMENT-RESPONSE (base64 JSON: success/transaction/network/payer) -> dict, or None."""
    if not header:
        return None
    try:
        data = json.loads(base64.b64decode(header))
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


@contextmanager
def override_api_key(key: str | None) -> Iterator[None]:
    """Hosted mode only: use ``key`` for this block when the request bound none.

    Carrier for ``_meta["maginary/api_key"]``: an agent that obtained a key
    mid-session (keyless x402 returns one on the first settlement) can't add
    a header to an existing connection, but it can set ``_meta`` on a tool
    call. An explicit Authorization header always wins; stdio mode ignores
    this entirely (single tenant, env key).
    """
    if not key or not is_hosted_mode() or _request_api_key.get():
        yield
        return
    token = _request_api_key.set(key)
    try:
        yield
    finally:
        _request_api_key.reset(token)


def get_generation(uuid: str) -> dict[str, Any]:
    """GET /gens/{uuid}/ — poll a specific generation."""
    with _client() as client:
        resp = client.get(f"{_base_url()}/gens/{uuid}/", headers=_headers())
        _raise_for_status(resp)
        return resp.json()


# Most MCP clients (Claude Desktop, Cursor — anything on the TypeScript SDK)
# kill a tool call at 60 s by default. A blocking wait must return under that,
# so a timeout here is a soft "still running, call again", not a failure.
DEFAULT_WAIT_TIMEOUT_S = 45.0


def wait_for_generation(
    uuid: str,
    timeout_s: float = DEFAULT_WAIT_TIMEOUT_S,
    initial_delay_s: float = 3.0,
    max_delay_s: float = 10.0,
) -> dict[str, Any]:
    """Poll ``get_generation`` until it lands in a terminal state.

    Uses linear backoff (start ``initial_delay_s``, +2 s per attempt, capped at
    ``max_delay_s``). Terminal states are ``done`` and ``failed`` (matched
    case-insensitively) — everything else is considered still-processing.

    Transient poll failures (a 502 while an LB restarts, a network blip) are
    tolerated: only ``_MAX_CONSECUTIVE_POLL_ERRORS`` failures in a row abort
    the wait. Aborting on the first blip would report a still-running
    generation as failed — and a retrying agent would double-spend credits.

    Raises :class:`TimeoutError` if the generation is still not terminal by
    ``timeout_s``. The MCP caller can surface that as a partial result to the
    LLM without breaking the tool contract.
    """
    deadline = time.monotonic() + timeout_s
    delay = initial_delay_s
    consecutive_errors = 0
    last_state: Any = None
    while True:
        try:
            gen = get_generation(uuid)
        except httpx.HTTPStatusError as exc:
            # 4xx is deterministic (bad uuid → 404, revoked key → 401):
            # retrying it just wastes the agent's time. Only 5xx is transient.
            if exc.response.status_code < 500:
                raise
            consecutive_errors += 1
            if consecutive_errors >= _MAX_CONSECUTIVE_POLL_ERRORS:
                raise
        except (httpx.TransportError, ValueError):  # network blip / bad JSON body
            consecutive_errors += 1
            if consecutive_errors >= _MAX_CONSECUTIVE_POLL_ERRORS:
                raise
        else:
            consecutive_errors = 0
            last_state = gen.get("processing_state")
            # Backend sends lowercase ('done'/'failed' — see Gen.ProcessingState);
            # compare case-insensitively so a casing change never deadlocks polling.
            if str(last_state).lower() in ("done", "failed"):
                return gen
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"Generation {uuid} still {last_state!r} after {timeout_s:g}s — it keeps "
                "running server-side; call wait_for_generation (or get_generation) again."
            )
        # Never sleep past the deadline: one more poll, then a prompt timeout.
        time.sleep(min(delay, remaining))
        delay = min(delay + 2.0, max_delay_s)
