"""Auth-header extraction for the hosted HTTP MCP server.

The ASGI middleware pulls the caller's key out of the raw ASGI header list
(list of (bytes, bytes)) and binds it per-request. Only a well-formed
`Authorization: Bearer <key>` yields a key; anything else yields None (the
tool then raises AuthError, same as a missing env key in stdio mode).
"""
from maginary_mcp.http_app import _bearer_from_headers


class TestBearerFromHeaders:

    def test_extracts_bearer_token(self):
        headers = [(b"authorization", b"Bearer mgk_abc123")]
        assert _bearer_from_headers(headers) == "mgk_abc123"

    def test_case_insensitive_scheme(self):
        headers = [(b"authorization", b"bearer mgk_abc123")]
        assert _bearer_from_headers(headers) == "mgk_abc123"

    def test_no_authorization_header(self):
        assert _bearer_from_headers([(b"content-type", b"application/json")]) is None

    def test_non_bearer_scheme_ignored(self):
        headers = [(b"authorization", b"Basic dXNlcjpwYXNz")]
        assert _bearer_from_headers(headers) is None

    def test_malformed_header_yields_none(self):
        assert _bearer_from_headers([(b"authorization", b"Bearer")]) is None
