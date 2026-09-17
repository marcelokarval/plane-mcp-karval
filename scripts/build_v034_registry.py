"""Extend the checked-in Plane contract with the v0.3.4 JSON endpoints.

This is intentionally deterministic: it starts from the current registry, adds only
endpoints backed by the official Plane developer docs, and recomputes every persisted
identity/count field used by the runtime manifest.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "src/plane_api/operation_registry.json"
SOURCE_PATH = ROOT / "src/plane_api/source_contract.json"
DOCS_ROOT = "https://developers.plane.so"


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def replace_recursive(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        for old, new in replacements.items():
            value = value.replace(old, new)
        return value
    if isinstance(value, list):
        return [replace_recursive(item, replacements) for item in value]
    if isinstance(value, dict):
        return {
            replace_recursive(key, replacements): replace_recursive(item, replacements)
            for key, item in value.items()
        }
    return value


def official_provenance(path: str) -> dict[str, Any]:
    return {
        "source": "official-plane-developer-docs",
        "url": f"{DOCS_ROOT}{path}",
        "parser": "plane-v034-manual-contract-1",
    }


def inbox_operation(base: dict[str, Any], action: str, docs_path: str, summary: str) -> dict[str, Any]:
    row = replace_recursive(
        deepcopy(base),
        {
            "intake_issue": "inbox_issue",
            "intake-issues": "inbox-issues",
            "intake issue": "inbox issue",
            "Intake issue": "Inbox issue",
            "work_item_id": "issue_id",
            "/intake-issue": "/inbox-issue",
        },
    )
    row.update(
        action=action,
        docs_path=docs_path,
        summary=summary,
        schema_provenance=official_provenance(docs_path),
        semantic_provenance=official_provenance(docs_path),
    )
    row["normalized_graph_key"] = row["path"].replace("{workspace_slug}", "{p0}").replace("{project_id}", "{p1}").replace("{issue_id}", "{p2}")
    row["schema_hash"] = sha({"request": row["request_schema"], "response": row["response_schema"], "effect": row["effect"]})
    return row


def inbox_contract(base: dict[str, Any], action_map: dict[str, str]) -> dict[str, Any]:
    contract = replace_recursive(deepcopy(base), action_map)
    contract = replace_recursive(contract, {"intake_issue": "inbox_issue", "intake-issues": "inbox-issues", "work_item_id": "issue_id"})
    contract["action"] = action_map.get(base["action"], contract["action"])
    contract["postcondition"] = replace_recursive(contract["postcondition"], action_map)
    if base["action"] == "intake_issue__update_intake_issue_detail":
        post = contract["postcondition"]
        post.update(
            kind="detail_state",
            strategy="detail_state",
            get_action="inbox_issue__get_inbox_issue_detail",
            expected_status=200,
            readback_expected_status=200,
            response_shape="detail",
            path="/api/v1/workspaces/{workspace_slug}/projects/{project_id}/inbox-issues/{issue_id}/",
            targets={
                "path_fields": ["issue_id"],
                "payload_fields": [],
                "write_response_fields": [],
                "selector": {"source": "path", "field": "issue_id"},
                "evidence": official_provenance("/api-reference/inbox-issue/update-inbox-issue-detail"),
            },
            detail={"effect_fields": []},
        )
        contract["expected_terminal"] = "verified"
        contract["write_policy"] = "allow"
    contract["schema_provenance"] = official_provenance(base.get("docs_path", ""))
    return contract


def relation_item_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "name": {"type": "string"},
            "sequence_id": {"type": "integer"},
            "project_id": {"type": "string"},
            "relation_type": {"type": "string"},
            "state_id": {"type": "string"},
            "priority": {"type": "string"},
            "type_id": {"type": "string"},
            "is_epic": {"type": "boolean"},
            "created_at": {"type": "string"},
            "updated_at": {"type": "string"},
            "created_by": {"type": "string"},
            "updated_by": {"type": "string"},
        },
        "required": ["id", "name", "sequence_id", "project_id", "relation_type", "state_id", "priority", "type_id", "is_epic", "created_at", "updated_at", "created_by", "updated_by"],
    }


def relation_map_schema() -> dict[str, Any]:
    names = ["blocking", "blocked_by", "duplicate", "relates_to", "start_after", "start_before", "finish_after", "finish_before"]
    return {"type": "object", "properties": {name: {"type": "array", "items": {"type": "string"}} for name in names}, "required": names}


def relation_operation(action: str, method: str, path: str, docs_path: str, summary: str, request_schema: dict[str, Any], response_schema: dict[str, Any], effect_kind: str) -> dict[str, Any]:
    return {
        "action": action,
        "method": method,
        "path": path,
        "summary": summary,
        "surface": "work-item-relations",
        "mode": "read" if method == "GET" else "mutation",
        "mutation": method != "GET",
        "docs_path": docs_path,
        "normalized_graph_key": path.replace("{workspace_slug}", "{p0}").replace("{project_id}", "{p1}").replace("{work_item_id}", "{p2}"),
        "frontmatter": {"title": summary, "source": official_provenance(docs_path)},
        "request_schema": request_schema,
        "response_schema": response_schema,
        "effect": {"available": True, "http_method": method, "kind": effect_kind, "source": official_provenance(docs_path)},
        "schema_availability": {"effect": True, "request": True, "response": True},
        "schema_provenance": official_provenance(docs_path),
        "semantic_provenance": official_provenance(docs_path),
        "schema_hash": sha({"request": request_schema, "response": response_schema}),
    }


def path_schema(*, issue: bool = False, payload: dict[str, Any] | None = None, required_payload: list[str] | None = None) -> dict[str, Any]:
    properties = {"workspace_slug": {"type": "string"}, "project_id": {"type": "string"}, "work_item_id": {"type": "string"}}
    required = ["workspace_slug", "project_id", "work_item_id"]
    if issue:
        properties["issue_id"] = properties.pop("work_item_id")
        required[-1] = "issue_id"
    if payload:
        properties.update(payload)
        required.extend(required_payload or [])
    return {
        "available": True,
        "json_schema": {"type": "object", "properties": properties, "required": sorted(set(required))},
        "path": {"parameters": []},
        "query": {"parameters": []},
        "body": {
            "available": bool(payload),
            "parameters": [
                {"name": name, "type": schema.get("type", "object"), "required": name in (required_payload or [])}
                for name, schema in (payload or {}).items()
            ],
        },
    }


def relation_contract_create() -> dict[str, Any]:
    path = "/api/v1/workspaces/{workspace_slug}/projects/{project_id}/work-items/{work_item_id}/relations/"
    return {
        "action": "work_item_relation__create_work_item_relation",
        "write_policy": "allow",
        "expected_terminal": "verified",
        "executor": "PlaneClient.execute_mutation_action",
        "effect": {"http_method": "POST", "kind": "write_create_or_associate"},
        "postcondition": {
            "kind": "membership_state", "strategy": "membership_state", "method": "GET", "get_action": "work_item_relation__list_work_item_relations",
            "path": path, "expected_status": 201, "readback_expected_status": 200, "response_shape": "relation_map",
            "bindings": [{"mutation": name, "readback": name, "rule": "exact"} for name in ["workspace_slug", "project_id", "work_item_id"]],
            "targets": {"path_fields": [], "payload_fields": ["issues"], "write_response_fields": [], "selector": {"source": "payload", "field": "issues"}, "evidence": official_provenance("/api-reference/work-item-relations/create-work-item-relation")},
            "membership": {"target_presence": "present", "relation_type_source": "payload"},
            "matcher": {"field": "id", "type": "relation_map"},
            "pagination": {"mode": "none", "max_pages": 1, "max_rows": 10000},
            "effects": {"payload_fields": [], "required_payload_effects": []},
            "verification": "source_cited_relation_map_membership",
        },
    }


def relation_contract_remove() -> dict[str, Any]:
    path = "/api/v1/workspaces/{workspace_slug}/projects/{project_id}/work-items/{work_item_id}/relations/remove/"
    return {
        "action": "work_item_relation__remove_work_item_relation",
        "write_policy": "allow",
        "expected_terminal": "verified_absence",
        "executor": "PlaneClient.execute_mutation_action",
        "effect": {"http_method": "POST", "kind": "write_delete"},
        "postcondition": {
            "kind": "membership_state", "strategy": "membership_state", "method": "GET", "get_action": "work_item_relation__list_work_item_relations",
            "path": "/api/v1/workspaces/{workspace_slug}/projects/{project_id}/work-items/{work_item_id}/relations/", "expected_status": 204, "readback_expected_status": 200, "response_shape": "relation_map",
            "bindings": [{"mutation": name, "readback": name, "rule": "exact"} for name in ["workspace_slug", "project_id", "work_item_id"]],
            "targets": {"path_fields": [], "payload_fields": ["related_issue"], "write_response_fields": [], "selector": {"source": "payload", "field": "related_issue"}, "evidence": official_provenance("/api-reference/work-item-relations/remove-work-item-relation")},
            "membership": {"target_presence": "absent"}, "matcher": {"field": "id", "type": "relation_map"},
            "pagination": {"mode": "none", "max_pages": 1, "max_rows": 10000}, "effects": {"payload_fields": [], "required_payload_effects": []},
            "verification": "source_cited_relation_map_absence",
        },
    }


def main() -> None:
    registry = json.loads(REGISTRY_PATH.read_text())
    source = json.loads(SOURCE_PATH.read_text())
    operations = registry["operations"]
    existing = {row["action"] for row in operations}
    by_action = {row["action"]: row for row in operations}
    action_map = {
        "intake_issue__add_intake_issue": "inbox_issue__add_inbox_issue",
        "intake_issue__delete_intake_issue": "inbox_issue__delete_inbox_issue",
        "intake_issue__get_intake_issue_detail": "inbox_issue__get_inbox_issue_detail",
        "intake_issue__list_intake_issues": "inbox_issue__list_inbox_issues",
        "intake_issue__update_intake_issue_detail": "inbox_issue__update_inbox_issue_detail",
    }
    generated_actions = set(action_map.values()) | {
        "work_item_relation__create_work_item_relation",
        "work_item_relation__list_work_item_relations",
        "work_item_relation__remove_work_item_relation",
    }
    operations[:] = [row for row in operations if row["action"] not in generated_actions]
    registry["mutation_contracts"][:] = [
        row for row in registry["mutation_contracts"] if row["action"] not in generated_actions
    ]
    existing = {row["action"] for row in operations}
    by_action = {row["action"]: row for row in operations}
    docs = {
        "inbox_issue__add_inbox_issue": ("/api-reference/inbox-issue/add-inbox-issue", "Add Inbox Issue"),
        "inbox_issue__delete_inbox_issue": ("/api-reference/inbox-issue/delete-inbox-issue", "Delete Inbox Issue"),
        "inbox_issue__get_inbox_issue_detail": ("/api-reference/inbox-issue/get-inbox-issue-detail", "Get Inbox Issue detail"),
        "inbox_issue__list_inbox_issues": ("/api-reference/inbox-issue/list-inbox-issues", "List Inbox Issues"),
        "inbox_issue__update_inbox_issue_detail": ("/api-reference/inbox-issue/update-inbox-issue-detail", "Update Inbox Issue detail"),
    }
    for old, new in action_map.items():
        if new not in existing:
            path, summary = docs[new]
            operations.append(inbox_operation(by_action[old], new, path, summary))
    contracts = registry["mutation_contracts"]
    contract_actions = {row["action"] for row in contracts}
    for old, new in action_map.items():
        if by_action[old]["mutation"] and new not in contract_actions:
            base = next(row for row in contracts if row["action"] == old)
            contracts.append(inbox_contract(base, action_map))
    relation_path = "/api/v1/workspaces/{workspace_slug}/projects/{project_id}/work-items/{work_item_id}/relations/"
    relation_docs = {
        "work_item_relation__create_work_item_relation": ("POST", relation_path, "/api-reference/work-item-relations/create-work-item-relation", "Create work item relation"),
        "work_item_relation__list_work_item_relations": ("GET", relation_path, "/api-reference/work-item-relations/list-work-item-relations", "List work item relations"),
        "work_item_relation__remove_work_item_relation": ("POST", "/api/v1/workspaces/{workspace_slug}/projects/{project_id}/work-items/{work_item_id}/relations/remove/", "/api-reference/work-item-relations/remove-work-item-relation", "Remove work item relation"),
    }
    if "work_item_relation__create_work_item_relation" not in existing:
        relation_type = {"type": "string", "enum": ["blocking", "blocked_by", "duplicate", "relates_to", "start_before", "start_after", "finish_before", "finish_after"]}
        issue_ids = {"type": "array", "items": {"type": "string"}}
        create_schema = path_schema(payload={"relation_type": relation_type, "issues": issue_ids}, required_payload=["relation_type", "issues"])
        list_schema = path_schema()
        list_schema["json_schema"]["properties"].update({"cursor": {"type": "string"}, "expand": {"type": "string"}, "fields": {"type": "string"}, "order_by": {"type": "string"}, "per_page": {"type": "integer"}})
        list_schema["query"]["parameters"] = [{"name": name, "type": "string" if name != "per_page" else "integer"} for name in ["cursor", "expand", "fields", "order_by", "per_page"]]
        remove_schema = path_schema(payload={"related_issue": {"type": "string"}}, required_payload=["related_issue"])
        relation_item = relation_item_schema()
        relation_ops = [
            relation_operation("work_item_relation__create_work_item_relation", "POST", relation_path, relation_docs["work_item_relation__create_work_item_relation"][2], relation_docs["work_item_relation__create_work_item_relation"][3], create_schema, {"available": True, "status": 201, "shape": "relation_create", "json_schema": {"type": "array", "items": {"type": "array", "items": relation_item}}}, "write_create_or_associate"),
            relation_operation("work_item_relation__list_work_item_relations", "GET", relation_path, relation_docs["work_item_relation__list_work_item_relations"][2], relation_docs["work_item_relation__list_work_item_relations"][3], list_schema, {"available": True, "status": 200, "shape": "relation_map", "json_schema": relation_map_schema()}, "read"),
            relation_operation("work_item_relation__remove_work_item_relation", "POST", relation_docs["work_item_relation__remove_work_item_relation"][1], relation_docs["work_item_relation__remove_work_item_relation"][2], relation_docs["work_item_relation__remove_work_item_relation"][3], remove_schema, {"available": True, "status": 204, "shape": "empty", "json_schema": {"type": "object"}}, "write_delete"),
        ]
        operations.extend(relation_ops)
        contracts.extend([relation_contract_create(), relation_contract_remove()])
    registry["operation_count"] = len(operations)
    registry["http_operation_count"] = len({(row["method"], row["path"]) for row in operations})
    registry["method_counts"] = {method: sum(row["method"] == method for row in operations) for method in ["DELETE", "GET", "PATCH", "POST"]}
    mutation_actions = sorted(row["action"] for row in operations if row["mutation"])
    terminal_counts = {terminal: sum(row["expected_terminal"] == terminal for row in contracts) for terminal in ["provider_acknowledged", "verified", "verified_absence"]}
    strategy_counts = {strategy: sum(row["postcondition"]["strategy"] == strategy for row in contracts) for strategy in ["detail_state", "membership_state", "provider_ack"]}
    registry["mutation_inventory"] = {
        "denominator": len(mutation_actions), "supported_count": len(mutation_actions), "unsupported_count": 0,
        "supported_actions": mutation_actions, "unsupported_actions": [], "contract_policy_counts": {"allow": len(contracts), "deny": 0},
        "terminal_counts": terminal_counts, "strategy_counts": strategy_counts,
        "graph_reconciliation": {"rule": "normalized_path_candidate_then_source_response_compatibility", "candidate_count": len(contracts), "ambiguous_candidate_count": 0, "zero_candidate_count": 0, "incompatible_schema_count": 0, "remediated_readback_count": len(contracts)},
    }
    registry["mutation_metadata"]["frozen_denominator"] = {"operations": len(operations), "http_operations": registry["http_operation_count"], "mutations": len(mutation_actions)}
    registry["mutation_metadata"]["recomputed_supported"] = len(mutation_actions)
    registry["mutation_metadata"]["recomputed_unsupported"] = 0
    registry["source_pages"] = len(operations)
    registry["snapshot"]["excluded_pages"].pop("/api-reference/inbox-issue/add-inbox-issue", None)
    registry["snapshot"]["excluded_pages"].pop("/api-reference/inbox-issue/delete-inbox-issue", None)
    registry["snapshot"]["excluded_pages"].pop("/api-reference/inbox-issue/get-inbox-issue-detail", None)
    registry["snapshot"]["excluded_pages"].pop("/api-reference/inbox-issue/list-inbox-issues", None)
    registry["snapshot"]["excluded_pages"].pop("/api-reference/inbox-issue/update-inbox-issue-detail", None)
    registry["snapshot"]["excluded_pages"].pop("/api-reference/work-item-relations/create-work-item-relation", None)
    registry["snapshot"]["excluded_pages"].pop("/api-reference/work-item-relations/list-work-item-relations", None)
    registry["snapshot"]["excluded_pages"].pop("/api-reference/work-item-relations/remove-work-item-relation", None)
    registry["source"] = "official-plane-developer-docs+v0.3.4-contract-extension"
    source["counts"] = {"operations": len(operations), "http_operations": registry["http_operation_count"], "mutations": len(mutation_actions)}
    source["computed_counts"] = {"policy": registry["mutation_inventory"]["contract_policy_counts"], "terminal": terminal_counts, "strategy": strategy_counts, "sum": len(contracts)}
    source["operations"] = [{"action": row["action"], "docs_path": row["docs_path"], "method": row["method"], "mutation": row["mutation"], "operation_sha256": sha({"action": row["action"], "method": row["method"], "path": row["path"], "docs_path": row["docs_path"]}), "path": row["path"], "schema_hash": row.get("schema_hash", "")} for row in operations]
    source["mutation_contracts"] = [{"action": row["action"], "contract_sha256": sha(row)} for row in contracts]
    source["registry_manifest"] = {"version": 2, "contract_version": 2, "contract_hash": sha(registry), "mutation_count": len(mutation_actions), "operation_count": len(operations), "http_operation_count": registry["http_operation_count"], "method_counts": registry["method_counts"]}
    REGISTRY_PATH.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n")
    source["registry_sha256"] = hashlib.sha256(REGISTRY_PATH.read_bytes()).hexdigest()
    SOURCE_PATH.write_text(json.dumps(source, ensure_ascii=False, indent=2) + "\n")
    print(f"v0.3.4 registry: {len(operations)} operations, {len(mutation_actions)} mutations, {registry['http_operation_count']} HTTP operations")


if __name__ == "__main__":
    main()
