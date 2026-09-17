"""Gate package/skill/registry identity before a Plane MCP release."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import tomllib
from typing import Any

from plane_api.manifest import validate_persisted_manifest

ROOT = Path(__file__).resolve().parents[1]


def check(expected_version: str) -> dict[str, Any]:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_version = str(pyproject["project"]["version"])
    skill_text = (ROOT / "skills/plane-mcp-operations/SKILL.md").read_text(encoding="utf-8")
    skill_match = re.search(r'metadata:\n\s+version:\s+["\']([^"\']+)', skill_text)
    skill_version = skill_match.group(1) if skill_match else None
    manifest = dict(validate_persisted_manifest())
    release_path = ROOT / f"RELEASE-{expected_version}.md"
    checks = {
        "package_version": package_version == expected_version,
        "skill_version": skill_version == expected_version,
        "release_notes": release_path.is_file(),
        "registry_contract": manifest["operation_count"] == 233 and manifest["mutation_count"] == 139,
        "source_contract": bool(manifest["contract_hash"]),
    }
    errors = [name for name, value in checks.items() if value is False]
    return {
        "status": "PASS" if not errors else "FAIL",
        "expected_version": expected_version,
        "package_version": package_version,
        "skill_version": skill_version,
        "manifest": manifest,
        "checks": checks,
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="0.3.4")
    args = parser.parse_args()
    try:
        result = check(args.version)
    except Exception as error:
        result = {"status": "FAIL", "error_class": type(error).__name__, "errors": [type(error).__name__]}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
