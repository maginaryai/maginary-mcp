#!/bin/bash

# Check if the script is being sourced
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    echo "This script must be sourced (. _actvenv.sh), not executed directly."
    exit 1
fi

# Returns 0 (true) if running inside a Docker/Podman container, 1 (false) otherwise.
# Uses Linux cgroup/mountinfo; on macOS both files are absent so both greps fail → not in container.
# Defined here so every script that sources _actvenv.sh can call it without duplicating the logic.
_in_container() {
    grep -Eq "(docker|podman)" /proc/1/cgroup 2>/dev/null || \
    grep -q docker /proc/self/mountinfo 2>/dev/null
}

if ! _in_container; then
    # Not in a container — activate the local venv.
    # Use BASH_SOURCE so this works regardless of the caller's CWD.
    # Namespaced name + unset avoids polluting the caller's environment.
    _ACTVENV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    . "${_ACTVENV_DIR}/venv/bin/activate"
    unset _ACTVENV_DIR
fi
