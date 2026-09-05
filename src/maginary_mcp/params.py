"""Parameter catalog loader.

Two sources of truth, in priority order:

1. **Live fetch** from ``https://maginary.ai/docs/parameters.json`` on process
   start. The endpoint is prerendered + CDN-cached, so it's cheap. Timeout is
   short (5 s) so a bad network never blocks the MCP server startup.
2. **Bundled snapshot** shipped inside the wheel at
   ``maginary_mcp/parameters_snapshot.json``. Refreshed by the maintainer via
   :mod:`maginary_mcp.scripts.refresh_snapshot`.

Consumers (see :mod:`maginary_mcp.server`) hit :func:`get_catalog` and get back
whatever is available. If both sources fail (never observed, but coverable),
we return an empty catalog with a warning field so tools degrade to "no
parameter data" rather than crashing the whole server.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx


LOG = logging.getLogger(__name__)

LIVE_URL = "https://maginary.ai/docs/parameters.json"
SNAPSHOT_PATH = Path(__file__).parent / "parameters_snapshot.json"

# Populated on first call to get_catalog(); reused for the process lifetime.
_CACHE: dict[str, Any] | None = None


def _load_bundled_snapshot() -> dict[str, Any]:
    """Read the bundled snapshot. Raises FileNotFoundError if missing (dev only)."""
    with SNAPSHOT_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _fetch_live(timeout: float = 5.0) -> dict[str, Any]:
    """Fetch the live catalog. Returns the parsed JSON or raises httpx.HTTPError."""
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        resp = client.get(LIVE_URL, headers={"accept": "application/json"})
        resp.raise_for_status()
        return resp.json()


def get_catalog() -> dict[str, Any]:
    """Return the parameter catalog. Cached for the process lifetime.

    Wire order: try live first, fall back to the bundled snapshot on any
    error. Warnings are logged, never re-raised — the server should stay up
    even if the docs site is having a bad day.
    """
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    try:
        catalog = _fetch_live()
        catalog.setdefault("_source", "live")
        _CACHE = catalog
        return catalog
    except Exception as exc:  # noqa: BLE001 — deliberate: fall back on any error
        LOG.warning("live parameter fetch failed (%s); falling back to bundled snapshot", exc)

    try:
        catalog = _load_bundled_snapshot()
        catalog.setdefault("_source", "bundled-snapshot")
        _CACHE = catalog
        return catalog
    except Exception as exc:  # noqa: BLE001
        LOG.error("bundled snapshot unreadable (%s); returning empty catalog", exc)
        _CACHE = {
            "schema_version": 1,
            "_source": "empty-fallback",
            "counts": {"total": 0, "live": 0, "dead": 0, "reserved": 0, "byCategory": {}},
            "categories": {},
            "statuses": {},
            "parameters": [],
        }
        return _CACHE


# ─── Query helpers used by the MCP tool functions ─────────────────────────


def all_parameters() -> list[dict[str, Any]]:
    return get_catalog().get("parameters", [])


def availability_map() -> str:
    """One paragraph naming EVERY flag and its state, for the tool contract.

    Agents read tool descriptions and server instructions and almost nothing
    else, so the index of what exists (and what is partial or rejected) lives
    there; details stay on demand in `get_parameter`. Built from the BUNDLED
    snapshot at import — deterministic and offline (never `get_catalog()`,
    which may fetch live and must not block stdio startup). ~120 tokens.
    """
    params = _load_bundled_snapshot().get("parameters", [])
    by_status: dict[str, list[str]] = {"live": [], "mostly-dead": [], "unimplemented": []}
    for p in params:
        name = f"--{p['name']}"
        values = p.get("values") or []
        if values and all(isinstance(v, str) and v.startswith("--") for v in values):
            name += " (" + "/".join(values) + ")"  # e.g. --output-count (--1/--2/--3/--4)
        by_status.setdefault(p.get("status", "live"), []).append(name)
    live, partial, reserved = by_status["live"], by_status["mostly-dead"], by_status["unimplemented"]
    parts = [f"Flags, live ({len(live)}): " + ", ".join(live) + "."]
    if partial:
        parts.append(f"Partial ({len(partial)}, only some models honour them): " + ", ".join(partial) + ".")
    if reserved:
        parts.append(f"Reserved ({len(reserved)}, the parser rejects them): " + ", ".join(reserved) + ".")
    parts.append("Any other --flag is rejected with `Unrecognized parameter`. Details: `get_parameter(name)`.")
    return " ".join(parts)


def find_parameter(name: str) -> dict[str, Any] | None:
    """Case-insensitive lookup by canonical name or alias."""
    needle = name.lstrip("-").strip().lower()
    if not needle:
        return None
    for p in all_parameters():
        if p.get("name", "").lower() == needle:
            return p
        aliases = p.get("aliases") or []
        if any(a.lower() == needle for a in aliases):
            return p
    return None


def search_parameters(
    query: str = "",
    category: str | None = None,
    status: str | None = None,
    include_reserved: bool = True,
) -> list[dict[str, Any]]:
    """Case-insensitive text search across name / aliases / description.

    Filters:
    - ``category`` restricts to a single ParamCategory string (see the
      ``categories`` map from ``get_catalog()``).
    - ``status``   restricts to a single ParamStatus string. Post-2026-07-11
      the catalog only ever emits ``live``, ``mostly-dead``, or
      ``unimplemented``.
    - ``include_reserved=False`` drops ``unimplemented`` (parser-recognized-
      but-blocked) entries. ``mostly-dead`` is always kept — those still
      work on some models.
    """
    needle = query.strip().lower()
    results: list[dict[str, Any]] = []
    for p in all_parameters():
        if category and p.get("category") != category:
            continue
        if status and p.get("status") != status:
            continue
        if not include_reserved and p.get("status") == "unimplemented":
            continue
        if needle:
            haystack_parts = [
                p.get("name", ""),
                *(p.get("aliases") or []),
                p.get("desc", ""),
                p.get("category", ""),
                *(p.get("values") or []),
                *(p.get("examples") or []),
            ]
            haystack = " ".join(str(x) for x in haystack_parts).lower()
            if needle not in haystack:
                continue
        results.append(p)
    return results
