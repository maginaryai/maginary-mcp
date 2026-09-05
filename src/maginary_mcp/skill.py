"""Install the bundled Agent Skill into the user's skills directory.

The skill teaches the `--flag` DSL and the generate→poll flow (MCP tools when
connected, raw REST otherwise). The canonical SKILL.md lives inside this
package (next to ``parameters_snapshot.json``) so wheels and editable
installs resolve it the same way — no reaching outside the package.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

SKILL_NAME = "maginary-image-gen"
BUNDLED_SKILL = Path(__file__).parent / "SKILL.md"
DEFAULT_SKILLS_DIR = Path.home() / ".claude" / "skills"

# Records the sha256 of the content *we* last installed, so upgrades can tell
# "unedited old version" (safe to overwrite) from "user's local edits" (refuse
# without force). Dotfile — skill loaders only read SKILL.md.
_HASH_FILE = ".maginary-installed-sha256"


class SkillExistsError(RuntimeError):
    """Target SKILL.md differs from anything we wrote and ``force`` is False."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def install_skill(skills_dir: Path | None = None, force: bool = False) -> Path:
    """Copy the bundled SKILL.md into ``skills_dir`` and return its new path.

    Re-running is always safe: an unedited install (matches the recorded hash
    of what we last wrote) is upgraded in place, while a locally modified copy
    is refused unless ``force`` is True.
    """
    content = BUNDLED_SKILL.read_bytes()
    target_dir = (skills_dir or DEFAULT_SKILLS_DIR) / SKILL_NAME
    target = target_dir / "SKILL.md"
    hash_file = target_dir / _HASH_FILE

    if target.exists() and not force:
        on_disk = _sha256(target.read_bytes())
        recorded = hash_file.read_text().strip() if hash_file.is_file() else None
        if on_disk not in (recorded, _sha256(content)):
            raise SkillExistsError(
                f"{target} differs from the bundled skill — local edits, or a "
                "copy from an older/manual install. Re-run with --force to "
                "overwrite it."
            )

    target_dir.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    hash_file.write_text(_sha256(content) + "\n")
    return target
