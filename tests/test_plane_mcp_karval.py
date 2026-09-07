from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any

from fastmcp import Client

import plane_mcp_karval.server as server_module
from plane_api import HTTP_OPERATION_COUNT, METHOD_COUNTS, MUTATION_COUNT, OPERATION_COUNT
from plane_api.client import _validate_json_schema, payload_fingerprint
from plane_mcp_karval.server import create_server
from plane_mcp_karval.issue_creation_readiness import readiness_fingerprint, validate_issue_creation_readiness


class FakePlaneClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def operation_descriptor(self, operation: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("descriptor", {"operation": operation, **kwargs}))
        return {
            "dry_run": True,
            "would_mutate": operation != "project__list_projects",
            "action": operation,
            "method": "GET" if operation == "project__list_projects" else "POST",
            "path_template": "/api/v1/workspaces/{workspace_slug}/projects/",
        }

    def execute_read_action(self, operation: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("read", {"operation": operation, **kwargs}))
        return {"ok": True, "operation": operation, "kwargs": kwargs}

    def get_current_user(self) -> dict[str, Any]:
        self.calls.append(("get_current_user", {}))
        return {"id": "user-1"}

    def list_projects(self, workspace_slug: str, **query: Any) -> dict[str, Any]:
        self.calls.append(("list_projects", {"workspace_slug": workspace_slug, **query}))
        return {"results": [{"id": "project-1"}]}

    def list_work_items(self, workspace_slug: str, project_id: str, **query: Any) -> dict[str, Any]:
        self.calls.append(
            ("list_work_items", {"workspace_slug": workspace_slug, "project_id": project_id, **query})
        )
        return {"results": [{"id": "issue-1"}]}

    def get_work_item(self, workspace_slug: str, project_id: str, work_item_id: str) -> dict[str, Any]:
        self.calls.append(
            (
                "get_work_item",
                {"workspace_slug": workspace_slug, "project_id": project_id, "work_item_id": work_item_id},
            )
        )
        return {"id": work_item_id}

    def search_work_items(self, workspace_slug: str, **query: Any) -> dict[str, Any]:
        self.calls.append(("search_work_items", {"workspace_slug": workspace_slug, **query}))
        return {"results": []}

    def execute_mutation_action(self, operation: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("mutation", {"operation": operation, **kwargs}))
        return {
            "action": operation,
            "mutation_applied": True,
            "readback_verified": True,
            "duplicate": False,
        }

    def reconcile_legacy_module_archive_attempt(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("reconcile_legacy_module_archive_attempt", kwargs))
        return {
            "governed": True,
            "action": "module__archive_module",
            "disposition": "effect_not_present_at_reconciliation_time",
            "readback_verified": True,
        }

    def governed_add_work_item_comment(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("comment", kwargs))
        return {
            "governed": True,
            "action": "add_work_item_comment",
            "comment_id": "comment-1",
            "mutation_applied": True,
            "readback_verified": True,
            "duplicate": False,
        }


def run(coro):
    return asyncio.run(coro)


def issue_readiness() -> dict[str, Any]:
    return {
        "contract_version": 1,
        "objective": "Create a governed, execution-ready issue.",
        "context": "The issue is created through the governed Plane MCP.",
        "scope": ["Create the bounded work item."],
        "non_goals": [],
        "acceptance_criteria": ["The work item has a complete execution contract."],
        "owner_disposition": "unassigned: owner will be selected during intake",
        "priority_rationale": "The requested workflow requires traceable readiness.",
        "dependencies": ["provider project discovery"],
        "validation_plan": ["independent provider GET readback"],
        "execution_units": ["intake"],
    }


def test_issue_creation_readiness_rejects_a_title_only_payload() -> None:
    errors = validate_issue_creation_readiness(None, {"name": "title only"})

    assert errors == ["issue_readiness is required for issue__add_issue"]


def test_issue_creation_readiness_requires_execution_fields_and_body() -> None:
    errors = validate_issue_creation_readiness(
        {"contract_version": 1, "objective": "short"},
        {"name": "title only", "description_html": ""},
    )

    assert "issue_readiness.scope must contain at least one bounded item" in errors
    assert "payload.description_html must contain an execution-ready body" in errors


def test_prepare_issue_creation_returns_the_exact_governed_fingerprint() -> None:
    payload = {
        "name": "create governed issue",
        "description_html": "<p>This execution-ready body contains the bounded objective, scope, non-goals, acceptance criteria, ownership disposition, priority rationale, dependencies, validation plan, and execution units required for governed intake.</p>",
    }

    async def check() -> None:
        async with Client(create_server()) as client:
            valid = await client.call_tool(
                "plane_prepare_issue_creation",
                {"payload": payload, "issue_readiness": issue_readiness()},
            )
            assert valid.data == {
                "valid": True,
                "errors": [],
                "issue_readiness_fingerprint": readiness_fingerprint(issue_readiness()),
                "issue_readiness_contract_version": 1,
            }

            invalid = await client.call_tool(
                "plane_prepare_issue_creation",
                {"payload": payload, "issue_readiness": {}},
            )
            assert invalid.data["valid"] is False
            assert invalid.data["issue_readiness_fingerprint"] is None
            assert "issue_readiness.scope must contain at least one bounded item" in invalid.data["errors"]

    run(check())


