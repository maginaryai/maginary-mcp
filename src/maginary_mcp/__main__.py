"""Entrypoint: ``python -m maginary_mcp`` or ``maginary-mcp`` (console script).

Both paths land here. Delegates to :mod:`maginary_mcp.server` which does the
actual MCP wiring. Kept trivial so the ``console_scripts`` binding in
``pyproject.toml`` never needs to change.
"""

from __future__ import annotations

from .server import main


if __name__ == "__main__":  # pragma: no cover
    main()
