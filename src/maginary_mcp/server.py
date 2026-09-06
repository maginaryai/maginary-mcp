"""MCP server exposing Maginary tools over stdio.

Uses the official ``mcp`` Python SDK's ``FastMCP`` convenience wrapper —
each ``@mcp.tool()`` function becomes a callable tool for MCP clients
(Claude Desktop, Continue, Cursor, custom).

Tools are grouped into four categories:

- **Catalog** (read-only, no auth) — parameter discovery from a bundled
  snapshot or live API.
- **Generation** (Bearer auth) — create, poll, and wait for image/video gens.
- **Onboarding** (unauthenticated or Basic auth) — account creation,
  verification polling, API key management.
- **Payment** (Basic or Bearer auth) — product listing, Stripe checkout,
  balance queries.
"""

from __future__ import annotations

import base64
import functools
import json
import logging
import os
import sys
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import CallToolResult, ImageContent, TextContent

from . import __version__
from .api import (
    DEFAULT_WAIT_TIMEOUT_S,
    AuthError,
    PaymentRequiredError,
    create_generation,
    execute_action as api_execute_action,
    fetch_image,
    override_api_key,
    get_generation as api_get_generation,
    upload_image as api_upload_image,
    wait_for_generation as api_wait_for_generation,
    register_account,
    get_account_status,
    create_api_key as api_create_api_key,
    list_api_keys as api_list_api_keys,
    revoke_api_key as api_revoke_api_key,
    list_products as api_list_products,
    create_checkout as api_create_checkout,
    SoftError,
    is_hosted_mode,
    persist_api_key,
    safe_error_message,
)
from .params import (
    availability_map,
    find_parameter,
    get_catalog,
    search_parameters as params_search,
)


LOG = logging.getLogger(__name__)

# The complete flag index (live / partial / reserved), generated from the
# bundled snapshot so it can never drift from the catalog. It goes into the
# server instructions AND the `generate` tool description: those two strings
# are the only thing most agents ever read about the DSL.
_DSL_MAP = availability_map()

mcp = FastMCP(
    name="maginary",
    instructions=(
        "Maginary is a Midjourney-style AI image + video generator with a `--flag` "
        "prompt DSL and an async HTTP API.\n\n"
        "**New users (no API key yet):** Use `create_account` → wait for email "
        "verification (poll with `check_account_status` — MUST be verified before "
        "proceeding) → `manage_api_key(action='create')` → `configure_api_key` → "
        "`get_products` → `checkout` (or just `generate` if you have a USDC wallet "
        "— x402 handles payment on-chain). The $10 novice_pack is the recommended "
        "starting point. API key creation and checkout both require a verified email.\n\n"
        "**Prompt DSL essentials:** flags go at the END of the prompt. `--1` `--2` "
        "`--3` `--4` = number of images (default 4; only specify if the user asks "
        "for a specific count), `--ar 16:9` = aspect ratio, `--v <model>` = model. "
        "If the user names a flag you don't know, call `get_parameter(name)` — "
        "never guess or drop it.\n\n"
        f"**Every flag that exists, and its state:** {_DSL_MAP}\n\n"
        "**Existing users:** Use `search_parameters` / `get_parameter` to discover "
        "which flags exist before building a prompt. Use `generate` to kick off a "
        "generation, then `wait_for_generation` (or a webhook callback) to fetch the "
        "resulting image / video URLs. Flags whose status is `dead`, `mostly-dead`, "
        "or `unimplemented` should be avoided."
    ),
)


# ─── Read-only catalog tools ──────────────────────────────────────────────


@mcp.tool()
def list_parameters(
    category: str | None = None,
    status: str | None = None,
    include_reserved: bool = False,
) -> dict[str, Any]:
    """List Maginary prompt-DSL parameters.

    Args:
        category: Restrict to one category (e.g. ``composition``, ``video``,
            ``model``, ``outpaint``). Call with no filters once — the response's
            ``categories`` / ``statuses`` maps are the full taxonomy.
        status: Restrict to one status (``live``, ``mostly-dead``,
            ``unimplemented``).
        include_reserved: When False (default) drop ``unimplemented``
            (recognized-but-blocked) parameters from the result.

    Returns:
        A dict with ``count``, ``source`` (``live`` vs. ``bundled-snapshot``),
        ``categories`` / ``statuses`` (the filter taxonomy), and ``parameters``
        (the array of matching entries).
    """
    matches = params_search(
        query="",
        category=category,
        status=status,
        include_reserved=include_reserved,
    )
    catalog = get_catalog()
    return {
        "count": len(matches),
        "source": catalog.get("_source", "unknown"),
        "categories": catalog.get("categories", {}),
        "statuses": catalog.get("statuses", {}),
        "parameters": matches,
    }


