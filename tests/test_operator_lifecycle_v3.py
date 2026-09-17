from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import plane_mcp_karval.server as server_module
from plane_mcp_karval.server import create_server


def run(coro):
    return asyncio.run(coro)


def lifecycle_comment(phase: str = "START") -> dict[str, Any]:
    return {
        "contract_version": 3,
        "phase": phase,
        "session_id": "codex-plane-mcp-v032",
        "task_stack_id": "CODEX-42",
        "agent_context": {
            "agent_runtime": "codex",
            "model": "gpt-5",
            "reasoning_effort": "medium",
            "reasoning_trace_policy": "metadata_only_no_chain_of_thought",
            "tool_surface": "plane-mcp-karval",
        },
        "occurred_at": "2026-09-15T04:10:00Z",
        "timestamp_source": "runtime",
        "unit_id": "operator-lifecycle-v3",
        "unit": "Plane operator lifecycle transition",
        "requested": "Start the bounded implementation.",
        "delivered": "Lifecycle transition authorized for execution.",
        "state_before": "ready",
        "state_after": "in_progress",
        "surfaces": ["src/plane_mcp_karval/server.py"],
        "evidence": ["provider-readback:precondition"],
        "decisions": ["Preserve the simple 0.3.1 lifecycle route."],
        "blockers": [],
        "residuals": [],
        "next_action": "Execute tests and review.",
        "owner": "codex",
    }


class OperatorClient:
    def __init__(self, *, comment_verified: bool = True) -> None:
        self.state = "ready-state"
        self.updated_at = "2026-09-15T04:09:00Z"
        self.comment_verified = comment_verified
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_work_item(self, workspace: str, project: str, item: str) -> dict[str, Any]:
        self.calls.append(("get", {"workspace": workspace, "project": project, "item": item}))
        return {"id": item, "state": self.state, "updated_at": self.updated_at}

    def execute_native_mutation_action(self, operation: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((operation, kwargs))
        if operation == "issue__update_issue_detail":
            self.state = kwargs["payload"]["state"]
            self.updated_at = "2026-09-15T04:10:01Z"
            return {"mutation_applied": True, "readback_verified": True, "provider_id": "issue-1"}
        return {
            "mutation_applied": True,
            "readback_verified": self.comment_verified,
            "provider_id": "comment-1",
        }


def operator_call(**overrides: Any) -> dict[str, Any]:
    payload = {
        "workspace_slug": "karval",
        "project_id": "project-1",
        "work_item_id": "issue-1",
        "expected_current_state_id": "ready-state",
        "expected_updated_at": "2026-09-15T04:09:00Z",
        "target_state_id": "in-progress-state",
        "comment": lifecycle_comment(),
        "approved_live_mutation": True,
        "approved_non_atomic_operator_transition": True,
        "authorization_basis": "explicit_human_operator_authorization",
        "authorized_workspace_slug": "karval",
        "authorized_project_id": "project-1",
        "authorized_work_item_id": "issue-1",
        "idempotency_key": "codex-42-operator-transition",
        "attempts": 1,
        "contract_version": 3,
    }
    payload.update(overrides)
    return payload


def test_operator_tool_is_registered_and_executes_bound_transition(monkeypatch, tmp_path) -> None:
    fake = OperatorClient()
    monkeypatch.setattr(server_module, "_client", lambda: fake)
    monkeypatch.setenv("PLANE_MUTATION_LEDGER", str(tmp_path / "ledger.json"))

    async def check() -> None:
        async with Client(create_server()) as client:
            names = {tool.name for tool in await client.list_tools()}
            assert "plane_operator_lifecycle_transition" in names
            result = await client.call_tool("plane_operator_lifecycle_transition", operator_call())
            assert result.data["status"] == "verified_non_atomic_operator_transition"
            assert result.data["contract_version"] == 3
            assert result.data["receipt"]["receipt_fingerprint"]
            assert result.data["state_readback_verified"] is True
            assert result.data["comment_readback_verified"] is True

    run(check())
    assert [name for name, _ in fake.calls] == [
        "get", "issue__update_issue_detail", "issue_comment__add_issue_comment", "get"
    ]
    assert fake.calls[1][1]["idempotency_key"] == "codex-42-operator-transition:state"
    assert fake.calls[2][1]["idempotency_key"] == "codex-42-operator-transition:comment"


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"contract_version": 2}, "contract_version=3"),
        ({"approved_live_mutation": False}, "live mutation approval"),
        ({"authorized_work_item_id": "other"}, "authorized work item"),
        ({"attempts": 2}, "exactly one"),
    ],
)
def test_operator_tool_rejects_unbound_or_downgraded_requests_before_provider(
    monkeypatch, override, message
) -> None:
    monkeypatch.setattr(
        server_module, "_client", lambda: (_ for _ in ()).throw(AssertionError("provider must not open"))
    )

    async def check() -> None:
        async with Client(create_server()) as client:
            with pytest.raises(ToolError, match=message):
                await client.call_tool("plane_operator_lifecycle_transition", operator_call(**override))

    run(check())


def test_operator_tool_reports_precondition_conflict_without_write(monkeypatch, tmp_path) -> None:
    fake = OperatorClient()
    monkeypatch.setattr(server_module, "_client", lambda: fake)
    monkeypatch.setenv("PLANE_MUTATION_LEDGER", str(tmp_path / "ledger.json"))

    async def check() -> None:
        async with Client(create_server()) as client:
            result = await client.call_tool(
                "plane_operator_lifecycle_transition",
                operator_call(expected_current_state_id="other-state"),
            )
            assert result.data == {
                "status": "precondition_failed",
                "reason": "state",
                "actual_state_id": "ready-state",
                "actual_updated_at": "2026-09-15T04:09:00Z",
                "mutation_applied": False,
            }

    run(check())
    assert [name for name, _ in fake.calls] == ["get"]


def test_operator_tool_exposes_comment_pending_after_verified_state(monkeypatch, tmp_path) -> None:
    fake = OperatorClient(comment_verified=False)
    monkeypatch.setattr(server_module, "_client", lambda: fake)
    monkeypatch.setenv("PLANE_MUTATION_LEDGER", str(tmp_path / "ledger.json"))

    async def check() -> None:
        async with Client(create_server()) as client:
            result = await client.call_tool("plane_operator_lifecycle_transition", operator_call())
            assert result.data["status"] == "target_confirmed_comment_pending"
            assert result.data["state_mutation_applied"] is True
            assert result.data["comment_readback_verified"] is False
            assert result.data["manual_reconciliation_required"] is True

    run(check())


def test_operator_progress_is_comment_only_annotation(monkeypatch, tmp_path) -> None:
    fake = OperatorClient()
    fake.state = "in-progress-state"
    monkeypatch.setattr(server_module, "_client", lambda: fake)
    monkeypatch.setenv("PLANE_MUTATION_LEDGER", str(tmp_path / "ledger.json"))
    comment = lifecycle_comment("PROGRESS")
    comment["state_before"] = comment["state_after"] = "in_progress"

    async def check() -> None:
        async with Client(create_server()) as client:
            result = await client.call_tool(
                "plane_operator_lifecycle_transition",
                operator_call(
                    expected_current_state_id="in-progress-state",
                    target_state_id="in-progress-state",
                    comment=comment,
                ),
            )
            assert result.data["status"] == "verified_annotation"
            assert result.data["state_mutation_applied"] is False

    run(check())
    assert [name for name, _ in fake.calls] == ["get", "issue_comment__add_issue_comment", "get"]
