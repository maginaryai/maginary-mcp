#!/usr/bin/env python3
"""Refresh the bundled parameters snapshot from the live docs endpoint.

Run this whenever ``sharedpy/parameter_definitions.py`` or
``landing/src/lib/data/parameters.ts`` changes and you want a new snapshot in
the ``maginary-mcp`` wheel. Not run at wheel build time — deliberately
manual, so a new snapshot always corresponds to a reviewed commit.

Usage:
    python scripts/refresh_snapshot.py
    python scripts/refresh_snapshot.py --url http://localhost:5173/docs/parameters.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

DEFAULT_URL = "https://maginary.ai/docs/parameters.json"
TARGET = Path(__file__).resolve().parent.parent / "src" / "maginary_mcp" / "parameters_snapshot.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL, help="Endpoint to fetch (default: production)")
    parser.add_argument(
        "--target",
        default=str(TARGET),
        help="Where to write the snapshot (default: bundled path)",
    )
    args = parser.parse_args(argv)

    print(f"→ fetching {args.url}")
    resp = httpx.get(args.url, timeout=10.0, follow_redirects=True)
    resp.raise_for_status()
    catalog = resp.json()

    target_path = Path(args.target)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(json.dumps(catalog, indent=2), encoding="utf-8")

    total = len(catalog.get("parameters", []))
    print(f"✓ wrote {target_path} ({total} parameters, schema v{catalog.get('schema_version')})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
