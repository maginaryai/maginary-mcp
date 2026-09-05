"""Non-JSON 4xx bodies must still yield a reason in the tool error message.

Regression for prod: the backend answered Django's bare ``DisallowedHost``
400 (HTML) because the compose-internal host wasn't allowed, and the agent saw
only ``HTTP 400 from /api/gens/`` with nothing to act on.
"""
import httpx

from maginary_mcp import api


def _err(status: int, body: str, content_type: str) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "http://backend:8000/api/gens/")
    resp = httpx.Response(status, content=body.encode(), headers={"content-type": content_type}, request=req)
    return httpx.HTTPStatusError("x", request=req, response=resp)


def test_html_4xx_surfaces_stripped_text():
    exc = _err(400, "<html><head><title>Bad Request (400)</title></head><body><h1>Bad Request (400)</h1></body></html>",
               "text/html")
    msg = api.safe_error_message(exc)
    assert msg.startswith("HTTP 400 from /api/gens/: ")
    assert "Bad Request (400)" in msg
    assert "<" not in msg


def test_json_detail_still_preferred():
    exc = _err(400, '{"prompt": ["This field is required."]}', "application/json")
    assert api.safe_error_message(exc) == "HTTP 400 from /api/gens/: prompt: This field is required."


def test_5xx_never_surfaces_body():
    exc = _err(500, "Traceback (most recent call last): secret", "text/plain")
    assert api.safe_error_message(exc) == "HTTP 500 from /api/gens/"


def test_empty_body_gives_bare_status():
    exc = _err(404, "", "text/plain")
    assert api.safe_error_message(exc) == "HTTP 404 from /api/gens/"