@mcp.tool()
def search_parameters(
    query: str,
    category: str | None = None,
    include_reserved: bool = False,
) -> dict[str, Any]:
    """Text-search over parameter names, aliases, descriptions, values, examples.

    Args:
        query: Substring match, case-insensitive.
        category: Optional single-category restriction.
        include_reserved: Whether to include ``unimplemented`` parameters.

    Returns:
        Dict with ``count``, ``source`` (``live`` vs. ``bundled-snapshot``),
        and ``parameters`` (ordered as they appear in the catalog).
    """
    matches = params_search(
        query=query,
        category=category,
        include_reserved=include_reserved,
    )
    return {
        "count": len(matches),
        "source": get_catalog().get("_source", "unknown"),
        "parameters": matches,
    }


@mcp.tool()
def get_parameter(name: str) -> dict[str, Any]:
    """Return the full record for a single parameter (canonical name or alias).

    Args:
        name: Parameter name with or without leading ``--`` (e.g. ``ar``,
            ``--ar``, ``aspect``). Case-insensitive.

    Returns:
        The parameter dict. Not-found is an ``isError`` result — surface it
        rather than fabricating a param.
    """
    p = find_parameter(name)
    if not p:
        return _error_result({
            "error": "not_found",
            "message": f"No such parameter: {name!r}. Try `search_parameters` to explore.",
        })
    return p


# ─── API-hitting tools (require MAGINARY_API_KEY) ─────────────────────────


def _as_result(data: dict[str, Any]) -> dict[str, Any] | CallToolResult:
    """Pass a success payload through; wrap api-layer soft errors as isError.

    Classification is by type: only ``api.soft_error()`` produces a
    ``SoftError``, so a backend success payload can never be misread as a
    tool failure no matter what fields it grows — and nothing is mutated.
    """
    if isinstance(data, SoftError):
        return _error_result(dict(data))
    return data


