"""Verify the frozen Plane registry/source-contract parity offline."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from plane_api.manifest import validate_persisted_manifest

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "src/plane_api/operation_registry.json"
SOURCE = ROOT / "src/plane_api/source_contract.json"
EXPECTED_V034 = {
    "/api-reference/inbox-issue/add-inbox-issue",
    "/api-reference/inbox-issue/delete-inbox-issue",
    "/api-reference/inbox-issue/get-inbox-issue-detail",
    "/api-reference/inbox-issue/list-inbox-issues",
    "/api-reference/inbox-issue/update-inbox-issue-detail",
    "/api-reference/work-item-relations/create-work-item-relation",
    "/api-reference/work-item-relations/list-work-item-relations",
    "/api-reference/work-item-relations/remove-work-item-relation",
}


def verify(registry_path: Path = REGISTRY, source_path: Path = SOURCE) -> dict[str, Any]:
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    source = json.loads(source_path.read_text(encoding="utf-8"))
    manifest = dict(validate_persisted_manifest(registry_path, source_path))
    operations = registry["operations"]
    actions = {row["action"] for row in operations}
    registered_docs = {row["docs_path"] for row in operations}
    excluded = set((registry.get("snapshot") or {}).get("excluded_pages") or {})
    missing_v034 = sorted(EXPECTED_V034 - registered_docs)
    stale_v034_exclusions = sorted(EXPECTED_V034 & excluded)
    mutation_actions = {row["action"] for row in operations if row.get("mutation") is True}
    contract_actions = {row["action"] for row in registry["mutation_contracts"]}
    schema_gaps = sorted(
        row["action"]
        for row in operations
        if row.get("docs_path") in EXPECTED_V034
        and (not row.get("request_schema", {}).get("available")
        or (not row.get("response_schema", {}).get("available")
            and row.get("response_schema", {}).get("shape") != "empty")
        or not row.get("schema_provenance"))
    )
    errors = []
    if missing_v034:
        errors.append("missing v0.3.4 documented endpoints")
    if stale_v034_exclusions:
        errors.append("v0.3.4 endpoints remain outside the registry denominator")
    if actions != {row["action"] for row in source["operations"]}:
        errors.append("source operation action set differs from registry")
    if mutation_actions != contract_actions:
        errors.append("mutation action set differs from mutation contracts")
    if schema_gaps:
        errors.append("one or more operations lack complete schema provenance")
    return {
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
        "manifest": manifest,
        "counts": {
            "operations": len(operations),
            "http_operations": registry["http_operation_count"],
            "mutations": len(mutation_actions),
            "get": registry["method_counts"]["GET"],
            "post": registry["method_counts"]["POST"],
            "patch": registry["method_counts"]["PATCH"],
            "delete": registry["method_counts"]["DELETE"],
        },
        "v034": {
            "registered": sorted(EXPECTED_V034 & registered_docs),
            "missing": missing_v034,
            "stale_exclusions": stale_v034_exclusions,
        },
        "outside_denominator": sorted(excluded),
        "schema_gaps": schema_gaps,
        "registry_byte_sha256": source.get("registry_sha256"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()
    try:
        result = verify()
    except Exception as error:
        result = {"status": "FAIL", "errors": [type(error).__name__]}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True) if args.as_json else json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
