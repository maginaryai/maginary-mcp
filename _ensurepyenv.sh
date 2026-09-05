#!/bin/bash

set -eou pipefail

cd "$(dirname "$0")"

echo "mcp: Ensure pyenv is installed"

# Function to add pyenv config to shell rc file
add_pyenv_config() {
    [[ $# -eq 2 ]] || { echo "add_pyenv_config: expected 2 args, got $#" >&2; return 1; }
    local SHELL_CONFIG="$1"
    local INIT_PATH_CMD="$2"

    if [ -f "$SHELL_CONFIG" ] && ! grep -q 'export PYENV_ROOT="$HOME/.pyenv"' "$SHELL_CONFIG"; then
        echo "Adding pyenv config to $SHELL_CONFIG"
        echo 'export PYENV_ROOT="$HOME/.pyenv"' >> "$SHELL_CONFIG"
        echo 'command -v pyenv >/dev/null || export PATH="$PYENV_ROOT/bin:$PATH"' >> "$SHELL_CONFIG"
        if [ -n "$INIT_PATH_CMD" ]; then
            echo "$INIT_PATH_CMD" >> "$SHELL_CONFIG"
        fi
        echo 'eval "$(pyenv init -)"' >> "$SHELL_CONFIG"
    fi
}

# Function to make pyenv available in current shell
setup_pyenv_env() {
    [[ $# -eq 1 ]] || { echo "setup_pyenv_env: expected 1 arg, got $#" >&2; return 1; }
    local INIT_PATH_CMD="$1"
    export PYENV_ROOT="$HOME/.pyenv"
    export PATH="$PYENV_ROOT/bin:$PATH"
    if [ -n "$INIT_PATH_CMD" ]; then
        eval "$INIT_PATH_CMD"
    fi
    eval "$(pyenv init -)"
}

# Check if pyenv is installed
if ! command -v pyenv >/dev/null 2>&1; then
    echo "pyenv not found, installing..."
    if [[ "$OSTYPE" == "darwin"* ]]; then
        # macOS — requires Homebrew
        if ! command -v brew >/dev/null 2>&1; then
            echo "Error: Homebrew is required to install pyenv on macOS."
            echo "Install it from https://brew.sh, then re-run."
            exit 1
        fi
        brew install pyenv
        INIT_PATH='eval "$(pyenv init --path)"'
    else
        # Linux/Unix — downloads and runs the official pyenv installer.
        # WARNING: this is a curl-to-bash install. Review the script at
        # https://github.com/pyenv/pyenv-installer before running in sensitive envs.
        PYENV_INSTALLER=$(mktemp)
        trap 'rm -f "$PYENV_INSTALLER"' EXIT
        curl -fsSL https://pyenv.run -o "$PYENV_INSTALLER"
        bash "$PYENV_INSTALLER"
        rm -f "$PYENV_INSTALLER"
        trap - EXIT
        INIT_PATH=""
    fi

    # Add to shell configs
    add_pyenv_config "$HOME/.zshrc" "$INIT_PATH"
    add_pyenv_config "$HOME/.bashrc" "$INIT_PATH"

    # Make pyenv available in current shell
    setup_pyenv_env "$INIT_PATH"

    echo "pyenv installed successfully"
fi