def _tool_errors(fn):
    """Failure contract for API-hitting tools, defined once.

    Nothing escapes as a raw exception; every failure is an ``isError``
    result whose ``error`` field is one of:

    - ``auth`` — missing or rejected credentials (fix the key, don't retry)
    - ``payment_required`` — 402 with ``billing_url`` + x402 ``challenge``
    - ``timeout`` — wait_for_generation deadline (the gen keeps running)
    - ``failed`` — anything else; ``message`` has the safe cause

    Order sensitivity: interpretation happens in the api layer (401 →
    AuthError, 402 → PaymentRequiredError); this only shapes results.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except AuthError as exc:
            return _error_result({"error": "auth", "message": safe_error_message(exc)})
        except PaymentRequiredError as exc:
            # The backend's x402 PaymentRequired is merged at the TOP level
            # (x402Version, accepts, resource, extensions): the x402 SDK's MCP
            # client detects a payable result by `isError` + top-level
            # `accepts`, then re-calls this tool with the signed payment in
            # `_meta["x402/payment"]`. Our failure-contract fields come after
            # so `error` stays the discriminator (the x402 human text is in
            # `message`). `challenge` is kept for one release (deprecated
            # duplicate).
            return _error_result({
                **(exc.challenge or {}),
                "error": "payment_required",
                "message": f"{exc} — or pay via x402 (USDC on Base).",
                "billing_url": exc.billing_url or "https://app.maginary.ai/dashboard",
                "challenge": exc.challenge,
            })
        except TimeoutError as exc:
            return _error_result({"error": "timeout", "message": safe_error_message(exc)})
        except Exception as exc:
            return _error_result({"error": "failed", "message": safe_error_message(exc)})
    return wrapper


def _error_result(body: dict[str, Any]) -> CallToolResult:
    """Wrap a failure as an ``isError`` tool result.

    One shape for every tool failure so agent frameworks that branch on
    ``isError`` never mistake a failed call for a successful result.

    Tools returning this stay annotated ``-> dict[str, Any]``: that's the
    success shape, and it drives FastMCP's outputSchema/structuredContent for
    successes. A ``| CallToolResult`` union is rejected by FastMCP's
    func_metadata, while a CallToolResult *return* passes through untouched
    (verified on mcp 1.19.0 and 1.29.1) — so the annotation deliberately
    describes the success arm only; docstrings carry the failure shapes.
    """
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(body, indent=2))],
        structuredContent=body,
        isError=True,
    )


# ── x402 over MCP ─────────────────────────────────────────────────────────
#
# Payment rides INSIDE tool calls (x402 SDK `x402/mcp`): a payable failure is
# an isError result with top-level `accepts`; the client signs and re-calls
# the same tool with the payment in `_meta["x402/payment"]`; the receipt goes
# back in `_meta["x402/payment-response"]`. The MCP forwards, the backend
# settles. `_meta["maginary/api_key"]` is our own carrier for a key obtained
# mid-session (keyless x402 returns one on the first settlement).

MCP_PAYMENT_META_KEY = "x402/payment"
MCP_PAYMENT_RESPONSE_META_KEY = "x402/payment-response"
MCP_API_KEY_META_KEY = "maginary/api_key"


def _request_meta(ctx: Context | None) -> dict[str, Any]:
    """The `_meta` of the current tool call, {} when absent (stdio, tests)."""
    if ctx is None:
        return {}
    try:
        meta = ctx.request_context.meta
    except (ValueError, AttributeError):
        return {}
    return dict(getattr(meta, "model_extra", None) or {}) if meta is not None else {}


def _with_receipt(record: dict[str, Any]) -> dict[str, Any] | CallToolResult:
    """A settled generation carries its on-chain receipt in `_meta` (SDK) and
    in `structuredContent.x402_receipt` (clients that ignore meta)."""
    receipt = record.get("x402_receipt")
    if not receipt:
        return record
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(record, indent=2))],
        structuredContent=record,
        isError=False,
        _meta={MCP_PAYMENT_RESPONSE_META_KEY: receipt},
    )


_VIDEO_EXTENSIONS = (".mp4", ".mov", ".webm", ".avi")


def _with_images(record: dict[str, Any]) -> dict[str, Any] | CallToolResult:
    """Embed output images as ImageContent for inline display in MCP clients.

    Only fires for done generations with image URLs.  Videos are skipped
    (no MCP ImageContent equivalent).  Degrades silently to URL-only on
    fetch failure.
    """
    if str(record.get("processing_state", "")).lower() != "done":
        return record

    urls = [
        u for u in (record.get("image_urls") or [])
        if not any(u.split("?")[0].lower().endswith(ext) for ext in _VIDEO_EXTENSIONS)
    ]
    if not urls:
        return record

    images: list[ImageContent] = []
    for url in urls:
        result = fetch_image(url)
        if result:
            data, mime = result
            images.append(ImageContent(
                type="image",
                data=base64.b64encode(data).decode(),
                mimeType=mime,
            ))

    if not images:
        return record

    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(record, indent=2)), *images],
        structuredContent=record,
        isError=False,
    )


@mcp.tool()
@_tool_errors
def generate(prompt: str, callback_url: str | None = None, ctx: Context | None = None) -> dict[str, Any]:
    """Kick off a generation via POST /api/gens/.

    Args:
        prompt: The full prompt string, including any ``--flag`` parameters.
            E.g. ``"a fox in autumn foliage --ar 16:9 --flagship"``.
            Flags go at the END of the prompt. The ones people need most:
            ``--1`` / ``--2`` / ``--3`` / ``--4`` = how many images (default 4;
            only specify if the user asks for a specific count),
            ``--ar 16:9`` = aspect ratio, ``--v <model>`` = model. Anything
            else: call ``get_parameter(name)`` or ``search_parameters``
            first — never guess a flag.

            **Image-to-image (img2img):** Place one or more public image URLs
            in the prompt, followed by editing instructions:
            ``"https://cdn.example.com/photo.webp reimagine as oil painting --ar 16:9"``
            The engine extracts URLs automatically and switches to img2img mode.
            Multiple URLs trigger multi-input mode (compositing/combining).
            Use ``upload_image`` first if images aren't already hosted.

            **Image-to-video:** Place an image URL in the prompt AND add
            ``--mp4`` plus video flags (``--5sec``, ``--1080p``). Or use
            ``execute_action`` with ``action_type="img2vid_basic"`` on a
            completed generation's image.

            **Style reference (--sref) is NOT img2img:** ``--sref <url>``
            copies the visual *style* of a reference image (colors, mood,
            composition) without using the image content as input. A bare URL
            in the prompt edits the actual image; ``--sref`` transfers style.

        callback_url: Optional HTTPS URL that will receive a webhook when the
            generation reaches done / failed. See
            https://maginary.ai/blog/webhooks-guide for signature verification.

    Returns:
        On success, the created generation record. Key fields: ``uuid`` (use
        to poll), ``action_type``, ``processing_state``,
        ``expected_output_count``.

        On failure, an ``isError`` result instead (nothing is raised), with a
        JSON body whose ``error`` field is one of:

        - ``"auth"`` — no/invalid API key. Surface the message directly to
          the human.
        - ``"payment_required"`` — out of credits. The body carries
          ``billing_url`` and ``challenge``: either send the human to
          ``billing_url`` to top up, or pay programmatically via x402 —
          ``challenge`` is the standard x402 payment-required payload (USDC
          on Base); settle it and retry this call.
        - ``"failed"`` — anything else (invalid prompt, rate limit, backend
          or network error); see ``message``.

        x402 over MCP: a ``payment_required`` result also carries the x402
        fields at the top level (``accepts``, ``resource``); an x402-capable
        client signs ``accepts[0]`` and calls this tool again with the payment
        in ``_meta["x402/payment"]``. The settled call returns the generation
        with ``x402_receipt`` (and ``_meta["x402/payment-response"]``); a
        wallet's first settlement also returns ``x402_account`` with an API
        key — pass it as ``_meta["maginary/api_key"]`` on later calls (or as
        the Authorization header of a new connection).
    """
    meta = _request_meta(ctx)
    payment = meta.get(MCP_PAYMENT_META_KEY)
    with override_api_key(meta.get(MCP_API_KEY_META_KEY)):
        record = create_generation(
            prompt=prompt, callback_url=callback_url,
            payment=payment if isinstance(payment, dict) else None)
    return _with_receipt(record)


def _append_dsl_map_to_tool(name: str) -> None:
    """Put the generated flag index into a tool's description (the docstring is
    static; the map is built from the snapshot at import)."""
    tool = mcp._tool_manager.get_tool(name)
    tool.description = (tool.description or "").rstrip() + \
        "\n\nEvery flag that exists, and its state: " + _DSL_MAP


_append_dsl_map_to_tool("generate")


@mcp.tool()
@_tool_errors
def get_generation(uuid: str, ctx: Context | None = None) -> dict[str, Any]:
    """Fetch a generation by UUID (GET /api/gens/{uuid}/).

    Args:
        uuid: The UUID returned by ``generate``.

    Returns:
        The full generation record. If terminal, ``image_urls[]`` holds the
        finished outputs and ``processing_result.slots[]`` the per-slot detail.
        NOTE: a generation that failed server-side is a SUCCESSFUL tool call
        returning ``processing_state: "failed"`` — always check the state,
        never infer success from the absence of a tool error.

        **Follow-up actions:** A completed generation's
        ``processing_result.available_actions`` maps slot indices to valid
        action types. E.g. ``{"0": ["upscale_2x", "vary_strong", ...],
        "global": ["reroll"]}``. Use ``execute_action`` with the ``uuid``,
        a chosen ``action_type``, and the ``parent_image_index`` (the slot
        key as an int) to run an action.

        Hosted: a key obtained mid-session may be passed as
        ``_meta["maginary/api_key"]``.
    """
    with override_api_key(_request_meta(ctx).get(MCP_API_KEY_META_KEY)):
        return _with_images(api_get_generation(uuid))


@mcp.tool()
@_tool_errors
def wait_for_generation(uuid: str, timeout_s: float = DEFAULT_WAIT_TIMEOUT_S,
                        ctx: Context | None = None) -> dict[str, Any]:
    """Poll ``get_generation`` on a backoff until it reaches done / failed.

    Args:
        uuid: The UUID returned by ``generate``.
        timeout_s: Return after this many seconds even if still running.
            Default 45 stays under the 60 s per-call limit most MCP clients
            enforce; a ``timeout`` result just means "call again". Only raise
            it (e.g. for video) on clients you know allow long tool calls.

    Returns:
        The terminal generation record — which includes generations that
        failed server-side: those are SUCCESSFUL tool calls returning
        ``processing_state: "failed"`` with empty ``image_urls``, so always
        check the state. On tool failure, an ``isError`` result whose
        ``error`` field is ``"timeout"`` (``message`` names the last
        observed state — the generation keeps running server-side and can be
        re-fetched with ``get_generation`` later), ``"auth"``, or
        ``"failed"``.

        **Follow-up actions:** A ``done`` generation's
        ``processing_result.available_actions`` maps slot indices to valid
        action types — e.g. ``{"0": ["upscale_2x", "vary_strong",
        "pan_left", "zoom_out_2x", "img2vid_basic", ...], "global":
        ["reroll"]}``. Use ``execute_action`` with the ``uuid``, a chosen
        ``action_type``, and the ``parent_image_index`` (the slot key as an
        int) to run an action on a specific output image.
    """
    with override_api_key(_request_meta(ctx).get(MCP_API_KEY_META_KEY)):
        return _with_images(api_wait_for_generation(uuid, timeout_s=timeout_s))


@mcp.tool()
@_tool_errors
def upload_image(image_base64: str, filename: str, ctx: Context | None = None) -> dict[str, Any]:
    """Upload an image to get a public URL for use in img2img prompts.

    Use this when the user's image isn't already at a public URL — the
    engine needs a URL to fetch the image from.

    Args:
        image_base64: The image file contents, base64-encoded.  Accepts
            PNG, JPEG, or WebP.
        filename: Original filename (e.g. ``"photo.png"``).  Used for
            Content-Disposition; the backend re-encodes to WebP regardless.

    Returns:
        Dict with ``url`` (the public CDN URL to use in a prompt or with
        ``--sref``), ``exists`` (true if the same image was already
        uploaded — deduplication by MD5), ``credits_deducted`` (upload
        credits used), and ``message``.

        On failure, an ``isError`` result — ``"auth"`` (no key),
        ``"payment_required"`` (no upload credits; same x402 flow as
        ``generate``), or ``"failed"``.
    """
    file_data = base64.b64decode(image_base64)
    with override_api_key(_request_meta(ctx).get(MCP_API_KEY_META_KEY)):
        return api_upload_image(file_data=file_data, filename=filename)


@mcp.tool()
@_tool_errors
def execute_action(
    generation_uuid: str,
    action_type: str,
    parent_image_index: int | None = None,
    prompt: str | None = None,
    callback_url: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Run a follow-up action on a completed generation's image.

    After ``generate`` → ``wait_for_generation``, the response's
    ``processing_result.available_actions`` lists what's possible per slot.
    Call this tool with one of those action types.

    Args:
        generation_uuid: UUID of the parent generation (from ``generate``).
        action_type: One of the values from ``available_actions`` — e.g.
            ``"upscale_2x"``, ``"upscale_1_5x"``, ``"vary_strong"``,
            ``"vary_subtle"``, ``"pan_left"``, ``"pan_right"``,
            ``"pan_up"``, ``"pan_down"``, ``"zoom_out_2x"``,
            ``"zoom_out_1_5x"``, ``"img2vid_basic"``, ``"reroll"``.
        parent_image_index: The slot index of the image to act on (0, 1,
            2, or 3 for a 4-image grid). Required for per-slot actions;
            omit for ``"reroll"`` (global action).
        prompt: Optional replacement prompt. For ``vary_*`` you can steer
            the variation with a new prompt; for ``img2vid_basic`` you can
            describe the desired motion.
        callback_url: Optional webhook URL (same as ``generate``).

    Returns:
        The newly created child generation record (same shape as
        ``generate``'s return — poll it with ``wait_for_generation``).

        On failure, same ``isError`` contract as ``generate``:
        ``"auth"``, ``"payment_required"`` (with x402 challenge),
        or ``"failed"``.
    """
    meta = _request_meta(ctx)
    payment = meta.get(MCP_PAYMENT_META_KEY)
    with override_api_key(meta.get(MCP_API_KEY_META_KEY)):
        record = api_execute_action(
            generation_uuid=generation_uuid,
            action_type=action_type,
            parent_image_index=parent_image_index,
            prompt=prompt,
            callback_url=callback_url,
            payment=payment if isinstance(payment, dict) else None,
        )
    return _with_receipt(record)


