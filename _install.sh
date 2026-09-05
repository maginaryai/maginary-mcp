#!/bin/bash

# DEV-ONLY local install. Uses pyenv (not in slim images), creates local venv/.
# Docker bypasses this entirely — see Dockerfile.

set -eou pipefail

cd "$(dirname "$0")"

echo "mcp: install"

./_ensurepyenv.sh

echo "mcp: install python via pyenv"
PY_VERS=$(cat .python-version)
if pyenv versions --bare | grep -Fx "$PY_VERS" >/dev/null; then
    echo "python $PY_VERS from .python-version already installed."
else
    echo "python $PY_VERS from .python-version not installed, installing"
    pyenv install
    echo "python $PY_VERS from .python-version installed"
fi
python --version

if [ ! -d venv ]; then
    echo "mcp: python venv"
    python -m venv venv
fi
. ./_actvenv.sh

echo "mcp: python reqs"
pip install -e '.[http]'
