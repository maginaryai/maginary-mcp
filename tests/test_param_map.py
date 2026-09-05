"""The parameter availability map: every flag and its state, in the contract.

Agents read the server instructions and tool descriptions and nothing else,
so the index of what exists must live there, generated from the bundled
snapshot so it cannot drift.
"""
import asyncio
import json

from maginary_mcp import params
from maginary_mcp.params import SNAPSHOT_PATH, availability_map
from maginary_mcp.server import mcp


def _snapshot():
    return json.load(open(SNAPSHOT_PATH))["parameters"]


def test_every_flag_appears_with_its_state():
    m = availability_map()
    for p in _snapshot():
        assert f"--{p['name']}" in m, p["name"]
    live = [p for p in _snapshot() if p["status"] == "live"]
    partial = [p for p in _snapshot() if p["status"] == "mostly-dead"]
    reserved = [p for p in _snapshot() if p["status"] == "unimplemented"]
    assert f"live ({len(live)})" in m
    assert f"Partial ({len(partial)}" in m
    assert f"Reserved ({len(reserved)}" in m
    # the reserved names sit in the reserved sentence, not the live one
    live_sentence = m.split("Partial")[0]
    for p in reserved:
        assert f"--{p['name']}" not in live_sentence


def test_output_count_shows_its_values_inline():
    assert "--output-count (--1/--2/--3/--4)" in availability_map()


def test_map_is_small_enough_to_live_in_every_context():
    assert len(availability_map()) < 1500  # ~350 tokens at most; it is ~120 today


def test_map_is_built_from_the_bundled_snapshot_not_a_live_fetch(monkeypatch):
    called = []
    monkeypatch.setattr(params, "get_catalog", lambda: called.append(1) or {"parameters": []})
    availability_map()
    assert called == []


def test_instructions_and_generate_description_carry_the_map():
    m = availability_map()
    assert m in mcp.instructions
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    assert "Reserved (" in tools["generate"].description
    assert "--output-count (--1/--2/--3/--4)" in tools["generate"].description
