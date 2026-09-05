#!/bin/bash

set -eou pipefail

cd "$(dirname "$0")"

. ./_actvenv.sh

python -c "import maginary_mcp" 2>/dev/null || {
    echo "maginary-mcp not installed, run: cd mcp && pip install -e '.[http]'" > /dev/stderr
    exit 1
}

export MAGINARY_BASE_URL=${MAGINARY_BASE_URL:-http://localhost:8000/api}

echo "mcp: starting streamable-http on port 8642"
echo "mcp: health → http://localhost:8642/health"

maginary-mcp-http
