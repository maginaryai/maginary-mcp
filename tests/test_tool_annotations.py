"""Every tool carries a title and honest MCP annotations.

Clients auto-approve `readOnlyHint` tools and always confirm `destructiveHint`
ones, so a wrong hint is a UX bug (nagging) or a safety bug (silent writes).
"""
import asyncio

import pytest

from maginary_mcp.server import mcp

READ_ONLY = {
    "list_parameters", "search_parameters", "get_parameter",
    "get_generation", "wait_for_generation",
    "check_account_status", "get_products", "get_balance",
}
DESTRUCTIVE = {"manage_api_key", "create_wallet_account"}  # both can revoke/rotate a live key
SPENDS_OR_CREATES = {
    "generate", "execute_action", "upload_image",
    "create_account", "checkout", "configure_api_key",
}


@pytest.fixture(scope="module")
def tools():
    return {t.name: t for t in asyncio.run(mcp.list_tools())}


def test_every_tool_is_classified(tools):
    assert set(tools) == READ_ONLY | DESTRUCTIVE | SPENDS_OR_CREATES


def test_every_tool_has_title_and_annotations(tools):
    for t in tools.values():
        assert t.title, t.name
        assert t.annotations is not None, t.name
        assert t.annotations.readOnlyHint is not None, t.name
        assert t.annotations.destructiveHint is not None, t.name


def test_read_only_tools_are_flagged_read_only(tools):
    for name in READ_ONLY:
        assert tools[name].annotations.readOnlyHint is True, name
        assert tools[name].annotations.destructiveHint is False, name


def test_writing_tools_are_not_read_only(tools):
    for name in DESTRUCTIVE | SPENDS_OR_CREATES:
        assert tools[name].annotations.readOnlyHint is False, name
    for name in DESTRUCTIVE:
        assert tools[name].annotations.destructiveHint is True, name
    for name in SPENDS_OR_CREATES:
        assert tools[name].annotations.destructiveHint is False, name


def test_catalog_tools_are_closed_world(tools):
    # They read a bundled/cached catalog, never the open internet.
    for name in ("list_parameters", "search_parameters", "get_parameter"):
        assert tools[name].annotations.openWorldHint is False, name