# ─── Onboarding tools (no API key required) ──────────────────────────────


@mcp.tool()
@_tool_errors
def create_account(email: str) -> dict[str, Any]:
    """Create a new Maginary account for the given email address.

    Returns the auto-generated password — display it to the user ONCE so they
    can save it. A verification email is sent; the user must click the link
    before the account can generate images.

    After verification, use ``manage_api_key(action='create')`` with
    ``email`` + ``password`` to get an API key, then ``configure_api_key``
    to activate it.

    Args:
        email: The user's email address.

    Returns:
        Dict with ``email``, ``password``, and ``message``. On failure, an
        ``isError`` result — e.g. ``error: "already_exists"`` (email taken:
        ask the user for their password or a different email),
        ``"rate_limited"``, or ``"failed"``.
    """
    return _as_result(register_account(email))


@mcp.tool()
@_tool_errors
def check_account_status(
    email: str | None = None,
    password: str | None = None,
) -> dict[str, Any]:
    """Check account verification status, credit balance, and API key count.

    Use this after ``create_account`` to poll whether the user has clicked the
    verification link. Pass ``email`` + ``password`` (from ``create_account``)
    for Basic auth, or omit both to use the configured API key.

    Args:
        email: Account email (for Basic auth).
        password: Account password (for Basic auth).

    Returns:
        Dict with ``verified`` (bool), ``email``, ``api_key_count``,
        ``credits_remaining``, ``uploads_remaining``.
    """
    return _as_result(get_account_status(email, password))


