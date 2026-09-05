"""Shared fixtures.

`client`: ONE hosted-app TestClient for the whole session. The streamable-HTTP
session manager can be started once per FastMCP instance (a module singleton),
so every module that talks to the hosted app must share this client instead of
building its own. Behaviour that varies per test (auth gate, resource URL) is
read from the environment at request time, so monkeypatching env inside a
test still works against the shared client.
"""
import pytest
from starlette.testclient import TestClient


@pytest.fixture(scope="session")
def client():
    from maginary_mcp.http_app import build_app
    with TestClient(build_app()) as c:
        yield c
