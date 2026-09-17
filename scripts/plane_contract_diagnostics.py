"""Print safe, offline Plane MCP contract diagnostics."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import tomllib
from typing import Any

from plane_api.manifest import validate_persisted_manifest

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
SKILL = ROOT / "skills/plane-mcp-operations/SKILL.md"
REGISTRY = ROOT / "src/plane_api/operation_registry.json"
SOURCE = ROOT / "src/plane_api/source_contract.json"


def diagnostics() -> dict[str, Any]:
    package_version = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]
    skill_text = SKILL.read_text(encoding="utf-8")
    skill_match = re.search(r'metadata:\n\s+version:\s+["\']([^"\']+)', skill_text)
    manifest = dict(validate_persisted_manifest(REGISTRY, SOURCE))
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    return {
        "status": "PASS",
        "package_version": package_version,
        "skill_version": skill_match.group(1) if skill_match else None,
        "contract_version": manifest["contract_version"],
        "contract_hash": manifest["contract_hash"],
        "operation_count": manifest["operation_count"],
        "http_operation_count": manifest["http_operation_count"],
        "mutation_count": manifest["mutation_count"],
        "method_counts": manifest["method_counts"],
        "outside_denominator": sorted((registry.get("snapshot") or {}).get("excluded_pages", {})),
        "provider_calls": 0,
        "live_mutations": 0,
        "secret_values_emitted": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()
    try:
        result = diagnostics()
    except Exception as error:
        result = {"status": "FAIL", "error_class": type(error).__name__, "provider_calls": 0, "live_mutations": 0}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True) if args.as_json else json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