# ─── API key management tools ───────────────────────────────────────────


@mcp.tool()
@_tool_errors
def manage_api_key(
    action: str,
    name: str | None = None,
    key_prefix: str | None = None,
    email: str | None = None,
    password: str | None = None,
) -> dict[str, Any]:
    """Create, list, or revoke Maginary API keys (up to 10 per account).

    Auth: pass ``email`` + ``password`` for Basic auth (onboarding), or omit
    both to use the configured API key (normal operation).

    Args:
        action: One of ``create``, ``list``, ``revoke``.
        name: Key name (required for ``create``).
        key_prefix: 8-char prefix of the key to revoke (required for ``revoke``).
        email: Account email (for Basic auth).
        password: Account password (for Basic auth).

    Returns:
        For ``create``: dict with ``raw_key`` (the full key — show once, then
        use ``configure_api_key`` to activate it), ``key_prefix``, ``name``.
        For ``list``: dict with ``keys`` array.
        For ``revoke``: success/error message.
    """
    if action == "create":
        if not name:
            return _error_result({"error": "validation", "message": "name is required for create"})
        return _as_result(api_create_api_key(name, email, password))
    if action == "list":
        return _as_result(api_list_api_keys(email, password))
    if action == "revoke":
        if not key_prefix:
            return _error_result({"error": "validation", "message": "key_prefix is required for revoke"})
        return _as_result(api_revoke_api_key(key_prefix, email, password))
    return _error_result({"error": "validation", "message": f"Unknown action {action!r}. Use create/list/revoke."})