def test_mutation_descriptor_exposes_the_exact_payload_fingerprint_contract() -> None:
    payload = {"description_html": "<p>execution-ready body</p>"}
    fake = FakePlaneClient()
    # The real descriptor contract is tested directly because the fake keeps a
    # deliberately smaller shape for unrelated MCP forwarding tests.
    from plane_api.client import PlaneClient, PlaneConfig

    client = PlaneClient(PlaneConfig(base_url="https://plane.example", token="test"))
    descriptor = client.operation_descriptor(
        "issue__update_issue_detail",
        path_params={"workspace_slug": "karval", "project_id": "project-1", "resource_id": "issue-1"},
        payload=payload,
    )

    template = descriptor["authorization_receipt_template"]
    assert template["payload_fingerprint"] == payload_fingerprint(payload)
    assert template["path_params"]["resource_id"] == "issue-1"


def test_registry_denominator_is_full_plane_coverage() -> None:
    result = server_module._catalog_payload(limit=500)

    assert OPERATION_COUNT == 225
    assert HTTP_OPERATION_COUNT == 223
    assert MUTATION_COUNT == 134
    assert METHOD_COUNTS == {"DELETE": 44, "GET": 91, "PATCH": 38, "POST": 52}
    assert result["operation_denominator"] == 225
    assert result["unique_http_operations"] == 223
    assert result["mutation_denominator"] == 134
    assert len(result["actions"]) == 50
    assert result["server"]["name"] == "plane-mcp-karval"


def test_server_does_not_depend_on_upstream_plane_mcp_server() -> None:
    source = inspect.getsource(server_module)

    assert "plane_mcp.server" not in source
    assert "plane_mcp.client" not in source
    assert "plane.models" not in source
    assert "plane_mcp_server" not in source


def test_mcp_lists_registry_first_tools() -> None:
    async def check() -> None:
        async with Client(create_server()) as client:
            tools = await client.list_tools()
            names = {tool.name for tool in tools}
            assert names == {
                "plane_catalog",
                "plane_action_descriptor",
                "plane_read_action",
                "plane_validate_work_item_contract",
                "plane_render_lifecycle_comment",
                "plane_add_lifecycle_comment",
                "plane_validate_title_contract",
                "plane_normalize_title",
                "plane_prepare_issue_creation",
                "plane_mutation_action",
                "plane_reconcile_legacy_module_archive_attempt",
                "plane_reconcile_module_update_attempt",
                "get_current_user",
                "list_projects",
                "list_work_items",
                "get_work_item",
                "search_work_items",
            }

    run(check())


def test_catalog_tool_returns_full_contract_metadata() -> None:
    async def check() -> None:
        async with Client(create_server()) as client:
            result = await client.call_tool(
                "plane_catalog",
                {"method": "GET", "mutation": False, "limit": 10},
            )
            data = result.data
            assert data["operation_denominator"] == 225
            assert data["server"]["mutation_count"] == 134
            assert all(row["method"] == "GET" for row in data["actions"])

    run(check())


def test_response_schema_accepts_plane_description_aliases() -> None:
    schema = {
        "type": "object",
        "required": ["id", "description"],
        "properties": {
            "id": {"type": "string"},
            "description": {"type": "string"},
        },
    }

    _validate_json_schema(
        {"id": "issue-1", "description_html": "<p>body</p>"},
        schema,
        context="mutation",
    )


def test_read_and_shortcut_tools_use_owned_client(monkeypatch) -> None:
    fake = FakePlaneClient()
    monkeypatch.setenv("PLANE_WORKSPACE_SLUG", "karval")
    monkeypatch.setattr(server_module, "_client", lambda: fake)

    async def check() -> None:
        async with Client(create_server()) as client:
            read = await client.call_tool(
                "plane_read_action",
                {
                    "operation": "project__list_projects",
                    "path_params": {"workspace_slug": "karval"},
                    "query": {"per_page": 20},
                },
            )
            projects = await client.call_tool("list_projects", {"per_page": 20})
            assert read.data["ok"] is True
            assert projects.data["results"] == [{"id": "project-1"}]

    run(check())
    assert fake.calls[0] == (
        "read",
        {
            "operation": "project__list_projects",
            "path_params": {"workspace_slug": "karval"},
            "query": {"per_page": 20},
        },
    )
    assert fake.calls[1] == ("list_projects", {"workspace_slug": "karval", "per_page": 20})


