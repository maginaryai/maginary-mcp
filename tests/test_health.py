"""Health endpoint for Docker/Traefik liveness checks."""
import pytest
from starlette.testclient import TestClient


@pytest.fixture
def client():
    from maginary_mcp.http_app import build_app
    return TestClient(build_app())


def test_health_returns_200(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_health_is_first_route():
    """Health route is inserted before MCP routes so it's always reachable."""
    from maginary_mcp.http_app import build_app
    app = build_app()
    paths = [r.path for r in app.routes]
    assert paths[0] == "/health"
    assert "/mcp" in paths