@mcp.tool()
@_tool_errors
def configure_api_key(api_key: str) -> dict[str, Any]:
    """Activate an API key. Local (stdio) servers persist it; hosted does not.

    Call this after ``manage_api_key(action='create')`` returns a ``raw_key``.
    On a local server the key is saved to ``~/.config/maginary/api_key``
    (chmod 600) and survives restarts. On the hosted server
    (mcp.maginary.ai) nothing can be stored — auth is per-request: the
    response will say ``persisted: false`` and the key must be sent as an
    ``Authorization: Bearer <key>`` header on every request (set it in the
    MCP client's connection config).

    Args:
        api_key: The full API key string returned by ``manage_api_key``.

    Returns:
        Confirmation dict.
    """
    if not api_key or len(api_key) < 16:
        return _error_result({"error": "validation", "message": "Invalid API key format"})
    if is_hosted_mode():
        # persist_api_key deliberately no-ops here (per-request tenancy) —
        # claiming "persisted" would send the agent into an auth failure on
        # its next generate call.
        return {
            "success": True,
            "persisted": False,
            "message": (
                "This hosted server stores nothing: authentication is "
                "per-request. Send the key on every request as an "
                "`Authorization: Bearer <key>` header (set it in the MCP "
                "client's connection config)."
            ),
        }
    persist_api_key(api_key)
    return {
        "success": True,
        "persisted": True,
        "message": "API key configured for this session and persisted.",
    }


