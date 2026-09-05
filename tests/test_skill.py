"""Agent Skill installer (``maginary-mcp --install-skill``).

The bundled SKILL.md must ship inside the package and install idempotently:
fresh installs write it, identical re-installs no-op, locally modified copies
are protected unless force=True.
"""
import hashlib

import pytest

from maginary_mcp.skill import BUNDLED_SKILL, SkillExistsError, install_skill


class TestBundledSkill:

    def test_ships_inside_package(self):
        assert BUNDLED_SKILL.is_file()
        assert BUNDLED_SKILL.read_text().startswith("---\nname: maginary-image-gen")

    def test_documents_rest_fallback(self):
        # The skill must stand alone without MCP — REST endpoints included.
        assert "app.maginary.ai/api/gens/" in BUNDLED_SKILL.read_text()


class TestInstallSkill:

    def test_fresh_install_writes_skill(self, tmp_path):
        target = install_skill(tmp_path)
        assert target == tmp_path / "maginary-image-gen" / "SKILL.md"
        assert target.read_bytes() == BUNDLED_SKILL.read_bytes()

    def test_reinstall_is_safe(self, tmp_path):
        first = install_skill(tmp_path)
        assert install_skill(tmp_path) == first
        assert first.read_bytes() == BUNDLED_SKILL.read_bytes()

    def test_upgrade_overwrites_unedited_old_version(self, tmp_path):
        # An older *official* install (matches its recorded hash) is not a
        # local edit — a new package version must upgrade it without --force.
        target = install_skill(tmp_path)
        old_official = b"---\nname: maginary-image-gen\n---\nold official v0.2"
        target.write_bytes(old_official)
        (tmp_path / "maginary-image-gen" / ".maginary-installed-sha256").write_text(
            hashlib.sha256(old_official).hexdigest()
        )
        assert install_skill(tmp_path) == target
        assert target.read_bytes() == BUNDLED_SKILL.read_bytes()

    def test_refuses_to_clobber_local_edits(self, tmp_path):
        target = install_skill(tmp_path)
        target.write_text("my precious local tweaks")
        with pytest.raises(SkillExistsError, match="--force"):
            install_skill(tmp_path)
        assert target.read_text() == "my precious local tweaks"

    def test_force_overwrites_local_edits(self, tmp_path):
        target = install_skill(tmp_path)
        target.write_text("my precious local tweaks")
        assert install_skill(tmp_path, force=True) == target
        assert target.read_bytes() == BUNDLED_SKILL.read_bytes()