def test_semantic_gate_tools_are_read_only_and_deterministic() -> None:
    async def check() -> None:
        async with Client(create_server()) as client:
            normalized = await client.call_tool(
                "plane_normalize_title",
                {
                    "title": "Elevar gates Plane MCP",
                    "labels": ["tipo:hardening"],
                    "title_context": "MCP",
                },
            )
            assert normalized.data["title"] == "🛡️ [MCP] Elevar gates Plane MCP"
            assert normalized.data["semantic_icon_id"] == "type.hardening"

            title = await client.call_tool(
                "plane_validate_title_contract",
                {
                    "title": "🛡️ [MCP] Elevar gates Plane MCP",
                    "labels": ["tipo:hardening"],
                    "title_context": "MCP",
                },
            )
            assert title.data["valid"] is True
            assert title.data["icon_contract_version"] == 1

            invalid = await client.call_tool(
                "plane_validate_work_item_contract",
                {"document": {"contract_version": 1}},
            )
            assert invalid.data["valid"] is False
            assert "work_item must be an object" in invalid.data["errors"]

            rendered = await client.call_tool(
                "plane_render_lifecycle_comment",
                {
                    "comment": {
                        "phase": "START",
                        "session_id": "codex-20260806-plane-gates",
                        "task_stack_id": "HERMES-plane-mcp-gates-20260806",
                        "agent_context": {
                            "agent_runtime": "codex",
                            "model": "gpt-5.6-terra",
                            "reasoning_effort": "high",
                            "reasoning_trace_policy": "metadata_only_no_chain_of_thought",
                            "tool_surface": "plane-mcp-karval",
                        },
                        "occurred_at": "2026-08-07T00:10:00Z",
                        "timestamp_source": "runtime",
                        "unit_id": "semantic-gates",
                        "unit": "Plane MCP semantic gate implementation",
                        "requested": "Start the semantic gate implementation work.",
                        "delivered": "Execution started with scoped files and tests.",
                        "state_before": "ready",
                        "state_after": "in_progress",
                        "surfaces": ["src/plane_mcp_karval/server.py"],
                        "evidence": ["cmd:pytest-not-yet-run"],
                        "decisions": [],
                        "blockers": [],
                        "residuals": [],
                        "next_action": "Implement MCP semantic gate tools and tests.",
                        "owner": "codex",
                    },
                    "output_format": "markdown",
                },
            )
            assert rendered.data["body_format"] == "markdown"
            assert "session_id=codex-20260806-plane-gates" in rendered.data["body"]
            assert "model=gpt-5.6-terra" in rendered.data["body"]

    run(check())


def test_mutation_tool_requires_registry_mutation_and_forwards_governed_fields(monkeypatch) -> None:
    fake = FakePlaneClient()
    monkeypatch.setattr(server_module, "_client", lambda: fake)

    async def check() -> None:
        async with Client(create_server()) as client:
            result = await client.call_tool(
                "plane_mutation_action",
                {
                    "operation": "issue__add_issue",
                    "path_params": {"workspace_slug": "karval", "project_id": "project-1"},
                    "payload": {"name": "create governed issue", "description_html": "<p>This execution-ready body contains the bounded objective, scope, non-goals, acceptance criteria, ownership disposition, priority rationale, dependencies, validation plan, and execution units required for governed intake.</p>"},
                    "issue_readiness": issue_readiness(),
                    "approved_live_mutation": True,
                    "authorization_receipt": {
                        "action": "issue__add_issue",
                        "method": "POST",
                        "approved_live_mutation": True,
                        "authorization_scope": "one_operation_one_target",
                        "path_params": {"workspace_slug": "karval", "project_id": "project-1"},
                        "payload_fingerprint": "test-only",
                        "issue_readiness_fingerprint": readiness_fingerprint(issue_readiness()),
                    },
                    "idempotency_key": "test-key-123456789",
                    "attempts": 1,
                },
            )
            assert result.data["mutation_applied"] is True

    run(check())
    assert fake.calls == [
        (
            "mutation",
            {
                "operation": "issue__add_issue",
                "path_params": {"workspace_slug": "karval", "project_id": "project-1"},
                "query": {},
                "payload": {"name": "create governed issue", "description_html": "<p>This execution-ready body contains the bounded objective, scope, non-goals, acceptance criteria, ownership disposition, priority rationale, dependencies, validation plan, and execution units required for governed intake.</p>"},
                "approved_live_mutation": True,
                "authorization_receipt": {
                    "action": "issue__add_issue",
                    "method": "POST",
                    "approved_live_mutation": True,
                    "authorization_scope": "one_operation_one_target",
                    "path_params": {"workspace_slug": "karval", "project_id": "project-1"},
                    "payload_fingerprint": "test-only",
                    "issue_readiness_fingerprint": readiness_fingerprint(issue_readiness()),
                },
                "idempotency_key": "test-key-123456789",
                "attempts": 1,
                "ledger_path": Path(server_module.DEFAULT_LEDGER).expanduser(),
            },
        )
    ]