# ─── Payment tools ───────────────────────────────────────────────────────


@mcp.tool()
@_tool_errors
def get_products() -> dict[str, Any]:
    """List available Maginary products/plans with pricing.

    No authentication required. Use this to present purchase options to the
    user. The ``novice_pack`` ($10, 150 credits) is the recommended starting
    point.

    Returns:
        Dict with ``count`` and ``products`` — each product carries ``id``,
        ``short_name``, ``title``, ``description``, ``price_cents``,
        ``credits``, ``uploads``, ``is_subscription``. (The backend sends a
        bare array; it is wrapped here because FastMCP validates tool output
        against the dict annotation and rejects a top-level list.)
    """
    products = api_list_products()
    return {"count": len(products), "products": products}


@mcp.tool()
@_tool_errors
def checkout(
    product_id: int,
    email: str | None = None,
    password: str | None = None,
) -> dict[str, Any]:
    """Create a Stripe checkout session for purchasing a product.

    Returns a ``checkout_url`` — the user must open it in a browser to
    complete payment. After payment, credits are provisioned automatically
    via webhook.

    If the agent has a USDC wallet, skip this entirely — just call
    ``generate`` and the x402 protocol handles payment on-chain.

    Args:
        product_id: Product ID from ``get_products``.
        email: Account email (for Basic auth during onboarding).
        password: Account password (for Basic auth during onboarding).

    Returns:
        Dict with ``checkout_url``. On failure, an ``isError`` result — e.g.
        ``error: "email_not_verified"`` until the user clicks the
        verification link, or ``"auth"`` / ``"failed"``.
    """
    return _as_result(api_create_checkout(product_id, email, password))


@mcp.tool()
@_tool_errors
def get_balance(
    email: str | None = None,
    password: str | None = None,
) -> dict[str, Any]:
    """Check remaining credits and uploads for the authenticated account.

    Args:
        email: Account email (for Basic auth).
        password: Account password (for Basic auth).

    Returns:
        Dict with ``credits_remaining`` and ``uploads_remaining``.
    """
    acct = get_account_status(email, password)
    return {
        "credits_remaining": acct.get("credits_remaining", 0),
        "uploads_remaining": acct.get("uploads_remaining", 0),
    }


# ─── Entrypoint ───────────────────────────────────────────────────────────


def main() -> None:
    """Run the MCP server over stdio. Called from the console script."""
    if "--install-skill" in sys.argv:
        from .skill import SkillExistsError, install_skill

        try:
            print(f"installed Agent Skill -> {install_skill(force='--force' in sys.argv)}")
        except (SkillExistsError, OSError) as exc:
            # OSError: unwritable skills dir, SKILL.md path occupied by a
            # directory, etc. — one readable line beats a traceback.
            raise SystemExit(f"could not install skill: {exc}")
        return

    logging.basicConfig(
        level=os.environ.get("MAGINARY_MCP_LOG_LEVEL", "INFO").upper(),
        # MCP wants stdout to be pure JSON-RPC — log to stderr.
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    LOG.info("maginary-mcp %s starting on stdio", __version__)
    mcp.run()
