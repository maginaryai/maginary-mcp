"""``wait_for_generation`` terminal-state detection.

The backend serializes ``processing_state`` lowercase ('done'/'failed' — Django
``Gen.ProcessingState`` TextChoices; frontend types agree). Polling must
terminate on those, case-insensitively, instead of timing out.
"""
import pytest

from maginary_mcp import api


def _poll_states(monkeypatch, states):
    seq = iter(states)
    monkeypatch.setattr(api, "get_generation", lambda uuid: {"processing_state": next(seq)})
    monkeypatch.setattr(api.time, "sleep", lambda s: None)


class TestTerminalStates:

    @pytest.mark.parametrize("terminal", ["done", "failed", "DONE", "FAILED"])
    def test_stops_on_terminal_state_any_case(self, monkeypatch, terminal):
        _poll_states(monkeypatch, ["queued", "running", terminal])
        gen = api.wait_for_generation("u1", timeout_s=60.0, initial_delay_s=0.0)
        assert gen["processing_state"] == terminal

    def test_times_out_while_still_running(self, monkeypatch):
        _poll_states(monkeypatch, ["running"] * 1000)
        with pytest.raises(TimeoutError):
            api.wait_for_generation("u1", timeout_s=0.0, initial_delay_s=0.0)


class TestClientTimeoutBudget:
    """Most MCP clients abort a tool call at 60 s (TypeScript SDK default), so
    the blocking wait must finish under that by default and never oversleep."""

    def test_default_timeout_under_60s(self):
        assert api.DEFAULT_WAIT_TIMEOUT_S < 60

    def test_tool_schema_advertises_the_same_default(self):
        import asyncio
        from maginary_mcp.server import mcp

        tool = next(t for t in asyncio.run(mcp.list_tools()) if t.name == "wait_for_generation")
        assert tool.inputSchema["properties"]["timeout_s"]["default"] == api.DEFAULT_WAIT_TIMEOUT_S

    def test_sleep_is_clamped_to_the_deadline(self, monkeypatch):
        clock = [0.0]
        sleeps: list[float] = []

        def fake_sleep(s):
            sleeps.append(s)
            clock[0] += s

        monkeypatch.setattr(api.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(api.time, "sleep", fake_sleep)
        monkeypatch.setattr(api, "get_generation", lambda uuid: {"processing_state": "running"})

        with pytest.raises(TimeoutError, match="call wait_for_generation"):
            api.wait_for_generation("u1", timeout_s=5.0, initial_delay_s=3.0)

        # 3 s backoff, then only the 2 s left — not a full 5 s step past the deadline.
        assert sleeps == [3.0, 2.0]
        assert clock[0] == 5.0

    def test_timeout_reaches_the_client_as_a_soft_error(self, monkeypatch):
        # Protocol-level: through mcp.call_tool(), not the raw function.
        import asyncio
        from maginary_mcp.server import mcp

        monkeypatch.setattr(api.time, "sleep", lambda s: None)
        monkeypatch.setattr(api, "get_generation", lambda uuid: {"processing_state": "running"})

        result = asyncio.run(mcp.call_tool("wait_for_generation", {"uuid": "u1", "timeout_s": 0}))
        assert result.isError is True
        assert result.structuredContent["error"] == "timeout"
        assert "call wait_for_generation" in result.structuredContent["message"]