def test_reconciliation_tool_is_narrow_and_forwards_only_its_governed_receipt(monkeypatch, tmp_path: Path) -> None:
    fake = FakePlaneClient()
    monkeypatch.setattr(server_module, "_client", lambda: fake)
    params = {"workspace_slug": "karval", "project_id": "project-1", "resource_id": "module-1"}

    async def check() -> None:
        async with Client(create_server()) as client:
            result = await client.call_tool(
                "plane_reconcile_legacy_module_archive_attempt",
                {
                    **params,
                    "key_hash": "a" * 64,
                    "attempts": 1,
                    "approved_live_reconciliation": True,
                    "authorization_receipt": {
                        "action": "module__archive_module", "method": "POST",
                        "approved_live_reconciliation": True,
                        "authorization_scope": "one_legacy_attempt_one_target",
                    },
                },
            )
            assert result.data["disposition"] == "effect_not_present_at_reconciliation_time"

    run(check())
    name, forwarded = fake.calls[0]
    assert name == "reconcile_legacy_module_archive_attempt"
    assert forwarded["attempts"] == 1
    assert forwarded["authorization_receipt"]["authorization_scope"] == "one_legacy_attempt_one_target"
    assert forwarded["ledger_path"] == Path(server_module.DEFAULT_LEDGER).expanduser()


def test_lifecycle_comment_tool_renders_and_forwards_governed_comment(monkeypatch) -> None:
    fake = FakePlaneClient()
    monkeypatch.setenv("PLANE_WORKSPACE_SLUG", "karval")
    monkeypatch.setattr(server_module, "_client", lambda: fake)

    comment = {
        "phase": "FINISH",
        "session_id": "codex-20260806-plane-gates",
        "task_stack_id": "HERMES-plane-mcp-gates-20260806",
        "agent_context": {
            "agent_runtime": "codex",
            "model": "gpt-5.6-terra",
            "reasoning_effort": "high",
            "reasoning_trace_policy": "metadata_only_no_chain_of_thought",
            "tool_surface": "plane-mcp-karval",
        },
        "occurred_at": "2026-08-07T01:10:00Z",
        "timestamp_source": "runtime",
        "unit_id": "semantic-gates",
        "unit": "Plane MCP semantic gate implementation",
        "requested": "Finish the semantic gate implementation with comment support.",
        "delivered": "Governed lifecycle comment tool rendered and forwarded.",
        "state_before": "review_qa",
        "state_after": "done",
        "surfaces": ["src/plane_mcp_karval/server.py"],
        "evidence": ["test:pytest-8-passed"],
        "decisions": ["Comment creation is exposed as a semantic governed shortcut."],
        "blockers": [],
        "residuals": [],
        "next_action": "Read back provider state and close the Plane issue.",
        "owner": "codex",
    }

    async def check() -> None:
        async with Client(create_server()) as client:
            result = await client.call_tool(
                "plane_add_lifecycle_comment",
                {
                    "project_id": "project-1",
                    "work_item_id": "issue-1",
                    "comment": comment,
                    "approved_live_mutation": True,
                    "authorization_scope": "one_work_item_one_operation",
                    "authorized_workspace_slug": "karval",
                        "authorized_project_id": "project-1",
                        "authorized_work_item_id": "issue-1",
                        "authorization_receipt": {
                            "action": "add_work_item_comment",
                            "method": "POST",
                            "authorization_id": "comment-approval-0001",
                            "key_hash": "test-only-key-hash",
                            "provider_instance_hash": "test-only-provider-hash",
                            "payload_fingerprint": "test-only-payload-fingerprint",
                            "path_params": {"workspace_slug": "karval", "project_id": "project-1", "work_item_id": "issue-1"},
                        },
                    "idempotency_key": "comment-key-123456789",
                    "attempts": 1,
                    "external_source": "codex",
                    "external_id": "HERMES-plane-mcp-gates-20260806:finish:1",
                },
            )
            assert result.data["comment_id"] == "comment-1"
            assert result.data["readback_verified"] is True

    run(check())
    assert fake.calls[0][0] == "comment"
    payload = fake.calls[0][1]["payload"]
    assert "<code>session_id=codex-20260806-plane-gates</code>" in payload["comment_html"]
    assert "<code>model=gpt-5.6-terra</code>" in payload["comment_html"]
    assert payload["external_source"] == "codex"
