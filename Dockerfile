# Hosted, multi-tenant Streamable-HTTP MCP server (mcp.maginary.ai).
# Runs alongside the engine on the same box; Django/agents reach it by URL.
# stdio distribution (uvx/pip) is unaffected — this image is HTTP-only.
FROM python:3.12-slim

WORKDIR /app

# Install the package with the `http` extra (pulls uvicorn). Copy metadata
# first for layer caching, then the source.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[http]"

# Backend REST base the tools forward to. Override at deploy time if the API
# lives elsewhere. The caller's own API key arrives per-request (Bearer header),
# so NO MAGINARY_API_KEY is set here — this server is multi-tenant.
ENV MAGINARY_BASE_URL="https://app.maginary.ai/api" \
    MAGINARY_MCP_HOST="0.0.0.0" \
    MAGINARY_MCP_PORT="8642"

EXPOSE 8642

# The MCP endpoint is served at /mcp. Front with the same reverse proxy as the
# engine, mapping mcp.maginary.ai -> this container:8642.
CMD ["maginary-mcp-http"]
