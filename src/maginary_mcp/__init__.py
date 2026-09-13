"""Maginary MCP server — expose the Maginary image / video generation API as MCP tools.

See ``server.py`` for the tool definitions.
"""

from importlib.metadata import PackageNotFoundError, version

# pyproject.toml is the single source of truth for the version (the publish
# script bumps it and syncs server.json). Read it from the installed
# distribution so the human page, logs and the server card never drift.
try:
    __version__ = version("maginary-mcp")
except PackageNotFoundError:  # source checkout without `pip install -e .`
    __version__ = "0.0.0+dev"
