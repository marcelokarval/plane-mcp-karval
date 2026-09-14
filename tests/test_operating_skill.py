"""Offline proof for the portable skill and its global installer."""
import asyncio
import importlib.util
from pathlib import Path
import re

from fastmcp import Client
import pytest
import yaml

from plane_mcp_karval.server import create_server

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills/plane-mcp-operations"


def installer():
    spec = importlib.util.spec_from_file_location("skill_installer", ROOT / "scripts/install_operating_skill.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_skill_structure_and_resource_links():
    text = (SKILL / "SKILL.md").read_text()
    metadata = yaml.safe_load(text.split("---", 2)[1])
    assert metadata["name"] == SKILL.name
    assert metadata["metadata"]["version"] == "0.3.2"
    assert len(metadata["description"]) <= 1024
    assert len(text.splitlines()) < 220
    for link in re.findall(r"\]\((references/[^)]+)\)", text):
        assert (SKILL / link).is_file()
    assert len(list(SKILL.rglob("*.md"))) == 4


def test_skill_tools_and_transition_arguments_match_public_mcp():
    async def check():
        async with Client(create_server()) as client:
            return {tool.name: tool.inputSchema for tool in await client.list_tools()}
    schemas = asyncio.run(check())
    for name in ("plane_mutation_action", "plane_lifecycle_transition", "plane_add_comment",
                 "plane_reconcile_mutation", "plane_capture_state_catalog", "plane_add_lifecycle_comment"):
        assert name in schemas
        assert name in (SKILL / "SKILL.md").read_text() + (SKILL / "references/recipes.md").read_text()
    transition = schemas["plane_lifecycle_transition"]["properties"]
    assert {"target_state_id", "expected_state_id", "expected_updated_at", "idempotency_key"} <= transition.keys()
    required = set(schemas["plane_mutation_action"].get("required", []))
    assert not required.intersection({"authorization_receipt", "approved_live_mutation", "issue_readiness"})


@pytest.mark.parametrize("term", ["ADMIT", "non-atomic", "readback_verified", "idempotency", "404", "401/403/500"])
def test_operating_boundaries_are_documented(term):
    text = "\n".join(p.read_text() for p in SKILL.rglob("*.md"))
    assert term in text


def test_install_preview_apply_idempotence_and_backup(tmp_path):
    module = installer()
    destination = tmp_path / "skills/plane-mcp-operations"
    assert module.install(SKILL, destination, apply=False)["in_sync"] is False
    assert not destination.exists()
    assert module.install(SKILL, destination, apply=True)["in_sync"] is True
    assert module.install(SKILL, destination, apply=True)["applied"] is False
    (destination / "SKILL.md").write_text("old local copy")
    result = module.install(SKILL, destination, apply=True)
    backup = Path(result["backup"])
    assert (backup / "SKILL.md").read_text() == "old local copy"
    assert not backup.is_relative_to(destination.parent)
    assert module.manifest(SKILL) == module.manifest(destination)


def test_installer_rejects_symlinks(tmp_path):
    destination = tmp_path / "alias"
    destination.symlink_to(SKILL, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        installer().install(SKILL, destination, apply=True)
